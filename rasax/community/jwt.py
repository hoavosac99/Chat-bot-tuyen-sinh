from typing import Dict, Union, Text, Optional, Tuple
from pathlib import Path

import jwt
from jwt import InvalidSignatureError

import rasax.community.utils.cli as cli_utils
import rasax.community.utils.common as common_utils
import rasax.community.utils.io as io_utils
from rasax.community import constants, cryptography, config as rasa_x_config

BEARER_TOKEN_PREFIX = "Bearer "


def encode_jwt(payload: Dict, private_key: Union[Text, bytes]) -> Text:
    """Encodes a payload into a signed JWT."""

    return jwt.encode(payload, private_key, algorithm=constants.JWT_METHOD).decode(
        "utf-8"
    )


def bearer_token(payload: Dict, private_key: Union[Text, bytes, None] = None) -> Text:
    """Creates a signed bearer token."""

    private_key = private_key or rasa_x_config.jwt_private_key
    return BEARER_TOKEN_PREFIX + encode_jwt(payload, private_key)


def verify_bearer_token(
    authorization_header_value: Optional[Text],
    public_key: Union[Text, bytes, None] = None,
) -> Dict:
    """Verifies whether a bearer token contains a valid JWT."""

    public_key = public_key or rasa_x_config.jwt_public_key
    if authorization_header_value is None:
        raise TypeError("Authorization header is `None`.")
    elif BEARER_TOKEN_PREFIX not in authorization_header_value:
        raise ValueError(
            f"Authorization header is not prefixed with "
            f"'{BEARER_TOKEN_PREFIX}'. Found header value "
            f"'{authorization_header_value}'."
        )

    authorization_header_value = authorization_header_value.replace(
        BEARER_TOKEN_PREFIX, ""
    )
    try:
        return jwt.decode(
            authorization_header_value, public_key, algorithms=constants.JWT_METHOD
        )
    except Exception:
        raise ValueError("Invalid bearer token.")


def initialise_jwt_keys(
    private_key_path: Optional[Text] = None, public_key_path: Optional[Text] = None
) -> None:
    """Read JWT keys from file and set them. Generate keys if files are not present."""

    if private_key_path is None:
        private_key_path = rasa_x_config.jwt_private_key_path

    if public_key_path is None:
        public_key_path = rasa_x_config.jwt_public_key_path

    if Path(private_key_path).is_file() and Path(public_key_path).is_file():
        common_utils.logger.debug(
            f"Attempting to set JWT keys from files '{rasa_x_config.jwt_private_key}' (private key) "
            f"and '{rasa_x_config.jwt_public_key}' (public key)."
        )
        private_key, public_key = _fetch_and_verify_jwt_keys_from_file(
            private_key_path, public_key_path
        )
    else:
        common_utils.logger.debug("Generating JWT RSA key pair.")
        private_key, public_key = cryptography.generate_rsa_key_pair()
        _ = _save_rsa_private_key_to_temporary_file(private_key)

    _set_keys(private_key, public_key)


def _save_rsa_private_key_to_temporary_file(private_key: bytes) -> Text:
    """Save RSA `private_key` to temporary file and return path."""
    private_key_temp_path = io_utils.create_temporary_file(private_key, mode="w+b")
    common_utils.logger.debug(
        f"Saved RSA private key to temporary file '{private_key_temp_path}'."
    )
    return private_key_temp_path


def _verify_keys(private_key: bytes, public_key: bytes) -> None:
    """Sign message with private key and decode with public key."""

    encoded = jwt.encode({}, private_key, algorithm=constants.JWT_METHOD)
    _ = jwt.decode(encoded, public_key, algorithms=constants.JWT_METHOD)


def _set_keys(private_key: Union[Text, bytes], public_key: Union[Text, bytes]) -> None:
    """Update `private_key` and `public_key` in `rasax.community.config`."""

    rasa_x_config.jwt_private_key = private_key
    rasa_x_config.jwt_public_key = public_key


def _fetch_and_verify_jwt_keys_from_file(
    private_key_path: Text, public_key_path: Text
) -> Tuple[bytes, bytes]:
    """Load the public and private JWT key files and verify them."""

    try:
        private_key = io_utils.read_file_as_bytes(private_key_path)
        public_key = io_utils.read_file_as_bytes(public_key_path)
        _verify_keys(private_key, public_key)
        return private_key, public_key
    except FileNotFoundError as e:
        error_message = f"Could not find key file. Error: '{e}'"
    except ValueError as e:
        error_message = (
            f"Failed to load key data. Make sure the key "
            f"files are enclosed with the "
            f"'-----BEGIN PRIVATE KEY-----' etc. tags. Error: '{e}'"
        )
    except InvalidSignatureError as e:
        error_message = f"Failed to verify key signature. Error: '{e}'"
    except Exception as e:
        error_message = f"Encountered error trying to verify JWT keys: '{e}'"

    cli_utils.print_error_and_exit(error_message)


def add_jwt_key_to_result(results: Dict) -> Dict:
    """Add JWT public key to a dictionary of 'version' results `results`.

    Follows basic jwks format: https://auth0.com/docs/jwks
    """

    results["keys"] = [
        {
            "alg": constants.JWT_METHOD,
            "key": io_utils.convert_bytes_to_string(rasa_x_config.jwt_public_key),
        }
    ]
    return results
