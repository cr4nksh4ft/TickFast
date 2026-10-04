from pathlib import Path

import pytest

from migrations.runner import MigrationError, load_migrations
from tickfast.states import SeatState


def test_initial_migrations_are_numbered_and_ordered():
    migrations = load_migrations()

    assert [migration.version for migration in migrations] == [1, 2]
    assert migrations[0].filename == "001_create_shows.sql"
    assert migrations[1].filename == "002_create_seats.sql"


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