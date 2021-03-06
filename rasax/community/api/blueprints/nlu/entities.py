import logging
from typing import Text, Dict, Optional

import rasax.community.constants as constants
from rasax.community.api.decorators import (
    rasa_x_scoped,
    inject_rasa_x_user,
)
from rasax.community.services.data_service import DataService
from sanic import Blueprint, response
from sanic.request import Request
from sanic.response import HTTPResponse

logger = logging.getLogger(__name__)


def blueprint():
    nlu_entities_endpoints = Blueprint("nlu_entities_endpoints")

    # noinspection PyUnusedLocal
    @nlu_entities_endpoints.route(
        "/projects/<project_id>/entities", methods=["GET", "HEAD"]
    )
    @rasa_x_scoped("entities.list")
    @inject_rasa_x_user()
    async def get_entities(
        request: Request, project_id: Text, user: Optional[Dict] = None
    ) -> HTTPResponse:
        """Fetches a list of unique entities present in training data."""

        entities = DataService.from_request(request).get_entities(project_id)
        return response.json(entities, headers={"X-Total-Count": len(entities)})

    return nlu_entities_endpoints
