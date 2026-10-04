CREATE TABLE show_user_usage (
    show_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    active_seat_count INTEGER UNSIGNED NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (show_id, user_id),
    CONSTRAINT chk_show_user_usage_nonnegative CHECK (active_seat_count >= 0),
    CONSTRAINT fk_show_user_usage_show FOREIGN KEY (show_id)
        REFERENCES shows (id) ON DELETE RESTRICT,
    CONSTRAINT fk_show_user_usage_user FOREIGN KEY (user_id)
        REFERENCES users (id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
