# Development setup

## Supported status

Windows 11 x64 is the only environment inspected so far. The design is cross-platform, but POSIX
commands below are unverified until they run on an actual machine. The initial runtime target is
CPython 3.12; `uv` may install a matching managed interpreter when one is unavailable.

Do not copy `.venv` between PCs. Clone/copy the source revision and lockfile, then create an
environment independently on each machine.

## Windows PowerShell

Install `uv` using an official Astral installation method, then from the repository directory run:

```powershell
uv sync --locked
uv run --locked article-reader doctor
uv run --locked article-reader voices list
uv run --locked article-reader evaluate-voices --fake --language sr
uv run --locked article-reader serve
uv run --locked pytest -q
```

The reader opens at `http://localhost:8765/`. `serve` also starts one durable background worker
that persists jobs, reading history, and audio to SQLite under the data directory, so reading
progress survives closing the page and restarting the app. Normal startup is always loopback-only.

For the inspected project workspace, all three approved models already live in
`runtime/voice-data`. Launch that exact setup with:

```powershell
.\runtime\venv312\Scripts\python.exe -m article_reader --data-dir runtime\voice-data serve
```

Keep the terminal open while using the page and press `Ctrl+C` to stop the server. Use
`serve --no-open` if the browser should not open automatically.

For normal use on this Windows workspace, double-click `start-reader.cmd` instead of typing
the command. It prefers the verified CPython 3.12 environment, detects the existing
`runtime/voice-data` models, and opens the reader automatically. Its server window must remain open
while the page is in use.

## Phone access on trusted Wi-Fi

LAN access is explicit on every launch. Double-click `start-reader-lan.cmd` and confirm its warning,
or run:

```powershell
.\runtime\venv312\Scripts\python.exe -m article_reader --data-dir runtime\voice-data serve --lan
```

The launcher selects the private address used by the default route, prints both the desktop and
phone URLs, and listens only on `127.0.0.1` plus that one private address. If auto-detection chooses
the wrong adapter (for example, a VPN or virtual switch), restart with an address shown by
`ipconfig`:

```powershell
.\runtime\venv312\Scripts\python.exe -m article_reader --data-dir runtime\voice-data serve --lan --bind 192.168.0.12
```

Open the desktop URL first, choose **Devices**, then scan the QR code or enter the eight-digit code
on the phone page. A code expires after five minutes and works once. Paired sessions survive an app
restart for 30 days by default; the desktop Devices panel lists and revokes them. Disconnect on the
phone revokes its current session. Stopping the server removes LAN exposure immediately.

Windows may show a firewall prompt the first time. Permit the private network only if you want
phone access; never enable public-network access or router port forwarding. Both devices must be on
the same non-isolated Wi-Fi. VPN routing, guest Wi-Fi isolation, a changed DHCP address, or a
sleeping host can prevent connection.

LAN mode is HTTP, not HTTPS. Authentication blocks unpaired application use, Host/Origin checks
block common browser attacks, and no permissive CORS is enabled, but traffic is not confidential
against local-network sniffing. Use only a trusted private network.

## POSIX shell (not yet tested)

```sh
uv sync --locked
uv run --locked article-reader doctor
uv run --locked article-reader voices list
uv run --locked article-reader evaluate-voices --fake --language sr
uv run --locked pytest -q
```

## Configuration and data

Copy `config.example.toml` to `config.toml` only when a local override is needed. Precedence is:
checked-in defaults, local TOML, `ARTICLE_READER_` environment variables, then explicit CLI values.

By default, runtime data uses the operating system's per-user application-data location. Set an
explicit data directory for development or diagnostics rather than placing databases, models,
audio, secrets, or private exports in Git. Spaces, non-ASCII usernames, and Cyrillic path segments
are supported design requirements.

No model is downloaded during normal startup, diagnostics, or a reading request. Voice installation
is an explicit, checksummed command.

Until the catalogue contains installed and approved coverage for all required languages/scripts,
`doctor` intentionally exits with status 1 while distinguishing a healthy host from an unready
product. Use `--data-dir` to keep development artifacts in an explicit machine-local path.

To exercise the current article-ingestion preview:

```powershell
uv run --locked article-reader fetch-article "https://example.com/" --output runtime/article.json
uv run --locked article-reader prepare-article "https://example.com/" --output runtime/prepared.json
```

Only public HTTP(S) pages are supported. The output manifest contains article text, so treat it as
private runtime data. Review its `needs_review` diagnostics before using the speech representation;
blocked, paywalled, or JavaScript-only sources should use `prepare-text` with a local UTF-8 file.

`prepare-article` exits 3 when it safely reaches an awaiting-input state. After inspecting a
review-required preview, rerun with `--accept-review`. Automatic Serbian Latin identification is
deliberately not claimed: use `--language sr --script latin`. Any explicit Serbian override requires
`--script latin` or `--script cyrillic`.
