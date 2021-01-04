import logging
from typing import Text, List, Optional, Dict, Callable, Any, Union, Type

import sqlalchemy
import sqlalchemy.exc
import sqlalchemy.orm
from alembic import op
from alembic.operations import BatchOperations, Operations
from sqlalchemy import Column, MetaData, Table, schema, Sequence, and_, func, CLOB
from sqlalchemy.dialects.sqlite.base import SQLiteDialect
from sqlalchemy.engine import reflection, RowProxy, Connection
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.orm import Session, aliased

logger = logging.getLogger(__name__)

ORACLE_DIALECT = "oracle"


def get_inspector() -> Inspector:
    """Get the inspector for the current bind."""
    bind = op.get_bind()
    inspector = reflection.Inspector.from_engine(bind)
    return inspector


def using_dialect(dialect: Text, bind: Optional[Connection] = None) -> bool:
    """Determine whether we're using SQL dialect `dialect` or not.

    Args:
        dialect: SQL dialect, e.g. 'postgresql'.
        bind: SQL session bind to use. A new one is fetched from
            `alembic.Operations` if no bind is provided.

    Returns:
        `True` if the current dialect is `dialect`.
    """
    if not bind:
        bind = op.get_bind()

    return bind.dialect.name == dialect


def table_exists(table: Text) -> bool:
    """Check whether a table of the given name exists."""
    inspector = get_inspector()
    tables = inspector.get_table_names()

    return table in tables


def index_exists(table: Text, index: Text) -> bool:
    """Check whether an index exists, for a given table.

    Args:
        table: Table name.
        index: Index name.

    Raises:
        ValueError: If the table does not exist.

    Returns:
        `True` if the specified index exists.
    """
    if not table_exists(table):
        raise ValueError(f"Table '{table}' does not exist.")

    inspector = get_inspector()
    return index in {index["name"] for index in inspector.get_indexes(table)}


def table_has_column(table: Text, column: Text) -> bool:
    """Check whether a table with a given column exists."""
    return get_column(table, column) is not None


def get_column(table: Text, column: Text) -> Optional[Dict[Text, Any]]:
    """Fetch `column` of `table`."""
    inspector = get_inspector()

    try:
        return next(
            (
                table
                for table in inspector.get_columns(table)
                if table["name"].lower() == column.lower()
            ),
            None,
        )
    except sqlalchemy.exc.NoSuchTableError:
        return None


def is_column_of_type(table: Text, column: Text, _type: Type) -> bool:
    """Check whether `column` of `table` is of type `_type`."""
    column = get_column(table, column)
    if not column:
        return False

    return isinstance(column["type"], _type)


def create_column(table: Text, column: Column) -> None:
    """Add `column` to `table`."""
    with op.batch_alter_table(table) as batch_op:
        batch_op.add_column(column)


def drop_column(table: Text, column: Text) -> None:
    """Drop `column` from `table`."""
    with op.batch_alter_table(table) as batch_op:
        batch_op.drop_column(column)


def create_primary_key(
    batch_op: BatchOperations,
    table_name: Text,
    primary_key_columns: List[Text],
    primary_key_name: Optional[Text] = None,
) -> None:
    """Create a new primary key and drops existing old ones."""
    old_primary_key_name = get_primary_key(table_name)
    drop_constraint(old_primary_key_name, batch_op)

    if not primary_key_name:
        primary_key_name = old_primary_key_name

    batch_op.create_primary_key(primary_key_name, columns=primary_key_columns)


def get_primary_key(table_name: Text) -> Text:
    """Return the name of the primary key in a table."""
    inspector = get_inspector()
    return inspector.get_pk_constraint(table_name)["name"]


def get_foreign_key(
    table_name: Text, referred_table: Text, constrained_column: Text
) -> Optional[Text]:
    """Return the name of the foreign key (FK) in a table.

    Args:
        table_name: Name of the table to inspect.
        referred_table: Name of the table that's referred to in the FK.
        constrained_column: Name of the column that's constrained (this is the column
            that is part of the table called `table_name`).

    Returns:
        The name of the FK if found, else `None`.
    """
    inspector = get_inspector()
    foreign_keys = inspector.get_foreign_keys(table_name)

    return next(
        (
            key["name"]
            for key in foreign_keys
            if key["referred_table"] == referred_table
            and constrained_column in key["constrained_columns"]
        ),
        None,
    )


def _get_meta() -> MetaData:
    meta = MetaData(bind=op.get_bind())
    meta.reflect()

    return meta


def drop_constraint(constraint_name: Optional[Text], batch_op: BatchOperations) -> None:
    """Drop constraint on table.

    The constraint can be a foreign key, a primary key or a unique constraint.

    Args:
        constraint_name: Name of the foreign key to drop.
        batch_op: Instance of `BatchOperations`.
    """
    if constraint_name is None:
        logger.debug("Cannot drop constraint with key 'None'.")
        return

    context = op.get_context()

    # in SQLite the constraint is unnamed and this does not work
    # (dropping the constraint is also not needed)
    if not isinstance(context.dialect, SQLiteDialect):
        batch_op.drop_constraint(constraint_name)


