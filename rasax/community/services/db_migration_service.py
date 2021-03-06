import logging
import os

from sanic import response, Sanic, request
from http import HTTPStatus

from typing import Any, Dict, Text
from sqlalchemy.orm import Session

import rasax.community.sql_migrations as sql_migrations
import rasax.community.constants as constants
import rasax.community.utils.common as rasa_x_utils
import rasax.community.config as rasa_x_config
import rasax.community.utils.cli as cli_utils
from rasax.community.database import utils as db_utils
from rasa import server as rasa_server

logger = logging.getLogger(__name__)
app = Sanic(__name__)


def _db_migrate() -> None:
    """Start the database migrations."""
    rasa_x_utils.update_log_level()

    if not rasa_x_config.should_run_database_migration_separately:
        logger.info(
            f"Database migration is disabled. Set the {constants.DATABASE_MIGRATION_SEPARATION_ENV}"
            f" environment variable to `True` if you want to run a database migration."
        )
        return

    logger.info("Starting the database migration service")

    with db_utils.session_scope() as session:
        sql_migrations.run_migrations(session)
        db_heads = db_utils.get_database_revision_heads(session)

    logger.info(f"The database migration has finished. DB revision: {db_heads}")


@app.route("/health")
async def _health(request: request.Request) -> response.HTTPResponse:
    migration_process = request.app.migration_process
    if not migration_process.is_alive() and migration_process.exitcode != os.EX_OK:
        return response.json(
            {
                "message": "The migration process is not alive",
                "process_exit_code": migration_process.exitcode,
            },
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )

    return response.json(
        {"message": "The database migration service is healthy."}, HTTPStatus.OK
    )


@app.route("/")
async def _get_migration_status(_) -> response.HTTPResponse:
    status = await migration_status()

    return response.json(status)


async def migration_status() -> Dict[Text, Any]:
    """Returns a DB migration status.

    Returns:
        A DB migration status.
    """
    status = "completed"

    with db_utils.session_scope() as session:
        migrations_pending = not db_utils.is_db_revision_latest(session)
        db_heads = db_utils.get_database_revision_heads(session)
        progress = sql_migrations.get_migration_progress(session)

    if not db_heads:
        db_heads = []
        status = "pending"
    elif migrations_pending:
        status = "in_progress"

    return {
        "status": status,
        "current_revision": db_heads,
        "target_revision": db_utils.get_migration_scripts_heads(),
        "progress_in_percent": progress,
    }


def main() -> None:
    """Start the database migration service"""

    rasa_x_utils.update_log_level()

    port = int(os.environ.get("SELF_PORT", "8000"))

    app.migration_process = rasa_x_utils.run_in_process(fn=_db_migrate, daemon=True)

    ssl_context = None
    if rasa_x_utils.is_enterprise_installed():
        ssl_context = rasa_server.create_ssl_context(
            ssl_certificate=rasa_x_config.ssl_certificate,
            ssl_keyfile=rasa_x_config.ssl_keyfile,
            ssl_ca_file=rasa_x_config.ssl_ca_file,
            ssl_password=rasa_x_config.ssl_password,
        )
    protocol = "https" if ssl_context else "http"

    cli_utils.print_success(
        f"Starting the database migration service ({protocol})... 🚀"
    )

    app.run(
        host="0.0.0.0", port=port, ssl=ssl_context,
    )


if __name__ == "__main__":
    main()
