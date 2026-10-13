"""Request models for the user's own sources (uploaded files and knowledge bases, requirement A4)."""

import os

from pydantic import BaseModel, Field

try:
    # request size limit for one file text: twice RA_OWN_MAX_FILE_CHARS, so that longer files are still cut with a note
    MAX_TEXT = 2 * int(os.environ.get("RA_OWN_MAX_FILE_CHARS") or 2_000_000)
except ValueError:
    MAX_TEXT = 4_000_000
PREVIEW_CHARS = 1500  # what the plan step keeps of a file (the job gets the whole text)


class OwnFileRequest(BaseModel):
    """An uploaded file the chat user may read: its extracted text and where it can be opened."""

    id: str = Field(max_length=200)
    name: str = Field(max_length=500)
    text: str = Field("", max_length=MAX_TEXT)
    chars: int | None = None  # length of the full text when `text` is only a preview
    date: str = Field("", max_length=40)  # file date, YYYY-MM-DD
    url: str = Field(
        "", max_length=2000
    )  # link the chat user can open (Open WebUI file route); empty = cite by name only


class OwnCollectionRequest(BaseModel):
    """A knowledge base the chat user may read."""

    id: str = Field(max_length=200)
    name: str = Field("", max_length=500)


class OwnSourcesRequest(BaseModel):
    """The user's own sources as the pipe checked them (files, knowledge bases, remarks)."""

    files: list[OwnFileRequest] = Field(default_factory=list, max_length=50)
    collections: list[OwnCollectionRequest] = Field(default_factory=list, max_length=50)
    base_url: str = Field(
        "", max_length=2000
    )  # public base URL of Open WebUI, for links to the files of knowledge base hits
    notes: list[str] = Field(
        default_factory=list, max_length=50
    )  # remarks of the pipe (e.g. files the user may not read)
