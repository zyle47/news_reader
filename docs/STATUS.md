# Implementation status

Updated: 2026-09-13

## Current milestone

M0–M4 are implemented. The local reader now supports explicit, authenticated phone access on a
trusted LAN while ordinary startup remains loopback-only. Durable jobs, progressive Piper audio,
history, and revisioned progress from M3 are preserved; a paired phone joins the desktop viewer so
both devices see the same library and resume position.

## M4 implementation

- `serve --lan` is required on every LAN launch, even if TOML enables LAN mode. It discovers the
  OS default-route private address or validates `--bind`, then gives Uvicorn two explicit sockets:
  `127.0.0.1` and one RFC1918/IPv6-ULA address. It never substitutes `0.0.0.0`; public,
  link-local, multicast, reserved, hostname, and unassigned targets are rejected. Forwarded
  headers are disabled.
- An authenticated loopback browser creates a five-minute one-time pairing offer. Its eight-digit
  code is held only as a digest in bounded locked memory, compared in constant time, consumed once,
  and rate-limited per source address. The QR puts the code after `#`, never in an HTTP request,
  access log, or referrer; the phone removes the fragment before its JSON redemption request.
  Restarting intentionally invalidates all outstanding offers.
- Migration 2 replaces M3's `viewer_tokens` seam with labelled, expiring, individually revocable
  `viewer_sessions`. Browsers receive a high-entropy HttpOnly/SameSite=Strict token; SQLite stores
  only SHA-256. Existing M3 tokens/readings migrate without loss. Sessions persist across restart
  and default to 30 days. The bounded device limit fails explicitly instead of silently revoking
  the desktop identity.
- Every sensitive route—voices, readings, article blocks, history, jobs, manifests, progress,
  sessions, and audio GET/HEAD/Range—is authenticated and owner-scoped. Revocation immediately
  removes API and audio access.
- Exact Host and Origin sets contain only the served endpoints. Mutations require an allowed
  Origin and JSON content type. CORS stays closed. All responses receive CSP, no-referrer,
  nosniff, frame denial, same-origin resource policy, a restrictive permissions policy, and
  no-store unless an endpoint supplies its deliberate private audio-cache policy.
- The responsive UI adds QR/manual pairing, device listing/revocation, disconnect, touch layouts,
  a compact sticky player, honest pending/reconnect messages, bounded exponential polling,
  keepalive progress writes, clean rendition switching, Media Session actions, and optional Screen
  Wake Lock. Article text still enters the DOM only through `textContent`/text nodes. There is no
  remote asset, service worker, or autoplay promise.
- `start-reader-lan.cmd` shows a warning and requires confirmation. `start-reader.cmd lan` is the
  terminal equivalent. Stopping the process closes both listeners.

## Security model

LAN mode targets a trusted home network plus potentially hostile webpages. Pairing/session
authentication, ownership, Host/Origin validation, JSON mutations, closed CORS, and CSP protect
application access and common browser attacks, including DNS rebinding and CSRF. Plain HTTP does
not encrypt article text, audio, pairing codes, or cookies against an on-path LAN attacker. The
launcher and UI disclose this. The app does not configure firewall rules, router forwarding,
public tunnels, or internet exposure.

## Verification evidence

- Deterministic offline coverage includes address selection, explicit opt-in, M3→M4 migration,
  restart/expiry/revocation/device limits, malformed/expired/reused/brute-forced codes,
  unauthenticated API and audio, QR bounds, Host/Origin/CORS enforcement, shared cross-device
  progress/conflicts, and authenticated HEAD/Range delivery.
- A real Windows dual-socket server bound to `127.0.0.1:8765` and `192.168.0.12:8765`; desktop
  access established a local session while the LAN endpoint exposed only the pairing shell and
  returned 401 for readings.
- A real browser created a QR/code in the desktop Devices panel, opened the LAN-IP page as a
  separate host identity, removed the fragment, paired, and reached the reader. This found a real
  insecure-context bug: `crypto.randomUUID()` is unavailable over LAN HTTP. M4 now feature-detects
  it and uses a non-authentication fallback; all authentication randomness remains server-side via
  Python `secrets`.
- Through the real paired LAN API, the established Intermagazin article selected `sr/cyrillic` and
  synthesized all three chunks with installed `sr-marko-medium`. The first chunk became ready in
  3.995 seconds and all three completed. Authenticated integration tests verify HEAD=200 and a
  ten-byte Range=206; the earlier live M3 real-audio smoke also verified Range=206 across restart.
- Smoke servers were terminated; `doctor` confirms port 8765 is free and the voice-data directory
  is ready. The runtime-only pre-migration copy is
  `runtime/voice-data/article-reader.pre-m4.sqlite3.backup`.

Release gates pass on CPython 3.12.14: `ruff check`, `ruff format --check` (116 files), strict
`mypy` (108 source files), JavaScript syntax checking, and `pytest` (388 passed, 1 skipped for the
documented Windows symlink privilege, 5 subtests). `uv lock --check` resolves 57 packages;
locked offline sync and offline sdist/wheel builds pass. A new isolated environment installed only
the built wheel and its 42 runtime dependencies from the offline cache, reported version 0.1.0,
served `/api/auth` successfully, and released its test port afterward. Archive inspection confirms
the M4 network/auth/QR/UI assets and both launchers are present while runtime data, SQLite files,
audio, local config, environments, and caches are absent.

The CLI catches the `KeyboardInterrupt` that Uvicorn can re-raise after completing shutdown, so a
Ctrl+C stop now returns success with `Article Reader stopped.` instead of printing a Python
`CancelledError`/`KeyboardInterrupt` traceback. A regression test fixes this behavior at the CLI
boundary; cleanup remains in `_run_serve`'s `finally` blocks.

## Known gaps

- No physical phone was available to the coding agent. Pairing is verified in a real browser at
  the real LAN address, but iOS/Android chunk transitions, sleep, media controls, and Wi-Fi
  reconnection still require the owner's short checklist.
- HTTP has no transport confidentiality. Internet-facing use requires a separate HTTPS and
  authentication design; it must not grow implicitly from this trusted-LAN mode.
- Audio eviction/playback leases/disk caps, portable bundles, PC B, and true multi-process worker
  supervision remain unimplemented.
- A genuinely hung Piper native call retains M3's accepted daemon-thread timeout limitation.

## Next concrete milestone

M5: implement whole-rendition LRU audio eviction with playback leases and disk/free-space caps,
then bounded versioned reading-bundle export/import. Finish with the physical-phone and PC B pilot.
