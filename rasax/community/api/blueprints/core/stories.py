import logging
from typing import Text, Dict, Any

from http import HTTPStatus
from sanic import Blueprint, response
from sanic.request import Request
from sanic.response import HTTPResponse

from rasa.shared.core.training_data.story_reader.story_reader import StoryParseError
from rasax.community.services.story_service import StoryService
from rasax.community.services.domain_service import DomainService
import rasax.community.utils.common as common_utils
import rasax.community.utils.io as io_utils
import rasax.community.utils.cli as cli_utils
import rasax.community.constants as constants
import rasax.community.telemetry as telemetry
import rasax.community.data as data
import rasax.community.config as rasa_x_config
from rasax.community.api.decorators import rasa_x_scoped, inject_rasa_x_user

logger = logging.getLogger(__name__)


def _domain_service(request: Request) -> DomainService:
    """Returns an instance of `DomainService` with the current request session.

    Args:
        request: Current HTTP request.

    Returns:
        `DomainService` instance.
    """
    return DomainService(request[constants.REQUEST_DB_SESSION_KEY])


def _story_file_format(request: Request) -> data.FileFormat:
    """Returns a valid Rasa Core story format, from the Content-Type header of
    an HTTP request.

    Args:
        request: Current HTTP request.

    Raises:
        ValueError: If the specified content type is invalid or empty.

    Returns:
        File format, either Markdown or YAML.
    """
    content_type = request.headers.get("Content-Type")
    if not content_type:
        raise ValueError("No value provided for Content-Type.")

    file_format = data.format_from_mime_type(content_type)

    if file_format not in [data.FileFormat.MARKDOWN, data.FileFormat.YAML]:
        raise ValueError("Story content type must be Markdown or YAML.")

    return file_format


