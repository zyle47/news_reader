const GLib = imports.gi.GLib;
const [, bytes] = GLib.file_get_contents(ARGV[0]);
const source = new TextDecoder().decode(bytes).replace(/\ninitialize\(\);\s*$/, "");
function assert(value, message) { if (!value) throw new Error(message); }
function fixture(onion) {
  const nodes = new Map();
  function element() {
    return {
      hidden: true, value: "", disabled: false, paused: true,
      dataset: {}, lastChild: { textContent: "" },
      classList: { toggle() {}, remove() {}, add() {} },
      addEventListener() {}, pause() { this.paused = true; },
      load() {}, removeAttribute(name) { delete this[name]; },
      replaceChildren() {}, scrollIntoView() {}, focus() {}, append() {},
    };
  }
  const document = {
    body: { dataset: { onionMode: String(onion) } },
    querySelector(id) { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); },
    querySelectorAll() { return []; }, addEventListener() {},
    createDocumentFragment: element, createElement: element,
  };
  const timers = new Map(); let nextTimer = 0;
  const calls = []; let handler;
  const fetch = async (path, options) => {
    calls.push({ path, options });
    const result = handler(path, options);
    return { ok: (result.status || 200) < 400, status: result.status || 200,
      headers: { get() { return "application/json"; } }, async json() { return result.body; } };
  };
  const window = { location: { hash: "" }, addEventListener() {} };
  const methods = new Function("document", "window", "navigator", "fetch", "history", "location", "localStorage", "setTimeout", "clearTimeout", "URLSearchParams",
    source + "\nreturn { initialize, loginOwner, api, state, elements, renderManifest, loadChunk };"
  )(document, window, {}, fetch, {}, {}, { getItem() { throw Error('Disabled'); } },
    (callback, delay) => { const id = ++nextTimer; timers.set(id, { callback, delay }); return id; },
    (id) => timers.delete(id), function () { this.get = () => null; });
  return { ...methods, calls, timers, route(fn) { handler = fn; } };
}
async function run() {
  const f = fixture(true);
  f.route(() => ({ body: { authenticated: false, onion_mode: true } }));
  await f.initialize();
  assert(!f.elements.loginPanel.hidden && f.elements.pairingPanel.hidden, "Onion opens login");
  assert(f.calls.length === 1 && f.calls[0].path === "/api/auth", "No private requests before authentication");
  f.elements.ownerPassword.value = "secret";
  f.route(() => ({ status: 401, body: { error: { code: "LOGIN_REJECTED" } } }));
  await f.loginOwner({ preventDefault() {} });
  assert(f.elements.ownerPassword.value === "", "Rejected password cleared");
  assert(f.elements.loginStatus.textContent.includes("owner password"), "Generic rejected login");
  assert(!f.elements.loginPanel.hidden && f.elements.pairingPanel.hidden, "Login rejection never offers LAN pairing");
  f.route(path => ({ body: path === "/api/voices" ? { voices: [] }
    : path === "/api/readings" ? { readings: [] }
    : { authenticated: true, onion_mode: true, lan_mode: false, loopback: false } }));
  f.elements.ownerPassword.value = "secret";
  await f.loginOwner({ preventDefault() {} });
  assert(!f.elements.appShell.hidden && f.elements.loginPanel.hidden, "Successful login opens library");
  assert(f.elements.ownerPassword.value === "", "Successful password cleared");
  assert(!f.elements.devicesButton.hidden && !f.elements.logoutButton.hidden, "Owner sees devices and sign out");
  assert(f.elements.createPairingButton.hidden, "Owner never sees pairing offer");
  const manifest = { rendition_id: "r1", voice_id: "voice", state: "ready", ready_prefix_count: 0,
    total_chunks: 1, chunks: [{ ordinal: 0, state: "evicted", audio_url: null }] };
  f.renderManifest(manifest);
  assert(!f.elements.regenerateButton.hidden, "Eviction offers regeneration");
  assert(f.elements.playerPosition.textContent === "Audio cleared", "Eviction is explained");
  manifest.chunks[0] = { ordinal: 0, state: "ready", audio_url: "/audio/one", speech_text: "hello", source_block_ordinals: [] };
  await f.loadChunk(0);
  assert(f.elements.audio.src === "/audio/one", "Audio loads after obtaining lease");
  const timer = [...f.timers.values()].find(timer => timer.delay === 45000);
  assert(timer, "Lease refresh scheduled below 60 seconds");
  const leaseCalls = f.calls.filter(call => call.path.endsWith("/lease")).length;
  await timer.callback();
  assert(f.calls.filter(call => call.path.endsWith("/lease")).length > leaseCalls, "Lease renews while paused");
  f.route(() => ({ status: 401, body: { error: { code: "AUTH_REQUIRED" } } }));
  try { await f.api("/api/voices"); } catch {}
  assert(!f.elements.loginPanel.hidden && f.elements.pairingPanel.hidden, "Expired owner session returns to login");
  assert(f.state.manifest === null && f.state.readingId === null, "Expired owner session clears player selection");
  const lan = fixture(false);
  lan.route(() => ({ body: { authenticated: false, onion_mode: false, lan_mode: true } }));
  await lan.initialize();
  assert(!lan.elements.pairingPanel.hidden && lan.elements.loginPanel.hidden, "LAN pairing preserved");
  print("UI smoke passed: onion login, password clearing, owner sessions, 401 recovery, eviction, paused leases, LAN pairing.");
}
const loop = GLib.MainLoop.new(null, false);
let failed = false;
run().catch(error => { failed = true; printerr(error.stack); }).finally(() => loop.quit());
loop.run();
if (failed) imports.system.exit(1);
