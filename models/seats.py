from datetime import datetime

import peewee as pw

from models.basemodel import BaseModel, get_database
from tickfast.states import SeatState

_EFFECTIVE_SEAT_STATUS_SQL = """CASE
	WHEN seats.status = 'held'
		AND idempotency_results.hold_state = 'held'
		AND idempotency_results.hold_expires_at <= CURRENT_TIMESTAMP(6)
	THEN 'available'
	ELSE seats.status
END"""


class Show(BaseModel):
    name = pw.CharField()
    price_paise = pw.IntegerField(constraints=[pw.Check("price_paise > 0")])
    created_at = pw.DateTimeField(default=datetime.now)

    class Meta:
        table_name = "shows"


class Seat(BaseModel):
    show = pw.ForeignKeyField(Show, backref="seats", on_delete="CASCADE")
    label = pw.CharField()
    status = pw.CharField(
        default=SeatState.AVAILABLE.value,
        constraints=[
            pw.Check(
                "status IN ("
                + ", ".join(repr(state.value) for state in SeatState)
                + ")"
            )
        ],
    )

    class Meta:
        table_name = "seats"
        indexes = ((("show", "label"), True),)


def get_show_state(show_id: int) -> dict[str, object] | None:
    database = get_database()
    with database.connection_context():
        show = Show.get_or_none(Show.id == show_id)
        if show is None:
            return None

        seat_rows = database.execute_sql(
            f"""
			SELECT seats.label,
				{_EFFECTIVE_SEAT_STATUS_SQL} AS status
			FROM seats
			LEFT JOIN idempotency_results
				ON idempotency_results.hold_id = seats.active_hold_id
				AND idempotency_results.hold_state = 'held'
			WHERE seats.show_id = %s
			ORDER BY seats.id
			""",
            (show.id,),
        ).fetchall()
        seats = [
            {"label": str(label), "status": str(status)} for label, status in seat_rows
        ]

    counts = {state.value: 0 for state in SeatState}
    for seat in seats:
        counts[SeatState(seat["status"]).value] += 1

    return {
        "id": show.id,
        "name": show.name,
        "price_paise": show.price_paise,
        "seats": seats,
        "counts": counts,
        "total_seats": len(seats),
    }


def get_recent_show_seat_counts(
    limit: int = 50,
) -> list[tuple[int, dict[str, int]]]:
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("show count limit must be between 1 and 1000")

    database = get_database()
    with database.connection_context():
        rows = database.execute_sql(
            f"""
			WITH recent_shows AS (
				SELECT id FROM shows ORDER BY id DESC LIMIT %s
			), effective_seats AS (
				SELECT recent_shows.id AS show_id,
					{_EFFECTIVE_SEAT_STATUS_SQL} AS status
				FROM recent_shows
				LEFT JOIN seats ON seats.show_id = recent_shows.id
				LEFT JOIN idempotency_results
					ON idempotency_results.hold_id = seats.active_hold_id
					AND idempotency_results.hold_state = 'held'
			)
			SELECT show_id,
				SUM(status = 'available') AS available,
				SUM(status = 'held') AS held,
				SUM(status = 'confirmed') AS confirmed
			FROM effective_seats
			GROUP BY show_id
			ORDER BY show_id DESC
			""",
            (limit,),
        ).fetchall()

    return [
        (
            int(show_id),
            {
                "available": int(available or 0),
                "held": int(held or 0),
                "confirmed": int(confirmed or 0),
            },
        )
        for show_id, available, held, confirmed in rows
    ]


def create_show(
    name: str, seat_labels: list[str], price_paise: int
) -> dict[str, object]:
    database = get_database()
    with database.connection_context(), database.atomic():
        show = Show.create(name=name, price_paise=price_paise)
        Seat.insert_many(
            [
                {
                    "show": show,
                    "label": label,
                    "status": SeatState.AVAILABLE.value,
                }
                for label in seat_labels
            ]
        ).execute()
        show_id = show.id

    result = get_show_state(show_id)
    if result is None:
        raise RuntimeError("Created show could not be read")
    return result
