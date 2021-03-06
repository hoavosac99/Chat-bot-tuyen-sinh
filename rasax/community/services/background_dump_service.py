import asyncio  # pytype: disable=pyi-error
import ctypes
import logging
import time
from multiprocessing import Value, context  # type: ignore
from typing import Text, Iterable, Optional, Set, Dict, Union, Tuple

import rasax.community.utils.common as common_utils
import rasax.community.utils.io as io_utils
import rasax.community.config as rasa_x_config
from rasax.community.database.service import DbService

logger = logging.getLogger(__name__)

BACKGROUND_DUMPING_JOB_ID = "background-file-dumping-job"
_NO_PENDING_CHANGES_TIMESTAMP = -1

# Used to check if everything is dumped which has to be dumped
# (i.e. we have to ensure everything was dumped before committing changes to Git).
_timestamp_of_oldest_pending_change = None
_timestamp_of_latest_pending_change = None


def initialize_global_state(mp_context: context.BaseContext) -> None:
    """Initialize the global state of the module.

    Args:
        mp_context: The current multiprocessing context.
    """
    set_global_state(
        mp_context.Value(ctypes.c_double, _NO_PENDING_CHANGES_TIMESTAMP),
        mp_context.Value(ctypes.c_double, _NO_PENDING_CHANGES_TIMESTAMP),
    )


def set_global_state(
    timestamp_of_oldest_pending_change: Value, timestamp_of_latest_pending_change: Value
) -> None:
    """Set the globally shared state of the module.

    Args:
        timestamp_of_oldest_pending_change: Timestamp of latest change which was dumped.
        timestamp_of_latest_pending_change: Timestamp of oldest change which has to be
            dumped.
    """
    global _timestamp_of_oldest_pending_change, _timestamp_of_latest_pending_change

    _timestamp_of_oldest_pending_change = timestamp_of_oldest_pending_change
    _timestamp_of_latest_pending_change = timestamp_of_latest_pending_change


def get_global_state() -> Tuple[Optional[Value], Optional[Value]]:
    """Get the global state of the module.

    Returns:
        Global state of the module which is shared among processes.
    """
    return _timestamp_of_oldest_pending_change, _timestamp_of_latest_pending_change


class DumpService(DbService):
    """Service which dumps the collected file changes to disk."""

    @staticmethod
    def dump_changes(
        configuration_change: Optional[Dict[Text, Text]] = None,
        domain_changed: bool = False,
        story_changes: Optional[Set[Text]] = None,
        nlu_changes: Optional[Set[Text]] = None,
        lookup_table_changes: Optional[Set[int]] = None,
    ) -> None:
        """Dump all changes to disk.

        Args:
            configuration_change: Properties of the model config which has to be dumped.
            domain_changed: `True` if the domain was changed and has to be dumped.
            story_changes: Story files which have to be dumped.
            nlu_changes: NLU files which have to be dumped.
            lookup_table_changes: IDs of the lookup tables which have to be dumped.
        """
        from rasax.community.database import utils as db_utils

        if not io_utils.should_dump():
            return

        with db_utils.session_scope() as session:
            dump_service = DumpService(session)
            dump_service._dump_changes(
                configuration_change,
                domain_changed,
                story_changes,
                nlu_changes,
                lookup_table_changes,
            )

    def _dump_changes(
        self,
        configuration_change: Optional[Dict[Text, Text]] = None,
        domain_changed: bool = False,
        story_changes: Optional[Set[Text]] = None,
        nlu_changes: Optional[Set[Text]] = None,
        lookup_table_changes: Optional[Set[int]] = None,
    ) -> None:
        logger.debug("Start dumping files to disk.")
        dumped_pending_changes_since = time.time()

        if configuration_change:
            self._dump_config(configuration_change)
        if domain_changed:
            self._dump_domain()
        if story_changes:
            self._dump_stories(story_changes)
        if nlu_changes:
            self._dump_nlu_files(nlu_changes)
        if lookup_table_changes:
            self._dump_lookup_tables(lookup_table_changes)

        _mark_dumping_as_done(dumped_pending_changes_since)

        logger.debug("Finished dumping files to disk.")

    def _dump_config(self, configuration_change: Dict[Text, Text]) -> None:
        from rasax.community.services.settings_service import SettingsService

        settings_service = SettingsService(self.session)
        settings_service.dump_config(
            configuration_change["team"], configuration_change["project_id"]
        )

    def _dump_domain(self) -> None:
        from rasax.community.services.domain_service import DomainService

        domain_service = DomainService(self.session)
        domain_service.dump_domain(rasa_x_config.project_name)

    def _dump_stories(self, story_changes: Set[Text]) -> None:
        from rasax.community.services.story_service import StoryService

        story_service = StoryService(self.session)
        for file in story_changes:
            story_service.dump_stories_to_file_system(file)

    def _dump_nlu_files(self, nlu_changes: Set[Text]) -> None:
        from rasax.community.services.data_service import DataService

        data_service = DataService(self.session)
        data_service.dump_nlu_data(rasa_x_config.project_name, files=nlu_changes)

    def _dump_lookup_tables(self, lookup_table_changes: Set[int]) -> None:
        from rasax.community.services.data_service import DataService

        data_service = DataService(self.session)
        data_service.dump_lookup_tables(lookup_table_changes)