def blueprint() -> Blueprint:
    stories_endpoints = Blueprint("stories_endpoints")

    @stories_endpoints.route("/stories", methods=["GET", "HEAD"])
    @rasa_x_scoped("stories.list", allow_api_token=True)
    async def get_stories(request):
        from rasa.shared.core.domain import Domain

        text_query = common_utils.default_arg(request, "q", None)
        fields = common_utils.fields_arg(request, {"name", "annotation.user", "id"})
        id_query = common_utils.list_arg(request, "id")

        distinct = common_utils.bool_arg(request, "distinct", True)
        stories = StoryService.from_request(request).fetch_stories(
            text_query, fields, id_query=id_query, distinct=distinct, fetch_rules=False
        )
        project_id = common_utils.default_arg(
            request, "project_id", rasa_x_config.project_name
        )

        output_format = data.format_from_mime_type(
            request.headers.get("Accept", "application/json"),
            default=data.FileFormat.JSON,
        )

        if output_format == data.FileFormat.GRAPHVIZ:
            domain_dict = _domain_service(request).get_or_create_domain(
                project_id, rasa_x_config.default_username
            )
            domain = Domain.from_dict(domain_dict)

            visualization = await StoryService.from_request(request).visualize_stories(
                stories, domain
            )

            if visualization:
                return response.text(visualization)
            else:
                return common_utils.error(
                    HTTPStatus.NOT_ACCEPTABLE,
                    "VisualizationNotAvailable",
                    "Cannot produce a visualization for the requested stories",
                )
        elif output_format == data.FileFormat.MARKDOWN:
            markdown = StoryService.from_request(request).get_stories_as_string(
                project_id, stories, data.FileFormat.MARKDOWN
            )

            return response.text(
                markdown,
                content_type="text/markdown",
                headers={
                    "Content-Disposition": "attachment;filename=stories.md",
                    "X-Total-Count": len(stories),
                },
            )
        elif output_format == data.FileFormat.YAML:
            yaml_content = StoryService.from_request(request).get_stories_as_string(
                project_id, stories, data.FileFormat.YAML
            )

            return response.text(
                yaml_content,
                content_type="text/yaml",
                headers={
                    "Content-Disposition": "attachment;filename=stories.yml",
                    "X-Total-Count": len(stories),
                },
            )
        else:
            return response.json(stories, headers={"X-Total-Count": len(stories)})

    @stories_endpoints.route("/stories/<story_id>", methods=["GET", "HEAD"])
    @rasa_x_scoped("stories.get", allow_api_token=True)
    async def get_story(request, story_id):
        from rasa.shared.core.domain import Domain

        story = StoryService.from_request(request).fetch_story(
            story_id, fetch_rule=False
        )
        project_id = common_utils.default_arg(
            request, "project_id", rasa_x_config.project_name
        )

        if not story:
            return common_utils.error(
                HTTPStatus.NOT_FOUND,
                "StoryNotFound",
                f"Story for id {story_id} could not be found",
            )

        output_format = data.format_from_mime_type(
            request.headers.get("Accept", "application/json"),
            default=data.FileFormat.JSON,
        )

        if output_format == data.FileFormat.GRAPHVIZ:
            domain_dict = _domain_service(request).get_or_create_domain(
                project_id, rasa_x_config.default_username
            )
            domain = Domain.from_dict(domain_dict)

            visualization = await StoryService.from_request(request).visualize_stories(
                [story], domain
            )

            if visualization:
                return response.text(visualization)

            return common_utils.error(
                HTTPStatus.NOT_ACCEPTABLE,
                "VisualizationNotAvailable",
                "Cannot produce a visualization for the requested story",
            )
        elif output_format == data.FileFormat.JSON:
            return response.json(story)

        return common_utils.error(
            HTTPStatus.BAD_REQUEST,
            "InvalidAcceptError",
            'Only Graphviz or JSON may be specified via the "Accept" header.',
        )

    @stories_endpoints.route("/stories", methods=["POST"])
    @rasa_x_scoped("stories.create")
    @inject_rasa_x_user(allow_api_token=True)
    async def add_stories(request: Request, user: Dict[Text, Any]) -> HTTPResponse:
        story_string = io_utils.convert_bytes_to_string(request.body)
        try:
            file_format = _story_file_format(request)
        except ValueError as e:
            logger.error(e)
            return common_utils.error(
                HTTPStatus.BAD_REQUEST, "InvalidContentTypeError", str(e)
            )

        try:
            saved_stories = await StoryService.from_request(request).save_stories(
                story_string,
                user["team"],
                rasa_x_config.project_name,
                user[constants.USERNAME_KEY],
                file_format=file_format,
            )

            telemetry.track_story_created(request.headers.get("Referer"))
            return response.json(
                saved_stories, headers={"X-Total-Count": len(saved_stories)}
            )
        except StoryParseError as e:
            logger.error(e.message)
            return common_utils.error(
                HTTPStatus.BAD_REQUEST,
                "StoryParseError",
                "Failed to parse story.",
                details=e.message,
            )

    @stories_endpoints.route("/stories", methods=["PUT"])
    @rasa_x_scoped("bulkStories.update")
    @inject_rasa_x_user()
    async def add_bulk_stories(request: Request, user: Dict[Text, Any]) -> HTTPResponse:
        story_string = io_utils.convert_bytes_to_string(request.body)
        try:
            file_format = _story_file_format(request)
        except ValueError as e:
            logger.error(e)
            return common_utils.error(
                HTTPStatus.BAD_REQUEST, "InvalidContentTypeError", str(e)
            )

        try:
            saved_stories = await StoryService.from_request(request).replace_stories(
                story_string,
                user["team"],
                rasa_x_config.project_name,
                user[constants.USERNAME_KEY],
                file_format=file_format,
                replace_rules=False,
            )
            if saved_stories is not None:
                return response.json(
                    saved_stories, headers={"X-Total-Count": len(saved_stories)}
                )
        except StoryParseError as e:
            logger.error(e.message)
            return common_utils.error(
                HTTPStatus.BAD_REQUEST,
                "StoryParseError",
                "Failed to parse stories.",
                details=e.message,
            )

    @stories_endpoints.route("/stories.md", methods=["GET", "HEAD"])
    @rasa_x_scoped("bulkStories.get", allow_api_token=True)
    async def get_bulk_stories(request):
        cli_utils.raise_warning(
            'The "/stories.md" endpoint is deprecated. Please use "/stories" instead, '
            'specifying "text/markdown" for the "Accept" header.',
            category=FutureWarning,
        )

        request.headers["Accept"] = "text/markdown"
        return await get_stories(request)

    @stories_endpoints.route("/stories/<story_id>", methods=["PUT"])
    @rasa_x_scoped("stories.update")
    @inject_rasa_x_user()
    async def modify_story(
        request: Request, story_id: Text, user: Dict[Text, Any]
    ) -> HTTPResponse:
        story_string = io_utils.convert_bytes_to_string(request.body)

        try:
            file_format = _story_file_format(request)
        except ValueError as e:
            logger.error(e)
            return common_utils.error(
                HTTPStatus.BAD_REQUEST, "InvalidContentTypeError", str(e)
            )

        try:
            updated_story = await StoryService.from_request(request).update_story(
                story_id,
                story_string,
                rasa_x_config.project_name,
                user,
                file_format,
                update_rule=False,
            )
            if not updated_story:
                return response.text("Story could not be found", HTTPStatus.NOT_FOUND)

            return response.json(updated_story)
        except ValueError as e:
            logger.error(e)
            return common_utils.error(
                HTTPStatus.BAD_REQUEST, "StoryContentsError", details=str(e),
            )
        except StoryParseError as e:
            logger.error(e.message)
            return common_utils.error(
                HTTPStatus.BAD_REQUEST,
                "StoryParseError",
                "Failed to modify story.",
                details=e.message,
            )

    @stories_endpoints.route("/stories/<story_id>", methods=["DELETE"])
    @rasa_x_scoped("stories.delete")
    async def delete_story(request: Request, story_id: Text) -> HTTPResponse:
        deleted = StoryService.from_request(request).delete_story(
            story_id, delete_rule=False
        )
        if deleted:
            return response.text("", HTTPStatus.NO_CONTENT)
        return common_utils.error(
            HTTPStatus.NOT_FOUND,
            "StoryNotFound",
            f"Failed to delete story with story with id '{story_id}'.",
        )

    return stories_endpoints
