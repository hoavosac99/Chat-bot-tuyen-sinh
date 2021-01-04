import http
import logging
import re
import warnings
from typing import Dict, Text, Any

from sanic.response import HTTPResponse

from sanic import Blueprint, response
from sanic.request import Request

from rasa.shared.constants import UTTER_PREFIX
from rasa.shared.nlu.constants import RESPONSE_IDENTIFIER_DELIMITER

import rasax.community.constants as constants
import rasax.community.utils.common as common_utils
import rasax.community.utils.cli as cli_utils
import rasax.community.config as rasa_x_config
from rasax.community.api.decorators import (
    rasa_x_scoped,
    inject_rasa_x_user,
    validate_schema,
)
from rasax.community.services import background_dump_service
from rasax.community.services.nlg_service import NlgService
from rasax.community.services.domain_service import DomainService

logger = logging.getLogger(__name__)

_retrieval_response_regex = re.compile(
    "^" + UTTER_PREFIX + "(.*)" + RESPONSE_IDENTIFIER_DELIMITER + ".*$"
)


def _nlg_service(request: Request) -> NlgService:
    return NlgService(request[constants.REQUEST_DB_SESSION_KEY])


def _domain_service(request: Request) -> DomainService:
    return DomainService(request[constants.REQUEST_DB_SESSION_KEY])


