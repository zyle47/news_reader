const elements = {
  form: document.querySelector("#article-form"),
  url: document.querySelector("#article-url"),
  language: document.querySelector("#language"),
  scriptGroup: document.querySelector("#script-group"),
  script: document.querySelector("#script"),
  fetchButton: document.querySelector("#fetch-button"),
  status: document.querySelector("#status"),
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
  currentSegment: document.querySelector("#current-segment"),
  audio: document.querySelector("#audio-player"),
  previousButton: document.querySelector("#previous-button"),
  nextButton: document.querySelector("#next-button"),
  speed: document.querySelector("#playback-speed"),
};

const state = {
  voices: [],
  preview: null,
  rendition: null,
  chunkIndex: 0,
  restoreTime: 0,
  lastProgressWrite: 0,
};

const languageNames = { en: "English", de: "German", sr: "Serbian" };
const reviewLabels = {
  missing_title: "The page did not expose a reliable title.",
  short_content: "The extracted article is unusually short.",
  few_blocks: "Only a small number of content sections were found.",
  restriction_page: "The page may be a consent, login, or restriction screen.",
  complex_content: "Some complex content needs human review.",
};

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
    headers: options.body ? { "Content-Type": "application/json" } : {},
  });
  const contentType = response.headers.get("content-type") || "";
  const document = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) {
    throw new Error(document?.error?.message || `Request failed (${response.status}).`);
  }
  return document;
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

