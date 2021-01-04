import time
from typing import Text, Dict, Any, Optional, List, Type

from sqlalchemy.orm import Session
from sqlalchemy.util import IdentitySet

import rasax.community.utils.common as common_utils
import rasax.community.utils.io as io_utils
import rasax.community.config as rasa_x_config
import rasax.community.constants as constants


class AnnotationDumpLogger:
    """Log who dumped training data and when.

    This is information is used as an indicator for a change in the status of the
    Integrated Version Control feature.
    """

    def __init__(self, session: Session) -> None:
        """Create an `AnnotationDumpTracker`.

        Args:
            session: The database session containing potential changes to the training
                data.
        """

        self._session = session
        self._training_data_classes = self._get_classes_to_log()

    @staticmethod
    def _get_classes_to_log() -> List[Type]:
        from rasax.community.database import data, domain, intent
        from rasax.community.database.admin import Project

        classes_to_log = [Project]
        for module in [data, domain, intent]:
            classes_to_log += common_utils.get_orm_classes_in_module(module)

        return classes_to_log

    def track(self, user: Optional[Dict[Text, Any]]) -> None:
        """Track potential training data changes in case they should have been dumped.

        Args:
            user: Rasa X user who sent the HTTP request and hence caused potential
                updates in the database.
        """

        if (
            rasa_x_config.LOCAL_MODE
            or not user
            or not common_utils.is_git_available()
            or not io_utils.should_dump()
        ):
            return

        if (
            self._was_training_data_added()
            or self._was_training_data_updated()
            or self._was_training_data_deleted()
        ):
            from rasax.community.services.integrated_version_control.git_service import (
                GitService,
            )

            git_service = GitService(self._session)
            annotator = user[constants.USERNAME_KEY]
            git_service.track_training_data_dumping(annotator, time.time())

    def _was_training_data_added(self) -> bool:
        return self._changes_include_training_data(self._session.new)

    def _changes_include_training_data(self, changes: IdentitySet) -> bool:
        return any(type(change) in self._training_data_classes for change in changes)

    def _was_training_data_updated(self) -> bool:
        return self._changes_include_training_data(self._session.dirty)

    def _was_training_data_deleted(self) -> bool:
        return self._changes_include_training_data(self._session.deleted)
