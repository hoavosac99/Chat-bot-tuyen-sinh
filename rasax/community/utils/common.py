import asyncio  # pytype: disable=pyi-error
import datetime
import decimal
import json
import logging
import os
import random
import re
import string
import typing
from contextlib import contextmanager
from hashlib import md5
from http import HTTPStatus
from types import ModuleType
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Set,
    Text,
    Tuple,
    Union,
    Callable,
    NamedTuple,
    Sequence,
    Collection,
    Awaitable,
    Type,
    Iterator,
    Coroutine,
    TYPE_CHECKING,
)

import aiohttp
import dateutil.parser
import isodate
from jsonschema import validate
from jsonschema.exceptions import ValidationError
from packaging import version
from rasax.community import global_state
from sanic import response, Sanic
from sanic.request import Request
from sanic.response import HTTPResponse
from sanic.views import CompositionView
from sqlalchemy.ext.declarative import DeclarativeMeta

import rasax.community
import rasax.community.constants as constants
import rasax.community.utils.cli as cli_utils
from rasax.community.api import json_schema

if TYPE_CHECKING:
    from multiprocessing import Process, BaseContext, Value  # type: ignore
    from multiprocessing.synchronize import Lock  # type: ignore

logger = logging.getLogger(__name__)


# SQL query result containing the result and the count
class QueryResult(NamedTuple):
    result: Union[Dict, List[Dict[Text, Any]]]
    count: int

    def __len__(self) -> int:
        """Return query count.

        Implemented here to override tuple's default __len__ which would return
        the amount of elements in the tuple (which could be misleading).
        """
        return self.count


def get_columns_from_fields(fields: Optional[List[Tuple[Text, bool]]]) -> List[Text]:
    """Get column names from field query which are explicitly included."""
    if fields:
        return [k.rsplit(".", 1)[-1] for k, v in fields if v]
    else:
        return []


def get_query_selectors(
    table: DeclarativeMeta, fields: List[Text]
) -> List[DeclarativeMeta]:
    """Create select statement based on fields list."""
    if fields:
        return [table.__table__.c[f] for f in fields]
    else:
        return [table]


def query_result_to_dict(
    query_result: List[Optional[Text]], fields: List[Tuple[Text, bool]]
) -> Dict[Text, Text]:
    """Convert row to dictionary matching the structure of the field queries.

    Note that for this to work properly, the dictionary keys need to match the column
    names of the database item that is queried.

    A result `["John Doe", 42] and a field query
    `[("username", True), ("user.age", True)]` would be converted to
    `{"username": "John Doe", "user": {"age": 42}}`.

    """
    fields = [k for k, v in fields if v]
    result = {}

    for i, f in enumerate(fields):
        _dot_notation_to_dict(result, f, query_result[i])

    return result


def _dot_notation_to_dict(dictionary: Dict, keys: Text, item: Any) -> None:
    """Creates a dictionary structure matching the given field query."""
    if "." in keys:
        key, rest = keys.split(".", 1)
        if key not in dictionary:
            dictionary[key] = {}
        _dot_notation_to_dict(dictionary[key], rest, item)
    else:
        dictionary[keys] = item


def filter_fields_from_dict(dictionary: Dict, fields: List[Tuple[Text, bool]]):
    """Gets only the specified fields from a dictionary."""

    # Create a dictionary which resembles our desired structure
    selector_dict = query_result_to_dict([None] * len(fields), fields)

    return common_items(dictionary, selector_dict)


def common_items(d1: Dict, d2: Dict):
    """Recursively get common parts of the dictionaries."""

    return {
        k: common_items(d1[k], d2[k]) if isinstance(d1[k], dict) else d1[k]
        for k in d1.keys() & d2.keys()
    }


def float_arg(
    request: Request, key: Text, default: Optional[float] = None
) -> Optional[float]:
    arg = default_arg(request, key, default)

    if arg is default:
        return arg

    try:
        return float(arg)
    except (ValueError, TypeError):
        logger.warning(f"Failed to convert '{arg}' to `float`.")
        return default


def int_arg(
    request: Request, key: Text, default: Optional[int] = None
) -> Optional[int]:
    arg = default_arg(request, key, default)

    if arg is default:
        return arg

    try:
        return int(arg)
    except (ValueError, TypeError):
        logger.warning(f"Failed to convert '{arg}' to `int`.")
        return default


