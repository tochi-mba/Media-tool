"""Site recipes.

A recipe describes how to get from a media query to a download on one particular site:
where to start, what to do, how to recognize the right result, and what to click. It is
data, not code, so pointing this service at a new site is a JSON file and not a release.

Placeholders available in ``start_url`` and in step values:

``{query}``
    The full search string, e.g. ``Severance S01E03``.
``{name}``
    Just the title.
``{episode_tag}``
    ``S01E03``, or empty when the item is not an episode.
``{year}``
    The year, or empty when none was given.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Self
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, model_validator

from media_tool.domain.media import MediaQuery


class RecipeError(ValueError):
    """A recipe file is missing, unreadable, or does not describe a usable flow."""


class StepAction(StrEnum):
    """What a step does to the page."""

    CLICK = "click"
    FILL = "fill"
    WAIT_FOR = "wait_for"


class Step(BaseModel):
    """One action performed on the page before the download is triggered."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: StepAction
    selector: str = Field(min_length=1)
    value: str | None = Field(
        default=None, description="Text to type. Required for 'fill', ignored otherwise."
    )

    @model_validator(mode="after")
    def _fill_needs_a_value(self) -> Self:
        if self.action is StepAction.FILL and self.value is None:
            msg = "a 'fill' step needs a value"
            raise ValueError(msg)
        return self


class MatchRule(BaseModel):
    """How to tell the right search result from the others."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text_contains: list[str] = Field(
        default_factory=list,
        description=(
            "Every entry must appear in the result's text, case-insensitively. "
            "Placeholders are substituted first; an entry that resolves to empty is "
            "dropped, so '{episode_tag}' simply does not constrain a film."
        ),
    )


class SiteRecipe(BaseModel):
    """A complete description of one site's download flow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, description="Identifies the recipe in logs and health.")
    start_url: str = Field(min_length=1, description="Where the flow begins. Accepts placeholders.")
    steps: list[Step] = Field(
        default_factory=list, description="Actions to perform after landing, in order."
    )
    result_selector: str | None = Field(
        default=None,
        description=(
            "Selector matching each candidate result. Omit when the start URL already "
            "lands on the item's own page."
        ),
    )
    match: MatchRule = Field(default_factory=MatchRule)
    download_trigger: Step = Field(
        description="The action that starts the download -- the click a human would make."
    )

    @model_validator(mode="after")
    def _matching_needs_results(self) -> Self:
        if self.match.text_contains and self.result_selector is None:
            msg = "match.text_contains needs a result_selector to match against"
            raise ValueError(msg)
        return self

    @classmethod
    def load(cls, path: Path) -> SiteRecipe:
        """Read and validate a recipe file.

        Raises:
            RecipeError: if the file is missing, is not valid JSON, or is not a usable
                recipe. The message names the file, since a bad recipe is a deployment
                mistake someone has to find.
        """
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as error:
            msg = f"cannot read recipe {str(path)!r}: {error}"
            raise RecipeError(msg) from error

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            msg = f"recipe {str(path)!r} is not valid JSON: {error}"
            raise RecipeError(msg) from error

        try:
            return cls.model_validate(payload)
        except ValueError as error:
            msg = f"recipe {str(path)!r} is not usable: {error}"
            raise RecipeError(msg) from error


Placeholders = Annotated[dict[str, str], "placeholder name to replacement"]


def placeholders_for(query: MediaQuery) -> Placeholders:
    """Build the substitutions available to a recipe for one query."""
    return {
        "query": query.search_terms(),
        "name": query.name,
        "episode_tag": query.episode_tag() or "",
        "year": str(query.year) if query.year is not None else "",
    }


def render(template: str, placeholders: Placeholders, *, url_encode: bool = False) -> str:
    """Substitute placeholders in ``template``.

    Unknown placeholders are left untouched rather than raising: a selector may
    legitimately contain braces, and failing a whole download over a stray one would be
    worse than passing it through.
    """
    rendered = template
    for key, value in placeholders.items():
        replacement = quote(value) if url_encode else value
        rendered = rendered.replace(f"{{{key}}}", replacement)
    return rendered
