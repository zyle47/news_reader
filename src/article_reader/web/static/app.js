const elements = {
  form: document.querySelector("#article-form"),
  url: document.querySelector("#article-url"),
  language: document.querySelector("#language"),
  scriptGroup: document.querySelector("#script-group"),
  script: document.querySelector("#script"),
  fetchButton: document.querySelector("#fetch-button"),
  status: document.querySelector("#status"),
  historyPanel: document.querySelector("#history-panel"),
  historyList: document.querySelector("#history-list"),
  reviewPanel: document.querySelector("#review-panel"),
  articleState: document.querySelector("#article-state"),
  articleTitle: document.querySelector("#article-title"),
  sourceLink: document.querySelector("#source-link"),
  warning: document.querySelector("#review-warning"),
  warningMessage: document.querySelector("#review-message"),
  reviewAccepted: document.querySelector("#review-accepted"),
  resolutionPanel: document.querySelector("#resolution-panel"),
  resolutionMessage: document.querySelector("#resolution-message"),
  resolveButton: document.querySelector("#resolve-button"),
  readyPanel: document.querySelector("#ready-panel"),
  voice: document.querySelector("#voice"),
  voiceHelp: document.querySelector("#voice-help"),
  generateButton: document.querySelector("#generate-button"),
  articleBlocks: document.querySelector("#article-blocks"),
  playerPanel: document.querySelector("#player-panel"),
  playerTitle: document.querySelector("#player-title"),
  playerPosition: document.querySelector("#player-position"),
  jobState: document.querySelector("#job-state"),
  currentSegment: document.querySelector("#current-segment"),
  audio: document.querySelector("#audio-player"),
  previousButton: document.querySelector("#previous-button"),
  nextButton: document.querySelector("#next-button"),
  cancelButton: document.querySelector("#cancel-button"),
  retryButton: document.querySelector("#retry-button"),
  speed: document.querySelector("#playback-speed"),
};

const state = {
  voices: [],
  readingId: null,
  reading: null,
  manifest: null,
  chunkIndex: 0,
  restoreTime: 0,
  lastProgressWrite: 0,
  progressRevision: null,
  readingPollHandle: null,
  manifestPollHandle: null,
  manifestPollToken: 0,
  playbackGeneration: 0,
  playerInitialized: false,
  autoplayPending: false,
};

const READING_POLL_MS = 1200;
const MANIFEST_POLL_MS = 1000;

const languageNames = { en: "English", de: "German", sr: "Serbian" };
const reviewLabels = {
  missing_title: "The page did not expose a reliable title.",
  short_content: "The extracted article is unusually short.",
  few_blocks: "Only a small number of content sections were found.",
  restriction_page: "The page may be a consent, login, or restriction screen.",
  complex_content: "Some complex content needs human review.",
};

const READING_ACTIVE_STATES = new Set(["queued", "preparing", "generating"]);
const JOB_ACTIVE_STATES = new Set(["queued", "running", "cancelling", "interrupted"]);
const JOB_RETRYABLE_STATES = new Set(["failed", "cancelled", "interrupted"]);

const tabId = crypto.randomUUID();
const playbackChannel = "BroadcastChannel" in window
  ? new BroadcastChannel("article-reader-playback")
  : null;

if (playbackChannel) {
  playbackChannel.addEventListener("message", (event) => {
    if (event.data?.type === "playing" && event.data.tabId !== tabId) {
      elements.audio.pause();
      showStatus("Playback moved to another Article Reader tab.");
    }
  });
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: options.body ? { "Content-Type": "application/json", ...options.headers } : {
      ...options.headers,
    },
  });
  const contentType = response.headers.get("content-type") || "";
  const document_ = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) {
    const error = new Error(document_?.error?.message || `Request failed (${response.status}).`);
    error.code = document_?.error?.code || null;
    error.status = response.status;
    throw error;
  }
  return document_;
}

function showStatus(message, error = false) {
  elements.status.textContent = message;
  elements.status.classList.toggle("error", error);
  elements.status.hidden = false;
}

