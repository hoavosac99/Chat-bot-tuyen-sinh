import logging
from http import HTTPStatus
from typing import Text, Dict, Any, Optional

import rasax.community.constants as constants
import rasax.community.utils.cli as cli_utils
import rasax.community.utils.common as common_utils
from aiohttp import ClientError
from rasax.community.api.decorators import (
    rasa_x_scoped,
    inject_rasa_x_user,
)
from rasax.community.services.logs_service import LogsService
from rasax.community.services.settings_service import SettingsService
from rasax.community.services.stack_service import StackService
from sanic import Blueprint, response
from sanic.request import Request
from sanic.response import HTTPResponse

logger = logging.getLogger(__name__)


def _stack_service(
    request: Request, project_id: Text, default_environment: Text
) -> StackService:
    settings_service = SettingsService(request[constants.REQUEST_DB_SESSION_KEY])

    environment = common_utils.deployment_environment_from_request(
        request, default_environment
    )
    service = settings_service.get_stack_service(environment, project_id)
    if not service:
        common_utils.error(
            HTTPStatus.NOT_FOUND,
            "ServiceNotFound",
            f"Service for requested environment '{environment}' not found.",
        )

    return service


async def _create_message_log_from_query(
    request: Request, project_id: Text, query: Text
) -> Dict[Text, Any]:
    stack_service = _stack_service(
        request, project_id, constants.RASA_WORKER_ENVIRONMENT
    )
    stack_service_has_model = await stack_service.has_active_model()

    if stack_service_has_model:
        parse_data = await stack_service.parse(query)
    else:
        # We don't have an active model to create an initial guess of the intent and
        # entities. We still want to be able to create a log, and will use parse data
        # that only contains the query under the `text` key.
        parse_data = {"text": query}

    logs_service = LogsService.from_request(request)
    return logs_service.create_log_from_parse_data(
        parse_data, created_from_model=stack_service_has_model
    )


def blueprint():
    logs_endpoints = Blueprint("logs_endpoints")

    @logs_endpoints.route("/projects/<project_id>/logs", methods=["GET", "HEAD"])
    @rasa_x_scoped("logs.list")
    async def suggestions(request: Request, project_id: Text) -> HTTPResponse:
        limit = common_utils.int_arg(request, "limit")
        offset = common_utils.int_arg(request, "offset", 0)
        text_query = common_utils.default_arg(request, "q", None)
        intent_query = common_utils.default_arg(request, "intent", None)
        exclude_training_data = common_utils.bool_arg(
            request, "exclude_training_data", True
        )
        sort_by = common_utils.enum_arg(
            request, "sort_by", {"id", "time", "confidence"}, "id"
        )
        sort_order = common_utils.enum_arg(
            request, "sort_order", {"asc", "desc"}, "desc"
        )
        fields = common_utils.fields_arg(
            request,
            {
                "user_input.intent.name",
                "user_input.text",
                "id",
                "time",
                "user_input.intent.confidence",
            },
        )
        if "distinct" in request.args:
            cli_utils.raise_warning(
                "The `distinct` query parameter is deprecated for this "
                "endpoint. Rasa X already de-duplicates the conversation logs "
                "which has the same effect as the `distinct` parameter."
            )

        suggested, total_suggestions = LogsService.from_request(request).fetch_logs(
            text_query,
            intent_query,
            fields,
            limit,
            offset,
            exclude_training_data,
            sort_by,
            sort_order,
        )

        return response.json(suggested, headers={"X-Total-Count": total_suggestions})

    # noinspection PyUnusedLocal
    @logs_endpoints.route("/projects/<project_id>/logs/<log_id>", methods=["DELETE"])
    @rasa_x_scoped("logs.delete")
    @inject_rasa_x_user()
    async def archive_one(
        request: Request, project_id: Text, log_id, user: Optional[Dict] = None
    ) -> HTTPResponse:
        success = LogsService.from_request(request).archive(log_id)
        if not success:
            return common_utils.error(
                HTTPStatus.BAD_REQUEST,
                "ArchiveLogFailed",
                f"Failed to archive log with log_id {log_id}",
            )
        return response.text("", HTTPStatus.NO_CONTENT)

    @logs_endpoints.route(
        "/projects/<project_id>/logs/<_hash>", methods=["GET", "HEAD"]
    )
    @rasa_x_scoped("logs.get")
    async def get_log_by_hash(
        request: Request, project_id: Text, _hash: Text
    ) -> HTTPResponse:
        log = LogsService.from_request(request).get_log_by_hash(_hash)
        if not log:
            return common_utils.error(
                HTTPStatus.NOT_FOUND,
                "NluLogError",
                f"Log with hash {_hash} could not be found",
            )

        return response.json(log.as_dict())

    @logs_endpoints.route("/projects/<project_id>/logs", methods=["POST"])
    @rasa_x_scoped("logs.create")
    async def add_log(request: Request, project_id: Text) -> HTTPResponse:
        query = common_utils.default_arg(request, "q", "")
        # if no query text found, check for json content
        if not query:
            query = request.json
            if not common_utils.check_schema("log", query):
                return common_utils.error(
                    HTTPStatus.BAD_REQUEST,
                    "WrongSchema",
                    "Please check the schema of your NLU query.",
                )

        try:
            created_log = await _create_message_log_from_query(
                request, project_id, query
            )
            return response.json(created_log)
        except ClientError as e:
            logger.warning(f"Parsing the message '{query}' failed. Error: {e}")
            return common_utils.error(
                HTTPStatus.NOT_FOUND,
                "NluParseFailed",
                f"Failed to parse NLU query ('{query}').",
                details=e,
            )

    return logs_endpoints
