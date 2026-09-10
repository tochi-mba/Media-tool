"""Site recipes: validation, loading, and placeholder rendering."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import pytest

from media_tool.domain.media import MediaQuery
from media_tool.providers.browser.recipes import (
    RecipeError,
    SiteRecipe,
    Step,
    StepAction,
    placeholders_for,
    render,
)

if TYPE_CHECKING:
    from pathlib import Path

MINIMAL = {
    "name": "example",
    "start_url": "https://example.test/search?q={query}",
    "download_trigger": {"action": "click", "selector": "a.download"},
}


def write_recipe(tmp_path: Path, payload: object, name: str = "recipe.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestValidation:
    def test_a_minimal_recipe_is_valid(self) -> None:
        recipe = SiteRecipe.model_validate(MINIMAL)

        assert recipe.name == "example"
        assert recipe.download_trigger.action is StepAction.CLICK

    def test_a_fill_step_requires_a_value(self) -> None:
        with pytest.raises(ValueError, match="needs a value"):
            Step(action=StepAction.FILL, selector="#q")

    def test_a_click_step_needs_no_value(self) -> None:
        assert Step(action=StepAction.CLICK, selector="a").value is None

    def test_matching_without_results_is_rejected(self) -> None:
        # Otherwise the rule would silently never apply.
        with pytest.raises(ValueError, match="result_selector"):
            SiteRecipe.model_validate({**MINIMAL, "match": {"text_contains": ["{name}"]}})

    def test_unknown_keys_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="typo_here"):
            SiteRecipe.model_validate({**MINIMAL, "typo_here": True})

    def test_an_unknown_action_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="action"):
            SiteRecipe.model_validate(
                {**MINIMAL, "download_trigger": {"action": "hover", "selector": "a"}}
            )

    def test_a_recipe_is_immutable(self) -> None:
        recipe = SiteRecipe.model_validate(MINIMAL)

        with pytest.raises(ValueError, match="frozen"):
            recipe.name = "other"


class TestLoading:
    def test_a_valid_file_loads(self, tmp_path: Path) -> None:
        path = write_recipe(tmp_path, MINIMAL)

        assert SiteRecipe.load(path).name == "example"

    def test_a_missing_file_names_itself(self, tmp_path: Path) -> None:
        with pytest.raises(RecipeError, match=re.escape("absent.json")):
            SiteRecipe.load(tmp_path / "absent.json")

    def test_malformed_json_names_itself(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")

        with pytest.raises(RecipeError, match="not valid JSON"):
            SiteRecipe.load(path)

    def test_a_structurally_invalid_recipe_names_itself(self, tmp_path: Path) -> None:
        path = write_recipe(tmp_path, {"name": "x"})

        with pytest.raises(RecipeError, match="not usable"):
            SiteRecipe.load(path)

    def test_the_shipped_example_recipe_is_valid(self) -> None:
        # It is the worked example the documentation points at, so it must actually work.
        from pathlib import Path as _Path

        recipe = SiteRecipe.load(_Path("recipes/example.json"))

        assert recipe.result_selector == ".result"


class TestPlaceholders:
    def test_a_series_supplies_every_placeholder(self) -> None:
        query = MediaQuery.create(name="Severance", season=1, episode=3)

        assert placeholders_for(query) == {
            "query": "Severance S01E03",
            "name": "Severance",
            "episode_tag": "S01E03",
            "year": "",
        }

    def test_a_film_has_no_episode_tag(self) -> None:
        placeholders = placeholders_for(MediaQuery.create(name="Dune", year=2021))

        assert placeholders["episode_tag"] == ""
        assert placeholders["year"] == "2021"

    def test_rendering_substitutes_every_placeholder(self) -> None:
        placeholders = placeholders_for(MediaQuery.create(name="Dune", year=2021))

        assert render("{name} ({year})", placeholders) == "Dune (2021)"

    def test_url_rendering_encodes_the_value(self) -> None:
        placeholders = placeholders_for(MediaQuery.create(name="The Wire", season=1))

        rendered = render("https://x.test/s?q={query}", placeholders, url_encode=True)

        assert rendered == "https://x.test/s?q=The%20Wire%20S01"

    def test_an_unknown_placeholder_is_left_alone(self) -> None:
        # A selector may legitimately contain braces; failing the whole download over
        # one would be worse than passing it through.
        placeholders = placeholders_for(MediaQuery.create(name="Dune"))

        assert render("div:nth-child({n})", placeholders) == "div:nth-child({n})"
