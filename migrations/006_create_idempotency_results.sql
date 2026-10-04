CREATE TABLE idempotency_results (
    show_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    idempotency_key VARBINARY(255) NOT NULL,
    request_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    reservation_id INTEGER NULL,
    response_status SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    response_body JSON NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (show_id, user_id, idempotency_key),
    KEY ix_idempotency_results_reservation (reservation_id),
    CONSTRAINT chk_idempotency_results_outcome CHECK (
        (response_status = 0 AND response_body IS NULL AND reservation_id IS NULL)
        OR (response_status = 201 AND response_body IS NOT NULL AND reservation_id IS NOT NULL)
        OR (response_status = 409 AND response_body IS NOT NULL AND reservation_id IS NULL)
    ),
    CONSTRAINT fk_idempotency_results_show FOREIGN KEY (show_id)
        REFERENCES shows (id) ON DELETE RESTRICT,
    CONSTRAINT fk_idempotency_results_user FOREIGN KEY (user_id)
        REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_idempotency_results_reservation FOREIGN KEY (reservation_id, show_id, user_id)
        REFERENCES reservations (id, show_id, user_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
