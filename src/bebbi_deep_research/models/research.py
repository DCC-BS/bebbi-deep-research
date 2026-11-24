"""Request models of the chat-turn and job endpoints."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from bebbi_deep_research.models.own_sources import OwnSourcesRequest


class RunWhen(StrEnum):
    """When a job may start: at once (daytime rule applies) or in the night window."""

    NOW = "now"
    NIGHT = "night"


class ClarifyRequest(BaseModel):
    """A research task for which clarifying questions are wanted."""

    model_config = ConfigDict(extra="forbid")  # an unknown field is a contract mismatch, not something to ignore

    task: str = Field(max_length=20_000)
    preset: str = Field("verwaltung_web", max_length=300)  # a preset of the file or a mixed one (mix:...)
    depth: str = Field("standard", max_length=100)
    user_id: str | None = Field(None, max_length=200)
    own_sources: OwnSourcesRequest | None = None
    # One Deep Research model: the answer also suggests the preset ("preset", or None), which the user confirms.
    suggest_preset: bool = False


class CreatePlanRequest(ClarifyRequest):
    """A research task to plan; `user_id` becomes the owner of the draft (checked on edit and submit)."""


class EditPlanRequest(BaseModel):
    """A change to a draft plan, in the user's words."""

    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(max_length=10_000)
    user_id: str | None = Field(None, max_length=200)


class SubmitJobRequest(BaseModel):
    """Start the research of an approved draft and deliver the report into a chat message."""

    model_config = ConfigDict(extra="forbid")

    draft_id: str = Field(max_length=100)
    user_id: str = Field(max_length=200)
    chat_id: str | None = Field(None, max_length=200)
    message_id: str | None = Field(None, max_length=200)
    when: RunWhen = RunWhen.NIGHT
    own_sources: OwnSourcesRequest | None = None


class SelectionRequest(BaseModel):
    """What the user picked in the Deep Research card: sources (step 1) and report style (step 2)."""

    model_config = ConfigDict(extra="forbid")

    web: bool = True
    mcp: list[str] = Field(default_factory=list, max_length=20)  # MCP servers of `choices.sources`
    own: str = Field("prefer", pattern="^(prefer|only|off)$")  # the user's files and knowledge bases
    style: str = Field(max_length=100)
    own_attached: bool = True  # the chat has files or knowledge bases (else the line does not mention them)
