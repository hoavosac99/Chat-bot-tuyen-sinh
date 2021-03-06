import logging
from typing import Text, Dict, Any

from http import HTTPStatus
from sanic import Blueprint, response
from sanic.request import Request
from sanic.response import HTTPResponse

from rasa.shared.core.domain import Domain
from rasa.shared.core.training_data.story_reader.story_reader import StoryParseError

from rasax.community.services.story_service import StoryService
from rasax.community.services.domain_service import DomainService
from rasax.community.api.decorators import rasa_x_scoped, inject_rasa_x_user
import rasax.community.data as data
import rasax.community.utils.io as io_utils
import rasax.community.constants as constants
import rasax.community.utils.common as common_utils
import rasax.community.config as rasa_x_config

logger = logging.getLogger(__name__)


def _domain_service(request: Request) -> DomainService:
    """Returns an instance of `DomainService` with the current request session.

    Args:
        request: Current HTTP request.

    Returns:
        `DomainService` instance.
    """
    return DomainService(request[constants.REQUEST_DB_SESSION_KEY])


def _check_content_type(request: Request) -> None:
    """Ensures the Content-Type of a request indicates YAML file format.

    Args:
        request: Current HTTP request.

    Raises:
        ValueError: If the specified content type is not YAML.
    """
    content_type = request.headers.get("Content-Type")
    if not content_type:
        raise ValueError("No value provided for Content-Type.")

    file_format = data.format_from_mime_type(content_type)

    if file_format != data.FileFormat.YAML:
        raise ValueError("Content-Type header must indicate 'application/x-yaml'.")