def time_arg(
    request: Request, key: Text, default: Optional[float] = None
) -> Optional[float]:
    """Return the value of a time query parameter.

    Returns `None` if no valid query parameter was found.
    Supports Unix format or ISO 8601."""
    arg = default_arg(request, key, default)

    # Unix format, e.g. 1541189171.389349
    try:
        return float(arg)
    except (ValueError, TypeError):
        pass

    # ISO 8601 format
    try:
        dt = dateutil.parser.parse(arg)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def duration_to_seconds(duration_string: Optional[Text]) -> Optional[float]:
    """Return the value of a duration query parameter.

    Returns `None` if no valid query parameter was found.
    Supports durations in ISO 8601 format"""
    try:
        t_delta = isodate.parse_duration(duration_string)
        if isinstance(t_delta, isodate.Duration):
            t_delta = t_delta.totimedelta(start=datetime.datetime.now())
        return t_delta.total_seconds()
    except (isodate.ISO8601Error, TypeError):
        return None


def _mark_arg_as_accessed(request: Request, name: Text) -> None:
    """Mark a query string argument as accessed for a request.

    Args:
        request: Current HTTP request.
        name: Name of the argument to mark as accessed.
    """
    if request and hasattr(request.ctx, "accessed_args"):
        request.ctx.accessed_args.add(name)


def bool_arg(request: Request, name: Text, default: bool = True) -> bool:
    _mark_arg_as_accessed(request, name)

    d = str(default)
    return request.args.get(name, d).lower() == "true"


def default_arg(
    request: Request, key: Text, default: Optional[Any] = None
) -> Optional[Any]:
    """Return an argument of the request or a default.

    Checks the `name` parameter of the request if it contains a value.
    If not, `default` is returned."""
    _mark_arg_as_accessed(request, key)

    found = request.args.get(key, None)
    if found is not None:
        return found
    else:
        return default


def deployment_environment_from_request(
    request: Request, default: Text = constants.DEFAULT_RASA_ENVIRONMENT
) -> Text:
    """Get deployment environment from environment query parameter."""

    return default_arg(
        request, "environment", default  # pytype: disable=bad-return-type
    )


def extract_numeric_value_from_header(
    request: Request, header: Text, key: Text
) -> Optional[float]:
    """Extract numeric value from request header `header: key=value`."""

    request_header = request.headers.get(header)

    if not request_header:
        return None

    try:
        return float(request_header.split(f"{key}=")[-1])
    except (ValueError, TypeError):
        return None


def enum_arg(
    request: Request,
    key: Text,
    possible_values: Set[Optional[Text]],
    default: Optional[Text],
) -> Optional[Text]:
    """Return the string value of a query parameter, if and only if it is
    contained in a pre-defined set of possible values. If the parameter does
    not have one of the possible values, return a pre-defined default, which
    must be contained in the list of possible values.

    Args:
        request: Received HTTP request.
        key: Name of parameter to read.
        possible_values: Set of values this parameter is allowed to take.
        default: Value to return if parameter does not have a valid value.

    Returns:
        Value read from query parameters.
    """
    _mark_arg_as_accessed(request, key)

    if default not in possible_values:
        raise ValueError(
            f"Default value '{default}' is not contained by possible values set."
        )

    value = request.args.get(key, default)
    if value not in possible_values:
        logger.warning(
            f"Invalid enum value '{value}' for parameter '{key}'. "
            f"Using default instead."
        )
        value = default

    return value


