import json
import logging
import asyncio  # pytype: disable=pyi-error
import os
from contextlib import contextmanager
from http import HTTPStatus
from sqlite3 import Connection as SQLite3Connection
from time import sleep
from typing import Union, Text, Any, Optional, Dict, List, Generator

import sqlalchemy
import sqlalchemy.event
from sanic import Sanic
from sanic.request import Request
from sanic.response import HTTPResponse
from sqlalchemy.engine.base import Engine, Connection
from sqlalchemy.engine.url import URL
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

import rasax.community.utils.cli as cli_utils
import rasax.community.config as rasa_x_config
import rasax.community.constants as constants

logger = logging.getLogger(__name__)
POSTGRESQL_SCHEMA = "POSTGRESQL_SCHEMA"
POSTGRESQL_POOL_SIZE = "SQL_POOL_SIZE"
POSTGRESQL_MAX_OVERFLOW = "SQL_MAX_OVERFLOW"
POSTGRESQL_DEFAULT_MAX_OVERFLOW = 100
POSTGRESQL_DEFAULT_POOL_SIZE = 50


async def setup_db(app: Sanic, is_local: Optional[bool] = None) -> None:
    """Create and initialize database."""
    if is_local is None:
        is_local = rasa_x_config.LOCAL_MODE

    url = get_db_url(is_local)

    # Wait for a DB migration
    if not rasa_x_config.LOCAL_MODE:
        app.register_middleware(_db_migration_status, "request")

        await wait_for_migrations(quiet=True)

    app.session_maker = create_session_maker(url)
    configure_session_attachment(app)


def configure_session_attachment(app: Sanic) -> None:
    """Connects the database management to the sanic lifecyle."""
    app.register_middleware(set_session, "request")
    app.register_middleware(remove_session, "response")


def _db_migration_status(request: Request) -> Optional[HTTPResponse]:
    """Redirects all requests if a DB migration is in progress.

    Args:
        request: An incoming HTTP request.

    Returns:
        The HTTP response.
    """
    if hasattr(request.app, "session_maker"):
        return None

    from sanic import response

    # For the '/config' backend it has to be returned 503 status code
    # otherwise Rasa Server exists because a 'endpoints' key can't be found
    if request.path.startswith("/api/config"):
        return response.text(
            "Not ready. The DB migration in progress.", HTTPStatus.SERVICE_UNAVAILABLE
        )
    elif not request.path.startswith("/api/health"):
        return response.redirect(f"{request.scheme}://{request.host}/api/health")


def _sql_query_parameters_from_environment() -> Optional[Dict]:
    # fetch db query dict from environment, needs to be stored as a json dump
    # https://docs.sqlalchemy.org/en/13/core/engines.html#sqlalchemy.engine.url.URL

    # skip if variable is not set
    db_query = os.environ.get("DB_QUERY")
    if not db_query:
        return None

    try:
        return json.loads(db_query)
    except (TypeError, ValueError):
        logger.exception(
            f"Failed to load SQL query dictionary from environment. "
            f"Expecting a json dump of a dictionary, but found '{db_query}'."
        )
        return None


def get_db_url(is_local: Optional[bool] = None) -> Union[Text, URL]:
    """Return the database connection url from the environment variables.

    Args:
        is_local: Whether we're running in local mode.

    Returns:
        The database URL.
    """
    if is_local is None:
        is_local = rasa_x_config.LOCAL_MODE

    # Users can also pass fully specified database urls instead of individual components
    if os.environ.get(constants.ENV_DB_URL):
        return os.environ[constants.ENV_DB_URL]

    if is_local and os.environ.get("DB_DRIVER") is None:
        return "sqlite:///rasa.db"

    from rasax.community.services.user_service import ADMIN

    return URL(
        drivername=os.environ.get("DB_DRIVER", "postgresql"),
        username=os.environ.get("DB_USER", ADMIN),
        password=os.environ.get("DB_PASSWORD", "password"),
        host=os.environ.get("DB_HOST", "db"),
        port=os.environ.get("DB_PORT", 5432),
        database=os.environ.get("DB_DATABASE", "rasa"),
        query=_sql_query_parameters_from_environment(),
    )


@sqlalchemy.event.listens_for(Engine, "connect")
def _on_database_connected(dbapi_connection: Any, _) -> None:
    """Configures the database after the connection was established."""

    if isinstance(dbapi_connection, SQLite3Connection):
        set_sqlite_pragmas(dbapi_connection, True)


