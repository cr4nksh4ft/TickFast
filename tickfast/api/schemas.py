from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tickfast.states import ReservationState, SeatState


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateShowRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=255)
    seats: list[str] = Field(min_length=1)
    price_paise: int = Field(gt=0, strict=True)

    @field_validator("name")
    @classmethod
    def name_is_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value

    @field_validator("seats")
    @classmethod
    def seats_are_unique_and_nonblank(cls, value: list[str]) -> list[str]:
        seats = [seat.strip() for seat in value]
        if any(not seat for seat in seats):
            raise ValueError("seat labels must not be blank")
        if any(len(seat) > 255 for seat in seats):
            raise ValueError("seat labels must be at most 255 characters")
        if len(seats) != len(set(seats)):
            raise ValueError("seat labels must be unique")
        return seats


class ReserveRequest(StrictRequest):
    seats: list[str] = Field(min_length=1)

    @field_validator("seats")
    @classmethod
    def seats_are_unique_and_nonblank(cls, value: list[str]) -> list[str]:
        seats = [seat.strip() for seat in value]
        if any(not seat for seat in seats):
            raise ValueError("seat labels must not be blank")
        if len(seats) != len(set(seats)):
            raise ValueError("seat labels must be unique")
        return seats


class SeatResponse(BaseModel):
    label: str
    status: SeatState


class SeatCounts(BaseModel):
    available: int
    held: int
    confirmed: int


class ShowState(BaseModel):
    id: int
    name: str
    price_paise: int
    seats: list[SeatResponse]
    counts: SeatCounts
    total_seats: int


class ReservationResponse(BaseModel):
    reservation_id: int
    show_id: int
    user_id: int
    seats: list[str]
    amount_paise: int
    status: ReservationState


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str
    errors: list[dict[str, Any]] | None = None


class ErrorResponse(BaseModel):
    detail: ErrorDetail