import json
from typing import Dict, Any, Text, Set, Union, List

import sqlalchemy as sa
from sqlalchemy.orm import object_session, relationship
from sqlalchemy import and_

import rasax.community.constants as constants
from rasax.community.database.base import Base
from rasax.community.database import utils


class Conversation(Base):
    """Stores the user's conversation and its metadata."""

    __tablename__ = "conversation"

    sender_id = sa.Column(sa.String, primary_key=True)
    number_user_messages = sa.Column(sa.Integer, default=0)
    latest_input_channel = sa.Column(sa.String)
    latest_event_time = sa.Column(sa.Float)  # latest event time as unix timestamp
    in_training_data = sa.Column(sa.Boolean, default=True)
    review_status = sa.Column(
        sa.String, default=constants.CONVERSATION_STATUS_UNREAD, nullable=False
    )

    minimum_action_confidence = sa.Column(sa.Float)
    maximum_action_confidence = sa.Column(sa.Float)
    minimum_intent_confidence = sa.Column(sa.Float)
    maximum_intent_confidence = sa.Column(sa.Float)

    evaluation = sa.Column(sa.Text)
    interactive = sa.Column(sa.Boolean, default=False)
    created_by = sa.Column(
        sa.String, sa.ForeignKey("rasa_x_user.username"), nullable=True, index=True
    )

    events = relationship(
        "ConversationEvent",
        cascade="all, delete-orphan",
        back_populates="conversation",
        order_by=lambda: ConversationEvent.timestamp.asc(),
    )

    message_logs = relationship("MessageLog", back_populates="conversation")

    unique_policies = relationship(
        "ConversationPolicyMetadata",
        cascade="all, delete-orphan",
        back_populates="conversation",
    )
    unique_actions = relationship(
        "ConversationActionMetadata",
        cascade="all, delete-orphan",
        back_populates="conversation",
    )
    unique_intents = relationship(
        "ConversationIntentMetadata",
        cascade="all, delete-orphan",
        back_populates="conversation",
    )
    unique_entities = relationship(
        "ConversationEntityMetadata",
        cascade="all, delete-orphan",
        back_populates="conversation",
    )

    corrected_messages = relationship(
        "ConversationMessageCorrection",
        cascade="all, delete-orphan",
        back_populates="conversation",
    )

    tags = relationship(
        "ConversationTag",
        secondary="conversation_to_tag_mapping",
        backref="conversations",
    )

    def tags_set(self) -> Set[int]:
        return {t.id for t in self.tags}

    @property
    def has_flagged_messages(self) -> bool:
        result = (
            object_session(self)
            .query(Conversation)
            .filter(
                and_(
                    Conversation.events.any(
                        and_(
                            ConversationEvent.conversation_id == self.sender_id,
                            ConversationEvent.is_flagged,
                        )
                    )
                )
            )
            .first()
        )
        return result is not None

    def as_dict(self) -> Dict[Text, Any]:
        from rasax.community.services.event_service import EventService

        result = {
            "sender_id": self.sender_id,
            "sender_name": EventService.get_sender_name(self),  # displayed in the UI
            "latest_event_time": self.latest_event_time,
            "latest_input_channel": self.latest_input_channel,
            "intents": [i.intent for i in self.unique_intents],
            "actions": [a.action for a in self.unique_actions],
            "minimum_action_confidence": self.minimum_action_confidence,
            "maximum_action_confidence": self.maximum_action_confidence,
            "minimum_intent_confidence": self.minimum_intent_confidence,
            "maximum_intent_confidence": self.maximum_intent_confidence,
            "in_training_data": self.in_training_data,
            "review_status": self.review_status,
            "policies": [p.policy for p in self.unique_policies],
            "n_user_messages": self.number_user_messages,
            "has_flagged_messages": self.has_flagged_messages,
            "corrected_messages": [
                {"message_timestamp": c.message_timestamp, "intent": c.intent}
                for c in self.corrected_messages
            ],
            "interactive": self.interactive,
            "tags": list(self.tags_set()),
            "created_by": self.created_by,
        }

        return result


class ConversationTag(Base):
    """Stores conversation tags."""

    __tablename__ = "conversation_tag"

    id = sa.Column(sa.Integer, utils.create_sequence(__tablename__), primary_key=True)
    value = sa.Column(sa.String, nullable=False, index=True)
    color = sa.Column(sa.String, nullable=False)

    def as_dict(self) -> Dict[Text, Union[Text, int, List[Text]]]:
        return {
            "id": self.id,
            "value": self.value,
            "color": self.color,
            "conversations": [m.sender_id for m in self.conversations],
        }


# Stores mapping between Conversation and ConversationTag
conversation_to_tag_mapping = sa.Table(
    "conversation_to_tag_mapping",
    Base.metadata,
    sa.Column(
        "conversation_id",
        sa.String,
        sa.ForeignKey("conversation.sender_id"),
        nullable=False,
        index=True,
    ),
    sa.Column(
        "tag_id",
        sa.String,
        utils.create_sequence("conversation_to_tag_mapping"),
        sa.ForeignKey("conversation_tag.id"),
        nullable=False,
        index=True,
    ),
)


