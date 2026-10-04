from enum import StrEnum


class UserRole(StrEnum):
    USER = "user"
    ADMIN = "admin"


class SeatState(StrEnum):
    AVAILABLE = "available"
    HELD = "held"
    CONFIRMED = "confirmed"


class ReservationState(StrEnum):
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"