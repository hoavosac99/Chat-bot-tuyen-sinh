import uuid
import json
import os
import logging
from typing import Text, Tuple, Dict, List, Any, Optional, Type

from rasax.community.database.service import DbService
from rasax.community.database.admin import ConfigValue

logger = logging.getLogger(__name__)


class ConfigKey:
    TELEMETRY_ENABLED = "METRICS_CONSENT"
    TELEMETRY_UUID = "UUID"


# A dictionary of all configuration options for `ConfigService`.
# Do not read os.environ from here! `ConfigService` does that already.
# Default values must not depend on the execution environment.
_CONFIG_SERVICE_DEFAULTS = {ConfigKey.TELEMETRY_UUID: uuid.uuid4().hex}


def get_runtime_config_and_errors(
    credentials_path: Text, endpoints_path: Text
) -> Tuple[Dict[Text, Text], List]:
    """Returns dictionary of runtime configs and possible errors.

    Runtime configs are read from `credentials_path` and `endpoints_path` (by
    default these are `/app/credentials.yml` and `/app/endpoints.yml`).
    Returns a dictionary with keys `credentials` and `endpoints`, containing the
    respective configs as yaml strings.
    """

    runtime_config = {}
    errors = []

    for key, filename in [
        ("credentials", credentials_path),
        ("endpoints", endpoints_path),
    ]:
        try:
            with open(filename) as f:
                runtime_config[key] = f.read()
        except OSError as e:
            errors.append(e)

    return runtime_config, errors


class InvalidConfigValue(Exception):
    """Exception raised when an error occurs when trying to serialize or
    deserialize a configuration value as JSON, or as a specific type the user
    requested (e.g. `bool`).

    """


class MissingConfigValue(Exception):
    """Exception raised when a configuration value is not present in the
    database, or in the environment variables.

    """

    def __init__(self, key: Text) -> None:
        super().__init__(
            f"The configuration value '{key}' is not present in the database or in the "
            f"environment variables."
        )


class ConfigService(DbService):
    """Service for reading and writing configuration values for the Rasa X server.

    Use this service for storing configuration values that can change during
    the execution of the Rasa X server, and that therefore should be persisted
    to the SQL database across server restarts.

    Any configuration value may be overidden by setting an environment variable
    with the same name.

    """

    def _get_value_from_env(self, key: Text) -> Any:
        """Read a configuration value from the environment.

        Args:
            key: Name of the configuration value.

        Returns:
            Configuration value.
        """

        try:
            return json.loads(os.environ[key])
        except json.decoder.JSONDecodeError:
            # If value is not JSON, simply return the string value
            return os.environ[key]

    def _get_value_from_database(self, key: Text) -> Any:
        """Read a configuration value from the database.

        Args:
            key: Name of the configuration value.

        Raises:
            InvalidConfigValue: if the configuration value could not be
                deserialized as JSON.
            MissingConfigValue: if the configuration value is not present.

        Returns:
            Configuration value.
        """

        config_value_json = self.query(ConfigValue).get(key)
        if config_value_json is None:
            raise MissingConfigValue(key)

        try:
            return json.loads(config_value_json.value)
        except json.decoder.JSONDecodeError:
            raise InvalidConfigValue(
                f"Could not JSON deserialize configuration value '{key}'."
            )

    def get_value(
        self, key: Text, expected_type: Optional[Type] = None, read_env: bool = True
    ) -> Any:
        """Fetch a configuration value stored in the database. If an
        environment variable has been set with the same name, return its value
        instead (only when `read_env` is `True`).

        Args:
            key: Name of the configuration value.
            expected_type: Expected type of the configuration value. If unset, do not
                check the retrieved value's type. This parameter is most useful for
                configuration options that we expect users to override via
                environment variables.
            read_env: If `True`, allow overriding configuration variables using
                environment variables.

        Raises:
            InvalidConfigValue: if the configuration value could not be
                deserialized as JSON.
            MissingConfigValue: if the configuration value is not present.

        Returns:
            Configuration value (`None` is a valid value).
        """

        if read_env and key in os.environ:
            logger.debug(f"Reading config key '{key}' from environment.")
            read_value = self._get_value_from_env(key)
        else:
            read_value = self._get_value_from_database(key)

        if expected_type and not isinstance(read_value, expected_type):
            raise InvalidConfigValue(
                f"Configuration value '{read_value}' (key '{key}') is not type "
                f"'{expected_type}'."
            )

        return read_value

    def set_value(self, key: Text, value: Optional[Any]) -> None:
        """Set a configuration value in the database. Pre-existing values will
        be overwritten.

        Args:
            key: Name of the configuration value.
            value: Value to stored (must be JSON serializable).

        Raises:
            InvalidConfigValue: if the value is not JSON serializable.
        """

        try:
            value_json = json.dumps(value)
        except TypeError as e:
            raise InvalidConfigValue(f"Could not JSON serialize value '{value}': {e}.")

        config_value_json = self.query(ConfigValue).get(key)

        if config_value_json is None:
            self.add(ConfigValue(key=key, value=value_json))
        else:
            config_value_json.value = value_json

    def initialize_configuration(self) -> None:
        """Ensure the database has values for all possible configuration
        options. Configuration options that are not present are set to their
        default values.

        This method does not perform any actions if the configuration values
        are already present in the database.
        """

        for key, default_value in _CONFIG_SERVICE_DEFAULTS.items():
            try:
                self.get_value(key, read_env=False)
                # Value is present, don't overwrite it
            except MissingConfigValue:
                # Value is not present, insert the default
                self.set_value(key, default_value)
