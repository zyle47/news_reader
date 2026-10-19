# Private Reader over Tor

This is a standalone Linux deployment, separate from `django_chat`. The 2026-10-19 host
trial works through a real Tor client. Physical phone/browser acceptance remains open; see
[TOR_ACCEPTANCE.md](TOR_ACCEPTANCE.md). The owner library lives on this server. Article sites
are fetched using the server's normal internet connection; the phone reaches Reader over Tor.

## Current installation

Run commands from `reader/news_reader`. The Compose project is `reader-onion` and publishes
no host ports. Reader and Tor share only a Unix socket and public hostname metadata, with
separate outbound networks. Reader cannot mount the Tor identity keys. Both services run
non-root with resource/log limits. Their status is visible with:

```sh
docker compose ps
docker compose logs --tail 30 reader reader-tor
docker compose exec -T reader cat /run/reader-onion/hostname
```

Open `http://<that-hostname>/` in your phone's Tor browser. The first installation generated
an owner password in `runtime/onion-initial-password.txt` on this host, mode 0600, in an ignored
directory. Read it locally; it is deliberately absent from logs and documentation. Choose
your own password using the reset procedure below, then remove that initial-password file.
Cookies remember a browser for up to 30 days. Tor's cookie clearing requires password login
again, restoring the same library. Browser security settings may affect JavaScript/audio.

## Automatic Linux startup

The installed user crontab starts Reader at boot using:

```cron
@reboot /bin/sh /home/zyle44/Documents/nemanja/reader/news_reader/deploy/start-on-boot.sh
```

`deploy/start-on-boot.sh` waits up to 180 seconds for Docker, starts the existing `reader-onion`
services, and waits for their health checks. It uses absolute command paths, prevents overlapping
runs and keeps a private `runtime/boot.log` with one rotated log file. It performs no image build,
pull, model installation or owner administration. Run it manually to check the same path:

```sh
/bin/sh deploy/start-on-boot.sh
tail -n 30 runtime/boot.log
```

Docker and cron are enabled at host boot. Container restart policies also restart these services
with Docker; the cron entry explicitly brings Reader back on the next boot even if you stopped it.
To keep Reader disabled across boots, remove/comment this specific crontab entry and stop both
services. If you move the checkout, update the absolute script path in `crontab -e`.

## Fresh installation

Docker Engine with Compose v2 and Linux x86_64 were tested. Other architectures are unverified.
The Dockerfiles pin their base-image digests and uv version; Python dependencies use `uv.lock`.
Debian package repositories still supply current package builds, so preserve the built image
IDs/archives for exact rollback rather than claiming byte-identical future apt builds.

```sh
docker compose build reader reader-tor
docker compose run --rm init
docker compose up -d reader-tor
docker compose run --rm models voices install en_US-ljspeech-medium --json
docker compose run --rm models voices install de_DE-thorsten-medium --json
docker compose run --rm models voices install sr-marko-medium --json
docker compose run --rm admin owner init
docker compose up -d reader
docker compose ps
docker compose exec -T reader cat /run/reader-onion/hostname
```

Installation explicitly downloads the three approved, pinned, checksummed voice models.
Serving mounts models read-only and never downloads them on a request. Keep the original
personal-use/provenance notes in [VOICE_REVIEWS.md](VOICE_REVIEWS.md). One Serbian model covers
Latin and Cyrillic. Readiness fails for missing/corrupt models; repair them while Reader is stopped.
For unattended setup, supply a private password on stdin:

```sh
docker compose run --rm -T admin owner init --password-file /dev/stdin < /private/path/password.txt
```

The password must be 12–256 characters and at most 1024 UTF-8 bytes. It never belongs in a CLI
argument, environment variable, Git file, image, or shared transcript. The app stores Argon2id
hashes and opaque session-token hashes. A persistent global throttle bounds password checks.

Tor creates its identity independently of Reader. Reader waits at most 60 seconds for the public
hostname, validates the v3 checksum, and freezes the exact Host/Origin boundary. Initial startup
binds the owner's hostname; replacement preserves it. A changed hostname fails closed.

## Password, devices, identity, and models

Use **Devices** to revoke individual browsers, or **Sign out** for the current browser. At the
device limit, an existing signed-in browser can revoke an old device. Server-side recovery:

```sh
docker compose stop reader
docker compose run --rm admin owner password
# Or revoke sessions without changing the password:
docker compose run --rm admin owner revoke-all
docker compose up -d reader
```