function hideStatus() {
  elements.status.hidden = true;
  elements.status.classList.remove("error");
  elements.status.textContent = "";
}

function clearTimer(name) {
  if (state[name] !== null) {
    clearTimeout(state[name]);
    state[name] = null;
  }
}

function currentLanguageRequest() {
  const language = elements.language.value;
  if (language === "sr" && !elements.script.value) {
    elements.script.focus();
    throw new Error("Choose Latin or Cyrillic for Serbian.");
  }
  return {
    language,
    script: language === "sr" ? elements.script.value : null,
  };
}

function updateScriptChoice() {
  const isSerbian = elements.language.value === "sr";
  elements.scriptGroup.hidden = !isSerbian;
  elements.script.disabled = !isSerbian;
  elements.script.required = isSerbian;
}

function resetPlayer() {
  elements.audio.pause();
  elements.audio.removeAttribute("src");
  elements.audio.load();
  elements.playerPanel.hidden = true;
  state.manifest = null;
  state.chunkIndex = 0;
  state.playerInitialized = false;
  state.playbackGeneration += 1;
  state.manifestPollToken += 1;
  clearTimer("manifestPollHandle");
  document.querySelectorAll(".article-block.active").forEach((block) => {
    block.classList.remove("active");
  });
}

function resetView() {
  clearTimer("readingPollHandle");
  clearTimer("manifestPollHandle");
  resetPlayer();
  state.readingId = null;
  state.reading = null;
  state.progressRevision = null;
  elements.reviewPanel.hidden = true;
  hideStatus();
}

function renderBlocks(blocks) {
  const fragment = document.createDocumentFragment();
  for (const block of blocks) {
    const item = document.createElement(block.kind === "heading" ? "h3" : "p");
    item.className = `article-block ${block.kind}`;
    item.dataset.ordinal = String(block.ordinal);
    if (!block.will_be_spoken) {
      item.classList.add("omitted");
      const note = document.createElement("span");
      note.className = "block-note";
      note.textContent = "Shown, but omitted from speech";
      item.append(note);
    } else if (block.requires_review) {
      const note = document.createElement("span");
      note.className = "block-note";
      note.textContent = "Review this section";
      item.append(note);
    }
    item.append(document.createTextNode(block.display_text));
    fragment.append(item);
  }
  elements.articleBlocks.replaceChildren(fragment);
}

function matchingVoices(language, script) {
  return state.voices.filter((voice) =>
    voice.installed
    && voice.evaluation_status === "approved"
    && voice.language === language
    && voice.scripts.includes(script)
  );
}

function renderVoiceChoices(reading) {
  const language = reading.article.language?.selected_language;
  const voices = matchingVoices(language, reading.article.language?.selected_script);
  const options = voices.map((voice) => {
    const option = document.createElement("option");
    option.value = voice.id;
    option.textContent = voice.display_name;
    return option;
  });
  elements.voice.replaceChildren(...options);
  elements.voice.disabled = voices.length === 0;
  elements.generateButton.disabled = voices.length === 0;
  elements.voiceHelp.textContent = voices.length
    ? "Audio is generated and stays local to this computer."
    : "No approved compatible voice is installed in this data directory.";
}

function jobStateLabel(job) {
  if (!job) return "";
  const labels = {
    queued: "Queued — waiting for the local worker",
    running: "Working…",
    cancelling: "Cancelling…",
    completed: "Done",
    failed: `Failed${job.error_message ? `: ${job.error_message}` : ""}`,
    cancelled: "Cancelled",
    interrupted: "Interrupted — will retry automatically, or press Retry",
  };
  return labels[job.state] || job.state;
}

function updateJobControls(job) {
  const cancellable = job && JOB_ACTIVE_STATES.has(job.state);
  const retryable = job && JOB_RETRYABLE_STATES.has(job.state);
  elements.cancelButton.hidden = !cancellable;
  elements.retryButton.hidden = !retryable;
  elements.jobState.hidden = !job;
  if (job) {
    elements.jobState.textContent = jobStateLabel(job);
    elements.jobState.classList.toggle("error", job.state === "failed");
    elements.jobState.classList.toggle(
      "attention",
      job.state === "interrupted" || job.state === "cancelling",
    );
  }
}

