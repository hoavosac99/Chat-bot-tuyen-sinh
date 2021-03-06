import logging
import time
from typing import Text, Dict, Any, Optional, List

from sanic.request import Request
from sqlalchemy.orm import Session

import rasax.community.config as rasa_x_config
import rasax.community.constants as constants
from rasax.community.database.model import NluEvaluation, NluEvaluationPrediction, Model
from rasax.community.database.service import DbService

from rasax.community.services.model_service import ModelService
from rasax.community.services.stack_service import StackService

logger = logging.getLogger(__name__)

MODEL_EVALUATION_TAG = "evaluation"


class EvaluationService(DbService):
    @staticmethod
    def from_request(
        request: Request, stack_service: StackService
    ) -> "EvaluationService":
        return EvaluationService(
            stack_service, request[constants.REQUEST_DB_SESSION_KEY]
        )

    def __init__(self, stack_service: "StackService", session: Session):
        self.stack_service = stack_service
        super().__init__(session)

    def persist_evaluation(
        self, project_id: Text, model: Text, result: Dict[Text, Any]
    ) -> NluEvaluation:
        intent_eval = result.get("intent_evaluation", None) or {}

        evaluation = NluEvaluation(
            model_id=model,
            report=intent_eval.get("report", "No metrics calculated"),
            accuracy=intent_eval.get("accuracy", 0),
            f1=intent_eval.get("f1_score", 0),
            precision=intent_eval.get("precision", 0),
            timestamp=time.time(),
        )

        # delete possible old evaluations
        self.delete_evaluation(project_id, model)

        self.add(evaluation)
        self.flush()  # flush to get the insertion id

        predictions = [
            NluEvaluationPrediction(
                text=p["text"],
                intent=p["intent"],
                predicted=p["predicted"],
                confidence=p.get("confidence", 0),
                evaluation_id=evaluation.id,
            )
            for p in intent_eval.get("predictions", [])
        ]

        self.bulk_save_objects(predictions)

        return evaluation

    def evaluation_for_model(
        self, project_id: Text, model: Text
    ) -> Optional[Dict[Text, Any]]:
        evaluation = (
            self.query(NluEvaluation)
            .filter(NluEvaluation.model_id == model)
            .join(Model)
            .filter(Model.project_id == project_id)
            .join(NluEvaluationPrediction)
            .first()
        )

        if evaluation:
            return evaluation.as_dict()
        else:
            return None

    def delete_evaluation(self, project_id: Text, model: Text) -> bool:
        to_delete = (
            self.query(NluEvaluation)
            .filter(NluEvaluation.model_id == model)
            .join(Model)
            .filter(Model.project_id == project_id)
            .all()
        )

        for e in to_delete:
            self.delete(e)

        deleted = self.session.deleted

        return deleted

    def formatted_evaluations(self, project_id: Text) -> List[Dict[Text, Any]]:
        results = self.evaluations(project_id)
        return [
            {
                "intent_evaluation": r["intent_evaluation"],
                "project_id": project_id,
                "model": r["model"],
            }
            for r in results
        ]

    def evaluations(self, project_id: Text) -> List[Dict[Text, Any]]:
        evaluations = (
            self.query(NluEvaluation)
            .join(Model)
            .filter(Model.project_id == project_id)
            .all()
        )

        return [e.as_dict() for e in evaluations]

    async def evaluate(
        self, training_data: List[Dict[Text, Any]], model_name: Text
    ) -> Optional[Dict]:
        model_service = ModelService(
            rasa_x_config.rasa_model_dir,
            self.session,
            constants.RASA_WORKER_ENVIRONMENT,
        )

        # Tag the model for the evaluation so that the stack worker can pull it
        _ = await model_service.tag_model(
            rasa_x_config.project_name, model_name, MODEL_EVALUATION_TAG
        )

        self.session.commit()  # commit so other service can pull this tag

        model_pull_path = ModelService.get_model_server_url(MODEL_EVALUATION_TAG)
        try:
            return await self.stack_service.evaluate_intents(
                training_data, model_pull_path
            )
        finally:
            model_service.delete_tag(
                rasa_x_config.project_name, model_name, MODEL_EVALUATION_TAG
            )
