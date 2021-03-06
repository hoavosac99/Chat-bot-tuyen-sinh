import json
import logging
import os
import typing
from collections import deque
from typing import Text, Optional, Union, Deque, Callable
import sqlalchemy.exc

from sanic.response import HTTPResponse

import rasax.community.config as rasa_x_config
import rasax.community.utils.common as common_utils
import rasax.community.utils.cli as cli_utils
import rasax.community.database.utils as db_utils
from rasax.community.services.analytics_service import AnalyticsService
from rasax.community.services.event_service import EventService
from rasax.community.services.logs_service import LogsService

if typing.TYPE_CHECKING:
    from multiprocessing import Process  # type: ignore
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

MAX_PENDING_EVENTS = 1000  # number of PendingEvents to keep in memory


def _run_liveness_app(port: int, consumer_type: Text) -> None:
    from sanic import Sanic
    from sanic import response

    app = Sanic(__name__)

    @app.route("/health")
    async def health(_) -> HTTPResponse:
        return response.text(f"{consumer_type} consumer is running.", 200)

    app.run(host="0.0.0.0", port=port, access_log=False)


class EventConsumer:
    """Abstract base class for all event consumers."""

    type_name = None

    def __init__(
        self,
        should_run_liveness_endpoint: bool = False,
        session: Optional["Session"] = None,
    ) -> None:
        """Abstract event consumer that implements a liveness endpoint.

        Args:
            should_run_liveness_endpoint: If `True`, runs a Sanic server as a
                background process that can be used to probe liveness of this service.
                The service will be exposed at a port defined by the
                `SELF_PORT` environment variable (5673 by default).
            session: SQLAlchemy session to use.

        """
        self.liveness_endpoint: Optional["Process"] = None
        self.start_liveness_endpoint_process(should_run_liveness_endpoint)

        self._session = session or db_utils.get_database_session(
            rasa_x_config.LOCAL_MODE
        )

        self.event_service = EventService(self._session)
        self.analytics_service = AnalyticsService(self._session)
        self.logs_service = LogsService(self._session)

        self.pending_events: Deque[PendingEvent] = deque(maxlen=MAX_PENDING_EVENTS)

    def __enter__(self) -> None:
        pass

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._session.close()

    @staticmethod
    def _run_liveness_endpoint_process(consumer_type: Text) -> "Process":
        """Run a Sanic app as a multiprocessing.Process and return it.

        Args:
            consumer_type: Event consumer type.

        Returns:
            Sanic endpoint app as a multiprocessing.Process.

        """
        port = int(os.environ.get("SELF_PORT", "5673"))
        p = common_utils.run_in_process(
            fn=_run_liveness_app, args=(port, consumer_type), daemon=True
        )

        logger.info(f"Started Sanic liveness endpoint at port '{port}'.")

        return p

    def start_liveness_endpoint_process(
        self, should_run_liveness_endpoint: bool
    ) -> None:
        """Start liveness endpoint multiprocessing.Process if
        `should_run_liveness_endpoint` is `True`, else do nothing."""

        if should_run_liveness_endpoint:
            self.liveness_endpoint = self._run_liveness_endpoint_process(self.type_name)

    def kill_liveness_endpoint_process(self) -> None:
        """Kill liveness endpoint multiprocessing.Process if it is active."""

        if self.liveness_endpoint and self.liveness_endpoint.is_alive():
            self.liveness_endpoint.terminate()
            logger.info(
                f"Terminated event consumer liveness endpoint process "
                f"with PID '{self.liveness_endpoint.pid}'."
            )

    def log_event(
        self,
        data: Union[Text, bytes],
        sender_id: Optional[Text] = None,
        event_number: Optional[int] = None,
        origin: Optional[Text] = None,
        import_process_id: Optional[Text] = None,
    ) -> None:
        """Handle an incoming event forwarding it to necessary services and handlers.

        Args:
            data: Event to be logged.
            sender_id: Conversation ID sending the event.
            event_number: Event number associated with the event.
            origin: Rasa environment origin of the event.
            import_process_id: Unique ID if the event comes from a `rasa export`
                process.

        """

        log_operation = self._event_log_operation(
            data, sender_id, event_number, origin, import_process_id
        )

        try:
            log_operation()

            self._session.commit()

            self._process_pending_events()
        except sqlalchemy.exc.IntegrityError as e:
            logger.warning(
                f"Saving event failed due to an 'IntegrityError'. This "
                f"means that the event is already stored in the "
                f"database. The event data was '{data}'. {e}"
            )
            self._session.rollback()
        except Exception as e:
            logger.error(e)
            self._save_event_as_pending(data, log_operation)
            self._session.rollback()

    def _event_log_operation(
        self,
        data: Union[Text, bytes],
        sender_id: Optional[Text] = None,
        event_number: Optional[int] = None,
        origin: Optional[Text] = None,
        import_process_id: Optional[Text] = None,
    ) -> Callable[[], None]:
        def _log() -> None:
            event = self.event_service.save_event(
                data,
                sender_id=sender_id,
                event_number=event_number,
                origin=origin,
                import_process_id=import_process_id,
            )

            self.logs_service.save_nlu_logs_from_event(
                data, event.id, event.conversation_id
            )
            self.analytics_service.save_analytics(data, sender_id=event.conversation_id)

            if common_utils.is_enterprise_installed():
                from rasax.enterprise import reporting  # pytype: disable=import-error

                reporting.report_event(json.loads(data), event.conversation_id)

        return _log

    def _save_event_as_pending(
        self,
        raw_event: Union[Text, bytes],
        on_save: Optional[Callable[[], None]] = None,
    ) -> None:
        """Add `ConversationEvent` to pending events.

        Args:
            raw_event: Consumed event which has to be saved later since the last try
                failed.
            on_save: `Callable` that will be called to persist the event.
        """
        if len(self.pending_events) >= MAX_PENDING_EVENTS:
            pending_event = self.pending_events.popleft()
            cli_utils.raise_warning(
                f"`PendingEvents` deque has exceeded its maximum length of "
                f"{MAX_PENDING_EVENTS}. The oldest event with data "
                f"{pending_event.raw_event} was removed."
            )

        self.pending_events.append(PendingEvent(raw_event, on_save))

    def _process_pending_events(self) -> None:
        """Process all pending events."""

        for pending_event in list(self.pending_events):
            try:
                pending_event.on_save()
                self._session.commit()
                self.pending_events.remove(pending_event)
            except Exception as e:
                self._session.rollback()
                logger.debug(
                    f"Cannot process the pending event with "
                    f"the following data: '{pending_event.raw_event}'."
                    f"Exception: {e}."
                )

    def consume(self):
        """Consume events."""
        raise NotImplementedError(
            "Each event consumer needs to implement the `consume()` method."
        )


class PendingEvent:
    """A class that represents a pending event — an event that will be saved later."""

    def __init__(
        self, raw_event: Union[Text, bytes], on_save: Optional[Callable[[], None]]
    ):
        """Create an instance of `PendingEvent`.

        Args:
            raw_event: Consumed event that needs to be saved later.
            on_save: a callback function that will be called after the event is added to
            the database.
        """
        self.raw_event = raw_event
        self.on_save: Optional[Callable[[], None]] = on_save