function renderReviewSection(reading) {
  const article = reading.article;
  elements.reviewPanel.hidden = false;
  elements.articleTitle.textContent = article.title || reading.title || "Untitled article";
  elements.playerTitle.textContent = article.title || reading.title || "Article audio";
  elements.sourceLink.href = article.final_url;
  elements.articleState.textContent = article.language?.selected_language
    ? `${languageNames[article.language.selected_language]} · ${article.language.selected_script}`
    : readingStateLabel(reading.state);
  renderBlocks(article.blocks);

  const needsReview = reading.state === "needs_review";
  elements.warning.hidden = !needsReview;
  if (needsReview) {
    elements.reviewAccepted.checked = false;
    const messages = article.review.reasons.map((reason) => reviewLabels[reason] || reason);
    elements.warningMessage.textContent = messages.join(" ")
      || "One or more sections need a quick human check.";
  }

  const needsLanguage = reading.state === "needs_language";
  const readyForVoice = reading.state === "ready_for_voice"
    || reading.state === "generating"
    || reading.state === "ready";
  elements.readyPanel.hidden = !readyForVoice;

  elements.resolutionPanel.hidden = !(needsReview || needsLanguage);
  if (needsReview || needsLanguage) {
    elements.resolutionMessage.textContent = needsLanguage
      ? "Choose the article language above, then prepare this saved extraction."
      : "Review the extracted text, confirm it above, then prepare this saved extraction.";
    elements.resolveButton.textContent = needsReview ? "Approve and prepare" : "Prepare article";
  }

  if (readyForVoice) {
    renderVoiceChoices(reading);
  }
}

function readingStateLabel(readingState) {
  const labels = {
    queued: "Queued — waiting for the local worker",
    preparing: "Fetching and extracting…",
    needs_review: "Review needed",
    needs_language: "Language choice needed",
    ready_for_voice: "Ready — choose a voice",
    generating: "Generating audio…",
    ready: "Ready",
    failed: "Failed",
    cancelled: "Cancelled",
    interrupted: "Interrupted",
  };
  return labels[readingState] || readingState;
}

function progressKey() {
  return state.readingId ? `article-reader:speed:${state.readingId}` : null;
}

function saveLocalSpeedHint() {
  const key = progressKey();
  if (key) localStorage.setItem(key, elements.speed.value);
}

function localSpeedHint() {
  const key = progressKey();
  if (!key) return null;
  const value = Number(localStorage.getItem(key));
  return [0.75, 1, 1.25, 1.5].includes(value) ? value : null;
}

async function saveProgress(force = false) {
  if (!state.readingId || !state.manifest) return;
  const now = Date.now();
  if (!force && now - state.lastProgressWrite < 4000) return;
  state.lastProgressWrite = now;
  try {
    const result = await api(`/api/readings/${state.readingId}/progress`, {
      method: "PUT",
      body: JSON.stringify({
        rendition_id: state.manifest.rendition_id,
        chunk_ordinal: state.chunkIndex,
        offset_seconds: elements.audio.currentTime || 0,
        speed: elements.audio.playbackRate,
        revision: state.progressRevision,
      }),
    });
    state.progressRevision = result.revision;
  } catch (error) {
    if (error.code === "PROGRESS_CONFLICT") {
      try {
        const reading = await api(`/api/readings/${state.readingId}`);
        state.progressRevision = reading.progress?.revision ?? null;
      } catch {
        // Best-effort resync; the next periodic save will try again.
      }
    }
  }
}

function highlightBlocks(chunk) {
  document.querySelectorAll(".article-block.active").forEach((block) => {
    block.classList.remove("active");
  });
  for (const ordinal of chunk.source_block_ordinals) {
    const block = document.querySelector(`.article-block[data-ordinal="${ordinal}"]`);
    block?.classList.add("active");
  }
}

