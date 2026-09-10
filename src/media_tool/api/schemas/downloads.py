"""Wire models for the download API.

Field descriptions and examples here are not decoration: they are what a client -- and,
once this is fronted by an MCP server, a model deciding whether to call the tool -- reads
to understand the contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from media_tool.domain.media import EARLIEST_FILM_YEAR, MAX_NAME_LENGTH

MAX_SEASON = 999
MAX_EPISODE = 99_999
MAX_YEAR = 2999


class MediaItemRequest(BaseModel):
    """One piece of media to fetch."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"name": "Severance", "season": 1, "episode": 3},
                {"name": "Dune", "year": 2021},
            ]
        },
    )

    name: Annotated[str, Field(min_length=1, max_length=MAX_NAME_LENGTH)] = Field(
        description="Title of the film or series. Required.",
    )
    season: Annotated[int, Field(ge=0, le=MAX_SEASON)] | None = Field(
        default=None,
        description="Season number. Zero is valid and means specials.",
    )
    episode: Annotated[int, Field(ge=0, le=MAX_EPISODE)] | None = Field(
        default=None,
        description=(
            "Episode number. May be given without a season for absolutely numbered "
            "series such as anime."
        ),
    )
    year: Annotated[int, Field(ge=EARLIEST_FILM_YEAR, le=MAX_YEAR)] | None = Field(
        default=None,
        description="Release year. Use for films; for series prefer season and episode.",
    )


class CreateDownloadJobRequest(BaseModel):
    """A batch of items to fetch."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "items": [
                        {"name": "Severance", "season": 1, "episode": 3},
                        {"name": "Dune", "year": 2021},
                    ],
                    "profile": "default",
                }
            ]
        },
    )

    items: list[MediaItemRequest] = Field(
        min_length=1,
        description=(
            "Items to fetch. Duplicates are downloaded once and the result is reported "
            "against every position they were submitted in."
        ),
    )
    profile: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description=(
            "Which of your stored credential profiles to use if the site needs a login. "
            "Names a profile you hold in keyring; omit it to use your default one. It "
            "never names an account -- whose credentials these are comes from your "
            "token, so this cannot be used to reach somebody else's."
        ),
    )


class QueryView(BaseModel):
    """How the service understood one submitted item."""

    name: str
    season: int | None = None
    episode: int | None = None
    year: int | None = None
    kind: str = Field(description="'series', 'movie', or 'unknown', inferred from the fields.")
    key: str = Field(description="Canonical identity used to deduplicate within a request.")


class ArtifactView(BaseModel):
    """The file that was captured."""

    filename: str
    size_bytes: int
    content_type: str
    sha256: str = Field(description="Digest of the stored bytes.")
    source_url: str = Field(description="Where the file was actually fetched from.")
    duration_seconds: float
    downloaded_at: datetime


class ItemErrorView(BaseModel):
    """Why one item produced no file."""

    code: str = Field(
        description=(
            "Stable reason: 'not_found', 'timeout', 'provider_unavailable', or "
            "'internal_error'. Safe to branch on."
        )
    )
    message: str


class ItemLinks(BaseModel):
    """Where to fetch what an item produced."""

    file: str | None = Field(default=None, description="Path to download the captured file.")


class ItemResultView(BaseModel):
    """One item's outcome."""

    index: int = Field(description="Position in the submitted list.")
    query: QueryView
    status: str = Field(description="'pending', 'succeeded', 'failed', or 'cancelled'.")
    artifact: ArtifactView | None = None
    error: ItemErrorView | None = None
    links: ItemLinks = Field(default_factory=ItemLinks)


class JobLinks(BaseModel):
    self: str


class CreateDownloadJobResponse(BaseModel):
    """Acknowledgement that a batch was accepted."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "job_id": "6f1c2d3e4a5b6c7d8e9f0a1b2c3d4e5f",
                    "status": "queued",
                    "item_count": 2,
                    "created_at": "2026-09-10T12:00:00Z",
                    "links": {"self": "/v1/downloads/6f1c2d3e4a5b6c7d8e9f0a1b2c3d4e5f"},
                }
            ]
        }
    )

    job_id: str
    status: str
    item_count: int
    created_at: datetime
    links: JobLinks


class JobView(BaseModel):
    """A job's current state and every item's outcome."""

    job_id: str
    status: str = Field(
        description=(
            "'queued', 'running', 'succeeded', 'partial', 'failed', or 'cancelled'. "
            "'partial' means some items produced files and some did not."
        )
    )
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    counts: dict[str, int] = Field(description="Item tallies; 'total' is always present.")
    results: list[ItemResultView]
