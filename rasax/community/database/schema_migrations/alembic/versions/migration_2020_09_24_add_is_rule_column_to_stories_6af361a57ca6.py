"""Add is_rule column to stories

Reason:
The `is_rule` property allows us to distinguish between rules and stories.

Revision ID: 6af361a57ca6
Revises: 3d91317b7460

"""
from alembic import op
import sqlalchemy as sa
import rasax.community.database.schema_migrations.alembic.utils as migration_utils
from rasax.community.services.user_service import ADMIN, ANNOTATOR


# revision identifiers, used by Alembic.
revision = "6af361a57ca6"
down_revision = "3d91317b7460"
branch_labels = None
depends_on = None

TABLE = "story"
COLUMN = "is_rule"


def upgrade():
    if not migration_utils.get_column(TABLE, COLUMN):
        migration_utils.create_column(
            TABLE, sa.Column(COLUMN, sa.Boolean(), default=False)
        )

    migration_utils.add_new_permission_to(ANNOTATOR, "rules.*")
    migration_utils.add_new_permission_to(ADMIN, "rules.*")


def downgrade():
    migration_utils.delete_permission_from(ANNOTATOR, "rules.*")
    migration_utils.delete_permission_from(ADMIN, "rules.*")

    if migration_utils.get_column(TABLE, COLUMN):
        migration_utils.drop_column(TABLE, COLUMN)
