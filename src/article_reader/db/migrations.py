"""Versioned SQLite schema migrations.

Migrations are plain SQL statements applied in order inside one write transaction each,
tracked with ``PRAGMA user_version``. Nothing here ever drops or resets user data: an
on-disk version newer than :data:`CURRENT_SCHEMA_VERSION` is refused by
``article_reader.db.connection`` rather than silently downgraded.

Table design intentionally avoids forward-referencing foreign keys (every ``REFERENCES``
points at a table created earlier in the same migration) so schema application order
never depends on SQLite's deferred foreign-key name resolution.
"""

from __future__ import annotations

_MIGRATION_0001 = (
    """
    CREATE TABLE instance (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        instance_id TEXT NOT NULL UNIQUE,
        label TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE viewers (
        viewer_id TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE viewer_tokens (
        token_hash TEXT PRIMARY KEY,
        viewer_id TEXT NOT NULL REFERENCES viewers (viewer_id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        revoked_at TEXT
    )
    """,
    "CREATE INDEX ix_viewer_tokens_viewer ON viewer_tokens (viewer_id)",
    """
    CREATE TABLE readings (
        reading_id TEXT PRIMARY KEY,
        viewer_id TEXT NOT NULL REFERENCES viewers (viewer_id) ON DELETE CASCADE,
        submitted_url TEXT NOT NULL,
        requested_language TEXT,
        requested_script TEXT,
        state TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_opened_at TEXT NOT NULL,
        deleted_at TEXT
    )
    """,
    "CREATE INDEX ix_readings_viewer ON readings (viewer_id, deleted_at, last_opened_at)",
    """
    CREATE TABLE articles (
        article_id TEXT PRIMARY KEY,
        reading_id TEXT NOT NULL UNIQUE REFERENCES readings (reading_id) ON DELETE CASCADE,
        submitted_url TEXT NOT NULL,
        final_url TEXT NOT NULL,
        canonical_url TEXT,
        title TEXT,
        language_hint TEXT,
        extraction_version TEXT NOT NULL,
        needs_review INTEGER NOT NULL CHECK (needs_review IN (0, 1)),
        review_reasons_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE article_blocks (
        article_id TEXT NOT NULL REFERENCES articles (article_id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL,
        kind TEXT NOT NULL,
        display_text TEXT NOT NULL,
        speech_text TEXT,
        requires_review INTEGER NOT NULL CHECK (requires_review IN (0, 1)),
        PRIMARY KEY (article_id, ordinal)
    )
    """,
    """
    CREATE TABLE article_decisions (
        article_id TEXT PRIMARY KEY REFERENCES articles (article_id) ON DELETE CASCADE,
        detection_evidence_json TEXT,
        script_evidence_json TEXT NOT NULL,
        selected_language TEXT,
        selected_script TEXT,
        selection_reason TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        review_accepted INTEGER NOT NULL CHECK (review_accepted IN (0, 1)),
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE prepared_segments (
        article_id TEXT NOT NULL REFERENCES articles (article_id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL,
        speech_text TEXT NOT NULL,
        speech_text_sha256 TEXT NOT NULL,
        source_block_ordinals_json TEXT NOT NULL,
        includes_title INTEGER NOT NULL CHECK (includes_title IN (0, 1)),
        normalizer_version TEXT NOT NULL,
        segmenter_version TEXT NOT NULL,
        PRIMARY KEY (article_id, ordinal)
    )
    """,
    """
    CREATE TABLE renditions (
        rendition_id TEXT PRIMARY KEY,
        reading_id TEXT NOT NULL REFERENCES readings (reading_id) ON DELETE CASCADE,
        article_id TEXT NOT NULL REFERENCES articles (article_id) ON DELETE CASCADE,
        voice_id TEXT NOT NULL,
        language TEXT NOT NULL,
        script TEXT NOT NULL,
        sample_rate_hz INTEGER NOT NULL,
        model_sha256 TEXT NOT NULL,
        config_sha256 TEXT NOT NULL,
        engine_version TEXT NOT NULL,
        settings_json TEXT NOT NULL,
        contract_hash TEXT NOT NULL,
        state TEXT NOT NULL,
        total_chunks INTEGER NOT NULL,
        manifest_revision INTEGER NOT NULL DEFAULT 0,
        error_code TEXT,
        error_message TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX ux_renditions_contract ON renditions (article_id, contract_hash)",
    "CREATE INDEX ix_renditions_reading ON renditions (reading_id, created_at)",
    """
    CREATE TABLE audio_chunks (
        rendition_id TEXT NOT NULL REFERENCES renditions (rendition_id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('ready', 'evicted')),
        relative_path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        byte_count INTEGER NOT NULL,
        duration_seconds REAL NOT NULL,
        publication_generation TEXT NOT NULL,
        published_at TEXT NOT NULL,
        PRIMARY KEY (rendition_id, ordinal)
    )
    """,
    """
    CREATE TABLE jobs (
        job_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('prepare', 'synthesize')),
        reading_id TEXT NOT NULL REFERENCES readings (reading_id) ON DELETE CASCADE,
        rendition_id TEXT REFERENCES renditions (rendition_id) ON DELETE CASCADE,
        state TEXT NOT NULL CHECK (
            state IN (
                'queued', 'running', 'cancelling', 'completed', 'failed',
                'cancelled', 'interrupted'
            )
        ),
        stage TEXT,
        attempt INTEGER NOT NULL DEFAULT 1,
        previous_job_id TEXT REFERENCES jobs (job_id),
        worker_generation TEXT,
        lease_expires_at TEXT,
        cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
        recovery_count INTEGER NOT NULL DEFAULT 0,
        error_code TEXT,
        error_message TEXT,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX ix_jobs_claim ON jobs (state, created_at, job_id)",
    "CREATE INDEX ix_jobs_reading ON jobs (reading_id, created_at)",
    (
        "CREATE UNIQUE INDEX ux_jobs_active_synth ON jobs (rendition_id) "
        "WHERE kind = 'synthesize' AND state IN ('queued', 'running', 'cancelling')"
    ),
    """
    CREATE TABLE idempotency_keys (
        viewer_id TEXT NOT NULL,
        operation TEXT NOT NULL,
        client_key TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        resource_id TEXT,
        response_json TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY (viewer_id, operation, client_key)
    )
    """,
    """
    CREATE TABLE progress (
        viewer_id TEXT NOT NULL,
        reading_id TEXT NOT NULL REFERENCES readings (reading_id) ON DELETE CASCADE,
        rendition_id TEXT REFERENCES renditions (rendition_id) ON DELETE CASCADE,
        chunk_ordinal INTEGER,
        offset_seconds REAL,
        speed REAL,
        revision INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (viewer_id, reading_id)
    )
    """,
)

