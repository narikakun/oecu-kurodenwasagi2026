CREATE TABLE calls (
    call_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    device_name VARCHAR(100) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    started_at DATETIME(3) NOT NULL,
    ended_at DATETIME(3) NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (call_id),
    INDEX idx_calls_started_at (started_at),
    INDEX idx_calls_device_started (device_name, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE ingest_events (
    event_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    received_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (event_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE transcripts (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    event_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    call_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    speaker VARCHAR(20) NOT NULL,
    transcript_text TEXT NOT NULL,
    occurred_at DATETIME(3) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_transcripts_event_id (event_id),
    INDEX idx_transcripts_call_time (call_id, occurred_at, id),
    CONSTRAINT fk_transcripts_call FOREIGN KEY (call_id)
        REFERENCES calls (call_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

