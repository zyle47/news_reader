# Article Reader

Article Reader is a local-first application that will extract useful article text and read it
aloud in Serbian, English, or German. The project is being implemented incrementally from
[`article-reader-project-plan.md`](article-reader-project-plan.md).

The current increment includes the architecture, diagnostics, three approved local Piper voices,
text preparation, a security-hardened public-HTTP(S) article fetch/extraction pipeline, and a
loopback browser preview reader. The
fluent project owner approved the English, German, and Serbian voices for personal article reading
after short and sustained listening. M2 now runs from a public URL through extraction, conservative
language/script selection, source-traceable speech segments, real Piper audio, and in-page playback.
Durable jobs and saved reading history come in later milestones.

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

`serve` opens `http://localhost:8765/`. Paste an article URL, review the extracted text, choose its
language/script when needed, generate audio, and listen without using the preparation CLI. The
preview includes section navigation, 0.75x-1.5x speed, same-browser position restore, and
cross-tab playback coordination. It is intentionally available only from this computer until LAN
authentication is implemented.

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
