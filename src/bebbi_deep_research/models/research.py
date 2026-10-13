"""Request models of the chat-turn and job endpoints."""

from enum import StrEnum

from pydantic import BaseModel, Field

from bebbi_deep_research.models.own_sources import OwnSourcesRequest


class RunWhen(StrEnum):
    """When a job may start: at once (daytime rule applies) or in the night window."""

    NOW = "now"
    NIGHT = "night"


class ClarifyRequest(BaseModel):
    """A research task for which clarifying questions are wanted."""

    task: str = Field(max_length=20_000)
    preset: str = Field("verwaltung_web", max_length=100)
    depth: str = Field("standard", max_length=100)
    user_id: str | None = Field(None, max_length=200)
    own_sources: OwnSourcesRequest | None = None


class CreatePlanRequest(ClarifyRequest):
    """A research task to plan; `user_id` becomes the owner of the draft (checked on edit and submit)."""


class EditPlanRequest(BaseModel):
    """A change to a draft plan, in the user's words."""

    instruction: str = Field(max_length=10_000)
    user_id: str | None = Field(None, max_length=200)


class SubmitJobRequest(BaseModel):
    """Start the research of an approved draft and deliver the report into a chat message."""

    draft_id: str = Field(max_length=100)
    user_id: str = Field(max_length=200)
    chat_id: str | None = Field(None, max_length=200)
    message_id: str | None = Field(None, max_length=200)
    when: RunWhen = RunWhen.NIGHT
    own_sources: OwnSourcesRequest | None = None
