from pathlib import Path

import pytest

from migrations.runner import MigrationError, load_migrations
from tickfast.states import SeatState, UserRole


def test_migrations_are_numbered_and_ordered():
    migrations = load_migrations()

    assert [migration.version for migration in migrations] == list(range(1, 10))
    assert [migration.filename for migration in migrations] == [
        "001_create_shows.sql",
        "002_create_seats.sql",
        "003_create_users.sql",
        "004_create_reservations.sql",
        "005_create_reservation_seats.sql",
        "006_create_idempotency_results.sql",
        "007_create_show_user_usage.sql",
        "008_add_hold_lease_to_idempotency.sql",
        "009_add_hold_ownership_to_seats.sql",
    ]


def test_users_migration_defines_generated_primary_key_and_role_constraint():
    user_migration = load_migrations()[2].statement

    assert "CREATE TABLE users" in user_migration
    assert "id INTEGER NOT NULL AUTO_INCREMENT" in user_migration
    assert "PRIMARY KEY (id)" in user_migration
    assert "role VARCHAR(20) NOT NULL DEFAULT 'user'" in user_migration
    assert all(f"'{role.value}'" in user_migration for role in UserRole)


def test_seat_migration_allows_exactly_the_python_seat_states():
    seat_migration = load_migrations()[1].statement

    for state in SeatState:
        assert f"'{state.value}'" in seat_migration


def test_reservation_migrations_define_history_and_identity_constraints():
    migrations = {migration.filename: migration.statement for migration in load_migrations()}
    reservations = migrations["004_create_reservations.sql"]
    reservation_seats = migrations["005_create_reservation_seats.sql"]
    idempotency_results = migrations["006_create_idempotency_results.sql"]
    show_user_usage = migrations["007_create_show_user_usage.sql"]

    assert "FOREIGN KEY (show_id)" in reservations
    assert "FOREIGN KEY (user_id)" in reservations
    assert "UNIQUE KEY uq_reservations_id_show_user (id, show_id, user_id)" in reservations
    assert "FOREIGN KEY (reservation_id)" in reservation_seats
    assert "FOREIGN KEY (seat_id)" in reservation_seats
    assert "PRIMARY KEY (reservation_id, seat_id)" in reservation_seats
    assert "PRIMARY KEY (show_id, user_id, idempotency_key)" in idempotency_results
    assert "request_hash CHAR(64)" in idempotency_results
    assert "response_body JSON" in idempotency_results
    assert "FOREIGN KEY (reservation_id, show_id, user_id)" in " ".join(
        idempotency_results.split()
    )
    assert "PRIMARY KEY (show_id, user_id)" in show_user_usage
    assert "active_seat_count INTEGER UNSIGNED" in show_user_usage

    hold_lease = migrations["008_add_hold_lease_to_idempotency.sql"]
    seat_holds = migrations["009_add_hold_ownership_to_seats.sql"]
    assert "ADD COLUMN hold_id BINARY(16) NULL" in hold_lease
    assert "ADD COLUMN hold_expires_at DATETIME(6) NULL" in hold_lease
    assert "ADD UNIQUE KEY uq_idempotency_results_hold_id (hold_id)" in hold_lease
    assert "ADD COLUMN active_hold_id BINARY(16) NULL" in seat_holds
    assert "FOREIGN KEY (active_hold_id, show_id)" in seat_holds


def test_migration_must_contain_only_one_statement(tmp_path: Path):
    (tmp_path / "001_invalid.sql").write_text(
        "CREATE TABLE first_table (id INTEGER); CREATE TABLE second_table (id INTEGER);",
        encoding="utf-8",
    )

    with pytest.raises(MigrationError, match="exactly one SQL statement"):
        load_migrations(tmp_path)