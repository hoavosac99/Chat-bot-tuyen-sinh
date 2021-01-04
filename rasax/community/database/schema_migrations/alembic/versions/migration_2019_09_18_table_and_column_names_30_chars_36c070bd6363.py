"""Rename too-long table name `conversation_message_correction`.

Reason:
In order to comply with Oracle 12c Enterprise Edition Release 12.1.0.2.0 requirements,
schema, table, and column names must be <= 30 characters.
https://docs.oracle.com/database/121/SQLRF/sql_elements008.htm#SQLRF51129

Revision ID: 36c070bd6363
Revises: 1d990f240f4d

"""
from alembic import op

import rasax.community.database.schema_migrations.alembic.utils as migration_utils


# revision identifiers, used by Alembic.
revision = "36c070bd6363"
down_revision = "1d990f240f4d"
branch_labels = None
depends_on = None


def upgrade():
    if migration_utils.table_exists("conversation_message_correction"):
        op.rename_table("conversation_message_correction", "message_correction")


def downgrade():
    # don't downgrade the table name, since we changed it in the initial migrations
    pass
