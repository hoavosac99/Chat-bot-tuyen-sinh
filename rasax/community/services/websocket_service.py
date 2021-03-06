import asyncio  # pytype: disable=pyi-error
import multiprocessing  # type: ignore
from enum import Enum
from asyncio import BaseEventLoop  # pytype: disable=pyi-error
import json
import logging
from typing import (
    Dict,
    Text,
    Any,
    List,
    Union,
    Iterable,
    NamedTuple,
    NoReturn,
    Optional,
)
from multiprocessing.context import BaseContext  # type: ignore

from websockets import WebSocketCommonProtocol  # type: ignore
import rasax.community.utils.common as common_utils

logger = logging.getLogger(__name__)

BROADCAST_RECIPIENT_ID = "ALL"
RECIPIENT_KEY = "recipient_id"
SCOPES_KEY = "scopes"


class MessageTopic(Enum):
    """Contains topics that can be used with messages."""

    MODELS = 0
    IVC = 1
    NLU = 2
    MESSAGES = 3

    def __str__(self) -> Text:
        return self.name.lower()


class Message:
    """Specifies a message that can be sent with websocket service."""

    def __init__(
        self,
        topic: MessageTopic,
        name: Text,
        data: Optional[Dict[Text, Any]] = None,
        recipient: Text = BROADCAST_RECIPIENT_ID,
        scopes: Optional[List[Text]] = None,
    ) -> None:
        """Create an instance of a Message.

        Args:
            topic: Topic of the websocket message. Topics are needed to group
                messages by functionality.
            name: Name of the websocket message.
            data: Any data that will be additionally provided with the message.
                This data should be JSON serializable.
            recipient: Name of the user that needs to receive the message. Note that
                multiple users might use the same username (e.g. `me` in the community
                edition). In this case every user gets the message. Use `BROADCAST_RECIPIENT_ID`
                to send this message to all users.
            scopes: The Rasa X permissions in frontend format (!) which the users have to
                have at least one of to receive the message.
    """
        self.topic = topic
        self.name = name
        self.data = data
        self.recipient = recipient
        self.scopes = scopes

    def as_dict(self) -> Dict[Text, Any]:
        """Returns a JSON-like representation of this Message object.

        Returns:
            Message's attributes."""
        return {
            "topic": str(self.topic),
            "name": self.name,
            "data": self.data or {},
            RECIPIENT_KEY: self.recipient,
            SCOPES_KEY: self.scopes,
        }


# Currently connected WebSockets. This is not synchronized among the Sanic workers.
# As each Sanic worker is a separate process this means that every Sanic worker has a
# different dictionary of connected websockets.
_websockets: Dict[WebSocketCommonProtocol, "ConnectionDetails"] = {}

# After the `websocket` blueprint is created this will contain one `Queue` per Sanic
# worker so we can communicate with each of worker individually.
_queues: List[multiprocessing.Queue] = []


def send_message(message: Message) -> None:
    """Send message to every Sanic worker by putting it in their `Queue`.

    Each Sanic worker will separately check if they have matching WebSocket connections
    and then forward the message to these or skip the message.

    Args:
        message: The message including recipient / scopes which have to be matched.
    """

    for queue in _queues:
        queue.put(message.as_dict())


def loop_for_messages_to_broadcast(queue_index: int, loop: BaseEventLoop) -> NoReturn:
    """Forward messages from the worker `Queue` to the connected WebSockets.

    Args:
        queue_index: The index of the `Queue` which belongs to this Sanic worker.
        loop: The event loop which will send the messages to the user.
    """
    queue_of_current_worker = _queues[queue_index]
    asyncio.set_event_loop(loop)

    try:
        while True:
            message = queue_of_current_worker.get()
            # Use the passed in, existing event loop of the Sanic worker to forward the
            # message instead of having a new event loop for within this thread.
            asyncio.run_coroutine_threadsafe(_forward_message(message), loop)
    except EOFError:
        # Will most likely happen when shutting down Rasa X.
        logger.debug(
            "WebSocket message queue of worker was closed. Stopping to listen for more "
            "messages on this worker."
        )


async def _forward_message(message: Dict[Text, Any]) -> None:
    """Forward a message to matching WebSocket connections.

    Args:
        message: The message.
    """
    recipient_id = message.get(RECIPIENT_KEY)
    message_scope = message.get(SCOPES_KEY)

    if recipient_id == BROADCAST_RECIPIENT_ID:
        return await _forward_message_to_connected_websockets(message)
    if recipient_id and not message_scope:
        return await _forward_message_to(recipient_id, message)
    if message_scope:
        return await _forward_message_to_authorized_users(
            recipient_id, message_scope, message
        )

    logger.warning(
        f"Message '{message}' could not be forwarded as it "
        f"does not contain all required fields."
    )


