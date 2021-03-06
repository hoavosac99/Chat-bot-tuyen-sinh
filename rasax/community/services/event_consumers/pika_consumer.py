import os
import logging
import typing
from contextlib import contextmanager
from typing import Any, Generator, Dict, List, Optional, Text, Union

import rasax.community.constants as constants
import rasax.community.utils.cli as cli_utils
from rasax.community.services.event_consumers.event_consumer import EventConsumer

if typing.TYPE_CHECKING:
    from pika.adapters.blocking_connection import BlockingChannel
    from pika import BlockingConnection, BasicProperties
    import pika
    from pika.connection import Parameters
    from pika.spec import Basic

logger = logging.getLogger("pika")

RASA_EXPORT_PROCESS_ID_HEADER_NAME = "rasa-export-process-id"


class PikaEventConsumer(EventConsumer):
    type_name = "pika"

    def __init__(
        self,
        url: Text,
        username: Text,
        password: Text,
        port: Union[Text, int, None] = 5672,
        queue: Optional[Text] = "rasa_production_events",
        should_run_liveness_endpoint: bool = False,
        **kwargs: Any,
    ):
        """Pika event consumer.

        Args:
            url: RabbitMQ url.
            username: RabbitMQ username.
            password: RabbitMQ password.
            port: RabbitMQ port.
            queue: RabbitMQ queue to be consumed.
            should_run_liveness_endpoint: If `True`, runs a simple Sanic server as a
                background process that can be used to probe liveness of this service.
                The service will be exposed at a port defined by the
                `SELF_PORT` environment variable (5673 by default).
            kwargs: Additional kwargs to be processed. If `queue` is not provided, and
                `queues` is present in `kwargs`, the first queue listed under
                `queues` will be used as the queue to consume.
        """

        self.queue = self._get_queue_from_args(queue, kwargs)
        self.url = url
        self.channel = _initialise_pika_channel(
            url, self.queue, username, password, port
        )
        super().__init__(should_run_liveness_endpoint)

    @classmethod
    def from_endpoint_config(
        cls, consumer_config: Optional[Dict], should_run_liveness_endpoint: bool,
    ) -> Optional["PikaEventConsumer"]:
        if consumer_config is None:
            logger.debug(
                "Could not initialise `PikaEventConsumer` from endpoint config."
            )
            return None

        return cls(
            **consumer_config,
            should_run_liveness_endpoint=should_run_liveness_endpoint,
        )

    @staticmethod
    def _get_queue_from_args(queue: Union[List[Text], Text, None], kwargs: Any) -> Text:
        """Get queue to consume for this event consumer.

        Args:
            queue: Value of the supplied `queue` argument.
            kwargs: Additional kwargs supplied to the `PikaEventConsumer` constructor.
                If `queue` is not supplied, the `queues` kwarg will be used instead.

        Returns:
            Queue this event consumer consumes.

        Raises:
            `ValueError` if no valid `queue` or `queues` argument was found.
        """
        queues: Union[List[Text], Text, None] = kwargs.pop("queues", None)

        if queues and isinstance(queues, list):
            first_queue = queues[0]
            if len(queues) > 1:
                cli_utils.raise_warning(
                    f"Found multiple queues under the `queues` parameter in the pika "
                    f"event consumer config. Will consume the first queue "
                    f"`{first_queue}`."
                )
            return first_queue

        elif queues:
            # `queues` is a string
            return queues  # pytype: disable=bad-return-type

        if queue and isinstance(queue, list):
            first_queue = queue[0]
            if len(queue) > 1:
                cli_utils.raise_warning(
                    f"Found multiple queues under the `queue` parameter in the pika "
                    f"event consumer config. Will consume the first queue "
                    f"`{first_queue}`."
                )
            return first_queue

        elif queue:
            # `queue` is a string
            return queue  # pytype: disable=bad-return-type

        raise ValueError(
            "Could not initialise `PikaEventConsumer` due to invalid "
            "`queues` or `queue` argument in constructor."
        )

    @staticmethod
    def _origin_from_message_properties(pika_properties: "BasicProperties",) -> Text:
        """Fetch message origin from the `app_id` attribute of the message
        properties.

        Args:
            pika_properties: Pika message properties.

        Returns:
            The message properties' `app_id` property if set, otherwise
            `rasax.community.constants.DEFAULT_RASA_ENVIRONMENT`.

        """
        return pika_properties.app_id or constants.DEFAULT_RASA_ENVIRONMENT

    @staticmethod
    def _export_process_id_from_message_properties(
        pika_properties: "BasicProperties",
    ) -> Optional[Text]:
        """Fetch the export process ID header.

        Args:
            pika_properties: Pika message properties.

        Returns:
            The value of the message properties' `rasa-export-process-id` header if
            present.

        """
        headers = pika_properties.headers

        return headers.get(RASA_EXPORT_PROCESS_ID_HEADER_NAME) if headers else None

    # noinspection PyUnusedLocal
    def _callback(
        self,
        ch: "BlockingChannel",
        method: "Basic.Deliver",
        properties: "BasicProperties",
        body: bytes,
    ):
        self.log_event(
            body,
            origin=self._origin_from_message_properties(properties),
            import_process_id=self._export_process_id_from_message_properties(
                properties
            ),
        )

    def consume(self):
        logger.info(f"Start consuming queue '{self.queue}' on pika url '{self.url}'.")
        self.channel.basic_consume(self.queue, self._callback, auto_ack=True)
        self.channel.start_consuming()


