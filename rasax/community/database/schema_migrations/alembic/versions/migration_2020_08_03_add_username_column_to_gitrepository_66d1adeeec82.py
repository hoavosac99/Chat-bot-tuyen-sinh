"""Add username column to GitRepository

Reason:
This migration adds the `username` column to `git_repository`. We need to store
this property so that we can clone repositories via HTTPS (the password is
provided via an API request, and is never stored locally).

Revision ID: 66d1adeeec82
Revises: 9fde59db88e6

"""
from alembic import op
import sqlalchemy as sa
import rasax.community.database.schema_migrations.alembic.utils as migration_utils


# revision identifiers, used by Alembic.
revision = "66d1adeeec82"
down_revision = "9fde59db88e6"
branch_labels = None
depends_on = None

TABLE = "git_repository"
COLUMN = "username"


def upgrade():
    if not migration_utils.get_column(TABLE, COLUMN):
        migration_utils.create_column(TABLE, sa.Column(COLUMN, sa.String(255)))


def downgrade():
    if migration_utils.get_column(TABLE, COLUMN):
        migration_utils.drop_column(TABLE, COLUMN)