async def _forward_message_to(recipient_id: Text, message: Dict[Text, Any]) -> None:
    """Forward a message to a single user if they have one or multiple WebSockets
       which are handled by this Sanic worker.

    Args:
        recipient_id: The name of the user.
        message: The message.
    """
    matching_websockets = _get_websockets_of_user(recipient_id)

    if not matching_websockets:
        return

    logger.debug(f"Send notification to recipient '{recipient_id}'.")
    await _send_to_websockets(matching_websockets, message)


def _get_websockets_of_user(recipient_id: Text) -> List[WebSocketCommonProtocol]:
    return [
        websocket
        for websocket, details in _websockets.items()
        if details.username == recipient_id
    ]


async def _forward_message_to_connected_websockets(message: Dict[Text, Any]) -> None:
    """Send a message to all WebSocket connections which are handled by this Sanic
       worker.

    Args:
        message: The message.
    """
    logger.debug(f"Broadcasting message: {message}")
    await _send_to_websockets(_websockets.keys(), message)


async def _forward_message_to_authorized_users(
    recipient_id: Optional[Text], scopes: List[Text], message: Dict[Text, Any]
) -> None:
    """Forward message to WebSockets of users who have at least one matching permission
       scope and which are handled by this Sanic worker.

    Args:
        recipient_id: Optional name of user who should get the message anyhow (
            ignores their scopes).
        scopes: The Rasa X frontend scope which the users must have at least one of.
        message: The message.
    """
    logger.debug(f"Sending message to users with the following scopes: {scopes}")

    selected_websockets = _get_websockets_of_users_with_matching_scopes(scopes)

    if recipient_id:
        selected_websockets += _get_websockets_of_user(recipient_id)

    await _send_to_websockets(selected_websockets, message)


def _get_websockets_of_users_with_matching_scopes(
    scopes: List[Text],
) -> List[WebSocketCommonProtocol]:
    """Find connected WebSockets of users who have at least one of the required scopes.

    Args:
        scopes: Scopes which each user has to match at least one of.

    Returns:
        The websockets of users with matching scopes.
    """
    return [
        websocket
        for websocket, connection in _websockets.items()
        if any(user_scope in scopes for user_scope in connection.user_scopes)
    ]


async def _send_to_websockets(
    selected_websockets: Iterable[WebSocketCommonProtocol], message: Dict[Text, Any]
) -> None:
    """Send the message to each of the selected WebSockets.

    Args:
        selected_websockets: WebSocket connections to send the message to.
        message: The message.
    """
    message_as_text = json.dumps(message)
    send_message_coroutines = [
        _send_to_socket(web_socket, message_as_text)
        for web_socket in selected_websockets
    ]
    await asyncio.gather(*send_message_coroutines)


async def _send_to_socket(websocket: WebSocketCommonProtocol, message: Text) -> None:
    try:
        await websocket.send(message)
    except Exception:
        # Most likely the closed the message.
        logger.debug(
            "Error when sending message to WebSocket. Removing connection "
            "from the authenticated users."
        )
        remove_websocket_connection(websocket)


def add_websocket_connection(
    username: Text, scopes: List[Text], websocket: WebSocketCommonProtocol
) -> None:
    """Save an established WebSocket connection of a user in this Sanic worker.

    Args:
        username: Name of the users.
        scopes: List of frontend permissions the user has.
        websocket: The WebSocket connection.
    """
    logger.debug(f"Authenticated websocket connection with user '{username}'.")
    _websockets[websocket] = ConnectionDetails(username, scopes)


def remove_websocket_connection(websocket: WebSocketCommonProtocol) -> None:
    """Remove a WebSocket connection from the stored WebSocket connection within this
       Sanic worker.

    This is e.g. done when the user closed their connection.

    Args:
        websocket: WebSocket connection which should be removed.
    """
    _websockets.pop(websocket, None)


class ConnectionDetails(NamedTuple):
    """Stores the web socket connection, its user and the scopes with which the user
    authenticated."""

    username: Text
    # These scopes were extracted from the JWT, hence they are in frontend format!
    user_scopes: Optional[List[Text]]


def initialize_websocket_queues(
    number_of_sanic_workers: int, mp_context: BaseContext
) -> None:
    """Initializes process safe `Queue`s for every worker.

    Args:
        number_of_sanic_workers: Number of Sanic workers which each will get assigned to
        one `Queue`.
        mp_context: The current multiprocessing context.
    """
    set_message_queues([mp_context.Queue() for _ in range(number_of_sanic_workers)])


def get_message_queues() -> List[multiprocessing.Queue]:
    """Get the queues which are used send WebSocket messages to each Sanic worker.

    Returns:
        The queues for the WebSocket messaging.
    """
    return _queues


def set_message_queues(queues: List[multiprocessing.Queue]) -> None:
    """Set the queues which are used send WebSocket messages to each Sanic worker.

    Args:
        queues: The queues.
    """
    global _queues
    _queues = queues
