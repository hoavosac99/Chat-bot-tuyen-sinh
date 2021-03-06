import json
import time
from typing import Any, Text, Dict, Optional, List, Union

import sqlalchemy as sa
from sqlalchemy.orm import relationship

from rasax.community.database.base import Base


class ConversationStatistic(Base):
    """Stores statistics about every user conversation."""

    __tablename__ = "conversation_statistic"

    project_id = sa.Column(
        sa.String, sa.ForeignKey("project.project_id"), primary_key=True
    )
    total_user_messages = sa.Column(sa.Integer, default=0)
    total_bot_messages = sa.Column(sa.Integer, default=0)
    # latest event time as unix timestamp
    latest_event_timestamp = sa.Column(sa.Float)
    latest_event_id = sa.Column(sa.Integer)
    intents = relationship(
        "ConversationIntentStatistic",
        cascade="all, delete-orphan",
        back_populates="statistic",
        order_by=lambda: ConversationIntentStatistic.count.desc(),
    )
    actions = relationship(
        "ConversationActionStatistic",
        cascade="all, delete-orphan",
        back_populates="statistic",
        order_by=lambda: ConversationActionStatistic.count.desc(),
    )
    entities = relationship(
        "ConversationEntityStatistic",
        cascade="all, delete-orphan",
        back_populates="statistic",
        order_by=lambda: ConversationEntityStatistic.count.desc(),
    )
    policies = relationship(
        "ConversationPolicyStatistic",
        cascade="all, delete-orphan",
        back_populates="statistic",
        order_by=lambda: ConversationPolicyStatistic.count.desc(),
    )

    def as_dict(self, limit: int = 3) -> Dict[Text, Union[int, List[Text]]]:
        result = conversation_statistics_dict(
            self.total_user_messages, self.total_bot_messages
        )

        if self.intents:
            result["top_intents"] = [i.intent for i in self.intents[:limit]]
        if self.actions:
            result["top_actions"] = [a.action for a in self.actions[:limit]]
        if self.entities:
            result["top_entities"] = [e.entity for e in self.entities[:limit]]
        if self.policies:
            result["top_policies"] = [p.policy for p in self.policies[:limit]]

        return result


def conversation_statistics_dict(
    n_user_messages: int = 0,
    n_bot_messages: int = 0,
    top_intents: Optional[List[Text]] = None,
    top_actions: Optional[List[Text]] = None,
    top_entities: Optional[List[Text]] = None,
    top_policies: Optional[List[Text]] = None,
) -> Dict[Text, Union[int, List[Text]]]:
    return {
        "user_messages": n_user_messages or 0,
        "bot_messages": n_bot_messages or 0,
        "top_intents": top_intents or [],
        "top_actions": top_actions or [],
        "top_entities": top_entities or [],
        "top_policies": top_policies or [],
    }


class ConversationIntentStatistic(Base):
    """Stores the unique intents which were detected in a conversation."""

    __tablename__ = "conversation_intent_statistic"

    project_id = sa.Column(
        sa.String, sa.ForeignKey("conversation_statistic.project_id"), primary_key=True
    )
    intent = sa.Column(sa.String, primary_key=True)
    count = sa.Column(sa.Integer, default=1)

    statistic = relationship("ConversationStatistic", back_populates="intents")


class ConversationActionStatistic(Base):
    """Stores the unique actions which were executed in a conversation."""

    __tablename__ = "conversation_action_statistic"

    project_id = sa.Column(
        sa.String, sa.ForeignKey("conversation_statistic.project_id"), primary_key=True
    )
    action = sa.Column(sa.String, primary_key=True)
    count = sa.Column(sa.Integer, default=1)

    statistic = relationship("ConversationStatistic", back_populates="actions")


class ConversationEntityStatistic(Base):
    """Stores the unique entities which were extracted in a conversation."""

    __tablename__ = "conversation_entity_statistic"

    project_id = sa.Column(
        sa.String, sa.ForeignKey("conversation_statistic.project_id"), primary_key=True
    )
    entity = sa.Column(sa.String, primary_key=True)
    count = sa.Column(sa.Integer, default=1)

    statistic = relationship("ConversationStatistic", back_populates="entities")


class ConversationPolicyStatistic(Base):
    """Stores the unique policies which were used in a conversation."""

    __tablename__ = "conversation_policy_statistic"

    project_id = sa.Column(
        sa.String, sa.ForeignKey("conversation_statistic.project_id"), primary_key=True
    )
    policy = sa.Column(sa.String, primary_key=True)
    count = sa.Column(sa.Integer, default=1)

    statistic = relationship("ConversationStatistic", back_populates="policies")


class ConversationSession(Base):
    """Stores sessions which describe isolated parts of conversations."""

    __tablename__ = "conversation_session"

    conversation_id = sa.Column(
        sa.String, sa.ForeignKey("conversation.sender_id"), primary_key=True
    )
    session_id = sa.Column(sa.Integer, primary_key=True)
    session_start = sa.Column(sa.Float)  # session start time as unix timestamp
    session_length = sa.Column(sa.Float, default=0.0)  # session length in seconds
    latest_event_time = sa.Column(sa.Float)  # latest event time as unix timestamp
    user_messages = sa.Column(sa.Integer, default=0)
    bot_messages = sa.Column(sa.Integer, default=0)
    # use an sa.Integer so we can easily aggregate the information with `count()`
    is_new_user = sa.Column(sa.Integer)
    in_training_data = sa.Column(sa.Boolean, default=True)


class AnalyticsCache(Base):
    """Caches the calculated analytic results for faster loading."""

    __tablename__ = "analytics_cache"

    cache_key = sa.Column(sa.String, primary_key=True)
    includes_platform_users = sa.Column(sa.Boolean, default=False, primary_key=True)
    # caching date as unix timestamp
    timestamp = sa.Column(sa.Float, default=time.time())
    result = sa.Column(sa.Text)

    def as_dict(self) -> Dict[Text, Any]:
        return {
            "key": self.cache_key,
            "includes_platform_users": self.includes_platform_users,
            "timestamp": self.timestamp,
            "result": json.loads(self.result),
        }
