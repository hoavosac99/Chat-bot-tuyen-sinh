import logging
from http import HTTPStatus
from typing import Text

import rasax.community.constants as constants
import rasax.community.utils.common as common_utils
from rasax.community.api.decorators import (
    rasa_x_scoped,
    validate_schema,
)
from rasax.community.services.data_service import DataService
from sanic import Blueprint, response
from sanic.request import Request
from sanic.response import HTTPResponse

logger = logging.getLogger(__name__)


def blueprint():
    nlu_synonyms_endpoints = Blueprint("nlu_synonyms_endpoints")

    @nlu_synonyms_endpoints.route(
        "/projects/<project_id>/synonyms", methods=["GET", "HEAD"]
    )
    @rasa_x_scoped("entity_synonyms.list", allow_api_token=True)
    async def get_entity_synonyms(request: Request, project_id: Text) -> HTTPResponse:
        """Get all entity synonyms and their mapped values."""
        data_service = DataService.from_request(request)

        mapped_value_query = common_utils.default_arg(request, "mapped_value")
        if mapped_value_query:
            matching_synonym = data_service.get_synonym_by_mapped_value(
                mapped_value_query, project_id
            )
            if matching_synonym:
                entity_synonyms = [matching_synonym]
            else:
                entity_synonyms = []
        else:
            entity_synonyms = data_service.get_entity_synonyms(
                project_id, nlu_format=False
            )

        return response.json(
            entity_synonyms, headers={"X-Total-Count": len(entity_synonyms)}
        )

    @nlu_synonyms_endpoints.route("/projects/<project_id>/synonyms", methods=["POST"])
    @rasa_x_scoped("entity_synonyms.create", allow_api_token=True)
    @validate_schema("entity_synonym")
    async def create_entity_synonym(request: Request, project_id: Text) -> HTTPResponse:
        """Create a new entity synonym with mapped values."""

        data_service = DataService.from_request(request)

        synonym_name = request.json["synonym_reference"]
        mapped_values = [item["value"] for item in request.json["mapped_values"]]

        if len(set(mapped_values)) != len(request.json["mapped_values"]):
            return common_utils.error(
                HTTPStatus.BAD_REQUEST,
                "EntitySynonymCreationFailed",
                "One or more mapped values were repeated.",
            )

        try:
            created = data_service.create_entity_synonym(
                project_id, synonym_name, mapped_values
            )
        except ValueError as e:
            logger.error(e)
            return common_utils.error(
                HTTPStatus.BAD_REQUEST, "EntitySynonymCreationFailed", str(e)
            )

        if created:
            return response.json(
                data_service.get_entity_synonym(project_id, created.id),
                status=HTTPStatus.CREATED,
            )
        else:
            return common_utils.error(
                HTTPStatus.BAD_REQUEST,
                "EntitySynonymCreationFailed",
                "An entity synonym with that value already exists.",
            )

    @nlu_synonyms_endpoints.route(
        "/projects/<project_id>/synonyms/<synonym_id:int>", methods=["GET", "HEAD"]
    )
    @rasa_x_scoped("entity_synonyms.get", allow_api_token=True)
    async def get_entity_synonym(
        request: Request, project_id: Text, synonym_id: int
    ) -> HTTPResponse:
        """Get a specific entity synonym and its mapped values."""

        data_service = DataService.from_request(request)
        entity_synonym = data_service.get_entity_synonym(project_id, synonym_id)
        if entity_synonym:
            return response.json(entity_synonym)
        else:
            return common_utils.error(
                HTTPStatus.NOT_FOUND,
                "GettingEntitySynonymFailed",
                f"Could not find entity synonym for ID '{synonym_id}'.",
            )

    @nlu_synonyms_endpoints.route(
        "/projects/<project_id>/synonyms/<synonym_id:int>", methods=["POST"]
    )
    @rasa_x_scoped("entity_synonym_values.create", allow_api_token=True)
    @validate_schema("entity_synonym_values")
    async def create_entity_synonym_mapped_values(
        request: Request, project_id: Text, synonym_id: int
    ) -> HTTPResponse:
        """Map new values to an existing entity synonym."""

        mapped_values = [item["value"] for item in request.json["mapped_values"]]

        try:
            created = DataService.from_request(
                request
            ).add_entity_synonym_mapped_values(project_id, synonym_id, mapped_values)
        except ValueError as e:
            logger.error(e)
            return common_utils.error(
                HTTPStatus.BAD_REQUEST, "EntitySynonymValuesCreationFailed", str(e)
            )

        if created is not None:
            return response.json(created, status=HTTPStatus.CREATED)
        else:
            return common_utils.error(
                HTTPStatus.BAD_REQUEST,
                "EntitySynonymValuesCreationFailed",
                "One or more mapped values already existed.",
            )

    @nlu_synonyms_endpoints.route(
        "/projects/<project_id>/synonyms/<synonym_id:int>", methods=["PUT"]
    )
    @rasa_x_scoped("entity_synonyms.update", allow_api_token=True)
    @validate_schema("entity_synonym_name")
    async def update_entity_synonym(
        request: Request, project_id: Text, synonym_id: int
    ) -> HTTPResponse:
        """Modify the text value (name) of an entity synonym."""

        data_service = DataService.from_request(request)

        try:
            updated = data_service.update_entity_synonym(
                project_id, synonym_id, request.json["synonym_reference"]
            )
        except ValueError as e:
            logger.error(e)
            return common_utils.error(
                HTTPStatus.NOT_FOUND,
                "EntitySynonymUpdateFailed",
                "Could not find entity synonym.",
            )

        if updated:
            return response.text("", HTTPStatus.NO_CONTENT)
        else:
            return common_utils.error(
                HTTPStatus.BAD_REQUEST,
                "EntitySynonymUpdateFailed",
                "An EntitySynonym with that value already exists.",
            )

    @nlu_synonyms_endpoints.route(
        "/projects/<project_id>/synonyms/<synonym_id:int>", methods=["DELETE"]
    )
    @rasa_x_scoped("entity_synonyms.delete", allow_api_token=True)
    async def delete_entity_synonym(
        request: Request, project_id: Text, synonym_id: int
    ) -> HTTPResponse:
        """Delete an entity synonym."""

        deleted = DataService.from_request(request).delete_entity_synonym(
            project_id, synonym_id
        )

        if deleted:
            return response.text("", HTTPStatus.NO_CONTENT)
        else:
            return common_utils.error(
                HTTPStatus.NOT_FOUND,
                "EntitySynonymDeletionFailed",
                "Could not find entity synonym.",
            )

    @nlu_synonyms_endpoints.route(
        "/projects/<project_id>/synonyms/<synonym_id:int>/<mapping_id:int>",
        methods=["DELETE"],
    )
    @rasa_x_scoped("entity_synonym_values.delete", allow_api_token=True)
    async def delete_entity_synonym_mapped_value(
        request: Request, project_id: Text, synonym_id: int, mapping_id: int
    ) -> HTTPResponse:
        """Delete an entity synonym mapped value."""

        data_service = DataService.from_request(request)
        try:
            deleted = data_service.delete_entity_synonym_mapped_value(
                project_id, synonym_id, mapping_id
            )
        except ValueError as e:
            logger.error(e)
            return common_utils.error(
                HTTPStatus.NOT_FOUND,
                "EntitySynonymValueDeletionFailed",
                "Could not find entity synonym.",
            )

        if deleted:
            return response.text("", HTTPStatus.NO_CONTENT)
        else:
            return common_utils.error(
                HTTPStatus.NOT_FOUND,
                "EntitySynonymValueDeletionFailed",
                "Could not find entity synonym mapped value.",
            )

    @nlu_synonyms_endpoints.route(
        "/projects/<project_id>/synonyms/<synonym_id:int>/<mapping_id:int>",
        methods=["PUT"],
    )
    @rasa_x_scoped("entity_synonym_values.update", allow_api_token=True)
    @validate_schema("entity_synonym_value")
    async def update_entity_synonym_mapped_value(
        request: Request, project_id: Text, synonym_id: int, mapping_id: int
    ) -> HTTPResponse:
        """Modify the text value of an existing value mapped to an entity synonym."""

        data_service = DataService.from_request(request)

        try:
            updated = data_service.update_entity_synonym_mapped_value(
                project_id, synonym_id, mapping_id, request.json["value"]
            )
        except ValueError as e:
            logger.error(e)
            return common_utils.error(
                HTTPStatus.BAD_REQUEST, "EntitySynonymValueUpdateFailed", str(e)
            )

        if updated:
            return response.text("", HTTPStatus.NO_CONTENT)
        else:
            return common_utils.error(
                HTTPStatus.BAD_REQUEST,
                "EntitySynonymValueUpdateFailed",
                "Another mapped value with that text value already exists.",
            )

    return nlu_synonyms_endpoints