def set_sqlite_pragmas(
    connection: Union[SQLite3Connection, Connection], enforce_foreign_keys: bool = True
) -> None:
    """Configures the connected SQLite database.

    - Enforce foreign key constraints.
    - Enable `WAL` journal mode.
    """
    if not isinstance(connection, SQLite3Connection):
        logger.debug("Connection is not an sqlite3 connection. Cannot set pragmas.")
        return

    cursor = connection.cursor()
    # Turn on the enforcement of foreign key constraints for SQLite.
    enforce_setting = "ON" if enforce_foreign_keys else "OFF"
    cursor.execute(f"PRAGMA foreign_keys={enforce_setting};")
    logger.debug(
        "Turned SQLite foreign key enforcement {}.".format(enforce_setting.lower())
    )
    # Activate SQLite WAL mode
    cursor.execute("PRAGMA journal_mode=WAL")
    logger.debug("Turned on SQLite WAL mode.")

    cursor.close()


def _is_oracle_url(url: Union[Text, URL]) -> bool:
    """Determine whether `url` configures an Oracle connection.

    Args:
        url: SQL connection URL.

    Returns:
        `True` if `url` is an Oracle connection URL.
    """
    if isinstance(url, str):
        return "oracle" in url

    return url.drivername == "oracle"


def _is_postgresql_url(url: Union[Text, "URL"]) -> bool:
    """Determine whether `url` configures a PostgreSQL connection.

    Args:
        url: SQL connection URL.

    Returns:
        `True` if `url` is a PostgreSQL connection URL.
    """
    if isinstance(url, str):
        return "postgresql" in url

    return url.drivername == "postgresql"


def _create_engine_kwargs(url: Union[Text, "URL"]) -> Dict:
    """Get `sqlalchemy.create_engine()` kwargs.

    Args:
        url: SQL connection URL.

    Returns:
        kwargs to be passed into `sqlalchemy.create_engine()`.
    """
    if not _is_postgresql_url(url):
        return {}

    kwargs = {}

    schema_name = os.environ.get(POSTGRESQL_SCHEMA)

    if schema_name:
        logger.debug(f"Using PostgreSQL schema '{schema_name}'.")
        kwargs["connect_args"] = {"options": f"-csearch_path={schema_name}"}

    # pool_size and max_overflow can be set to control the number of
    # connections that are kept in the connection pool. Not available
    # for SQLite, and only  tested for PostgreSQL. See
    # https://docs.sqlalchemy.org/en/13/core/pooling.html#sqlalchemy.pool.QueuePool
    kwargs["pool_size"] = int(
        os.environ.get(POSTGRESQL_POOL_SIZE, POSTGRESQL_DEFAULT_POOL_SIZE)
    )
    kwargs["max_overflow"] = int(
        os.environ.get(POSTGRESQL_MAX_OVERFLOW, POSTGRESQL_DEFAULT_MAX_OVERFLOW)
    )

    return kwargs


def _create_engine_oracle_kwargs(url: Union[Text, URL]) -> Dict[Text, List[str]]:
    """Get Oracle-specific `sqlalchemy.create_engine()` kwargs.

    Args:
        url: SQL connection URL.

    Returns:
        Oracle-specific kwargs to be passed into `sqlalchemy.create_engine()`.
    """
    if not _is_oracle_url(url):
        return {}

    # `SYSTEM` and `SYSAUX` tablespaces are excluded by default
    # https://docs.sqlalchemy.org/en/13/dialects/oracle.html#table-names-with-system-sysaux-tablespaces
    return {"exclude_tablespaces": []}


def create_session_maker(url: Union[Text, URL]) -> sessionmaker:
    """Create a new sessionmaker factory.

    A sessionmaker factory generates new Sessions when called.
    """
    import sqlalchemy.exc

    echo = bool(os.getenv("DB_ECHO"))

    # Database might take a while to come up
    while True:
        try:
            engine = sqlalchemy.create_engine(
                url,
                **_create_engine_kwargs(url),
                **_create_engine_oracle_kwargs(url),
                echo=echo,
            )
            return sessionmaker(bind=engine)
        except sqlalchemy.exc.OperationalError as e:
            logger.warning(e)
            sleep(5)


@contextmanager
def session_scope(
    db_url: Union[Text, URL, None] = None
) -> Generator["Session", None, None]:
    """Provide a transactional scope around a series of operations.

    Args:
        db_url: Database URL to use to create the session.

    Yields:
        The SQLAlchemy session.
    """
    session = get_database_session(rasa_x_config.LOCAL_MODE, db_url)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _ensure_schema_exists(session: "Session") -> None:
    """Ensure that the requested PostgreSQL schema exists in the database.

    Args:
        session: Session used to inspect the database.

    Raises:
        `ValueError` if the requested schema does not exist.
    """
    schema_name = os.environ.get(POSTGRESQL_SCHEMA)

    if not schema_name:
        return

    engine = session.get_bind()

    if _is_postgresql_url(engine.url):
        query = sqlalchemy.exists(
            sqlalchemy.select([(sqlalchemy.text("schema_name"))])
            .select_from(sqlalchemy.text("information_schema.schemata"))
            .where(sqlalchemy.text(f"schema_name = '{schema_name}'"))
        )
        if not session.query(query).scalar():
            raise ValueError(schema_name)


