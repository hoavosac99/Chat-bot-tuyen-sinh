"""Merge the migrations from the 0.24.x patch release with
Rasa X databases which didn't run this backported migration.

Reason:
Merge the migrations from the 0.24.x branch into master.

Revision ID: 9e7a97234e85
Revises: ac3fba1c2b86, b67a67032c7f

"""

# revision identifiers, used by Alembic.
revision = "9e7a97234e85"
down_revision = ("ac3fba1c2b86", "b67a67032c7f")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
