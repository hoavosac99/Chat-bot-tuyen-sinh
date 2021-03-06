"""Add new `tests.*` permission for admins

Reason:
As a part of improved model testing feature users can create tests.
To do so, users need new permissions, and we're giving this permissions
to admins and annotators by default.

Revision ID: 66bada3d94ae
Revises: 68a8a531a5ee

"""
from alembic import op
import sqlalchemy as sa
import rasax.community.database.schema_migrations.alembic.utils as migration_utils


# revision identifiers, used by Alembic.
revision = "66bada3d94ae"
down_revision = "68a8a531a5ee"
branch_labels = None
depends_on = None


def upgrade():
    from rasax.community.services.user_service import ADMIN, ANNOTATOR

    migration_utils.add_new_permission_to(ADMIN, "tests.*")
    migration_utils.add_new_permission_to(ANNOTATOR, "tests.*")


def downgrade():
    pass