def get_database_session(
    is_local: bool = False, db_url: Union[Text, URL, None] = None
) -> Session:
    """Create a new database session.

    Please use `session_scope` wherever possible. Using this function requires you to
    manage the session state (committing, error handling, closing) yourself.

    Args:
        is_local: `True` if a local SQLite database should be used.
        db_url: Database URL to use to create the session.

    Returns:
        A new database session.
    """
    if not db_url:
        db_url = get_db_url(is_local)

    session_maker = create_session_maker(db_url)
    session = session_maker()

    try:
        _ensure_schema_exists(session)
    except ValueError as e:
        cli_utils.print_error_and_exit(
            f"Requested PostgreSQL schema '{e}' was not found in the database. To "
            f"continue, please create the schema by running 'CREATE DATABASE {e};' "
            f"or unset the POSTGRESQL_SCHEMA environment variable in order to use "
            f"the default schema. Exiting application."
        )
    return session


async def set_session(request: Request) -> None:
    """Add a new database session to the current `Request` object.

    The added session has a listener connected to it which will fire whenever
    changes are submitted to the database.

    Args:
        request: An incoming HTTP request.
    """
    from rasax.community.api import decorators

    session = request.app.session_maker()
    request[constants.REQUEST_DB_SESSION_KEY] = session

    user = await decorators.user_from_request(request)
    from rasax.community.database.events.annotation_dump_logger import (
        AnnotationDumpLogger,
    )

    tracker = AnnotationDumpLogger(session)

    @sqlalchemy.event.listens_for(session, "before_flush")
    def on_before_flush(*_: Any) -> None:
        tracker.track(user)


async def remove_session(request: Request, response: HTTPResponse) -> None:
    """Closes the database session after the request."""
    db_session = request.get(constants.REQUEST_DB_SESSION_KEY)
    if not db_session:
        return
    # HTTP codes 4xx and 5xx are error codes
    if response.status < HTTPStatus.BAD_REQUEST:
        db_session.commit()
    else:
        db_session.rollback()

    db_session.close()


def create_sequence(table_name: Text) -> sqlalchemy.Sequence:
    from rasax.community.database.base import Base

    sequence_name = f"{table_name}_seq"
    return sqlalchemy.Sequence(sequence_name, metadata=Base.metadata, optional=True)


def get_migration_scripts_heads() -> List[Text]:
    """Get the head revisions from all migration scripts.

    Returns:
        List of all head revisions, read from the migration scripts.
    """
    from alembic.script import ScriptDirectory
    from rasax.community.database.schema_migrations.alembic import ALEMBIC_DIR

    script_dir = ScriptDirectory(ALEMBIC_DIR)
    return script_dir.get_heads()


def get_database_revision_heads(session: Session) -> Optional[List[Text]]:
    """Get the head revisions from a database. If all migration branches are merged,
    then the result should be a single value.

    Args:
        session: Database session.

    Returns:
        List of head revisions for the connected database.
    """
    from alembic.runtime.migration import MigrationContext

    # noinspection PyBroadException
    try:
        context = MigrationContext.configure(session.connection())
        return list(context.get_current_heads())
    except Exception:
        logger.warning("Unable to get database revision heads.")
        return None


def is_db_revision_latest(session: Session, quiet: bool = False) -> bool:
    """Return whether the database has been updated with the latest migration scripts
    using Alembic.

    Args:
        session: Database session.
        quiet: Don't print log messages.

    Returns:
        `True` if the current database revisions match the current migration scripts.
    """
    if quiet:
        log_fn = logger.debug
    else:
        log_fn = logger.warning

    script_heads = get_migration_scripts_heads()
    db_heads = get_database_revision_heads(session)

    latest = (script_heads == db_heads) and bool(db_heads)
    if not latest:
        log_fn("DB revision(s) do not match migration scripts revision(s):")
        log_fn(f"DB revision: {db_heads}")
        log_fn(f"Migration scripts revision: {script_heads}")

    return latest


async def wait_for_migrations(quiet: bool = False, check_interval: float = 4) -> None:
    """Loop continuously until all database migrations have been executed.

    Args:
        quiet: Don't print log messages.
        check_interval: Interval in seconds with which is check the migration progress.
    """

    if quiet:
        log_fn = logger.debug
    else:
        log_fn = logger.warning

    if not quiet:
        logger.info("Waiting until database migrations have been executed...")

    migrations_pending = True
    while migrations_pending:
        try:
            with session_scope() as session:
                migrations_pending = not is_db_revision_latest(session, quiet)
        except Exception as e:
            log_fn(f"Could not retrieve the database revision due to: {e}.")
        if migrations_pending:
            log_fn(
                f"Database revision does not match migrations' latest, trying again in {check_interval} seconds."
            )
            await asyncio.sleep(check_interval)

    log_fn("Check for database migrations completed.")
