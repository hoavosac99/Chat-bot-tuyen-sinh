"""Merge heads af3596f6982f and 8a8562256a8e.

Reason:
This migration merges two alembic heads.

Revision ID: eb2b98905e7e
Revises: af3596f6982f, 8a8562256a8e

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "eb2b98905e7e"
down_revision = ("af3596f6982f", "8a8562256a8e")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
