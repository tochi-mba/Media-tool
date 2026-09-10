"""Submitting and tracking download jobs."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Response, status
from fastapi.responses import FileResponse

from media_tool.api.dependencies import ContainerDep
from media_tool.api.schemas.common import Problem
from media_tool.api.schemas.downloads import (
    ArtifactView,
    CreateDownloadJobRequest,
    CreateDownloadJobResponse,
    ItemErrorView,
    ItemLinks,
    ItemResultView,
    JobLinks,
    JobView,
    QueryView,
)
from media_tool.domain.artifacts import DownloadArtifact
from media_tool.domain.errors import InvalidMediaQueryError, JobNotFoundError
from media_tool.domain.jobs import ItemStatus, Job, JobItem
from media_tool.domain.media import MediaQuery

router = APIRouter(prefix="/v1/downloads", tags=["downloads"])

MAX_WAIT_SECONDS = 60.0
PROBLEM_RESPONSES: dict[int | str, dict[str, object]] = {
    status.HTTP_404_NOT_FOUND: {"model": Problem, "description": "No such job."},
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": Problem,
        "description": "The request could not be understood.",
    },
}

JobId = Annotated[str, Path(description="Job identifier returned when the batch was accepted.")]
ItemIndex = Annotated[int, Path(ge=0, description="Position of the item in the submitted list.")]


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="create_download_job",
    summary="Submit media items to download",
    description=(
        "Accepts a list of media items and starts fetching a file for each one in the "
        "background. Returns immediately with a job id; poll `get_download_job` for "
        "progress, or pass its `wait_seconds` parameter to be told once the job is "
        "finished. Items repeated within one request are downloaded once and reported "
        "against every position they were submitted in."
    ),
    response_model=CreateDownloadJobResponse,
    responses=PROBLEM_RESPONSES,
)
async def create_download_job(
    payload: CreateDownloadJobRequest,
    container: ContainerDep,
    response: Response,
) -> CreateDownloadJobResponse:
    """Validate a batch, register it, and start work."""
    settings = container.settings
    if len(payload.items) > settings.max_items_per_request:
        msg = (
            f"a request may contain at most {settings.max_items_per_request} items, "
            f"got {len(payload.items)}"
        )
        raise InvalidMediaQueryError(msg)

    queries = [
        MediaQuery.create(name=item.name, season=item.season, episode=item.episode, year=item.year)
        for item in payload.items
    ]

    job = Job.create(queries=queries, now=container.clock.now())
    await container.jobs.add(job)
    await container.runner.submit(job)

    location = _job_path(job.job_id)
    response.headers["Location"] = location
    response.headers["Retry-After"] = "1"

    return CreateDownloadJobResponse(
        job_id=job.job_id,
        status=job.status.value,
        item_count=job.item_count,
        created_at=job.created_at,
        links=JobLinks(self=location),
    )


@router.get(
    "/{job_id}",
    operation_id="get_download_job",
    summary="Check a download job",
    description=(
        "Reports a job's status and each item's outcome. By default it answers "
        "immediately with whatever the current state is. Pass `wait_seconds` to hold "
        "the request open until the job finishes -- up to 60 seconds -- which avoids "
        "having to poll in a loop. A successful item includes a link to fetch its file."
    ),
    response_model=JobView,
    responses=PROBLEM_RESPONSES,
)
async def get_download_job(
    job_id: JobId,
    container: ContainerDep,
    wait_seconds: Annotated[
        float,
        Query(
            ge=0,
            le=MAX_WAIT_SECONDS,
            description=(
                "Seconds to wait for the job to finish before answering. 0 (the "
                "default) answers straight away."
            ),
        ),
    ] = 0.0,
) -> JobView:
    """Return a job, optionally waiting for it to settle first."""
    ttl = container.settings.job_ttl_seconds
    job = await container.jobs.get(job_id, ttl_seconds=ttl)

    if wait_seconds > 0:
        job = await container.jobs.wait_for_terminal(job_id, timeout=wait_seconds)

    return _job_view(job)


@router.delete(
    "/{job_id}",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="cancel_download_job",
    summary="Cancel a download job",
    description=(
        "Abandons whatever is still outstanding. Items that already finished keep their "
        "outcome -- a file that was captured is still available. Cancelling a job that "
        "has already finished leaves it untouched."
    ),
    response_model=JobView,
    responses=PROBLEM_RESPONSES,
)
async def cancel_download_job(job_id: JobId, container: ContainerDep) -> JobView:
    """Stop a job and report where it got to."""
    job = await container.jobs.get(job_id, ttl_seconds=container.settings.job_ttl_seconds)
    await container.runner.cancel(job)

    return _job_view(job)


@router.get(
    "/{job_id}/items/{index}/file",
    operation_id="fetch_download_file",
    summary="Download a captured file",
    description=(
        "Streams the file captured for one item. Returns 404 if the item produced no "
        "file, and 410 once the retention window has removed it."
    ),
    response_class=FileResponse,
    responses={
        status.HTTP_200_OK: {
            "content": {"application/octet-stream": {}},
            "description": "The captured file.",
        },
        status.HTTP_404_NOT_FOUND: {"model": Problem, "description": "No such job or item."},
        status.HTTP_410_GONE: {"model": Problem, "description": "The file has been purged."},
    },
)
async def fetch_download_file(
    job_id: JobId, index: ItemIndex, container: ContainerDep
) -> FileResponse:
    """Serve one captured artifact."""
    job = await container.jobs.get(job_id, ttl_seconds=container.settings.job_ttl_seconds)
    item = job.item(index)

    if item.artifact is None:
        msg = f"item {index} of job {job_id!r} produced no file ({item.status.value})"
        raise JobNotFoundError(msg)

    path = container.artifacts.locate(job_id=job_id, index=index, filename=item.artifact.filename)

    return FileResponse(
        path,
        media_type=item.artifact.content_type,
        filename=item.artifact.filename,
    )


def _job_path(job_id: str) -> str:
    return f"{router.prefix}/{job_id}"


def _job_view(job: Job) -> JobView:
    return JobView(
        job_id=job.job_id,
        status=job.status.value,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        counts=job.counts(),
        results=[_item_view(job, item) for item in job.items],
    )


def _item_view(job: Job, item: JobItem) -> ItemResultView:
    return ItemResultView(
        index=item.index,
        query=_query_view(item.query),
        status=item.status.value,
        artifact=_artifact_view(item.artifact),
        error=(
            ItemErrorView(code=item.error.code, message=item.error.message)
            if item.error is not None
            else None
        ),
        links=ItemLinks(
            file=(
                f"{_job_path(job.job_id)}/items/{item.index}/file"
                if item.status is ItemStatus.SUCCEEDED
                else None
            )
        ),
    )


def _query_view(query: MediaQuery) -> QueryView:
    return QueryView(
        name=query.name,
        season=query.season,
        episode=query.episode,
        year=query.year,
        kind=query.kind.value,
        key=query.key,
    )


def _artifact_view(artifact: DownloadArtifact | None) -> ArtifactView | None:
    if artifact is None:
        return None

    return ArtifactView(
        filename=artifact.filename,
        size_bytes=artifact.size_bytes,
        content_type=artifact.content_type,
        sha256=artifact.sha256,
        source_url=artifact.source_url,
        duration_seconds=artifact.duration_seconds,
        downloaded_at=artifact.downloaded_at,
    )