def fields_arg(request: Request, possible_fields: Set[Text]) -> List[Tuple[str, bool]]:
    """Looks for the `fields` parameter in the request, which contains

    the fields for filtering. Returns the set
    of fields that are part of `possible_fields` and are also in the
    query. `possible_fields` is a set of strings, each of the form `a.b.c`.
    Ex: a query of `?fields[a][b][c]=false` yields
    `[('a.b.c', False)]`."""
    for key in request.args.keys():
        if "fields" in key:
            _mark_arg_as_accessed(request, key)

    # create a list of keys, e.g. for `?fields[a][b]=true`
    # we have an element `fields[a][b]`
    keys = [k for k in request.args.keys() if "fields" in k]

    # get the bool values for these fields, store in list of 2-tuples
    # [(key, True/False), (...)]
    data = []
    for k in keys:
        # get the bool value
        b = bool_arg(request, k)

        # translate the key to a tuple representing the nested structure
        # fields[a][b]=true becomes ("a.b", True)
        # get the content between brackets, [<CONTENT>], as a list
        d = re.findall(r"\[(.*?)\]", k)

        if d and d[0]:
            data.append((".".join(d), b))

    # finally, return only those entries in data whose key is in
    # the set of possible keys
    out = []
    for d in data:
        if d[0] in possible_fields:
            out.append(d)
        else:
            logger.warning(
                "Cannot add field {}, as it is not part of `possible_fields` {}".format(
                    d[0], possible_fields
                )
            )
    return out


def list_arg(
    request: Request, key: Text, delimiter: Text = ","
) -> Optional[List[Text]]:
    """Return an argument of the request separated into a list or None."""
    _mark_arg_as_accessed(request, key)

    found = request.args.get(key)
    if found is not None:
        return found.split(delimiter)
    else:
        return None


def handle_deprecated_request_parameters(
    request: Request, old_name: Text, new_name: Text
) -> None:
    """Modify a request to account for a deprecated request parameter following a rename.

    Replace the deprecated parameter in the input request with the corresponding new
    one. Also do this when the parameter is used as fields parameter.

    Args:
        request: The request to fix.
        old_name: The deprecated name of the parameter.
        new_name: The new name of the parameter.
    """
    _mark_arg_as_accessed(request, new_name)

    if request.args.get(old_name):
        cli_utils.raise_warning(
            f"Your request includes the parameter '{old_name}'. This has been "
            f"deprecated and renamed to '{new_name}'. The '{old_name}' parameter will "
            f"no longer work in future versions of Rasa X. Please use '{new_name}' "
            f"instead.",
            FutureWarning,
        )
        request.args[new_name] = request.args.pop(old_name)

    keys = [k for k in request.args.keys() if "fields" in k]
    for k in keys:
        new_k = "fields"

        # get the content between brackets, [<CONTENT>], as a list, e.g. "fields[a][b]"
        # becomes [a, b]
        d = re.findall(r"\[(.*?)\]", k)

        for field in d:
            if field == old_name:
                new_k += f"[{new_name}]"
            else:
                new_k += f"[{field}]"
        request.args[new_k] = request.args.pop(k)


def list_routes(app):
    """List all the routes of a sanic application.

    Mainly used for debugging.
    """

    from urllib.parse import unquote

    output = []
    for endpoint, route in app.router.routes_all.items():
        if endpoint[:-1] in app.router.routes_all and endpoint[-1] == "/":
            continue

        options = {}
        for arg in route.parameters:
            options[arg] = f"[{arg}]"

        methods = ",".join(route.methods)
        if not isinstance(route.handler, CompositionView):
            handlers = [route.name]
        else:
            handlers = {v.__name__ for v in route.handler.handlers.values()}
        name = ", ".join(handlers)
        line = unquote(f"{endpoint:40s} {methods:20s} {name}")
        output.append(line)

    for line in sorted(output):
        print(line)


def check_schema(schema_identifier: Text, data: Dict) -> bool:
    try:
        validate(data, json_schema[schema_identifier])
        return True
    except ValidationError:
        return False


def get_text_hash(text: Union[str, bytes, None]) -> str:
    """Calculate the md5 hash of a string."""
    if text is None:
        text = b""
    elif not isinstance(text, bytes):
        text = text.encode()
    return md5(text).hexdigest()


