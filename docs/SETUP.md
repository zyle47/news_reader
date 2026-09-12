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
progress survives closing the page and restarting the app. It is loopback-only; do not change the
bind address or enable LAN mode until the access-code/session milestone is implemented.

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
