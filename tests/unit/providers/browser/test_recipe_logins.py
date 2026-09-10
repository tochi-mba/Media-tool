"""Recipes that type a stored login.

Two rules, and both are about a recipe being honest. It must say which login it uses, so
this service knows what to resolve. And it may only ever type one into a form field --
never into a URL, a selector, or a match term, because those are logged.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from media_tool.core.keyring.credentials import FormSecrets
from media_tool.providers.browser.recipes import (
    MissingSecretError,
    SiteRecipe,
    render_secrets,
)

SECRETS = FormSecrets(
    service="somesite", fields={"username": "eve", "password": "hunter2", "totp": "123456"}
)

TRIGGER = {"action": "click", "selector": "a.download"}


def recipe(**overrides: object) -> SiteRecipe:
    base: dict[str, object] = {
        "name": "example",
        "start_url": "https://example.test/{query}",
        "download_trigger": TRIGGER,
    }
    return SiteRecipe.model_validate({**base, **overrides})


def login_steps() -> list[dict[str, object]]:
    return [
        {"action": "fill", "selector": "#user", "value": "{secret.username}"},
        {"action": "fill", "selector": "#pass", "value": "{secret.password}"},
        {"action": "click", "selector": "button[type=submit]"},
    ]


class TestDeclaringALogin:
    def test_a_recipe_that_types_a_secret_must_say_whose(self) -> None:
        # There is nothing to resolve otherwise, and the download would fail at the
        # login form rather than at load.
        with pytest.raises(ValidationError, match="must declare which login"):
            recipe(steps=login_steps())

    def test_a_declared_login_makes_it_valid(self) -> None:
        made = recipe(steps=login_steps(), login={"service": "somesite"})

        assert made.login is not None
        assert made.login.service == "somesite"

    def test_a_login_that_is_never_typed_is_rejected(self) -> None:
        # It would send this service to keyring for a credential it does not use, which
        # is a needless call and a needless failure mode.
        with pytest.raises(ValidationError, match="must type at least one"):
            recipe(login={"service": "somesite"})

    def test_a_recipe_with_no_login_is_still_valid(self) -> None:
        # Most sites need none, and that must stay the easy case.
        assert recipe().login is None


class TestWhereASecretMayGo:
    def test_not_in_the_start_url(self) -> None:
        # A password in a URL is a password in an access log, a Referer header, and
        # somebody's history.
        with pytest.raises(ValidationError, match="only appear in the value"):
            recipe(
                start_url="https://example.test/?pw={secret.password}",
                steps=login_steps(),
                login={"service": "somesite"},
            )

    def test_not_in_a_selector(self) -> None:
        with pytest.raises(ValidationError, match="only appear in the value"):
            recipe(
                steps=[
                    *login_steps(),
                    {"action": "click", "selector": "#{secret.username}"},
                ],
                login={"service": "somesite"},
            )

    def test_not_in_a_match_term(self) -> None:
        with pytest.raises(ValidationError, match="only appear in the value"):
            recipe(
                steps=login_steps(),
                result_selector=".result",
                match={"text_contains": ["{secret.username}"]},
                login={"service": "somesite"},
            )

    def test_not_in_the_download_trigger_selector(self) -> None:
        with pytest.raises(ValidationError, match="only appear in the value"):
            recipe(
                steps=login_steps(),
                download_trigger={"action": "click", "selector": "a[data={secret.password}]"},
                login={"service": "somesite"},
            )

    def test_yes_in_a_fill_value(self) -> None:
        assert recipe(steps=login_steps(), login={"service": "somesite"}).steps[0].value == (
            "{secret.username}"
        )


class TestSubstitution:
    def test_each_field_is_substituted_by_name(self) -> None:
        assert render_secrets("{secret.username}", SECRETS) == "eve"
        assert render_secrets("{secret.password}", SECRETS) == "hunter2"

    def test_a_value_may_mix_a_secret_with_ordinary_text(self) -> None:
        assert render_secrets("user:{secret.username}", SECRETS) == "user:eve"

    def test_any_field_keyring_holds_can_be_typed(self) -> None:
        # Keyring decides what a login has -- a TOTP code, an account number. Fixing the
        # set here would mean a release every time somebody stored something new.
        assert render_secrets("{secret.totp}", SECRETS) == "123456"

    def test_a_field_the_login_does_not_have_is_refused(self) -> None:
        with pytest.raises(MissingSecretError, match="pin"):
            render_secrets("{secret.pin}", SECRETS)

    def test_the_refusal_names_the_service_and_the_field(self) -> None:
        # Both halves are needed to fix it: which stored login, and what to add to it.
        with pytest.raises(MissingSecretError) as caught:
            render_secrets("{secret.pin}", SECRETS)

        assert "somesite" in str(caught.value)
        assert "pin" in str(caught.value)

    def test_text_with_no_placeholder_is_untouched(self) -> None:
        assert render_secrets("plain", SECRETS) == "plain"

    def test_an_ordinary_placeholder_is_left_for_the_other_pass(self) -> None:
        # The two substitutions are separate on purpose, and neither does the other's
        # job by accident.
        assert render_secrets("{query}", SECRETS) == "{query}"