async function loadChunk(index, { autoplay = false, restoreTime = 0 } = {}) {
  const manifest = state.manifest;
  if (!manifest || index < 0 || index >= manifest.chunks.length) return;
  const chunk = manifest.chunks[index];
  if (chunk.state !== "ready") return;
  const generation = state.playbackGeneration;
  saveProgress(true);
  state.chunkIndex = index;
  elements.audio.src = chunk.audio_url;
  elements.audio.playbackRate = Number(elements.speed.value);
  elements.currentSegment.textContent = chunk.speech_text;
  elements.playerPosition.textContent = `Section ${index + 1} of ${manifest.total_chunks}`;
  elements.previousButton.disabled = index === 0;
  elements.nextButton.disabled = index === manifest.total_chunks - 1;
  highlightBlocks(chunk);
  if (restoreTime > 0) state.restoreTime = restoreTime;
  if (autoplay) {
    try {
      await elements.audio.play();
    } catch {
      if (generation === state.playbackGeneration) {
        showStatus("Audio is ready. Press Play to continue.");
      }
    }
  }
}

function renderManifest(manifest) {
  const previousManifest = state.manifest;
  state.manifest = manifest;
  elements.playerPanel.hidden = false;

  if (!state.playerInitialized && manifest.ready_prefix_count > 0) {
    state.playerInitialized = true;
    const progress = state.reading?.progress;
    const restore = progress && progress.rendition_id === manifest.rendition_id
      ? progress
      : null;
    const savedSpeed = localSpeedHint();
    if (savedSpeed) elements.speed.value = String(savedSpeed);
    state.progressRevision = state.reading?.progress?.revision ?? null;
    const index = restore && restore.chunk_ordinal < manifest.ready_prefix_count
      ? restore.chunk_ordinal
      : 0;
    loadChunk(index, {
      autoplay: state.autoplayPending,
      restoreTime: restore?.offset_seconds || 0,
    });
    state.autoplayPending = false;
    elements.playerPanel.scrollIntoView({ behavior: "smooth", block: "end" });
  } else if (previousManifest && elements.audio.ended) {
    const nextIndex = state.chunkIndex + 1;
    if (nextIndex < manifest.ready_prefix_count) {
      loadChunk(nextIndex, { autoplay: true });
    }
  }

  const totalKnown = manifest.total_chunks;
  if (manifest.state === "ready") {
    showStatus(`Ready · ${totalKnown} section(s) generated.`);
  } else if (manifest.state === "generating") {
    showStatus(`Generating audio: ${manifest.ready_prefix_count} of ${totalKnown} section(s) ready.`);
  } else if (manifest.state === "failed") {
    showStatus(
      manifest.error_message || "Generation failed. The sections already ready remain playable.",
      true,
    );
  } else if (manifest.state === "cancelled") {
    showStatus("Cancelled. The sections already ready remain playable.");
  }
}

async function pollManifestOnce(renditionId, token) {
  try {
    const manifest = await api(`/api/renditions/${renditionId}/manifest`);
    if (token !== state.manifestPollToken) return;
    renderManifest(manifest);
    const settled = manifest.state === "ready" || manifest.state === "failed"
      || manifest.state === "cancelled";
    if (!settled) {
      state.manifestPollHandle = setTimeout(
        () => pollManifestOnce(renditionId, token),
        MANIFEST_POLL_MS,
      );
    }
  } catch {
    if (token !== state.manifestPollToken) return;
    state.manifestPollHandle = setTimeout(
      () => pollManifestOnce(renditionId, token),
      MANIFEST_POLL_MS,
    );
  }
}

function startManifestPolling(renditionId) {
  clearTimer("manifestPollHandle");
  const token = ++state.manifestPollToken;
  pollManifestOnce(renditionId, token);
}

async function pollReadingOnce() {
  const readingId = state.readingId;
  if (!readingId) return;
  try {
    const reading = await api(`/api/readings/${readingId}`);
    if (state.readingId !== readingId) return;
    state.reading = reading;
    renderReadingDocument(reading);
  } catch (error) {
    showStatus(error.message, true);
    return;
  }
  const reading = state.reading;
  const jobActive = reading.job && JOB_ACTIVE_STATES.has(reading.job.state);
  const readingActive = READING_ACTIVE_STATES.has(reading.state);
  if (jobActive || readingActive) {
    state.readingPollHandle = setTimeout(pollReadingOnce, READING_POLL_MS);
  }
}