def secure_filename(filename: str) -> str:
    """Pass it a filename and it will return a secure version of it.

    This filename can then safely be stored on a regular file system
    and passed to :func:`os.path.join`.

    Function is adapted from
    https://github.com/pallets/werkzeug/blob/master/werkzeug/utils.py#L253

    :copyright: (c) 2014 by the Werkzeug Team, see
        https://github.com/pallets/werkzeug/blob/master/AUTHORS
        for more details.
    :license: BSD, see NOTICE for more details.
    """

    _filename_ascii_strip_re = re.compile(r"[^A-Za-z0-9_.-]")
    _windows_device_files = (
        "CON",
        "AUX",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "LPT1",
        "LPT2",
        "LPT3",
        "PRN",
        "NUL",
    )

    if isinstance(filename, str):
        from unicodedata import normalize

        filename = normalize("NFKD", filename).encode("ascii", "ignore")
        filename = filename.decode("ascii")
    for sep in os.path.sep, os.path.altsep:
        if sep:
            filename = filename.replace(sep, " ")
    filename = str(_filename_ascii_strip_re.sub("", "_".join(filename.split()))).strip(
        "._"
    )

    # on nt a couple of special files are present in each folder.  We
    # have to ensure that the target file is not such a filename.  In
    # this case we prepend an underline
    if (
        os.name == "nt"
        and filename
        and filename.split(".")[0].upper() in _windows_device_files
    ):
        filename = "_" + filename

    return filename


def error(
    status: int,
    reason: Text,
    message: Optional[Text] = None,
    details: Any = None,
    help_url: Optional[Text] = None,
) -> HTTPResponse:
    if isinstance(details, Exception):
        details = str(details)
    return response.json(
        {
            "version": rasax.community.__version__,
            "status": "failure",
            "message": message,
            "reason": reason,
            "details": details or {},
            "help": help_url,
            "code": status,
        },
        status=status,
    )


def random_password(password_length=12):
    """Generate a random password of length `password_length`.

    Implementation adapted from
    https://pynative.com/python-generate-random-string/.
    """

    random_source = string.ascii_letters + string.digits
    password = random.choice(string.ascii_lowercase)
    password += random.choice(string.ascii_uppercase)
    password += random.choice(string.digits)

    for _ in range(password_length - 3):
        password += random.choice(random_source)

    password_list = list(password)

    random.SystemRandom().shuffle(password_list)

    return "".join(password_list)


class DecimalEncoder(json.JSONEncoder):
    """Json encoder that properly dumps python decimals as floats."""

    def default(self, o):
        if isinstance(o, decimal.Decimal):
            return float(o)
        return super().default(o)


def is_enterprise_installed() -> bool:
    """Check if Rasa Enterprise is installed."""

    # When running Rasa X/Enterprise using docker-compose for local
    # development, we mount the entire Python codebase (for X and Enterprise)
    # into the Rasa X server container. For this reason, the `import
    # rasax.enterprise` check will always work. To work around this, the
    # docker-compose.dev.yml file specifies to the server what Docker image it
    # has used to run the server.  Using this, we can check if we're using X or
    # Enterprise.
    if "RASA_X_IMAGE" in os.environ:
        image = os.environ["RASA_X_IMAGE"]
        if image == "rasa-x":
            return False
        elif image == "rasa-x-ee":
            return True

        logger.warning(f"Unexpected value for RASA_X_IMAGE: {image}")

    try:
        import rasax.enterprise

        return True
    except ImportError:
        return False


def update_log_level():
    """Set the log level to log level defined in config."""

    from rasax.community.config import log_level

    logging.basicConfig(level=log_level)
    logging.getLogger("rasax").setLevel(log_level)

    packages = ["rasa", "apscheduler"]
    for p in packages:
        # update log level of package
        logging.getLogger(p).setLevel(log_level)
        # set propagate to 'False' so that logging messages are not
        # passed to the handlers of ancestor loggers.
        logging.getLogger(p).propagate = False

    from sanic.log import logger, error_logger, access_logger

    logger.setLevel(log_level)
    error_logger.setLevel(log_level)
    access_logger.setLevel(log_level)

    logger.propagate = False
    error_logger.propagate = False
    access_logger.propagate = False


def _get_newer_stable_version(all_versions, current_version):
    most_recent_version = version.parse(current_version)
    for v in all_versions:
        parsed = version.parse(v)
        if not parsed.is_prerelease and parsed > most_recent_version:
            most_recent_version = parsed
    return str(most_recent_version)


