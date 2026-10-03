import peewee as pw
from playhouse.pool import PooledMySQLDatabase

from utils import env

db = PooledMySQLDatabase(
    env('DB_DATABASE'),
    user=env('DB_USERNAME'),
    password=env('DB_PASSWORD'),
    host=env('DB_HOST'),
    port=env('DB_PORT'),
    charset='utf8mb4',
    max_connections=env('DB_MAX_CONNECTIONS'),
    stale_timeout=120
)

class BaseModel(pw.Model):
    class Meta:
        database = db

    def __init__(self, **kwargs):
        pw.Model.__init__(self, **kwargs)