function startReadingPolling() {
  clearTimer("readingPollHandle");
  pollReadingOnce();
}

function renderReadingDocument(reading) {
  if (reading.article) {
    renderReviewSection(reading);
  } else {
    elements.reviewPanel.hidden = false;
    elements.articleTitle.textContent = reading.title || "Preparing…";
    elements.articleState.textContent = readingStateLabel(reading.state);
    elements.articleBlocks.replaceChildren();
    elements.warning.hidden = true;
    elements.resolutionPanel.hidden = true;
    elements.readyPanel.hidden = true;
  }
  updateJobControls(reading.job);
  if (reading.rendition) {
    if (!state.manifest || state.manifest.rendition_id !== reading.rendition.rendition_id) {
      startManifestPolling(reading.rendition.rendition_id);
    }
  }
}

async function openReading(readingId, { touchHistory = true } = {}) {
  resetPlayer();
  clearTimer("readingPollHandle");
  state.readingId = readingId;
  state.reading = null;
  hideStatus();
  try {
    const reading = await api(`/api/readings/${readingId}`);
    state.reading = reading;
    renderReadingDocument(reading);
    elements.reviewPanel.scrollIntoView({ behavior: "smooth", block: "start" });
    startReadingPolling();
    if (touchHistory) loadHistory();
  } catch (error) {
    showStatus(error.message, true);
  }
}

async function submitReading(event) {
  event.preventDefault();
  resetView();
  elements.reviewPanel.hidden = true;
  elements.fetchButton.disabled = true;
  showStatus("Queuing this article for the local worker…");
  try {
    const choices = currentLanguageRequest();
    const result = await api("/api/readings", {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ url: elements.url.value.trim(), ...choices }),
    });
    await openReading(result.reading_id);
  } catch (error) {
    showStatus(error.message, true);
  } finally {
    elements.fetchButton.disabled = false;
  }
}

async function resolveReading() {
  if (!state.readingId || !state.reading) return;
  if (state.reading.state === "needs_review" && !elements.reviewAccepted.checked) {
    showStatus("Please confirm that you reviewed the extracted article first.", true);
    elements.reviewAccepted.focus();
    return;
  }
  elements.resolveButton.disabled = true;
  showStatus("Preparing the reviewed text…");
  try {
    const choices = currentLanguageRequest();
    const reading = await api(`/api/readings/${state.readingId}/resolve`, {
      method: "POST",
      body: JSON.stringify({ ...choices, accept_review: elements.reviewAccepted.checked }),
    });
    state.reading = reading;
    renderReadingDocument(reading);
    if (READING_ACTIVE_STATES.has(reading.state)) startReadingPolling();
  } catch (error) {
    showStatus(error.message, true);
  } finally {
    elements.resolveButton.disabled = false;
  }
}

async function generateAudio() {
  if (!state.readingId || !elements.voice.value) return;
  elements.generateButton.disabled = true;
  showStatus("Queuing audio generation…");
  state.autoplayPending = true;
  try {
    await api(`/api/readings/${state.readingId}/renditions`, {
      method: "POST",
      body: JSON.stringify({ voice_id: elements.voice.value }),
    });
    startReadingPolling();
  } catch (error) {
    state.autoplayPending = false;
    showStatus(error.message, true);
  } finally {
    elements.generateButton.disabled = false;
  }
}

async function cancelActiveJob() {
  const job = state.reading?.job;
  if (!job) return;
  elements.cancelButton.disabled = true;
  try {
    await api(`/api/jobs/${job.job_id}/cancel`, { method: "POST" });
    await pollReadingOnce();
  } catch (error) {
    showStatus(error.message, true);
  } finally {
    elements.cancelButton.disabled = false;
  }
}

async function retryFailedJob() {
  const job = state.reading?.job;
  if (!job) return;
  elements.retryButton.disabled = true;
  try {
    await api(`/api/jobs/${job.job_id}/retry`, { method: "POST" });
    startReadingPolling();
  } catch (error) {
    showStatus(error.message, true);
  } finally {
    elements.retryButton.disabled = false;
  }
}

