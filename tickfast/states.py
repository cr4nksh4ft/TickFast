from enum import StrEnum


class SeatState(StrEnum):
    AVAILABLE = "available"
    HELD = "held"
    CONFIRMED = "confirmed"


class ReservationState(StrEnum):
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"