async def check_for_updates(timeout_sec: float = 1) -> Optional[Text]:
    """Check whether there is a newer version of Rasa X.

    Args:
        timeout_sec: max timeout for request in seconds.

    Returns:
        Available update version or `None`.
    """
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                constants.RASA_X_DOCKERHUB_TAGS_URL, timeout=timeout_sec
            ) as resp:
                if resp.status != HTTPStatus.OK:
                    return None
                current_version = rasax.community.__version__
                latest_tagged_version = _get_latest_tagged_version_from_docker_hub(
                    await resp.json()
                )
                if version.parse(latest_tagged_version) > version.parse(
                    current_version
                ):
                    return latest_tagged_version
        except Exception as e:
            logging.debug(f"Failed to check available updates: {e}")

    return None


def _get_latest_tagged_version_from_docker_hub(
    docker_hub_tags: Dict[Text, Any]
) -> Optional[Text]:
    """Parses response from docker hub and extracts latest stable tagged version.

    Args:
        docker_hub_tags: Response from Docker Hub will a list of all tags.
        current_version: Current version of Rasa X.

    Returns:
        Newer Rasa X version or `None`.
    """

    tags = docker_hub_tags.get("results")
    if not tags:
        return None

    latest_version = "0.0.0"

    for tag in tags:
        candidate_version = tag.get("name")
        if not candidate_version or not candidate_version.replace(".", "").isdigit():
            continue
        try:
            if version.parse(candidate_version) > version.parse(latest_version):
                latest_version = candidate_version
        except ValueError:
            # This will skip "latest", "alpha" and other named tags
            continue

    return latest_version


def run_operation_in_single_sanic_worker(
    app: Sanic, f: Union[Callable[[], Union[None, Awaitable]]]
) -> None:
    """Run operation `f` in a single Sanic worker."""

    from multiprocessing.sharedctypes import Value  # noqa: F811
    from ctypes import c_bool

    lock = Value(c_bool, False)

    async def execute():
        if lock.value:
            return

        with lock.get_lock():
            lock.value = True

        if asyncio.iscoroutinefunction(f):
            await f()
        else:
            f()

    app.add_task(execute)


def truncate_float(_float: float, decimal_places: int = 4) -> float:
    """Truncate float to `decimal_places` after the decimal separator."""

    return float(f"%.{decimal_places}f" % _float)


def add_plural_suffix(s: Text, obj: Union[Sequence, Collection]) -> Text:
    """Add plural suffix to replacement field in string `s`.

    The plural suffix is based on `obj` having a length greater than 1.
    """

    is_plural = len(obj) > 1
    return s.format("s" if is_plural else "")


def decode_base64(encoded: Text, encoding: Text = "utf-8") -> Text:
    """Decodes a base64-encoded string."""

    import base64

    return base64.b64decode(encoded).decode(encoding)


def encode_base64(original: Text, encoding: Text = "utf-8") -> Text:
    """Encodes a string to base64."""

    import base64

    return base64.b64encode(original.encode(encoding)).decode(encoding)


def in_continuous_integration() -> bool:
    """Returns `True` if currently running inside a continuous integration context (e.g.
    Travis CI)."""
    return any(env in os.environ for env in ["CI", "TRAVIS", "GITHUB_WORKFLOW"])


def should_dump() -> bool:
    """Whether data should be dumped to disk."""

    import rasax.community.config as rasa_x_config

    return bool(rasa_x_config.PROJECT_DIRECTORY.value)


def is_git_available() -> bool:
    """Checks if `git` is available in the current environment.

    Returns:
        `True` in case `git` is available, otherwise `False`.
    """

    try:
        import git

        return True
    except ImportError as e:
        logger.error(
            f"An error happened when trying to import the Git library. "
            f"Possible reasons are that Git is not installed or the "
            f"`git` executable cannot be found. 'Integrated Version Control' "
            f"won't be available until this is fixed. Details: {str(e)}"
        )
    return False


def mp_context() -> "BaseContext":
    """Get the multiprocessing context for this Rasa X server.

    Returns:
        Multiprocessing context. Use the context to create processes and
        multiprocessing.Values or multiprocessing.Arrays that you need
        to share between processes.
    """
    import multiprocessing  # type: ignore
    import rasax.community.config as rasa_x_config

    return multiprocessing.get_context(rasa_x_config.MP_CONTEXT)  # type: ignore


