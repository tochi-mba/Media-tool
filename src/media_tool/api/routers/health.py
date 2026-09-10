"""Liveness and dependency health."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from media_tool.api.dependencies import ContainerDep
from media_tool.api.schemas.health import CheckResult, HealthResponse
from media_tool.core.version import service_version

router = APIRouter(tags=["health"])

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"


@router.get(
    "/healthy",
    operation_id="get_health",
    summary="Report service health",
    description=(
        "Returns the service version, uptime, and the state of every dependency: "
        "identity, the job store, the download provider, and artifact storage. "
        "Responds 200 when everything is usable and 503 when any check fails, with the "
        "same body shape either way."
    ),
    response_model=HealthResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthResponse}},
)
async def get_health(container: ContainerDep, response: Response) -> HealthResponse:
    """Check every dependency and summarize."""
    storage = container.artifacts.health()
    provider_ready = await container.provider.healthy()
    identity_ready = await container.authenticator.healthy()

    checks = {
        "identity": CheckResult(
            # Degraded, not failed, is not a distinction this endpoint makes: an
            # instance that cannot say who anybody is cannot serve a single /v1 request,
            # so it should be taken out of rotation rather than left to answer 503s one
            # at a time.
            status=STATUS_OK if identity_ready else STATUS_DEGRADED,
            detail={"mode": container.authenticator.describes, "ready": identity_ready},
        ),
        "job_store": CheckResult(
            status=STATUS_OK,
            detail={"jobs": await container.jobs.count()},
        ),
        "provider": CheckResult(
            status=STATUS_OK if provider_ready else STATUS_DEGRADED,
            detail={"name": container.provider.name, "ready": provider_ready},
        ),
        "storage": CheckResult(
            status=STATUS_OK if storage.writable else STATUS_DEGRADED,
            detail={"writable": storage.writable, "free_bytes": storage.free_bytes},
        ),
    }

    healthy = all(check.status == STATUS_OK for check in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status=STATUS_OK if healthy else STATUS_DEGRADED,
        version=service_version(),
        environment=container.settings.environment,
        uptime_seconds=round(container.uptime_seconds, 3),
        checks=checks,
    )
