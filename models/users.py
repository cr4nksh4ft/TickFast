from datetime import datetime

import peewee as pw

from models.basemodel import BaseModel, get_database
from tickfast.states import UserRole


class User(BaseModel):
    id = pw.AutoField()
    role = pw.CharField(
        default=UserRole.USER.value,
        constraints=[
            pw.Check(
                "role IN ("
                + ", ".join(repr(role.value) for role in UserRole)
                + ")"
            )
        ],
    )
    created_at = pw.DateTimeField(default=datetime.now)

    class Meta:
        table_name = "users"


def create_user(role: UserRole = UserRole.USER) -> User:
    database = get_database()
    with database.connection_context():
        return User.create(role=role.value)


def get_user_by_id(user_id: int) -> User | None:
    if type(user_id) is not int or user_id <= 0:
        return None

    database = get_database()
    with database.connection_context():
        return User.get_or_none(User.id == user_id)