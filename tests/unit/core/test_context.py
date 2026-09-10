"""The request id travels by context variable so no function has to thread it through."""

from __future__ import annotations

import asyncio

import pytest

from media_tool.core.context import bind_request_id, get_request_id, new_request_id


def test_no_request_id_outside_a_request() -> None:
    assert get_request_id() is None


def test_new_request_id_is_unique() -> None:
    assert new_request_id() != new_request_id()


def test_binding_sets_and_restores() -> None:
    with bind_request_id("abc123") as bound:
        assert bound == "abc123"
        assert get_request_id() == "abc123"

    assert get_request_id() is None


def test_bindings_nest() -> None:
    with bind_request_id("outer"):
        with bind_request_id("inner"):
            assert get_request_id() == "inner"
        assert get_request_id() == "outer"


def test_binding_is_restored_even_when_the_body_raises() -> None:
    def explode() -> None:
        raise RuntimeError

    with pytest.raises(RuntimeError), bind_request_id("boom"):
        explode()

    assert get_request_id() is None


async def test_concurrent_tasks_do_not_leak_ids_into_each_other() -> None:
    seen: dict[str, str | None] = {}

    async def handler(request_id: str) -> None:
        with bind_request_id(request_id):
            await asyncio.sleep(0)
            seen[request_id] = get_request_id()

    await asyncio.gather(handler("one"), handler("two"), handler("three"))

    assert seen == {"one": "one", "two": "two", "three": "three"}