_MIGRATION_0002 = (
    """
    CREATE TABLE viewer_sessions (
        session_id TEXT PRIMARY KEY,
        token_hash TEXT NOT NULL UNIQUE,
        viewer_id TEXT NOT NULL REFERENCES viewers (viewer_id) ON DELETE CASCADE,
        label TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        revoked_at TEXT
    )
    """,
    """
    INSERT INTO viewer_sessions (
        session_id, token_hash, viewer_id, label, created_at, last_seen_at, expires_at, revoked_at
    )
    SELECT
        lower(hex(randomblob(16))), token_hash, viewer_id, 'Existing browser', created_at,
        created_at, '9999-12-31T23:59:59+00:00', revoked_at
    FROM viewer_tokens
    """,
    "DROP TABLE viewer_tokens",
    "CREATE INDEX ix_viewer_sessions_viewer ON viewer_sessions (viewer_id, revoked_at, expires_at)",
)

MIGRATIONS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (1, "Initial durable schema: viewers, readings, articles, renditions, jobs.", _MIGRATION_0001),
    (2, "Revocable, expiring browser sessions for authenticated LAN access.", _MIGRATION_0002),
)

CURRENT_SCHEMA_VERSION: int = MIGRATIONS[-1][0]

__all__ = ["CURRENT_SCHEMA_VERSION", "MIGRATIONS"]
