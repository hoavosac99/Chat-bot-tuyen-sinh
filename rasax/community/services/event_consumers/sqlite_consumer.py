import contextlib
import logging
import time
from typing import Dict, Generator, Optional, Text, Union, TYPE_CHECKING

import sqlalchemy
import sqlalchemy.orm

import rasax.community.constants as constants
import rasax.community.services.event_consumers.event_consumer as event_consumer

if TYPE_CHECKING:
    from sqlalchemy.engine.url import URL

logger = logging.getLogger(__name__)


class SQLiteEventConsumer(event_consumer.EventConsumer):
    type_name = "sql"

    def __init__(self, should_run_liveness_endpoint: bool = False):
        self.producer = SQLEventBroker()
        super().__init__(should_run_liveness_endpoint)

    def consume(self):
        logger.info("Start consuming SQLite events from database 'events.db'.")
        with self.producer.session_scope() as session:
            while True:
                new_events = (
                    session.query(self.producer.SQLBrokerEvent)
                    .order_by(self.producer.SQLBrokerEvent.id.asc())
                    .all()
                )

                for event in new_events:
                    self.log_event(
                        event.data,
                        sender_id=event.sender_id,
                        event_number=event.id,
                        origin=constants.DEFAULT_RASA_ENVIRONMENT,
                    )
                    session.delete(event)
                    session.commit()

                time.sleep(0.01)


class SQLEventBroker:
    """Save events into an SQL database.

    All events will be stored in a table called `events`.

    """

    from sqlalchemy.ext.declarative import declarative_base

    Base = declarative_base()

    class SQLBrokerEvent(Base):
        __tablename__ = "events"
        id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
        sender_id = sqlalchemy.Column(sqlalchemy.String(255))
        data = sqlalchemy.Column(sqlalchemy.Text)

    @staticmethod
    def _get_db_url(
        dialect: Text = "sqlite",
        host: Optional[Text] = None,
        port: Optional[int] = None,
        db: Text = "rasa.db",
        username: Text = None,
        password: Text = None,
        login_db: Optional[Text] = None,
        query: Optional[Dict] = None,
    ) -> Union[Text, "URL"]:
        """Build an SQLAlchemy `URL` object representing the parameters needed
        to connect to an SQL database.

        Args:
            dialect: SQL database type.
            host: Database network host.
            port: Database network port.
            db: Database name.
            username: User name to use when connecting to the database.
            password: Password for database user.
            login_db: Alternative database name to which initially connect, and create
                the database specified by `db` (PostgreSQL only).
            query: Dictionary of options to be passed to the dialect and/or the
                DBAPI upon connect.

        Returns:
            URL ready to be used with an SQLAlchemy `Engine` object.
        """
        from urllib import parse

        # Users might specify a url in the host
        if host and "://" in host:
            # assumes this is a complete database host name including
            # e.g. `postgres://...`
            return host
        elif host:
            # add fake scheme to properly parse components
            parsed = parse.urlsplit(f"scheme://{host}")

            # users might include the port in the url
            port = parsed.port or port
            host = parsed.hostname or host

        return sqlalchemy.engine.url.URL(
            dialect,
            username,
            password,
            host,
            port,
            database=login_db if login_db else db,
            query=query,
        )

    def __init__(
        self,
        dialect: Text = "sqlite",
        host: Optional[Text] = None,
        port: Optional[int] = None,
        db: Text = "events.db",
        username: Optional[Text] = None,
        password: Optional[Text] = None,
    ) -> None:
        engine_url = self._get_db_url(dialect, host, port, db, username, password)

        logger.debug(f"SQLEventBroker: Connecting to database: '{engine_url}'.")

        self.engine = sqlalchemy.create_engine(engine_url)
        self.Base.metadata.create_all(self.engine)
        self.sessionmaker = sqlalchemy.orm.sessionmaker(bind=self.engine)

    @contextlib.contextmanager
    def session_scope(self) -> Generator["sqlalchemy.orm.Session", None, None]:
        """Provide a transactional scope around a series of operations."""
        session = self.sessionmaker()
        try:
            yield session
        finally:
            session.close()