def rename_or_create_index(
    table_name: Text,
    old_index_name: Text,
    new_index_name: Text,
    indexed_columns: List[Text],
) -> None:
    """Rename an index in case it exists, create it otherwise."""
    with op.batch_alter_table(table_name) as batch_op:
        # Databases might save indexes in a lower / upper case version
        matching_index_name = _find_matching_index(old_index_name, table_name)

        if matching_index_name:
            batch_op.drop_index(matching_index_name)

        existing_new_index_name = _find_matching_index(new_index_name, table_name)

        if not existing_new_index_name:
            batch_op.create_index(new_index_name, indexed_columns)


def _find_matching_index(index_name_to_find: Text, table_name: Text) -> Optional[Text]:
    inspector = get_inspector()

    return next(
        (
            index["name"]
            for index in inspector.get_indexes(table_name)
            if index["name"].lower() == index_name_to_find.lower()
        ),
        None,
    )


class ColumnTransformation:
    """Describes the modification which should be done to the column of a table."""

    def __init__(
        self,
        column_name: Text,
        new_column_args: Optional[List] = None,
        new_column_kwargs: Optional[Dict] = None,
        modify_from_column_value: Optional[Callable[[Any], Any]] = None,
        modify_from_row: Optional[Callable[[RowProxy], Any]] = None,
        new_column_name: Optional[Text] = None,
    ) -> None:
        """Create a transformation.

        Args:
            column_name: Current name of the column which should be changed.
            new_column_args: Args which are passed to `Column`.
            new_column_kwargs: Kwargs which are passed to `Column`.
            modify_from_column_value: Operation which should be applied when copying
                data from the old column to the new column.
            modify_from_row: Operation which should be applied when copying data from
                the old row to the new column.
            new_column_name: Name of the new column. If `None` is given, it's assumed
                that the old column should be replaced.
        """
        self.column_name = column_name
        self.new_column_args = new_column_args or []
        self.new_column_kwargs = new_column_kwargs or {}
        self.modify_from_column_value = modify_from_column_value
        self.modify_from_row = modify_from_row
        self._new_column_name = new_column_name

    @property
    def new_column_name(self) -> Text:
        if self._new_column_name:
            return self._new_column_name
        else:
            # return a temporary name
            return self.column_name + "_tmp"

    def new_column_replaces_old_column(self) -> bool:
        return self._new_column_name is None


def modify_columns(table_name: Text, columns: List[ColumnTransformation]) -> None:
    """Modify multiple columns of a table.

    Args:
        table_name: Name of the table.
        columns: Column modifications which should be applied to the table.
    """
    # do nothing on an empty list of modifications
    if not columns:
        return

    session = _get_session()

    with op.batch_alter_table(table_name) as batch_op:
        # Create temporary columns including modifications
        for transformation in columns:
            new_column_name = transformation.new_column_name
            batch_op.add_column(
                sqlalchemy.Column(
                    new_column_name,
                    *transformation.new_column_args,
                    **transformation.new_column_kwargs,
                )
            )

    with op.batch_alter_table(table_name) as batch_op:
        metadata = _get_meta()
        table = Table(table_name, metadata, autoload=True)

        for transformation in columns:
            logger.debug(
                f"Start modifying column '{new_column_name}' in "
                f"table '{table_name}'."
            )
            new_column_name = transformation.new_column_name

            # Use SQLAlchemy Core cause the ORM mappings can change over time
            if (
                transformation.modify_from_row
                or transformation.modify_from_column_value
            ):
                _modify_one_row_at_a_time(table, session, transformation)
            else:
                # If it's just copying over things, we can do that quicker
                update_query = table.update().values(
                    {table.c[new_column_name]: table.c[transformation.column_name]}
                )
                session.execute(update_query)

            session.commit()

            if transformation.new_column_replaces_old_column():
                # Delete old column
                batch_op.drop_column(transformation.column_name)
                # Rename temporary column to replace old one
                batch_op.alter_column(
                    column_name=new_column_name,
                    new_column_name=transformation.column_name,
                )


def _get_session() -> Session:
    bind = op.get_bind()
    return sqlalchemy.orm.Session(bind=bind)


def _get_temporary_column_name(column_name: Text) -> Text:
    return column_name + "_tmp"


