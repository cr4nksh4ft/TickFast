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

		seats = list(
			Seat.select(Seat.label, Seat.status)
			.where(Seat.show == show)
			.order_by(Seat.label)
			.dicts()
		)

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
