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

Login values are substituted separately and are deliberately not in that list. A recipe
types them with ``{secret.username}``, ``{secret.password}``, ``{secret.totp}`` -- any
field name keyring holds for the service -- and they are only ever substituted into a
``fill`` step's value. Not into the start URL, not into a selector, not into a match
term. A password in a URL is a password in an access log, in a Referer header, and in
somebody's browser history, and the way to make that impossible is to have no code path
that puts one there.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Self
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, model_validator

from media_tool.domain.media import MediaQuery

if TYPE_CHECKING:
    from media_tool.core.keyring.credentials import FormSecrets

SECRET_PLACEHOLDER = re.compile(r"\{secret\.([A-Za-z0-9_]+)\}")
"""How a recipe asks for a stored login value. Field names are keyring's, not ours."""


class RecipeError(ValueError):
    """A recipe file is missing, unreadable, or does not describe a usable flow."""


class MissingSecretError(RecipeError):
    """A recipe asks for a login field that the stored credential does not have."""


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


class Login(BaseModel):
    """Which stored login this recipe needs in order to work."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    service: str = Field(
        min_length=1,
        description=(
            "The service name this login is stored under in keyring. Not necessarily "
            "the recipe's name: several recipes can drive one site, and one login can "
            "serve several recipes."
        ),
    )


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
    login: Login | None = Field(
        default=None,
        description=(
            "The stored login this recipe types in. Required if any step uses a "
            "'{secret.*}' placeholder, and pointless without one."
        ),
    )

    @model_validator(mode="after")
    def _matching_needs_results(self) -> Self:
        if self.match.text_contains and self.result_selector is None:
            msg = "match.text_contains needs a result_selector to match against"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _secrets_are_declared_and_only_typed(self) -> Self:
        """Keep a recipe honest about the credentials it uses and where it puts them.

        Both directions are errors, and both are deployment mistakes worth catching at
        startup rather than halfway through somebody's download: a recipe that types a
        secret without saying whose has nothing to resolve, and one that declares a login
        it never types sends this service to keyring for a credential it will not use.
        """
        typed = any(SECRET_PLACEHOLDER.search(step.value or "") for step in self._fill_steps)
        if typed and self.login is None:
            msg = "a recipe that types a '{secret.*}' value must declare which login it needs"
            raise ValueError(msg)
        if self.login is not None and not typed:
            msg = "a recipe that declares a login must type at least one '{secret.*}' value"
            raise ValueError(msg)

        elsewhere = [self.start_url, *self._non_typed_text()]
        if any(SECRET_PLACEHOLDER.search(text) for text in elsewhere):
            msg = (
                "'{secret.*}' may only appear in the value of a 'fill' step: a login "
                "value in a URL or a selector ends up in logs and history"
            )
            raise ValueError(msg)
        return self

    @property
    def _fill_steps(self) -> list[Step]:
        return [
            step for step in [*self.steps, self.download_trigger] if step.action is StepAction.FILL
        ]

    def _non_typed_text(self) -> list[str]:
        """Every part of the recipe a secret must never reach."""
        steps = [*self.steps, self.download_trigger]
        values = [step.value or "" for step in steps if step.action is not StepAction.FILL]
        return [
            *(step.selector for step in steps),
            *values,
            *self.match.text_contains,
            self.result_selector or "",
        ]

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


def render_secrets(template: str, secrets: FormSecrets) -> str:
    """Substitute ``{secret.*}`` placeholders from a resolved login.

    A separate pass from :func:`render`, taking a separate argument, so that the values
    it handles cannot be substituted anywhere else by accident. Nothing about the result
    is logged: it is a password.

    Raises:
        MissingSecretError: if the recipe asks for a field the stored login does not
            have. Raised before the browser is launched, so the caller is told what is
            missing rather than watching a login form fail.
    """

    def replace(match: re.Match[str]) -> str:
        field = match.group(1)
        if field not in secrets.fields:
            msg = (
                f"the stored login for {secrets.service!r} has no {field!r} field, "
                f"which this recipe needs"
            )
            raise MissingSecretError(msg)
        return secrets.fields[field]

    return SECRET_PLACEHOLDER.sub(replace, template)
