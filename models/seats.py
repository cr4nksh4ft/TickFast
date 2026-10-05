from datetime import datetime

import peewee as pw

from models.basemodel import BaseModel, get_database
from tickfast.states import SeatState


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
			"""
			SELECT seats.label,
				CASE
					WHEN seats.status = 'held'
						AND idempotency_results.hold_state = 'held'
						AND idempotency_results.hold_expires_at <= CURRENT_TIMESTAMP(6)
					THEN 'available'
					ELSE seats.status
				END AS status
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
			{"label": str(label), "status": str(status)}
			for label, status in seat_rows
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
