CREATE TABLE reservations (
    id INTEGER NOT NULL AUTO_INCREMENT,
    show_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    amount_paise BIGINT UNSIGNED NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'confirmed',
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    cancelled_at DATETIME(6) NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_reservations_id_show_user (id, show_id, user_id),
    KEY ix_reservations_show_user_status (show_id, user_id, status),
    CONSTRAINT chk_reservations_amount_positive CHECK (amount_paise > 0),
    CONSTRAINT chk_reservations_status CHECK (
        status IN ('confirmed', 'cancelled')
    ),
    CONSTRAINT chk_reservations_cancelled_at CHECK (
        (status = 'confirmed' AND cancelled_at IS NULL)
        OR (status = 'cancelled' AND cancelled_at IS NOT NULL)
    ),
    CONSTRAINT fk_reservations_show FOREIGN KEY (show_id)
        REFERENCES shows (id) ON DELETE RESTRICT,
    CONSTRAINT fk_reservations_user FOREIGN KEY (user_id)
        REFERENCES users (id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
