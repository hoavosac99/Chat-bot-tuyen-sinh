import asyncio  # pytype: disable=pyi-error
import json
import os
import time
from json import JSONDecodeError
from multiprocessing.context import BaseContext  # type: ignore
from multiprocessing.managers import ListProxy  # pytype: disable=pyi-error
from threading import Thread
from typing import Tuple, Text, List, Dict, Optional

from sanic import Blueprint, Sanic
import logging

from sanic.request import Request
from websockets import WebSocketCommonProtocol

import rasax.community.constants as constants
import rasax.community.utils.common as common_utils
from rasax.community.services import websocket_service  # pytype: disable=pyi-error
from rasax.community import jwt, config
from rasax.community.constants import USERNAME_KEY

logger = logging.getLogger(__name__)

# The single Sanic workers synchronize their process IDs so that we can assign each
# of them a queue to communicate with.
_worker_ids: Optional[ListProxy] = None

AUTHORIZATION_KEY = "Authorization"


def blueprint() -> Blueprint:
    """Create endpoints for handling WebSocket connections.

    Returns:
        Blueprint which handles WebSocket connections.
    """
    socket_endpoints = Blueprint("sockets")

    @socket_endpoints.listener("after_server_start")
    async def assign_workers_queues(_: Sanic, loop: asyncio.BaseEventLoop) -> None:
        """Assign each Sanic worker a process safe queue.

        We assign each Sanic worker a process safe queue so that we can send messages
        to them which they then can forward to any connected WebSockets.

        Args:
            _: The Sanic app.
            loop: The event loop of the Sanic worker.
        """

        _register_worker_with_process_id()
        number_of_sanic_workers = len(websocket_service.get_message_queues())
        await _loop_until_all_workers_registered(number_of_sanic_workers)
        worker_index = _get_own_worker_index()

        # Loop in a `Thread` to not block `EventLoop`.
        # Thread will be killed on exit due to `daemon=True`.
        message_consumer = Thread(
            target=websocket_service.loop_for_messages_to_broadcast,
            args=(worker_index, loop),
            daemon=True,
        )
        message_consumer.start()

    @socket_endpoints.websocket("/ws")
    async def receive_websocket_message(
        _: Request, ws: WebSocketCommonProtocol
    ) -> None:
        """Handle incoming WebSocket connections.

        Args:
            _: The `Sanic` request object.
            ws: The new WebSocket connection.
        """
        logger.debug("New websocket connected.")
        username = None

        try:
            while True:
                message = await ws.recv()
                parsed = json.loads(message)
                username, scopes = _get_credentials_from_token(parsed)
                websocket_service.add_websocket_connection(username, scopes, ws)
        except (ValueError, KeyError, JSONDecodeError) as e:
            logger.debug(f"Authentication of connected websocket failed. Error: {e}.")
        except asyncio.CancelledError:
            logger.debug(f"WebSocket connection to '{username}' was closed.")
            websocket_service.remove_websocket_connection(ws)
        except Exception as e:
            logger.warning(
                f"There was an error in the WebSocket connection with user "
                f"'{username}'. Closing the connection. {e}"
            )
        finally:
            if not ws.closed:
                await ws.close()

    return socket_endpoints


def _get_credentials_from_token(message: Dict) -> Tuple[Text, List[Text]]:
    """Verify the Bearer token in the message and extract username and the scopes.

    Args:
        message: The message which the user send via WebSocket.

    Returns:
        User name and frontend permissions which are attached to the JWT.

    Raises:
        KeyError: If no Bearer token is included in the message.
        ValueError: If the Bearer token is invalid.
    """
    token = message[AUTHORIZATION_KEY]
    token_payload = jwt.verify_bearer_token(token, config.jwt_public_key)

    return (
        token_payload[constants.USERNAME_KEY],
        token_payload.get(websocket_service.SCOPES_KEY, []),
    )


def initialize_global_sanic_worker_states(
    number_of_sanic_workers: int, mp_context: BaseContext
) -> None:
    """Initializes process safe variables which are shared by the Sanic workers.

    - `Queue`s for every worker which will contain messages which are forwared to
      matching WebSocket connections.
    - A list which is used to coordinate the Sanic workers.

    Args:
        number_of_sanic_workers: Number of Sanic workers which each will get assigned to
        one `Queue`.
        mp_context: The current multiprocessing context.
    """
    websocket_service.initialize_websocket_queues(number_of_sanic_workers, mp_context)

    global _worker_ids
    _worker_ids = common_utils.mp_context().Manager().list()


def _register_worker_with_process_id() -> None:
    """Register worker by adding its process ID to a synchronized list."""
    if _worker_ids is not None:
        _worker_ids.append(os.getpid())
    else:
        logger.error("List of Sanic worker IDs wasn't initialized!")


async def _loop_until_all_workers_registered(
    number_of_sanic_workers: int, timeout_in_seconds: float = 5
) -> None:
    """Wait until all Sanic workers added their process ID to the synchronized list.

    Args:
        number_of_sanic_workers: Number of expected Sanic workers.
        timeout_in_seconds: If not all workers registered within this time, we raise
            an exception.

    Raises:
        RuntimeError: In case not all Sanic workers registered in time.
    """
    start = time.time()
    while (
        not _all_sanic_workers_registered(number_of_sanic_workers)
        and time.time() - start <= timeout_in_seconds
    ):
        await asyncio.sleep(0.1)

    if not _all_sanic_workers_registered(number_of_sanic_workers):
        raise RuntimeError(
            f"Expected that {number_of_sanic_workers} sanic workers register to handle "
            f"WebSocket connections. After {timeout_in_seconds} seconds "
            f"only {len(_worker_ids)} registered."
        )


def _all_sanic_workers_registered(expected_number_of_workers: int) -> bool:
    """Check if all Sanic workers added their process IDs to the synchronized list.

    Args:
        expected_number_of_workers: Expected number of Sanic workers.

    Returns:
        `True` if all workers registered.
    """
    return len(_worker_ids) == expected_number_of_workers


def _get_own_worker_index() -> int:
    """Get index of current worker among the other workers

    Returns:
        Index of worker. `0` is the smallest possible index. `NUMBER_SANIC_WORKERS - 1`
            is the greatest possible index.
    """
    return sorted(_worker_ids).index(os.getpid())
