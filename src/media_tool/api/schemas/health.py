"""The health payload."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CheckResult(BaseModel):
    """One dependency's contribution to overall health."""

    status: str = Field(description="'ok' or 'degraded'.")
    detail: dict[str, object] = Field(
        default_factory=dict, description="Check-specific facts, such as free bytes."
    )


class HealthResponse(BaseModel):
    """What ``GET /healthy`` returns.

    Reported at both levels deliberately: the top-level status is what a load balancer
    reads, the per-check detail is what a human reads at three in the morning.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "status": "ok",
                    "version": "0.1.0",
                    "environment": "local",
                    "uptime_seconds": 12.34,
                    "checks": {
                        "job_store": {"status": "ok", "detail": {"jobs": 3}},
                        "provider": {"status": "ok", "detail": {"name": "stub"}},
                        "storage": {
                            "status": "ok",
                            "detail": {"writable": True, "free_bytes": 12345678},
                        },
                    },
                }
            ]
        }
    )

    status: str = Field(description="'ok' when every check passed, otherwise 'degraded'.")
    version: str = Field(description="Running version of the service.")
    environment: str = Field(description="Which deployment this is.")
    uptime_seconds: float = Field(description="Seconds since the process started serving.")
    checks: dict[str, CheckResult] = Field(description="Per-dependency results.")
