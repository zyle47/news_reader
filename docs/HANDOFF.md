# AI handoff

Updated: 2026-09-13

## Current boundary

M0–M4 are implemented for this local-first English/German/Serbian article reader. M3's durable
SQLite queue, progressive Piper chunks, protected audio, history, and revisioned progress remain
authoritative. M4 adds explicit paired-phone access, durable revocable sessions, dual-socket LAN
serving, and the mobile browser seam. Ordinary `serve` is still loopback-only.

## Architecture map

Dependencies point inward: CLI/API/worker → application services → application-owned ports →
domain. Concrete SQLite, networking, extraction, Piper, filesystem, and QR libraries stay in
adapters/composition roots.

- `application/services/access_control.py`: bounded in-memory one-time pairing policy, per-client
  attempt limits, durable session issuance/authentication/revocation. Clock and randomness are
  injectable; raw codes/tokens are never persisted.
- `application/ports/persistence.py`, `db/migrations.py`, `db/repositories/viewers.py`: schema v2
  `viewer_sessions` boundary. Existing v1 tokens migrate. Session lookup checks expiry and
  revocation; phone and initiating desktop intentionally share one `viewer_id`.
- `network.py`: RFC1918/IPv6-ULA discovery and validation. Default-route source probing is ordered
  ahead of hostname adapters so a physical Wi-Fi/Ethernet address wins over virtual switches.
- `cli.py`: `serve --lan [--bind PRIVATE_IP]` composes the same app/worker but pre-binds exactly
  `127.0.0.1` plus one selected private address and disables proxy-header trust.
- `api/app.py`: Host/Origin/JSON boundary, universal response headers, session resolution, pairing,
  device listing/revocation, and all M3 durable routes. Only loopback may bootstrap a local viewer
  or create a pairing offer; every sensitive route including HEAD/Range audio requires a session.
- `security/pairing_qr.py`: the only Segno import; returns bounded inline SVG data for the local
  fragment URL.
- `web/`: paired-device UI, responsive mobile player, reconnect polling, progress keepalive,
  Media Session and optional Wake Lock. Untrusted article content continues to use text nodes only.
- `worker/loop.py`, `storage/durable_audio.py`, safe fetch/extraction, and speech adapters retain
  the M2/M3 invariants documented in `docs/DECISIONS.md`.

## Non-negotiable invariants

- LAN exposure needs `serve --lan` on every launch; never silently fall back to `0.0.0.0`, a public
  address, permissive CORS, forwarded headers, or unauthenticated audio.
- The pairing code belongs only in the response to an authenticated loopback request and a URL
  fragment. Never put access codes/session tokens in query strings, logs, docs, or filenames.
- Plain HTTP is not confidential. Do not claim HTTPS properties, open firewall/router rules, or
  add public tunnels under the LAN flag.
- All submitted article URLs retain the DNS-pinned, redirect-revalidated SSRF policy in ADR-014.
- Never fetch/synthesize inside an API handler or DB transaction. A worker publication still needs
  the current generation token and cancellation check.
- Preserve original article text, explicit Serbian script selection, extraction review gates, and
  approved voice compatibility. Runtime article/audio/session data never enters Git/packages.
- Keep CPython `>=3.12,<3.13`, deterministic offline tests, and strict typing.

## Run

```powershell
# Desktop only
.\runtime\venv312\Scripts\python.exe -m article_reader --data-dir runtime\voice-data serve

# Phone on the same trusted Wi-Fi (or double-click start-reader-lan.cmd)
.\runtime\venv312\Scripts\python.exe -m article_reader --data-dir runtime\voice-data serve --lan

# Override a wrongly selected VPN/virtual adapter
.\runtime\venv312\Scripts\python.exe -m article_reader --data-dir runtime\voice-data serve --lan --bind 192.168.0.12
```

Open the desktop URL, choose **Devices**, then scan/enter the five-minute one-time code. The Devices
panel revokes other browsers; **Disconnect** revokes the phone itself. Stop the process to end both
listeners.

## Verified state and next task

See `docs/STATUS.md` for exact evidence. Real Windows dual-socket and separate-host browser pairing
worked at `192.168.0.12`; a real paired API run fetched the Intermagazin Serbian/Cyrillic article
and generated three Marko chunks, first ready in 3.995 seconds. No physical phone or PC B was
available, so do not convert browser evidence into a device claim. Final gates pass: 388 tests,
strict typing, lint/format/JavaScript checks, locked offline sync/build, archive inspection, and a
fresh wheel-only install plus loopback serve smoke test.

Next: M5 whole-rendition LRU audio eviction with playback leases/disk thresholds, followed by
bounded versioned reading-bundle export/import and the physical-phone/PC B pilot. Do not mix active
SQLite files across PCs.