function formatRelativeTime(isoString) {
  const then = new Date(isoString).getTime();
  const minutes = Math.max(0, Math.round((Date.now() - then) / 60000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}

async function loadHistory() {
  try {
    const result = await api("/api/readings");
    const readings = result.readings || [];
    elements.historyPanel.hidden = readings.length === 0;
    const fragment = document.createDocumentFragment();
    for (const reading of readings) {
      const item = document.createElement("li");
      item.className = "history-item";

      const main = document.createElement("div");
      main.className = "history-item-main";
      const titleButton = document.createElement("button");
      titleButton.type = "button";
      titleButton.className = "history-item-title";
      titleButton.textContent = reading.title || reading.submitted_url;
      titleButton.addEventListener("click", () => openReading(reading.reading_id));
      const meta = document.createElement("span");
      meta.className = "history-item-meta";
      meta.textContent = `${readingStateLabel(reading.state)} · ${formatRelativeTime(reading.last_opened_at)}`;
      main.append(titleButton, meta);

      const actions = document.createElement("div");
      actions.className = "history-item-actions";
      const deleteButton = document.createElement("button");
      deleteButton.type = "button";
      deleteButton.className = "icon-button";
      deleteButton.textContent = "Delete";
      deleteButton.addEventListener("click", async () => {
        deleteButton.disabled = true;
        try {
          await api(`/api/readings/${reading.reading_id}`, { method: "DELETE" });
          if (state.readingId === reading.reading_id) resetView();
          loadHistory();
        } catch (error) {
          showStatus(error.message, true);
          deleteButton.disabled = false;
        }
      });
      actions.append(deleteButton);

      item.append(main, actions);
      fragment.append(item);
    }
    elements.historyList.replaceChildren(fragment);
  } catch {
    // History is a convenience; a failure here should not block the main flow.
  }
}

elements.form.addEventListener("submit", submitReading);
elements.language.addEventListener("change", updateScriptChoice);
elements.resolveButton.addEventListener("click", resolveReading);
elements.generateButton.addEventListener("click", generateAudio);
elements.cancelButton.addEventListener("click", cancelActiveJob);
elements.retryButton.addEventListener("click", retryFailedJob);
elements.previousButton.addEventListener("click", () => {
  loadChunk(state.chunkIndex - 1, {});
});
elements.nextButton.addEventListener("click", () => {
  loadChunk(state.chunkIndex + 1, {});
});
elements.speed.addEventListener("change", () => {
  elements.audio.playbackRate = Number(elements.speed.value);
  elements.audio.preservesPitch = true;
  saveLocalSpeedHint();
  saveProgress(true);
});
elements.audio.addEventListener("play", () => {
  playbackChannel?.postMessage({ type: "playing", tabId });
});
elements.audio.addEventListener("pause", () => saveProgress(true));
elements.audio.addEventListener("timeupdate", () => saveProgress(false));
elements.audio.addEventListener("loadedmetadata", () => {
  if (state.restoreTime > 0 && state.restoreTime < elements.audio.duration) {
    elements.audio.currentTime = state.restoreTime;
  }
  state.restoreTime = 0;
});
elements.audio.addEventListener("ended", async () => {
  const manifest = state.manifest;
  if (manifest && state.chunkIndex + 1 < manifest.ready_prefix_count) {
    await loadChunk(state.chunkIndex + 1, { autoplay: true });
  } else if (manifest && state.chunkIndex + 1 < manifest.total_chunks) {
    showStatus("Waiting for the next section to finish generating…");
    saveProgress(true);
  } else {
    saveProgress(true);
    showStatus("Finished — nice work giving your eyes a rest.");
  }
});
window.addEventListener("beforeunload", () => saveProgress(true));

async function initialize() {
  updateScriptChoice();
  try {
    const response = await api("/api/voices");
    state.voices = response.voices;
  } catch (error) {
    showStatus(`Could not inspect local voices: ${error.message}`, true);
  }
  await loadHistory();
}

initialize();
