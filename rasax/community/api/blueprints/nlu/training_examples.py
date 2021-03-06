import json
import logging
from http import HTTPStatus
from typing import Text, Dict, Optional

import rasax.community.constants as constants
import rasax.community.telemetry as telemetry
import rasax.community.utils.cli as cli_utils
import rasax.community.utils.common as common_utils
import rasax.community.data
from rasax.community.api.decorators import (
    rasa_x_scoped,
    inject_rasa_x_user,
    validate_schema,
)
from rasax.community.data import format_from_mime_type
from rasax.community.services.data_service import DataService
from rasax.community.services.intent_service import INTENT_MAPPED_TO_KEY
from rasax.community.services.intent_service import IntentService
from sanic import Blueprint, response
from sanic.request import Request
from sanic.response import HTTPResponse


import rasa.shared.nlu.training_data.loading

logger = logging.getLogger(__name__)


def blueprint():
    nlu_training_examples_endpoints = Blueprint("nlu_training_examples_endpoints")

    @nlu_training_examples_endpoints.route(
        "/projects/<project_id>/data", methods=["GET", "HEAD"]
    )
    @rasa_x_scoped("examples.list")
    async def training_examples_deprecated(
        request: Request, project_id: Text
    ) -> HTTPResponse:
        cli_utils.raise_warning(
            'The endpoint "/data" is deprecated, please use "/training_examples" '
            "instead",
            category=FutureWarning,
        )
        return await training_examples(request, project_id)

    @nlu_training_examples_endpoints.route(
        "/projects/<project_id>/training_examples", methods=["GET", "HEAD"]
    )
    @rasa_x_scoped("examples.list")
    async def training_examples(request: Request, project_id: Text) -> HTTPResponse:
        """Retrieve the training data for the project."""

        limit = common_utils.int_arg(request, "limit")
        offset = common_utils.int_arg(request, "offset", 0)
        text_query = common_utils.default_arg(request, "q", None)
        intent_query = common_utils.default_arg(request, "intent", None)
        entity_query = common_utils.bool_arg(request, "entities", False)
        sort_by_descending_id = common_utils.bool_arg(request, "sorted", True)
        fields = common_utils.fields_arg(
            request, {"intent", "text", "id", "entities", "entities.entity"}
        )
        distinct = common_utils.bool_arg(request, "distinct", True)
        data_query = DataService.from_request(request).get_training_data(
            project_id=project_id,
            sort_by_descending_id=sort_by_descending_id,
            text_query=text_query,
            intent_query=intent_query,
            entity_query=entity_query,
            fields_query=fields,
            limit=limit,
            offset=offset,
            distinct=distinct,
        )

        return response.json(
            data_query.result, headers={"X-Total-Count": data_query.count}
        )

    # noinspection PyUnusedLocal
    @nlu_training_examples_endpoints.route(
        "/projects/<project_id>/data/<_hash>", methods=["GET", "HEAD"]
    )
    @rasa_x_scoped("examples.get")
    async def training_example_by_hash_deprecated(
        request: Request, project_id: Text, _hash: Text
    ) -> HTTPResponse:
        cli_utils.raise_warning(
            'The endpoint "/data/<hash>" is deprecated, please use '
            '"/training_examples/hash" instead',
            category=FutureWarning,
        )
        return await training_example_by_hash(request, project_id)

    # noinspection PyUnusedLocal
    @nlu_training_examples_endpoints.route(
        "/projects/<project_id>/training_examples/<_hash>", methods=["GET", "HEAD"]
    )
    @rasa_x_scoped("examples.get")
    async def training_example_by_hash(
        request: Request, project_id: Text, _hash: Text
    ) -> HTTPResponse:
        """Retrieve a training example by its hash."""
        example = DataService.from_request(request).get_example_by_hash(
            project_id, _hash
        )
        if not example:
            return common_utils.error(
                HTTPStatus.NOT_FOUND,
                "TrainingExampleError",
                "Example could not be found.",
            )

        return response.json(example)

    # noinspection PyUnusedLocal
    @nlu_training_examples_endpoints.route(
        "/projects/<project_id>/dataWarnings", methods=["GET", "HEAD"]
    )
    @rasa_x_scoped("warnings.get")
    async def training_data_warnings_deprecated(
        request: Request, project_id: Text
    ) -> HTTPResponse:
        """Retrieve the training data warnings for the project."""
        cli_utils.raise_warning(
            'The endpoint "/dataWarnings" is deprecated, please use '
            '"/training_data_warnings" instead',
            category=FutureWarning,
        )

        return await training_data_warnings(request, project_id)

    # noinspection PyUnusedLocal
    @nlu_training_examples_endpoints.route(
        "/projects/<project_id>/training_data_warnings", methods=["GET", "HEAD"]
    )
    @rasa_x_scoped("warnings.get")
    async def training_data_warnings(
        request: Request, project_id: Text
    ) -> HTTPResponse:
        """Retrieve the training data warnings for the project."""

        return response.json(
            DataService.from_request(request).get_training_data_warnings(
                project_id=project_id
            )
        )

    @nlu_training_examples_endpoints.route(
        "/projects/<project_id>/data.json", methods=["GET", "HEAD"]
    )
    @rasa_x_scoped("bulkData.get", allow_api_token=True)
    async def training_data_as_json_deprecated(
        request: Request, project_id: Text
    ) -> HTTPResponse:
        """Download the training data for the project in Rasa NLU json format."""
        cli_utils.raise_warning(
            'The endpoint "/data.json" is deprecated, please use '
            '"/training_data" instead',
            category=FutureWarning,
        )

        data_service = DataService.from_request(request)
        content = data_service.create_formatted_training_data(project_id)

        return response.text(
            json.dumps(content, indent=4, ensure_ascii=False),
            content_type="application/json",
            headers={"Content-Disposition": "attachment;filename=nlu.train.json"},
        )

    @nlu_training_examples_endpoints.route(
        "/projects/<project_id>/data.md", methods=["GET", "HEAD"]
    )
    @rasa_x_scoped("bulkData.get", allow_api_token=True)
    async def training_data_as_md(request: Request, project_id: Text) -> HTTPResponse:
        """Download the training data for the project in Rasa NLU markdown format."""

        cli_utils.raise_warning(
            'The endpoint "/data.md" is deprecated, please use '
            '"/training_data" instead',
            category=FutureWarning,
        )

        data_service = DataService.from_request(request)
        content = data_service.get_nlu_training_data_object(project_id=project_id)

        return response.text(
            content.nlu_as_markdown(),
            content_type="text/markdown",
            headers={"Content-Disposition": "attachment;filename=nlu.train.md"},
        )

    @nlu_training_examples_endpoints.route(
        "/projects/<project_id>/training_data", methods=["GET", "HEAD"]
    )
    @rasa_x_scoped("bulkData.get", allow_api_token=True)
    async def training_data(request: Request, project_id: Text) -> HTTPResponse:
        """Download the training data for the project in a specified format."""
        accept_header = request.headers.get("Accept", "application/x-yaml")
        output_format = format_from_mime_type(accept_header)

        if output_format not in [
            rasax.community.data.FileFormat.YAML,
            rasax.community.data.FileFormat.MARKDOWN,
            rasax.community.data.FileFormat.JSON,
        ]:
            return common_utils.error(
                HTTPStatus.BAD_REQUEST,
                "Failed to get training data in a specified format. ",
                "Format must be YAML, Markdown or JSON.",
            )

        data_service = DataService.from_request(request)
        content = data_service.get_nlu_training_data_object(project_id=project_id)

        if output_format == rasax.community.data.FileFormat.YAML:
            result = content.nlu_as_yaml()
        if output_format == rasax.community.data.FileFormat.MARKDOWN:
            result = content.nlu_as_markdown()
        if output_format == rasax.community.data.FileFormat.JSON:
            result = content.nlu_as_json()

        return response.text(
            result,
            content_type=accept_header,
            headers={
                "Content-Disposition": f"attachment;filename=nlu.train{output_format}"
            },
        )

    @nlu_training_examples_endpoints.route(
        "/projects/<project_id>/data", methods=["POST"]
    )
    @rasa_x_scoped("examples.create")
    @inject_rasa_x_user()
    @validate_schema("data")
    async def add_training_example_deprecated(
        request: Request, project_id: Text, user: Optional[Dict] = None
    ) -> HTTPResponse:
        cli_utils.raise_warning(
            'The endpoint "POST /data" is deprecated, please use '
            '"POST /training_examples" instead',
            category=FutureWarning,
        )
        return await add_training_example(request, project_id, user)

    @nlu_training_examples_endpoints.route(
        "/projects/<project_id>/training_examples", methods=["POST"]
    )
    @rasa_x_scoped("examples.create")
    @inject_rasa_x_user()
    @validate_schema("data")
    async def add_training_example(
        request: Request, project_id: Text, user: Optional[Dict] = None
    ) -> HTTPResponse:
        """Add a new training example to the project."""

        rjs = request.json
        data_service = DataService.from_request(request)
        example_hash = common_utils.get_text_hash(rjs.get("text"))
        existing_example = data_service.get_example_by_hash(project_id, example_hash)
        if existing_example:
            example_id = existing_example["id"]
            example = data_service.replace_example(user, project_id, rjs, example_id)

        else:
            example = data_service.save_example(
                user[constants.USERNAME_KEY], project_id, rjs
            )

        if not example:
            return common_utils.error(
                HTTPStatus.BAD_REQUEST, "SaveExampleError", "Example could not be saved"
            )

        telemetry.track_message_annotated_from_referrer(request.headers.get("Referer"))
        return response.json(example)

    @nlu_training_examples_endpoints.route(
        "/projects/<project_id>/data/<example_id:int>", methods=["PUT"]
    )
    @rasa_x_scoped("examples.update")
    @inject_rasa_x_user()
    @validate_schema("data")
    async def update_example_deprecated(
        request: Request, project_id: Text, example_id, user: Optional[Dict] = None
    ) -> HTTPResponse:
        cli_utils.raise_warning(
            'The endpoint "PUT /data/<example_id>" is deprecated, please use '
            '"PUT /training_examples/<example_id>" instead',
            category=FutureWarning,
        )

        return await update_example(request, project_id, example_id, user)

    @nlu_training_examples_endpoints.route(
        "/projects/<project_id>/training_examples/<example_id:int>", methods=["PUT"]
    )
    @rasa_x_scoped("examples.update")
    @inject_rasa_x_user()
    @validate_schema("data")
    async def update_example(
        request: Request, project_id: Text, example_id, user: Optional[Dict] = None
    ) -> HTTPResponse:
        """Update an existing training example."""

        rjs = request.json
        data_service = DataService.from_request(request)
        example_hash = common_utils.get_text_hash(rjs.get("text"))
        existing_example = data_service.get_example_by_hash(project_id, example_hash)
        if existing_example and example_id != existing_example["id"]:
            return common_utils.error(
                HTTPStatus.BAD_REQUEST,
                "ExampleUpdateFailed",
                "Example with this text already exists",
            )

        mapped_to = rjs.get("intent_mapped_to")
        if mapped_to:
            intent_service = IntentService(request[constants.REQUEST_DB_SESSION_KEY])
            intent = {INTENT_MAPPED_TO_KEY: mapped_to}
            intent_service.update_temporary_intent(rjs["intent"], intent, project_id)
            intent_service.add_example_to_temporary_intent(
                rjs[intent], example_hash, project_id
            )
        updated_example = data_service.replace_example(
            user, project_id, rjs, example_id
        )
        if not updated_example:
            return common_utils.error(
                HTTPStatus.BAD_REQUEST,
                "ExampleUpdateFailed",
                "Example could not be updated",
            )

        return response.json(updated_example)

    # noinspection PyUnusedLocal
    @nlu_training_examples_endpoints.route(
        "/projects/<project_id>/data/<example_id:int>", methods=["DELETE"]
    )
    @rasa_x_scoped("examples.delete")
    @inject_rasa_x_user()
    async def training_data_delete_deprecated(
        request: Request, project_id: Text, example_id: int, user: Optional[Dict]
    ) -> HTTPResponse:
        cli_utils.raise_warning(
            'The endpoint "DELETE /data/<example_id>" is deprecated, please use '
            '"DELETE /training_examples/<example_id>" instead',
            category=FutureWarning,
        )

        return await training_data_delete(request, project_id, example_id, user)

    # noinspection PyUnusedLocal
    @nlu_training_examples_endpoints.route(
        "/projects/<project_id>/training_examples/<example_id:int>", methods=["DELETE"],
    )
    @rasa_x_scoped("examples.delete")
    @inject_rasa_x_user()
    async def training_data_delete(
        request: Request, project_id: Text, example_id: int, user: Optional[Dict]
    ) -> HTTPResponse:
        """Remove a training example from the project by id."""

        success = DataService.from_request(request).delete_example(example_id)
        if success:
            return response.text("", HTTPStatus.NO_CONTENT)

        return common_utils.error(
            HTTPStatus.NOT_FOUND,
            "DeleteTrainingExampleFailed",
            "Training example with example_id '{}' could not be "
            "deleted".format(example_id),
        )

    @nlu_training_examples_endpoints.route(
        "/projects/<project_id>/data", methods=["PUT"]
    )
    @rasa_x_scoped("bulkData.update")
    @inject_rasa_x_user()
    async def training_data_bulk_deprecated(
        request: Request, project_id: Text, user: Optional[Dict] = None
    ) -> HTTPResponse:
        cli_utils.raise_warning(
            'The endpoint "PUT /data" is deprecated, please use '
            '"PUT /training_examples" instead',
            category=FutureWarning,
        )

        return await training_data_bulk(request, project_id, user)

    @nlu_training_examples_endpoints.route(
        "/projects/<project_id>/training_examples", methods=["PUT"]
    )
    @rasa_x_scoped("bulkData.update")
    @inject_rasa_x_user()
    async def training_data_bulk(
        request: Request, project_id: Text, user: Optional[Dict] = None
    ) -> HTTPResponse:
        """Replace existing training samples with the posted data."""

        content_type = request.headers.get("Content-Type")
        file_format = format_from_mime_type(content_type)

        try:
            if file_format not in [
                rasax.community.data.FileFormat.YAML,
                rasax.community.data.FileFormat.MARKDOWN,
                rasax.community.data.FileFormat.JSON,
            ]:
                raise ValueError("Story content type must be YAML, Markdown or JSON.")

            data_service = DataService.from_request(request)
            if file_format == rasax.community.data.FileFormat.JSON:
                data = request.json
                target_format = rasa.shared.nlu.training_data.loading.RASA
            elif file_format == rasax.community.data.FileFormat.MARKDOWN:
                data = request.body
                target_format = rasa.shared.nlu.training_data.loading.MARKDOWN
            elif file_format == rasax.community.data.FileFormat.YAML:
                data = request.body
                target_format = rasa.shared.nlu.training_data.loading.RASA_YAML

            data_service.replace_data(
                project_id, data, target_format, user[constants.USERNAME_KEY],
            )

            return response.text("Bulk upload of training data successful.")
        except ValueError as e:
            logger.error(e)
            return common_utils.error(
                HTTPStatus.BAD_REQUEST,
                "AddTrainingDataFailed",
                "Failed to add training data",
                details=e,
            )

    return nlu_training_examples_endpoints