def _initialise_pika_channel(
    url: Text,
    queue: Text,
    username: Text,
    password: Text,
    port: Union[Text, int] = 5672,
    connection_attempts: int = 20,
    retry_delay_in_seconds: float = 5,
) -> "BlockingChannel":
    """Initialise a Pika channel with a durable queue.

    Args:
        url: Pika url.
        queue: Pika queue to declare.
        username: Username for authentication with Pika url.
        password: Password for authentication with Pika url.
        port: port of the Pika url.
        connection_attempts: Number of channel attempts before giving up.
        retry_delay_in_seconds: Delay in seconds between channel attempts.

    Returns:
        Pika `BlockingChannel` with declared queue.
    """
    connection = _initialise_pika_connection(
        url, username, password, port, connection_attempts, retry_delay_in_seconds
    )

    return _declare_pika_channel_with_queue(connection, queue)


@contextmanager
def _pika_log_level(temporary_log_level: int) -> Generator[None, None, None]:
    """Change the log level of the `pika` library.

    The log level will remain unchanged if the current log level is 10 (`DEBUG`) or
    lower.

    Args:
        temporary_log_level: Temporary log level for pika. Will be reverted to
        previous log level when context manager exits.
    """
    pika_logger = logging.getLogger("pika")
    old_log_level = pika_logger.level
    is_debug_mode = logging.root.level <= logging.DEBUG

    if not is_debug_mode:
        pika_logger.setLevel(temporary_log_level)

    yield

    pika_logger.setLevel(old_log_level)


def _create_rabbitmq_ssl_options(
    rabbitmq_host: Optional[Text] = None,
) -> Optional["pika.SSLOptions"]:
    """Create RabbitMQ SSL options.

    Requires the following environment variables to be set:

        RABBITMQ_SSL_CLIENT_CERTIFICATE - path to the SSL client certificate (required)
        RABBITMQ_SSL_CLIENT_KEY - path to the SSL client key (required)
        RABBITMQ_SSL_CA_FILE - path to the SSL CA file for verification (optional)
        RABBITMQ_SSL_KEY_PASSWORD - SSL private key password (optional)

    Details on how to enable RabbitMQ TLS support can be found here:
    https://www.rabbitmq.com/ssl.html#enabling-tls

    Args:
        rabbitmq_host: RabbitMQ hostname

    Returns:
        Pika SSL context of type `pika.SSLOptions` if
        the RABBITMQ_SSL_CLIENT_CERTIFICATE and RABBITMQ_SSL_CLIENT_KEY
        environment variables are valid paths, else `None`.
    """
    client_certificate_path = os.environ.get("RABBITMQ_SSL_CLIENT_CERTIFICATE")
    client_key_path = os.environ.get("RABBITMQ_SSL_CLIENT_KEY")

    if client_certificate_path and client_key_path:
        import pika
        import ssl

        logger.debug(f"Configuring SSL context for RabbitMQ host '{rabbitmq_host}'.")

        ca_file_path = os.environ.get("RABBITMQ_SSL_CA_FILE")
        key_password = os.environ.get("RABBITMQ_SSL_KEY_PASSWORD")

        ssl_context = ssl.create_default_context(
            purpose=ssl.Purpose.CLIENT_AUTH, cafile=ca_file_path
        )
        ssl_context.load_cert_chain(
            client_certificate_path, keyfile=client_key_path, password=key_password
        )
        return pika.SSLOptions(ssl_context, rabbitmq_host)
    else:
        return None


def _get_pika_parameters(
    url: Text,
    username: Text,
    password: Text,
    port: Union[Text, int] = 5672,
    connection_attempts: int = 20,
    retry_delay_in_seconds: float = 5,
) -> "Parameters":
    """Create Pika `Parameters`.

    Args:
        url: Pika url
        username: username for authentication with Pika url
        password: password for authentication with Pika url
        port: port of the Pika url
        connection_attempts: number of channel attempts before giving up
        retry_delay_in_seconds: delay in seconds between channel attempts

    Returns:
        `pika.ConnectionParameters` which can be used to create a new connection to a
        broker.
    """
    import pika

    if url.startswith("amqp"):
        # user supplied an AMQP URL containing all the info
        parameters = pika.URLParameters(url)
        parameters.connection_attempts = connection_attempts
        parameters.retry_delay = retry_delay_in_seconds
        if username:
            parameters.credentials = pika.PlainCredentials(username, password)
    else:
        # url seems to be just the url, so we use our parameters
        parameters = pika.ConnectionParameters(
            host=url,
            port=port,
            credentials=pika.PlainCredentials(username, password),
            connection_attempts=connection_attempts,
            # Wait between retries since
            # it can take some time until
            # RabbitMQ comes up.
            retry_delay=retry_delay_in_seconds,
            ssl_options=_create_rabbitmq_ssl_options(url),
        )

    return parameters


def _initialise_pika_connection(
    url: Text,
    username: Text,
    password: Text,
    port: Union[Text, int] = 5672,
    connection_attempts: int = 20,
    retry_delay_in_seconds: float = 5,
) -> "BlockingConnection":
    """Create a Pika `BlockingConnection`.

    Args:
        url: Pika url
        username: username for authentication with Pika url
        password: password for authentication with Pika url
        port: port of the Pika url
        connection_attempts: number of channel attempts before giving up
        retry_delay_in_seconds: delay in seconds between channel attempts

    Returns:
        `pika.BlockingConnection` with provided parameters
    """
    import pika

    with _pika_log_level(logging.CRITICAL):
        parameters = _get_pika_parameters(
            url, username, password, port, connection_attempts, retry_delay_in_seconds
        )
        return pika.BlockingConnection(parameters)


def _declare_pika_channel_with_queue(
    connection: "BlockingConnection", queue: Text
) -> "BlockingChannel":
    """Declare a durable queue on Pika channel."""
    channel = connection.channel()
    channel.queue_declare(queue, durable=True)

    return channel