class ConversationEvent(Base):
    """Stores a single event which happened during a conversation."""

    __tablename__ = "conversation_event"

    id = sa.Column(sa.Integer, utils.create_sequence(__tablename__), primary_key=True)
    conversation_id = sa.Column(
        sa.String, sa.ForeignKey("conversation.sender_id"), index=True, nullable=False
    )
    conversation = relationship("Conversation", back_populates="events")

    type_name = sa.Column(sa.String, nullable=False)
    timestamp = sa.Column(
        sa.Float, index=True, nullable=False
    )  # time of the event as unix timestamp
    intent_name = sa.Column(sa.String)
    action_name = sa.Column(sa.String)
    slot_name = sa.Column(sa.String)
    slot_value = sa.Column(sa.Text)
    policy = sa.Column(sa.String)
    is_flagged = sa.Column(sa.Boolean, default=False, nullable=False)
    data = sa.Column(sa.Text)
    message_log = relationship("MessageLog", back_populates="event", uselist=False)
    evaluation = sa.Column(sa.Text)
    rasa_environment = sa.Column(sa.String, default=constants.DEFAULT_RASA_ENVIRONMENT)

    def as_rasa_dict(self) -> Dict[Text, Any]:
        """Return a JSON-like representation of the internal Rasa (framework)
        event referenced by this `ConversationEvent`. Attach some information
        specific to Rasa X as part of the Rasa event metadata.

        Returns:
            A JSON-like representation of the Rasa event referenced by this
                database entity.
        """

        d = json.loads(self.data)

        # Add some metadata specific to Rasa X (namespaced with "rasa_x_")
        metadata = d.get("metadata") or {}
        metadata.update({"rasa_x_flagged": self.is_flagged, "rasa_x_id": self.id})
        d["metadata"] = metadata

        return d


class MessageLog(Base):
    """Stores the intent classification results of the user messages.

    Indexed columns:
    - `id` (Revision: `2a216ed121dd`)
    - `hash` (Revision: `af3596f6982f`)
    - `(archived, in_training_data)` (Revision: `af3596f6982f`)
    """

    __tablename__ = "message_log"

    id = sa.Column(sa.Integer, utils.create_sequence(__tablename__), primary_key=True)
    hash = sa.Column(sa.String, index=True)
    model = sa.Column(sa.String)
    archived = sa.Column(sa.Boolean, default=False)
    time = sa.Column(sa.Float)  # time of the log as unix timestamp
    text = sa.Column(sa.Text)
    intent = sa.Column(sa.String)
    confidence = sa.Column(sa.Float)
    intent_ranking = sa.Column(sa.Text)
    entities = sa.Column(sa.Text)
    in_training_data = sa.Column(sa.Boolean, default=False)

    event_id = sa.Column(sa.Integer, sa.ForeignKey("conversation_event.id"))
    event = relationship(
        "ConversationEvent", uselist=False, back_populates="message_log"
    )

    conversation_id = sa.Column(sa.String, sa.ForeignKey("conversation.sender_id"))
    conversation = relationship("Conversation", back_populates="message_logs")

    def as_dict(self) -> Dict[Text, Any]:
        return {
            "id": self.id,
            "time": self.time,
            "model": self.model,
            "hash": self.hash,
            "conversation_id": self.conversation_id,
            "event_id": self.event_id,
            "user_input": {
                "text": self.text,
                "intent": {"name": self.intent, "confidence": self.confidence},
                "intent_ranking": json.loads(self.intent_ranking),
                "entities": json.loads(self.entities),
            },
        }


class ConversationPolicyMetadata(Base):
    """Stores the distinct set of used policies in a conversation."""

    __tablename__ = "conversation_policy_metadata"

    conversation_id = sa.Column(
        sa.String, sa.ForeignKey("conversation.sender_id"), primary_key=True
    )
    policy = sa.Column(sa.String, primary_key=True)
    conversation = relationship("Conversation", back_populates="unique_policies")


class ConversationActionMetadata(Base):
    """Stores the distinct set of used actions in a conversation."""

    __tablename__ = "conversation_action_metadata"

    conversation_id = sa.Column(
        sa.String, sa.ForeignKey("conversation.sender_id"), primary_key=True
    )
    action = sa.Column(sa.String, primary_key=True)
    conversation = relationship("Conversation", back_populates="unique_actions")


class ConversationIntentMetadata(Base):
    """Stores the distinct set of used intents in a conversation."""

    __tablename__ = "conversation_intent_metadata"

    conversation_id = sa.Column(
        sa.String, sa.ForeignKey("conversation.sender_id"), primary_key=True
    )

    intent = sa.Column(sa.String, primary_key=True)
    conversation = relationship("Conversation", back_populates="unique_intents")


class ConversationEntityMetadata(Base):
    """Stores the distinct set of used entities in a conversation."""

    __tablename__ = "conversation_entity_metadata"

    conversation_id = sa.Column(
        sa.String, sa.ForeignKey("conversation.sender_id"), primary_key=True
    )

    entity = sa.Column(sa.String, primary_key=True)
    conversation = relationship("Conversation", back_populates="unique_entities")


class ConversationMessageCorrection(Base):
    """Stores post hoc corrections of intents in a conversation."""

    __tablename__ = "message_correction"

    conversation_id = sa.Column(
        sa.String, sa.ForeignKey("conversation.sender_id"), primary_key=True
    )

    # time of the message correction as unix timestamp
    message_timestamp = sa.Column(sa.Float, primary_key=True)
    intent = sa.Column(sa.String)
    conversation = relationship("Conversation", back_populates="corrected_messages")
