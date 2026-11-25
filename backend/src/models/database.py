from redis import Redis
from sqlalchemy import create_engine, event, insert, select

from src.config.settings import DATABASE_URL, REDIS_URL
from src.models.tables import audit

_is_sqlite = DATABASE_URL.startswith('sqlite')
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args={'timeout': 30, 'check_same_thread': False} if _is_sqlite else {},
)
if _is_sqlite:
    @event.listens_for(engine, 'connect')
    def _sqlite_pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute('PRAGMA journal_mode=WAL')
        cursor.execute('PRAGMA busy_timeout=30000')
        cursor.execute('PRAGMA foreign_keys=ON')
        cursor.close()

cache = Redis.from_url(REDIS_URL, socket_connect_timeout=2, socket_timeout=2)


def fetch_one(connection, table, key):
    return connection.execute(select(table).where(table.c.id == key)).mappings().one_or_none()


def add_event(connection, actor, action, subject, at):
    connection.execute(insert(audit).values(actor=actor, action=action, subject=subject, at=at))
