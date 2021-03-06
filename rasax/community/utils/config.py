import logging
import os
from typing import Any, Dict, Optional, Text

import rasax.community.constants as constants
import rasax.community.utils.io as io_utils
import rasax.community.utils.yaml as yaml_utils

GLOBAL_USER_CONFIG_PATH = os.path.expanduser("~/.config/rasa/global.yml")

logger = logging.getLogger(__name__)


class InvalidConfigError(ValueError):
    """Raised if an invalid configuration is encountered."""

    def __init__(self, message: Text) -> None:
        super().__init__(message)


def _read_global_config() -> Dict[Text, Any]:
    """Read global Rasa configuration."""
    # noinspection PyBroadException
    try:
        content = yaml_utils.read_yaml(io_utils.read_file(GLOBAL_USER_CONFIG_PATH))
        if isinstance(content, dict):
            return content
        else:
            return {}
    except Exception:
        # if things go south we pretend there is no config
        return {}


def write_global_config_value(name: Text, value: Any) -> None:
    """Read global Rasa configuration."""

    try:
        os.makedirs(os.path.dirname(GLOBAL_USER_CONFIG_PATH), exist_ok=True)

        c = _read_global_config()
        c[name] = value
        yaml_utils.write_yaml_file(c, GLOBAL_USER_CONFIG_PATH)
    except Exception as e:
        logger.warning(f"Failed to write global config. Error: {e}. Skipping.")


def read_global_config_value(name: Text, unavailable_ok: bool = True) -> Any:
    """Read a value from the global Rasa configuration."""

    def not_found():
        if unavailable_ok:
            return None
        else:
            raise ValueError(f"Configuration '{name}' key not found.")

    if not os.path.exists(GLOBAL_USER_CONFIG_PATH):
        return not_found()

    c = _read_global_config()

    if name in c:
        return c[name]
    else:
        return not_found()


def are_terms_accepted() -> Optional[bool]:
    """Check whether the user already accepted the term."""

    return read_global_config_value(constants.CONFIG_FILE_TERMS_KEY)
