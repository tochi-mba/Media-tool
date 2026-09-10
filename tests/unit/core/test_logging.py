"""Logs are machine-readable in deployment and human-readable locally."""

from __future__ import annotations

import json

import pytest

from media_tool.core.config import LogFormat
from media_tool.core.context import bind_account, bind_request_id
from media_tool.core.logging import (
    add_account,
    add_request_id,
    configure_logging,
    get_logger,
)
from media_tool.domain.accounts import AccountId


@pytest.fixture(autouse=True)
def _reset_logging() -> None:
    configure_logging(level="INFO", log_format=LogFormat.JSON)


def test_json_output_is_parseable(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", log_format=LogFormat.JSON)

    get_logger("test").info("job_started", job_id="abc", items=3)

    record = json.loads(capsys.readouterr().out)
    assert record["event"] == "job_started"
    assert record["job_id"] == "abc"
    assert record["items"] == 3
    assert record["level"] == "info"
    assert "timestamp" in record


def test_console_output_is_not_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", log_format=LogFormat.CONSOLE)

    get_logger("test").info("job_started")

    out = capsys.readouterr().out
    assert "job_started" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_level_filtering_drops_quieter_records(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="WARNING", log_format=LogFormat.JSON)

    logger = get_logger("test")
    logger.info("ignored")
    logger.warning("kept")

    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "kept"


def test_request_id_is_attached_to_every_record(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", log_format=LogFormat.JSON)

    with bind_request_id("req-42"):
        get_logger("test").info("handled")

    assert json.loads(capsys.readouterr().out)["request_id"] == "req-42"


def test_request_id_is_omitted_outside_a_request(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", log_format=LogFormat.JSON)

    get_logger("test").info("startup")

    assert "request_id" not in json.loads(capsys.readouterr().out)


def test_add_request_id_processor_is_a_no_op_without_a_binding() -> None:
    assert add_request_id(None, "info", {"event": "x"}) == {"event": "x"}


def test_add_request_id_processor_injects_when_bound() -> None:
    with bind_request_id("r1"):
        assert add_request_id(None, "info", {"event": "x"}) == {"event": "x", "request_id": "r1"}


def test_the_account_is_attached_to_every_record(capsys: pytest.CaptureFixture[str]) -> None:
    # Whose request this was, on every record it produced -- including the ones the job
    # runner emits later, which inherit the binding with the task.
    configure_logging(level="INFO", log_format=LogFormat.JSON)

    with bind_account(AccountId.parse("acct_alice")):
        get_logger("test").info("download_started")

    assert json.loads(capsys.readouterr().out)["account"] == "acct_alice"


def test_records_outside_a_request_have_no_account_key() -> None:
    # Absent rather than present-and-null, so a query can filter on existence -- the same
    # rule the request id follows.
    assert add_account(None, "info", {"event": "startup"}) == {"event": "startup"}