Resetting the password revokes every owner session while preserving the owner library. These
commands require Reader stopped; they share its advisory volume lock. Never delete the lock
file to force access. Desktop/LAN sessions cannot authenticate to onion mode or vice versa.

If Tor keys were intentionally replaced, explicitly bind the new hostname while Reader is stopped:

```sh
docker compose stop reader
docker compose run --rm admin owner bind-hostname --hostname-file /run/reader-onion/hostname
docker compose up -d reader
```

Prefer restoring the original Tor state if replacement was accidental. Rebinding revokes owner
sessions; it never copies or exposes private keys. Model repair uses the explicit installation
commands while Reader is stopped, then restart Reader to verify all models again.

## Health and storage

`/health/live` and `/health/ready` expose only booleans, require the correct Host, and create no
session. Compose checks Reader through its Unix socket. Readiness checks model verification at
startup, worker ownership, database availability, disk floor, and overdue worker jobs. Tor health
uses an authenticated local control socket to check bootstrap; it does not prove outside reachability.

Audio has a 2 GiB default budget and a 1 GiB free-disk floor. The worker reserves space before
generation, evicts complete least-recently-used renditions, and cleans deleted/orphaned files.
Playback leases renew every 45 seconds with a 180-second reconnect grace; active HTTP responses
remain pinned until completion. Audio can be regenerated; text and progress survive audio eviction.
History admits at most 200 saved readings by default and asks you to delete an older one at the
limit. Job/idempotency metadata is bounded. `STORAGE_FULL` is recoverable; free server disk or
delete readings instead of repeatedly regenerating. Long screen-off pauses can outlast a lease.

The current WAV stream is about 353 kbit/s (roughly 159 MB/hour). Tor/browser network conditions
can still cause buffering. Media Session and Wake Lock are optional; background playback on a
particular phone is an acceptance check, not a guarantee.

## Backup, restore, update, rollback

Stop both services for a consistent library and Tor identity backup. Models can be reinstalled
from the registry and are excluded. The archives contain the owner/library and onion private keys;
keep the backup directory private and outside Git/live volumes. Do not publish a restored Tor
identity while the original service is online.

```sh
docker compose stop reader reader-tor
python3 tools/volume_backup.py backup --output /private/new-reader-backup --project reader-onion
docker compose up -d reader-tor reader
# Validates archives first, refuses existing target volumes; does not start services:
python3 tools/volume_backup.py restore --input /private/new-reader-backup --project reader-restore-check
```

Read `python3 tools/volume_backup.py --help` for the exact supported options. An isolated restore
creates `reader-restore-check_data` and `reader-restore-check_tor-state`. Use matching application
and Tor images from the manifest. Run Compose with `-p reader-restore-check` for its other volumes,
initialize permissions, and reinstall models before serving. **Keep its Tor service stopped** if
the original identity is active. A new distinct onion identity requires a separate fresh Tor-state
volume and explicit owner hostname rebind, rather than publishing a clone.

Before updating, preserve a stopped-service backup and image IDs (`docker image inspect`). Save
the images with `docker image save` if rollback must survive local image pruning. Review changes,
rebuild deliberately, and replace Reader/Tor with Compose. No automatic Git pulls or dependency
updates occur. A migrated database cannot be downgraded: older code refuses a newer schema. Rollback
uses the matching old image and its pre-migration backup in new isolated volumes; readings created
after that backup are not in the restored library. Never run `docker compose down -v` as routine cleanup.

## Verification

```sh
.venv/bin/pytest -q
.venv/bin/mypy src tests
.venv/bin/ruff check src tests tools deploy
.venv/bin/ruff format --check src tests tools deploy
python3 tools/test_claude_harness.py
gjs tools/test_ui_smoke.js src/article_reader/web/static/app.js
python3 tools/container_lock_smoke.py
docker compose run --rm -T admin evaluate-voices --voice-id en_US-ljspeech-medium --json
```

`tools/tor_smoke.py` is an opt-in HTTP/API check using either the private Unix socket or a
disposable SOCKS client. Feed the password over stdin. It explicitly handles the Secure cookie
for one onion hostname, so its result cannot substitute for a Tor Browser cookie round-trip.
Use `--article-url https://www.python.org/doc/essays/blurb/` for a public extraction/speech/Range
smoke. This leaves the test reading in history and refuses to bypass extraction review. See
the dated report for actual results and the phone checks still needed.