def _modify_one_row_at_a_time(
    table: Table, session: Session, transformation: ColumnTransformation
) -> None:
    """Modify a table by applying a Python function to each row.

    Note: If the table is large, this can be an expensive operation since every row
        is retrieved, modified, and written back to the database.

    Args:
        table: The table which is modified.
        session: The current database session.
        transformation: The column transformation which should be applied to the table.

    """
    rows = session.execute(table.select()).fetchall()
    for row in rows:
        old_value = getattr(row, transformation.column_name)

        if transformation.modify_from_column_value:
            new_value = transformation.modify_from_column_value(old_value)
        elif transformation.modify_from_row:
            new_value = transformation.modify_from_row(row)
        else:
            new_value = old_value
        update_query = (
            sqlalchemy.update(table)
            .values({transformation.new_column_name: new_value})
            .where(table.c[transformation.column_name] == old_value)
        )
        session.execute(update_query)


def dialect_supports_sequences() -> bool:
    return op._proxy.migration_context.dialect.supports_sequences


def create_sequence(table_name: Text, suffix="_seq") -> None:
    if dialect_supports_sequences():
        op.execute(schema.CreateSequence(Sequence(table_name + suffix)))


def add_new_permission_to(role: Text, permission: Text) -> None:
    """Add a new permission to a role."""
    metadata = _get_meta()
    session = _get_session()

    projects = session.execute(
        Table("project", metadata, autoload=True).select()
    ).fetchall()

    permission_table = get_reflected_table("permission", session)
    for project in projects:
        existing_permissions = session.execute(
            permission_table.select(
                and_(
                    permission_table.c.project == project.project_id,
                    permission_table.c.role_id == role,
                    permission_table.c.permission == permission,
                )
            )
        ).fetchall()

        if len(existing_permissions) == 0:
            insert_query = permission_table.insert().values(
                project=project.project_id, role_id=role, permission=permission
            )
            session.execute(insert_query)

    session.commit()


def delete_permission_from(role: Text, permission: Text) -> None:
    """Deletes a permission from a role."""
    metadata = _get_meta()
    session = _get_session()

    projects = session.execute(
        Table("project", metadata, autoload=True).select()
    ).fetchall()

    permission_table = get_reflected_table("permission", session)
    for project in projects:
        delete_query = permission_table.delete().where(
            and_(
                permission_table.c.project == project.project_id,
                permission_table.c.role_id == role,
                permission_table.c.permission == permission,
            )
        )
        session.execute(delete_query)

    session.commit()


def get_reflected_table(
    table_name: Text, alembic_op: Union[Operations, Session]
) -> Table:
    """Get reflected table."""
    metadata = MetaData(bind=alembic_op.get_bind())
    metadata.reflect()

    return Table(table_name, metadata, autoload=True)


def delete_duplicate_rows(
    table: Text,
    column_names: List[Text],
    id_column_name: Text = "id",
    message: Optional[Text] = None,
) -> None:
    """Delete duplicate entries in `table`.

    Duplicate entries are those that have more than one row with the same
    entries in all of `column_names`.

    Args:
        table: Name of the table to deduplicate.
        column_names: Columns to base the deduplication on.
        id_column_name: Name of the table's ID column.
        message: Debug message. This message should explain the motivation for this
            migration.
    """

    def _is_clob_column(column: Column) -> bool:
        """Determine whether `column` is of type `CLOB`."""
        return isinstance(column.type, CLOB)

    bind = op.get_bind()
    session = Session(bind=bind)

    is_oracle_connection = using_dialect(ORACLE_DIALECT, bind)

    aliased_table_1 = aliased(get_reflected_table(table, session), name="a")
    aliased_table_2 = aliased(get_reflected_table(table, session), name="b")

    id_column_1 = aliased_table_1.c[id_column_name]
    id_column_2 = aliased_table_2.c[id_column_name]

    unique_columns_1 = [aliased_table_1.c[column] for column in column_names]
    unique_columns_2 = [aliased_table_2.c[column] for column in column_names]

    and_query = [id_column_1 < id_column_2]

    for column_1, column_2 in zip(unique_columns_1, unique_columns_2):

        if is_oracle_connection and _is_clob_column(column_1):
            # a successful `DBMS_LOB` comparison returns 0
            # https://docs.oracle.com/cd/B19306_01/appdev.102/b14258/d_lob.htm#i1016668
            comparison = func.DBMS_LOB.Compare(column_1, column_2) == 0
        else:
            comparison = column_1 == column_2

        and_query.append(comparison)

    q = session.query(id_column_1).join(aliased_table_2, and_(*and_query))

    duplicate_entries = session.execute(q).fetchall()

    ids_to_delete = [entry[0] for entry in duplicate_entries]

    if ids_to_delete:
        if message:
            logger.debug(message)

        logger.debug(
            f"Deleting {len(ids_to_delete)} duplicate records in '{table}' table."
        )
        reflected_table = get_reflected_table(table, session)
        id_column = reflected_table.c[id_column_name]
        delete_query = reflected_table.delete().where(id_column.in_(ids_to_delete))
        session.execute(delete_query)

    session.commit()
