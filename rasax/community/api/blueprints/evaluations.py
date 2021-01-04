import logging
from http import HTTPStatus
from typing import Text

import rasax.community.constants as constants
import rasax.community.utils.common as common_utils
from aiohttp import ClientError
from rasax.community.api.blueprints.models import _model_service
from rasax.community.api.decorators import rasa_x_scoped
from rasax.community.services.data_service import DataService
from rasax.community.services.evaluation_service import EvaluationService
from rasax.community.services.stack_service import StackService
from sanic import Blueprint, response
from sanic.request import Request
from sanic.response import HTTPResponse

logger = logging.getLogger(__name__)


def blueprint():
    evaluations_endpoints = Blueprint("evaluations_endpoints")

    @evaluations_endpoints.route(
        "/projects/<project_id>/evaluations", methods=["GET", "HEAD"]
    )
    @rasa_x_scoped("models.evaluations.list", allow_api_token=True)
    async def evaluations(request: Request, project_id: Text) -> HTTPResponse:
        """Fetches a list of Rasa NLU evaluations."""
        stack_service = StackService.from_request(
            request, project_id, constants.RASA_WORKER_ENVIRONMENT
        )
        evaluation_service = EvaluationService.from_request(request, stack_service)
        results = evaluation_service.formatted_evaluations(project_id)
        return response.json(results, headers={"X-Total-Count": len(results)})

    @evaluations_endpoints.route(
        "/projects/<project_id>/evaluations/<model>", methods=["PUT"]
    )
    @rasa_x_scoped("models.evaluations.update", allow_api_token=True)
    async def put_evaluation(
        request: Request, project_id: Text, model: Text
    ) -> HTTPResponse:
        data = DataService.from_request(request).get_training_data(project_id).result
        model_service = _model_service(request)
        model_object = model_service.get_model_by_name(project_id, model)
        if not model_object:
            return common_utils.error(
                HTTPStatus.BAD_REQUEST,
                "NluEvaluationFailed",
                f"Could not find requested model '{model}'.",
            )

        stack_service = StackService.from_request(
            request, project_id, constants.RASA_WORKER_ENVIRONMENT
        )
        evaluation_service = EvaluationService.from_request(request, stack_service)

        try:
            content = await evaluation_service.evaluate(data, model)
            evaluation = evaluation_service.persist_evaluation(
                project_id, model, content
            )
            return response.json(evaluation.as_dict())
        except ClientError as e:
            return common_utils.error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "NluEvaluationFailed",
                f"Failed to create evaluation for model '{model}'.",
                details=e,
            )

    @evaluations_endpoints.route(
        "/projects/<project_id>/evaluations/<model>", methods=["GET", "HEAD"]
    )
    @rasa_x_scoped("models.evaluations.get", allow_api_token=True)
    async def get_evaluation(
        request: Request, project_id: Text, model: Text
    ) -> HTTPResponse:
        stack_service = StackService.from_request(
            request, project_id, constants.RASA_WORKER_ENVIRONMENT
        )
        evaluation_service = EvaluationService.from_request(request, stack_service)
        evaluation = evaluation_service.evaluation_for_model(project_id, model)
        if evaluation:
            return response.json(evaluation)
        else:
            return common_utils.error(
                HTTPStatus.NOT_FOUND,
                "NluEvaluationNotFound",
                "Could not find evaluation. "
                "A PUT to this endpoint will create an evaluation.",
            )

    @evaluations_endpoints.route(
        "/projects/<project_id>/evaluations/<model>", methods=["DELETE"]
    )
    @rasa_x_scoped("models.evaluations.delete", allow_api_token=True)
    async def delete_evaluation(
        request: Request, project_id: Text, model: Text
    ) -> HTTPResponse:
        stack_service = StackService.from_request(
            request, project_id, constants.RASA_WORKER_ENVIRONMENT
        )
        evaluation_service = EvaluationService.from_request(request, stack_service)
        delete = evaluation_service.delete_evaluation(project_id, model)
        if delete:
            return response.text("", HTTPStatus.NO_CONTENT)
        else:
            return common_utils.error(
                HTTPStatus.NOT_FOUND,
                "NluEvaluationNotFound",
                "Could not find evaluation. "
                "A PUT to this endpoint will create an evaluation.",
            )

    return evaluations_endpoints
