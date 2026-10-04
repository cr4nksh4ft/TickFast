from pathlib import Path

import pytest

from migrations.runner import MigrationError, load_migrations
from tickfast.states import SeatState, UserRole


def test_initial_migrations_are_numbered_and_ordered():
    migrations = load_migrations()

    assert [migration.version for migration in migrations] == [1, 2, 3]
    assert migrations[0].filename == "001_create_shows.sql"
    assert migrations[1].filename == "002_create_seats.sql"
    assert migrations[2].filename == "003_create_users.sql"


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


def test_migration_must_contain_only_one_statement(tmp_path: Path):
    (tmp_path / "001_invalid.sql").write_text(
        "CREATE TABLE first_table (id INTEGER); CREATE TABLE second_table (id INTEGER);",
        encoding="utf-8",
    )

    with pytest.raises(MigrationError, match="exactly one SQL statement"):
        load_migrations(tmp_path)