def blueprint() -> Blueprint:
    rules_endpoints = Blueprint("rules_endpoints")

    @rules_endpoints.route("/rules", methods=["GET", "HEAD"])
    @rasa_x_scoped("rules.list", allow_api_token=True)
    async def get_rules(request: Request) -> HTTPResponse:
        query = common_utils.default_arg(request, "q")

        limit = common_utils.int_arg(request, "limit")
        offset = common_utils.int_arg(request, "offset", 0)
        id_query = common_utils.list_arg(request, "id")

        project_id = common_utils.default_arg(
            request, "project_id", rasa_x_config.project_name
        )

        output_format = data.format_from_mime_type(
            request.headers.get("Accept", "application/json"),
            default=data.FileFormat.JSON,
        )

        rules = StoryService.from_request(request).fetch_stories(
            text_query=query,
            fetch_rules=True,
            limit=limit,
            offset=offset,
            id_query=id_query,
        )

        if output_format == data.FileFormat.GRAPHVIZ:
            domain_dict = _domain_service(request).get_or_create_domain(
                project_id, rasa_x_config.default_username
            )
            domain = Domain.from_dict(domain_dict)

            visualization = await StoryService.from_request(request).visualize_stories(
                rules, domain
            )

            if visualization:
                return response.text(visualization)

            return common_utils.error(
                HTTPStatus.NOT_ACCEPTABLE,
                "VisualizationNotAvailable",
                "Cannot produce a visualization for the requested rules",
            )
        elif output_format == data.FileFormat.YAML:
            yaml_content = StoryService.from_request(request).get_stories_as_string(
                project_id, rules, data.FileFormat.YAML
            )

            return response.text(
                yaml_content,
                content_type="application/x-yaml",
                headers={"Content-Disposition": "attachment;filename=rules.yml"},
            )
        else:
            return response.json(rules, headers={"X-Total-Count": len(rules)})

    @rules_endpoints.route("/rules", methods=["POST"])
    @rasa_x_scoped("rules.create", allow_api_token=True)
    @inject_rasa_x_user(allow_api_token=True)
    async def create_rule(request: Request, user: Dict[Text, Any]) -> HTTPResponse:
        rule_string = io_utils.convert_bytes_to_string(request.body)

        try:
            _check_content_type(request)
        except ValueError as e:
            logger.error(e)
            return common_utils.error(
                HTTPStatus.BAD_REQUEST, "InvalidContentTypeError", str(e)
            )

        try:
            saved_stories = await StoryService.from_request(request).save_stories(
                rule_string,
                user["team"],
                rasa_x_config.project_name,
                user[constants.USERNAME_KEY],
                file_format=data.FileFormat.YAML,
            )

            return response.json(
                saved_stories, headers={"X-Total-Count": len(saved_stories)}
            )
        except StoryParseError as e:
            logger.error(e.message)
            return common_utils.error(
                HTTPStatus.BAD_REQUEST,
                "StoryParseError",
                "Failed to parse rules.",
                details=e.message,
            )

    @rules_endpoints.route("/rules", methods=["PUT"])
    @rasa_x_scoped("bulkRules.update", allow_api_token=True)
    @inject_rasa_x_user(allow_api_token=True)
    async def update_rules(request: Request, user: Dict[Text, Any]) -> HTTPResponse:
        rule_string = io_utils.convert_bytes_to_string(request.body)

        try:
            _check_content_type(request)
        except ValueError as e:
            logger.error(e)
            return common_utils.error(
                HTTPStatus.BAD_REQUEST, "InvalidContentTypeError", str(e)
            )

        try:
            saved_rules = await StoryService.from_request(request).replace_stories(
                story_string=rule_string,
                team=user["team"],
                project_id=rasa_x_config.project_name,
                username=user[constants.USERNAME_KEY],
                file_format=data.FileFormat.YAML,
                replace_rules=True,
            )

            return response.json(
                saved_rules, headers={"X-Total-Count": len(saved_rules)}
            )
        except StoryParseError as e:
            logger.error(e.message)
            return common_utils.error(
                HTTPStatus.BAD_REQUEST,
                "StoryParseError",
                "Failed to parse rules.",
                details=e.message,
            )

    @rules_endpoints.route("/rules/<rule_id>", methods=["PUT"])
    @rasa_x_scoped("rules.update", allow_api_token=True)
    @inject_rasa_x_user(allow_api_token=True)
    async def update_rule(
        request: Request, rule_id: Text, user: Dict[Text, Any]
    ) -> HTTPResponse:
        rule_string = io_utils.convert_bytes_to_string(request.body)

        try:
            _check_content_type(request)
        except ValueError as e:
            logger.error(e)
            return common_utils.error(
                HTTPStatus.BAD_REQUEST, "InvalidContentTypeError", str(e)
            )

        try:
            updated_rule = await StoryService.from_request(request).update_story(
                story_id=rule_id,
                story_string=rule_string,
                project_id=rasa_x_config.project_name,
                user=user,
                file_format=data.FileFormat.YAML,
                update_rule=True,
            )

            if not updated_rule:
                return response.text(
                    "No rule found with provided ID.", HTTPStatus.NOT_FOUND
                )

            return response.json(updated_rule)
        except ValueError as e:
            logger.error(e)
            return common_utils.error(
                HTTPStatus.BAD_REQUEST, "RuleContentsError", details=str(e),
            )
        except StoryParseError as e:
            logger.error(e.message)
            return common_utils.error(
                HTTPStatus.BAD_REQUEST,
                "StoryParseError",
                "Failed to modify rule.",
                details=e.message,
            )

    @rules_endpoints.route("/rules/<rule_id>", methods=["DELETE"])
    @rasa_x_scoped("rules.delete", allow_api_token=True)
    async def delete_rule(request: Request, rule_id: Text) -> HTTPResponse:
        deleted = StoryService.from_request(request).delete_story(
            rule_id, delete_rule=True
        )
        if deleted:
            return response.text("", HTTPStatus.NO_CONTENT)

        return common_utils.error(
            HTTPStatus.NOT_FOUND,
            "RuleNotFound",
            f"Failed to delete rule with story with id '{rule_id}'.",
        )

    return rules_endpoints
