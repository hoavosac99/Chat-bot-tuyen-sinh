"""Add a 'review_status' column to the 'conversation' table.

Reason:
This is a new feature we're adding to Rasa X: the ability
to set a 'Review Status' on a conversation.

Revision ID: 90b60aff4920
Revises: 479084222950

"""
from alembic import op
import sqlalchemy as sa

import rasax.community.database.schema_migrations.alembic.utils as migration_utils
import rasax.community.constants as constants


# revision identifiers, used by Alembic.
revision = "90b60aff4920"
down_revision = "479084222950"
branch_labels = None
depends_on = None

TABLE_NAME = "conversation"
COLUMN_NAME = "review_status"


def upgrade() -> None:
    migration_utils.create_column(
        TABLE_NAME,
        sa.Column(
            COLUMN_NAME,
            sa.String(50),
            default=constants.CONVERSATION_STATUS_UNREAD,
            server_default=constants.CONVERSATION_STATUS_UNREAD,
            nullable=False,
        ),
    )


def downgrade() -> None:
    migration_utils.drop_column(TABLE_NAME, COLUMN_NAME)
