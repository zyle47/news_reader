# Article Reader

Article Reader is a local-first application that extracts useful article text and reads it aloud
in Serbian, English, or German. The project is being implemented incrementally from
[`article-reader-project-plan.md`](article-reader-project-plan.md).

The current M4 build has the complete local reading path: security-hardened public-HTTP(S)
extraction, approved local Piper voices, durable SQLite jobs/audio/history/progress, progressive
playback, and an optional paired-phone mode. Normal startup remains loopback-only. Explicit LAN
startup listens on loopback plus one selected private interface, and every reading/job/audio route
requires a revocable browser session. A phone joins the desktop library through a five-minute,
single-use code or QR link; raw session tokens are never stored or placed in URLs.

## Development setup

Install `uv`, then run:

```text
uv sync --locked
uv run --locked article-reader doctor
uv run --locked article-reader voices list
uv run --locked article-reader evaluate-voices --fake --language sr
uv run --locked article-reader serve
uv run --locked pytest -q
```

`serve` opens `http://localhost:8765/` and starts the durable worker in the background. Paste an
article URL; the page queues it, polls until extraction finishes, then walks through review and
language resolution when needed. Choose a voice to queue audio generation — playback starts on the
first ready section instead of waiting for the whole article, and you can cancel or retry while it
generates. The reader keeps section navigation, 0.75x-1.5x speed, cross-tab playback coordination,
a recent-readings history list, and persists playback position and history across restarts. It is
available only from this computer unless LAN mode is deliberately enabled.

To listen from a phone on the same trusted Wi-Fi, double-click **`start-reader-lan.cmd`**, or run:

```powershell
.\runtime\venv312\Scripts\python.exe -m article_reader --data-dir runtime\voice-data serve --lan
```

Open **Devices** on the desktop page and scan the QR code or enter its one-time code on the phone.
The app never opens router ports or creates public exposure. LAN mode uses ordinary HTTP: pairing
controls application access, but traffic is not confidential against someone who can sniff the
local network. Close the server window or press `Ctrl+C` to stop both listeners immediately.

On the currently inspected Windows host, all three approved voices are installed under
`runtime/voice-data`, so the direct command is:

```powershell
.\runtime\venv312\Scripts\python.exe -m article_reader --data-dir runtime\voice-data serve
```

Or simply double-click **`start-reader.cmd`** in this folder. Keep its small server window
open while listening; closing it stops the local app.

To try the real Piper engine, explicitly install an approved voice (downloads and SHA-256-verifies a
real ~61 MiB model over HTTPS) and evaluate it:

```text
uv run --locked article-reader voices install en_US-ljspeech-medium
uv run --locked article-reader evaluate-voices --voice-id en_US-ljspeech-medium
```

After the three approved voices are installed, `doctor` reports the host ready. It remains
fail-closed on a machine where any required model is absent or fails checksum verification.

To prepare a local text file (normalize + segment, no article fetching involved) or run a
long-form voice evaluation against your own text:

```text
uv run --locked article-reader prepare-text <your-file.txt> --language en
uv run --locked article-reader evaluate-voices --voice-id en_US-ljspeech-medium --text-file <your-file.txt>
```

`--script` is required whenever the language/voice supports more than one script (Serbian).

To fetch and inspect a public article now:

```text
uv run --locked article-reader fetch-article "https://example.com/article" --output runtime/article.json
```

The command follows only revalidated public HTTP(S) redirects, downloads bounded HTML, extracts
ordered blocks, and atomically writes a versioned JSON preview. Inspect `needs_review`,
`review_reasons`, and any block with `requires_review` before treating the result as complete. Some
paywalled, consent-gated, or JavaScript-only pages require the existing manual-text path.

To run the complete M2 preparation pipeline:

```text
uv run --locked article-reader prepare-article "https://example.com/article" --output runtime/prepared-article.json
```

The default `--language auto` uses a local detector and abstains on short, low-confidence,
unsupported, metadata-conflicting, or Serbian/Croatian/Bosnian-ambiguous text. Resolve an article
review with `--accept-review` only after inspecting the preview. For Serbian Latin, explicitly use
`--language sr --script latin`; Serbian Cyrillic can be detected automatically when evidence is
strong, or overridden with `--language sr --script cyrillic`.

See `docs/SETUP.md`, `docs/STATUS.md`, and `docs/HANDOFF.md` for setup, verified status, and a
compact continuation guide.