def blueprint() -> Blueprint:
    nlg_endpoints = Blueprint("nlg_endpoints")

    @nlg_endpoints.route("/responses", methods=["GET", "HEAD"])
    @rasa_x_scoped("responseTemplates.list", allow_api_token=True)
    async def get_responses(request: Request) -> HTTPResponse:
        text_query = common_utils.default_arg(request, "q", None)
        response_query = common_utils.default_arg(
            request, constants.RESPONSE_NAME_KEY, None
        )
        fields = common_utils.fields_arg(
            request, {"text", constants.RESPONSE_NAME_KEY, "id"}
        )

        limit = common_utils.int_arg(request, "limit")
        offset = common_utils.int_arg(request, "offset", 0)

        responses, total_number = _nlg_service(request).fetch_responses(
            text_query, response_query, fields, limit, offset
        )

        return response.json(responses, headers={"X-Total-Count": total_number})

    @nlg_endpoints.route("/templates", methods=["GET", "HEAD"])
    @rasa_x_scoped("responseTemplates.list", allow_api_token=True)
    async def get_templates(request: Request) -> HTTPResponse:
        cli_utils.raise_warning(
            'The endpoint "/templates" is deprecated, please use "/responses" instead',
            category=FutureWarning,
        )
        common_utils.handle_deprecated_request_parameters(
            request, "template", constants.RESPONSE_NAME_KEY
        )
        return await get_responses(request)

    @nlg_endpoints.route("/responseGroups", methods=["GET", "HEAD"])
    @rasa_x_scoped("responseTemplates.list", allow_api_token=True)
    async def get_grouped_responses(request: Request) -> HTTPResponse:
        common_utils.handle_deprecated_request_parameters(
            request, "template", constants.RESPONSE_NAME_KEY
        )
        text_query = common_utils.default_arg(request, "q", None)
        response_query = common_utils.default_arg(
            request, constants.RESPONSE_NAME_KEY, None
        )

        responses, total_number = _nlg_service(request).get_grouped_responses(
            text_query, response_query
        )

        return response.json(responses, headers={"X-Total-Count": total_number})

    @nlg_endpoints.route("/responseGroups/<response_name>", methods=["PUT", "HEAD"])
    @rasa_x_scoped("responseTemplates.update")
    @inject_rasa_x_user()
    @validate_schema("nlg/response_name")
    async def rename_response(
        request: Request, response_name: Text, user: Dict[Text, Any]
    ) -> HTTPResponse:
        rjs = request.json
        _nlg_service(request).rename_responses(
            response_name, rjs, user[constants.USERNAME_KEY]
        )
        return response.text("", status=http.HTTPStatus.OK)

    @nlg_endpoints.route("/responses", methods=["POST"])
    @rasa_x_scoped("responseTemplates.create", allow_api_token=True)
    @inject_rasa_x_user(allow_api_token=True)
    @validate_schema("nlg/response")
    async def add_response(request: Request, user: Dict[Text, Any]) -> HTTPResponse:
        rjs = request.json
        domain_service = _domain_service(request)
        domain_id = domain_service.get_domain_id(rasa_x_config.project_name)

        try:
            saved_response = _nlg_service(request).save_response(
                rjs, user[constants.USERNAME_KEY], domain_id=domain_id
            )

            # If the response corresponds to a retrieval intent,
            # add that intent to the domain if it doesn't already exist

            response_name = saved_response[constants.RESPONSE_NAME_KEY]
            retrieval_response = _retrieval_response_regex.match(response_name)

            if retrieval_response:
                intent_name = retrieval_response.group(1)
                project_id = rasa_x_config.project_name
                if not domain_service.intent_exists(project_id, intent_name):
                    domain_service.add_new_intent(project_id, intent_name)

            background_dump_service.add_domain_change()

            return response.json(saved_response, http.HTTPStatus.CREATED)

        except (AttributeError, ValueError) as e:
            status_code = (
                http.HTTPStatus.UNPROCESSABLE_ENTITY
                if isinstance(e, AttributeError)
                else http.HTTPStatus.BAD_REQUEST
            )

            return common_utils.error(
                status_code,
                "WrongResponse",
                "Could not add the specified response.",
                str(e),
            )

    @nlg_endpoints.route("/templates", methods=["POST"])
    @rasa_x_scoped("responseTemplates.create", allow_api_token=True)
    @validate_schema("nlg/response")
    async def add_template(request: Request) -> HTTPResponse:
        cli_utils.raise_warning(
            'The endpoint "/templates" is deprecated, please use "/responses" instead.',
            category=FutureWarning,
        )
        return await add_response(request)

    @nlg_endpoints.route("/responses", methods=["PUT"])
    @rasa_x_scoped("bulkResponseTemplates.update", allow_api_token=True)
    @inject_rasa_x_user(allow_api_token=True)
    @validate_schema("nlg/response_bulk")
    async def update_responses(request: Request, user: Dict[Text, Any]) -> HTTPResponse:
        """Delete old bot responses and replace them with the responses in the
        payload."""

        rjs = request.json
        domain_id = _domain_service(request).get_domain_id(rasa_x_config.project_name)
        try:
            inserted_count = _nlg_service(request).replace_responses(
                rjs, user[constants.USERNAME_KEY], domain_id=domain_id
            )
            background_dump_service.add_domain_change()

            return response.text(f"Successfully uploaded {inserted_count} responses.")

        except AttributeError as e:
            status_code = (
                http.HTTPStatus.UNPROCESSABLE_ENTITY
                if isinstance(e, AttributeError)
                else http.HTTPStatus.BAD_REQUEST
            )
            return common_utils.error(
                status_code,
                "WrongResponse",
                "Could not update the specified response.",
                str(e),
            )

    @nlg_endpoints.route("/templates", methods=["PUT"])
    @rasa_x_scoped("bulkResponseTemplates.update", allow_api_token=True)
    @validate_schema("nlg/response_bulk")
    async def update_templates(request: Request) -> HTTPResponse:
        cli_utils.raise_warning(
            'The endpoint "/templates" is deprecated, please use "/responses" instead.',
            category=FutureWarning,
        )
        return await update_responses(request)

    @nlg_endpoints.route("/responses/<response_id:int>", methods=["PUT"])
    @rasa_x_scoped("responseTemplates.update", allow_api_token=True)
    @inject_rasa_x_user(allow_api_token=True)
    @validate_schema("nlg/response")
    async def modify_response(
        request: Request, response_id: int, user: Dict[Text, Any]
    ) -> HTTPResponse:
        rjs = request.json
        try:
            updated_response = _nlg_service(request).update_response(
                response_id, rjs, user[constants.USERNAME_KEY]
            )
            background_dump_service.add_domain_change()
            return response.json(updated_response.as_dict())

        except (KeyError, AttributeError, ValueError) as e:
            if isinstance(e, KeyError):
                status_code = http.HTTPStatus.NOT_FOUND
            elif isinstance(e, AttributeError):
                status_code = http.HTTPStatus.UNPROCESSABLE_ENTITY
            else:
                status_code = http.HTTPStatus.BAD_REQUEST

            return common_utils.error(
                status_code,
                "WrongResponse",
                "Could not modify the specified response.",
                str(e),
            )

    @nlg_endpoints.route("/templates/<response_id:int>", methods=["PUT"])
    @rasa_x_scoped("responseTemplates.update", allow_api_token=True)
    @validate_schema("nlg/response")
    async def modify_template(request: Request, response_id: int) -> HTTPResponse:
        cli_utils.raise_warning(
            'The endpoint "/templates/<responses_id>" is deprecated, please use '
            '"/responses/<responses_id>" instead.',
            category=FutureWarning,
        )
        return await modify_response(request, response_id)

    @nlg_endpoints.route("/responses/<response_id:int>", methods=["DELETE"])
    @rasa_x_scoped("responseTemplates.delete", allow_api_token=True)
    async def delete_response(request: Request, response_id: int) -> HTTPResponse:
        deleted = _nlg_service(request).delete_response(response_id)
        if deleted:
            background_dump_service.add_domain_change()
            return response.text("", http.HTTPStatus.NO_CONTENT)
        return common_utils.error(
            http.HTTPStatus.NOT_FOUND,
            "ResponseNotFound",
            "Response could not be found.",
        )

    @nlg_endpoints.route("/templates/<response_id:int>", methods=["DELETE"])
    @rasa_x_scoped("responseTemplates.delete", allow_api_token=True)
    async def delete_template(request: Request, response_id: int) -> HTTPResponse:
        cli_utils.raise_warning(
            'The endpoint "/templates/<responses_id>" is deprecated, please use '
            '"/responses/<responses_id>" instead.',
            category=FutureWarning,
        )
        return await delete_response(request, response_id)

    return nlg_endpoints
