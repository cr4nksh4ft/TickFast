ALTER TABLE idempotency_results
    ADD COLUMN hold_id BINARY(16) NULL,
    ADD COLUMN hold_state VARCHAR(20) NULL,
    ADD COLUMN hold_expires_at DATETIME(6) NULL,
    ADD UNIQUE KEY uq_idempotency_results_hold_id (hold_id),
    ADD UNIQUE KEY uq_idempotency_results_hold_show (hold_id, show_id),
    ADD KEY ix_idempotency_results_hold_expiry (hold_state, hold_expires_at),
    ADD CONSTRAINT chk_idempotency_results_hold_state CHECK (
        (hold_state IS NULL AND hold_id IS NULL AND hold_expires_at IS NULL)
        OR (hold_state = 'held' AND hold_id IS NOT NULL AND hold_expires_at IS NOT NULL)
        OR (hold_state = 'expired' AND hold_id IS NULL AND hold_expires_at IS NULL)
    )
