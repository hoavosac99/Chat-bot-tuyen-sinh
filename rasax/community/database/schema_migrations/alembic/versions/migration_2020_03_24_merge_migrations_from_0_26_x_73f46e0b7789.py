"""Merge migrations from 0.26.x

Reason:
Merge the migrations from the 0.26.x branch into master.

Revision ID: 73f46e0b7789
Revises: ef93223786ba, 66bada3d94ae

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "73f46e0b7789"
down_revision = ("ef93223786ba", "66bada3d94ae")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