function setBusy(busy) {
  elements.fetchButton.disabled = busy;
  elements.resolveButton.disabled = busy;
  elements.generateButton.disabled = busy || elements.voice.disabled || !state.preview?.preparation;
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
  state.rendition = null;
  state.chunkIndex = 0;
  document.querySelectorAll(".article-block.active").forEach((block) => {
    block.classList.remove("active");
  });
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

function matchingVoices(preview) {
  const language = preview.language.selected_language;
  const script = preview.language.selected_script;
  return state.voices.filter((voice) =>
    voice.installed
    && voice.evaluation_status === "approved"
    && voice.language === language
    && voice.scripts.includes(script)
  );
}

function renderVoiceChoices(preview) {
  const voices = matchingVoices(preview);
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
    ? `${preview.preparation.segment_count} listening sections · audio stays local`
    : "No approved compatible voice is installed in this data directory.";
}

function renderPreview(preview) {
  state.preview = preview;
  resetPlayer();
  elements.reviewPanel.hidden = false;
  elements.articleTitle.textContent = preview.title || "Untitled article";
  elements.playerTitle.textContent = preview.title || "Article audio";
  elements.sourceLink.href = preview.final_url;
  const language = preview.language.selected_language;
  const script = preview.language.selected_script;
  elements.articleState.textContent = language
    ? `${languageNames[language]} · ${script}`
    : "Language choice needed";
  renderBlocks(preview.blocks);

  const needsReview = preview.review.required && !preview.review.accepted;
  elements.warning.hidden = !needsReview;
  elements.reviewAccepted.checked = false;
  if (needsReview) {
    const messages = preview.review.reasons.map((reason) => reviewLabels[reason] || reason);
    elements.warningMessage.textContent = messages.join(" ")
      || "One or more sections need a quick human check.";
  }

  const ready = preview.status === "ready";
  elements.readyPanel.hidden = !ready;
  elements.resolutionPanel.hidden = ready;
  if (ready) {
    renderVoiceChoices(preview);
    showStatus(
      `Ready: ${preview.preparation.segment_count} sections and `
      + `${preview.preparation.speech_character_count.toLocaleString()} characters.`
    );
  } else {
    elements.resolutionMessage.textContent = preview.status === "needs_language_override"
      ? "Choose the article language above, then prepare this saved extraction."
      : "Review the extracted text, confirm it above, then prepare this saved extraction.";
    elements.resolveButton.textContent = needsReview ? "Approve and prepare" : "Prepare article";
    showStatus("The extraction is ready for your review.");
  }
  elements.reviewPanel.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function fetchArticle(event) {
  event.preventDefault();
  hideStatus();
  resetPlayer();
  elements.reviewPanel.hidden = true;
  setBusy(true);
  showStatus("Fetching and extracting the article on this computer…");
  try {
    const choices = currentLanguageRequest();
    const preview = await api("/api/previews", {
      method: "POST",
      body: JSON.stringify({
        url: elements.url.value.trim(),
        ...choices,
        accept_review: false,
      }),
    });
    renderPreview(preview);
  } catch (error) {
    showStatus(error.message, true);
  } finally {
    setBusy(false);
  }
}

async function resolvePreview() {
  if (!state.preview) return;
  if (state.preview.review.required && !elements.reviewAccepted.checked) {
    showStatus("Please confirm that you reviewed the extracted article first.", true);
    elements.reviewAccepted.focus();
    return;
  }
  setBusy(true);
  showStatus("Preparing the reviewed text without fetching the page again…");
  try {
    const choices = currentLanguageRequest();
    const preview = await api(`/api/previews/${state.preview.preview_id}/prepare`, {
      method: "POST",
      body: JSON.stringify({
        ...choices,
        accept_review: elements.reviewAccepted.checked,
      }),
    });
    renderPreview(preview);
  } catch (error) {
    showStatus(error.message, true);
  } finally {
    setBusy(false);
  }
}

function progressKey() {
  if (!state.preview || !state.rendition) return null;
  return `article-reader:progress:${state.preview.final_url}:${state.rendition.voice_id}`;
}

function saveProgress(force = false) {
  const key = progressKey();
  if (!key) return;
  const now = Date.now();
  if (!force && now - state.lastProgressWrite < 5000) return;
  state.lastProgressWrite = now;
  localStorage.setItem(key, JSON.stringify({
    chunkIndex: state.chunkIndex,
    currentTime: elements.audio.currentTime || 0,
    speed: elements.audio.playbackRate,
  }));
}

function storedProgress() {
  const key = progressKey();
  if (!key) return null;
  try {
    const saved = JSON.parse(localStorage.getItem(key));
    if (
      Number.isInteger(saved?.chunkIndex)
      && saved.chunkIndex >= 0
      && Number.isFinite(saved?.currentTime)
      && saved.currentTime >= 0
    ) {
      return saved;
    }
  } catch {
    // A malformed local-only hint is safely ignored.
  }
  return null;
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

async function loadChunk(index, autoplay = false, restoreTime = 0) {
  if (!state.rendition || index < 0 || index >= state.rendition.chunks.length) return;
  saveProgress(true);
  state.chunkIndex = index;
  const chunk = state.rendition.chunks[index];
  elements.audio.src = chunk.audio_url;
  elements.audio.playbackRate = Number(elements.speed.value);
  elements.currentSegment.textContent = chunk.speech_text;
  elements.playerPosition.textContent = `Section ${index + 1} of ${state.rendition.chunks.length}`;
  elements.previousButton.disabled = index === 0;
  elements.nextButton.disabled = index === state.rendition.chunks.length - 1;
  highlightBlocks(chunk);
  if (restoreTime > 0) {
    state.restoreTime = restoreTime;
  }
  if (autoplay) {
    try {
      await elements.audio.play();
    } catch {
      showStatus("Audio is ready. Press Play to continue.");
    }
  }
}

async function generateAudio() {
  if (!state.preview || !elements.voice.value) return;
  setBusy(true);
  showStatus(
    `Generating ${state.preview.preparation.segment_count} audio sections locally. `
    + "Keep this page open…"
  );
  try {
    state.rendition = await api(`/api/previews/${state.preview.preview_id}/audio`, {
      method: "POST",
      body: JSON.stringify({ voice_id: elements.voice.value }),
    });
    elements.playerPanel.hidden = false;
    const saved = storedProgress();
    if (saved?.speed && [0.75, 1, 1.25, 1.5].includes(saved.speed)) {
      elements.speed.value = String(saved.speed);
    }
    const index = saved && saved.chunkIndex < state.rendition.chunks.length
      ? saved.chunkIndex
      : 0;
    await loadChunk(index, false, saved?.currentTime || 0);
    const minutes = Math.max(1, Math.round(state.rendition.total_duration_seconds / 60));
    showStatus(`Audio ready · about ${minutes} min · press Play when you’re ready.`);
    elements.playerPanel.scrollIntoView({ behavior: "smooth", block: "end" });
  } catch (error) {
    showStatus(error.message, true);
  } finally {
    setBusy(false);
  }
}

elements.form.addEventListener("submit", fetchArticle);
elements.language.addEventListener("change", updateScriptChoice);
elements.resolveButton.addEventListener("click", resolvePreview);
elements.generateButton.addEventListener("click", generateAudio);
elements.previousButton.addEventListener("click", () => loadChunk(state.chunkIndex - 1, false));
elements.nextButton.addEventListener("click", () => loadChunk(state.chunkIndex + 1, false));
elements.speed.addEventListener("change", () => {
  elements.audio.playbackRate = Number(elements.speed.value);
  elements.audio.preservesPitch = true;
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
  if (state.rendition && state.chunkIndex + 1 < state.rendition.chunks.length) {
    await loadChunk(state.chunkIndex + 1, true);
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
}

initialize();
