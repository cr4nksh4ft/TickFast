CREATE TABLE reservation_seats (
    reservation_id INTEGER NOT NULL,
    seat_id INTEGER NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (reservation_id, seat_id),
    KEY ix_reservation_seats_seat_id (seat_id),
    CONSTRAINT fk_reservation_seats_reservation FOREIGN KEY (reservation_id)
        REFERENCES reservations (id) ON DELETE RESTRICT,
    CONSTRAINT fk_reservation_seats_seat FOREIGN KEY (seat_id)
        REFERENCES seats (id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
