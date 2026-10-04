CREATE TABLE seats (
    id INTEGER NOT NULL AUTO_INCREMENT,
    show_id INTEGER NOT NULL,
    label VARCHAR(255) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'available',
    PRIMARY KEY (id),
    CONSTRAINT chk_seats_status CHECK (
        status IN ('available', 'held', 'confirmed')
    ),
    CONSTRAINT uq_seats_show_label UNIQUE (show_id, label),
    CONSTRAINT fk_seats_show FOREIGN KEY (show_id)
        REFERENCES shows (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;