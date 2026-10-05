ALTER TABLE seats
    ADD COLUMN active_hold_id BINARY(16) NULL,
    ADD KEY ix_seats_active_hold_id (active_hold_id),
    ADD CONSTRAINT chk_seats_hold_pointer CHECK (
        (status = 'held' AND active_hold_id IS NOT NULL)
        OR (status <> 'held' AND active_hold_id IS NULL)
    ),
    ADD CONSTRAINT fk_seats_active_hold FOREIGN KEY (active_hold_id, show_id)
        REFERENCES idempotency_results (hold_id, show_id) ON DELETE RESTRICT