def _run_in_process_target(
    fn: Callable[..., Any], state: global_state.GlobalState, *args: List[Any]
) -> None:
    """Helper function for `run_in_process`. Runs function `fn` after setting
    up telemetry events queue for the new process.

    Args:
        fn: Function to call in this process.
        state: State which is shared among the processes and which has to be initialized
            to the shared values upon the start of the process.
        args: Arguments for function `fn`.
    """

    # Set a new event loop for this process. Currently, using an event loop
    # from a forked-off process is not supported in Python (see
    # https://bugs.python.org/issue21998). This aspect of asyncio however is
    # not documented. To solve the problem, just create a new event loop for
    # this process. Sanic also creates new event loops for each of its worker
    # processes, so this solution is equal to theirs:
    # https://github.com/huge-success/sanic/blob/master/sanic/server.py#L857
    asyncio.set_event_loop(asyncio.new_event_loop())

    global_state.set_global_state(state)

    # Run the actual function we wanted to run
    fn(*args)


def run_in_process(
    fn: Callable[..., Any], args: Tuple = (), daemon: Optional[bool] = None
) -> "Process":
    """Runs a function in a separate process using multiprocessing.
    To start the new process, the global default multiprocessing context will
    be used by calling `mp_context`.

    This function ensures that some global objects used by Rasa X are copied
    over to the child process, when the multiprocessing context type is 'spawn'
    (when 'fork' is used, this process is automatic, but this function should
    be used anyways to cover both cases). These objects are:

    - Telemetry events queue.
    - Background scheduler jobs queue.
    - Value of config.LOCAL_MODE.
    - Value of config.PROJECT_DIRECTORY.

    Args:
        fn: Starting point for new process.
        args: Arguments to pass to `fn` when creating the new process.
        daemon: Set the new process' damon flag. See documentation of
            `multiprocessing.Process` for more details.

    Returns:
        The created process.
    """
    p = mp_context().Process(
        target=_run_in_process_target,
        args=(fn, global_state.get_global_state()) + args,
        daemon=daemon,
    )
    p.start()

    return p


def run_in_loop(coro: Coroutine[Any, Any, Any]) -> Any:
    """Runs a function in a event loop.

    Args:
        coro: A coroutine to run in a event loop.

    Returns:
        The result of that function.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def get_uptime() -> float:
    """Return the process uptime in seconds.

    Returns:
        Number of seconds elapsed since this process has started. More
        specifically, get the number of seconds elapsed since the `config`
        module was imported for the first time.
    """
    import rasax.community.config as rasa_x_config
    import time

    return time.time() - rasa_x_config.PROCESS_START


def get_orm_classes_in_module(module: ModuleType) -> List[Type]:
    """Return all SQLAlchemy ORM classes in a module.

    Args:
        module: The module which should be searched.

    Returns:
        The classes in the module. Note that this can also include imported classes.
    """
    from rasax.community.database.base import Base

    return [
        c for _, c in get_classes_in_module(module) if issubclass(c, Base) and c != Base
    ]


def get_classes_in_module(module: ModuleType) -> List[Tuple[Text, Type]]:
    """Return all classes in a given module.

    Args:
        module: The module which should be searched.

    Returns:
        The name of the classes and the classes themselves which were found in the
        module. Note that this can also include imported classes.
    """
    import inspect

    return inspect.getmembers(module, inspect.isclass)


def deduplicate_preserving_order(values: List[Any]) -> List[Any]:
    """Deduplicate `values` while keeping the order.

    Args:
        values: A list of values which may contain duplicates.

    Returns:
        The values without duplicates and with preserved order.
    """
    from collections import OrderedDict

    return list(OrderedDict.fromkeys(values))


def coalesce(a: Optional[Any], b: Any) -> Any:
    """Return first input parameter if it's not `None`,
    otherwise return second input parameter.

    Args:
        a: First input parameter.
        b: Second input parameter.

    Returns:
        `a` if it's not None, otherwise `b`.
    """
    return a if a is not None else b


@contextmanager
def acquire_lock_no_block(lock: "Lock") -> Iterator[bool]:
    """Try to acquire a lock without blocking.

    Args:
        lock: The lock object.

    Returns:
        `True` if acquired the lock, `False` otherwise.
    """
    result = lock.acquire(block=False)
    yield result
    if result:
        lock.release()


class RasaLicenseException(Exception):
    """Exception raised when a user attempts to use a Rasa Enterprise feature
    from a Rasa X installation."""