def add_model_configuration_change(team: Text, project_id: Text) -> None:
    """Notify the background service that the model configuration has to be dumped.

    Args:
        team: Team which the model configuration belongs to.
        project_id: Project the model configuration belongs to.
    """
    _modify_job(configuration_change={"team": team, "project_id": project_id})


def _modify_job(**kwargs: Union[bool, Set[Union[Text, int]], Dict[Text, Text]]) -> None:
    from rasax.community import scheduler  # pytype: disable=pyi-error

    scheduler.modify_job(BACKGROUND_DUMPING_JOB_ID, **kwargs)
    _set_timestamp_of_changes(time.time())


def _set_timestamp_of_changes(timestamp: float) -> None:
    with _timestamp_of_oldest_pending_change.get_lock():
        if _timestamp_of_oldest_pending_change.value == _NO_PENDING_CHANGES_TIMESTAMP:
            _timestamp_of_oldest_pending_change.value = timestamp
        else:
            _timestamp_of_oldest_pending_change.value = min(
                timestamp, _timestamp_of_oldest_pending_change.value
            )
    with _timestamp_of_latest_pending_change.get_lock():
        _timestamp_of_latest_pending_change.value = max(
            _timestamp_of_latest_pending_change.value, timestamp
        )


def _mark_dumping_as_done(dumped_pending_changes_since: float) -> None:
    with _timestamp_of_oldest_pending_change.get_lock():
        with _timestamp_of_latest_pending_change.get_lock():
            _timestamp_of_oldest_pending_change.value = max(
                dumped_pending_changes_since, _timestamp_of_latest_pending_change.value
            )


def add_domain_change() -> None:
    """Notify the background service that the domain has to be dumped."""
    _modify_job(domain_changed=True)


def add_story_change(story_file: Text) -> None:
    """Notify the background service that a story file has to be dumped.

    Args:
        story_file: Path to the file which was changed.
    """
    _modify_job(story_changes={story_file})


def add_nlu_changes(changed_nlu_files: Iterable[Text]) -> None:
    """Notify the background service that an NLU file has to be dumped.

    Args:
        changed_nlu_files: Paths of the files which were changed.
    """
    _modify_job(nlu_changes=set(changed_nlu_files))


def add_lookup_table_change(
    changed_lookup_table_id: int, referencing_nlu_file: Text
) -> None:
    """Notify the background service that a lookup table has to be dumped.

    Args:
        changed_lookup_table_id: ID of the lookup table which was changed.
        referencing_nlu_file: Path to the NLU file which references the lookup table.
    """
    _modify_job(
        lookup_table_changes={changed_lookup_table_id},
        nlu_changes={referencing_nlu_file},
    )


async def wait_for_pending_changes_to_be_dumped() -> None:
    """Wait until currently scheduled changes are dumped to disk."""
    now = time.time()

    if not _pending_changes_before(now):
        return

    _trigger_immediate_dumping()

    while _pending_changes_before(now):
        logger.debug("Waiting for pending changes to be dumped... ")
        await asyncio.sleep(0.05)


def _pending_changes_before(timestamp: float) -> bool:
    last_dump_was_before_now = (
        _timestamp_of_oldest_pending_change.value != _NO_PENDING_CHANGES_TIMESTAMP
        and _timestamp_of_oldest_pending_change.value < timestamp
    )
    last_dump_after_latest_change = (
        _timestamp_of_oldest_pending_change.value
        > _timestamp_of_latest_pending_change.value
    )
    return last_dump_was_before_now and not last_dump_after_latest_change


def _trigger_immediate_dumping() -> None:
    from rasax.community import scheduler  # pytype: disable=pyi-error

    scheduler.run_job_immediately(BACKGROUND_DUMPING_JOB_ID)


def cancel_pending_dumping_job() -> None:
    """Stop currently scheduled dumping of files in the background."""
    from rasax.community import scheduler  # pytype: disable=pyi-error

    scheduler.cancel_job(BACKGROUND_DUMPING_JOB_ID)
    _set_timestamp_of_changes(_NO_PENDING_CHANGES_TIMESTAMP)
