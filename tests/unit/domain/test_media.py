"""MediaQuery is the vocabulary the whole service speaks. Its rules are pinned here."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from media_tool.domain.errors import InvalidMediaQueryError
from media_tool.domain.media import EARLIEST_FILM_YEAR, MediaKind, MediaQuery


class TestNormalization:
    def test_surrounding_whitespace_is_stripped(self) -> None:
        assert MediaQuery.create(name="  Severance  ").name == "Severance"

    def test_internal_whitespace_is_collapsed(self) -> None:
        assert MediaQuery.create(name="The\t Wire\n\n2").name == "The Wire 2"

    def test_empty_names_are_rejected(self) -> None:
        with pytest.raises(InvalidMediaQueryError, match="name"):
            MediaQuery.create(name="   ")

    def test_overlong_names_are_rejected(self) -> None:
        with pytest.raises(InvalidMediaQueryError, match="200"):
            MediaQuery.create(name="x" * 201)

    def test_a_name_of_exactly_the_limit_is_accepted(self) -> None:
        assert MediaQuery.create(name="x" * 200).name == "x" * 200

    def test_control_characters_are_rejected(self) -> None:
        with pytest.raises(InvalidMediaQueryError):
            MediaQuery.create(name="Sever\x00ance")


class TestKindInference:
    def test_a_season_means_a_series(self) -> None:
        assert MediaQuery.create(name="Severance", season=1).kind is MediaKind.SERIES

    def test_an_episode_alone_means_a_series(self) -> None:
        # Anime is routinely numbered absolutely, with no season at all.
        assert MediaQuery.create(name="One Piece", episode=1071).kind is MediaKind.SERIES

    def test_a_year_alone_means_a_film(self) -> None:
        assert MediaQuery.create(name="Dune", year=2021).kind is MediaKind.MOVIE

    def test_a_bare_name_is_unknown(self) -> None:
        assert MediaQuery.create(name="Dune").kind is MediaKind.UNKNOWN

    def test_a_season_and_a_year_together_still_mean_a_series(self) -> None:
        assert MediaQuery.create(name="Fargo", season=2, year=2015).kind is MediaKind.SERIES


class TestBounds:
    def test_season_zero_is_valid_because_specials_exist(self) -> None:
        assert MediaQuery.create(name="Doctor Who", season=0).season == 0

    def test_negative_seasons_are_rejected(self) -> None:
        with pytest.raises(InvalidMediaQueryError, match="season"):
            MediaQuery.create(name="Severance", season=-1)

    def test_negative_episodes_are_rejected(self) -> None:
        with pytest.raises(InvalidMediaQueryError, match="episode"):
            MediaQuery.create(name="Severance", episode=-1)

    def test_years_before_the_first_film_are_rejected(self) -> None:
        with pytest.raises(InvalidMediaQueryError, match="year"):
            MediaQuery.create(name="Nope", year=EARLIEST_FILM_YEAR - 1)

    def test_years_far_in_the_future_are_rejected(self) -> None:
        far_future = datetime.now(UTC).year + 6
        with pytest.raises(InvalidMediaQueryError, match="year"):
            MediaQuery.create(name="Nope", year=far_future)

    def test_announced_titles_a_few_years_out_are_accepted(self) -> None:
        soon = datetime.now(UTC).year + 2
        assert MediaQuery.create(name="Dune Part Three", year=soon).year == soon


class TestEpisodeTag:
    @pytest.mark.parametrize(
        ("season", "episode", "expected"),
        [
            (1, 3, "S01E03"),
            (0, 1, "S00E01"),
            (12, 145, "S12E145"),
            (2, None, "S02"),
            (None, 1071, "E1071"),
            (None, None, None),
        ],
    )
    def test_tag_formatting(self, season: int | None, episode: int | None, expected: str) -> None:
        query = MediaQuery.create(name="Show", season=season, episode=episode)

        assert query.episode_tag() == expected


class TestSearchTerms:
    def test_a_series_search_includes_the_episode_tag(self) -> None:
        query = MediaQuery.create(name="Severance", season=1, episode=3)

        assert query.search_terms() == "Severance S01E03"

    def test_a_film_search_includes_the_year(self) -> None:
        assert MediaQuery.create(name="Dune", year=2021).search_terms() == "Dune 2021"

    def test_a_bare_name_searches_for_just_the_name(self) -> None:
        assert MediaQuery.create(name="Dune").search_terms() == "Dune"

    def test_a_series_with_a_year_prefers_the_episode_tag(self) -> None:
        query = MediaQuery.create(name="Fargo", season=2, year=2015)

        assert query.search_terms() == "Fargo S02"


class TestCanonicalKey:
    def test_series_key(self) -> None:
        query = MediaQuery.create(name="Severance", season=1, episode=3)

        assert query.key == "series:severance:s1:e3"

    def test_movie_key(self) -> None:
        assert MediaQuery.create(name="Dune", year=2021).key == "movie:dune:2021"

    def test_unknown_key(self) -> None:
        assert MediaQuery.create(name="Dune").key == "unknown:dune"

    def test_the_key_folds_case_and_spacing(self) -> None:
        left = MediaQuery.create(name="  the   WIRE ", season=1)
        right = MediaQuery.create(name="The Wire", season=1)

        assert left.key == right.key

    def test_different_episodes_get_different_keys(self) -> None:
        first = MediaQuery.create(name="Severance", season=1, episode=3)
        second = MediaQuery.create(name="Severance", season=1, episode=4)

        assert first.key != second.key

    def test_a_year_does_not_leak_into_a_series_key(self) -> None:
        # Otherwise the same episode submitted with and without a year would be
        # downloaded twice.
        with_year = MediaQuery.create(name="Fargo", season=2, year=2015)
        without_year = MediaQuery.create(name="Fargo", season=2)

        assert with_year.key == without_year.key


class TestValueSemantics:
    def test_queries_are_frozen(self) -> None:
        query = MediaQuery.create(name="Dune")

        with pytest.raises(AttributeError):
            query.name = "Other"  # type: ignore[misc]

    def test_equal_inputs_produce_equal_queries(self) -> None:
        assert MediaQuery.create(name="Dune", year=2021) == MediaQuery.create(
            name="Dune", year=2021
        )

    def test_queries_are_hashable_so_they_can_deduplicate(self) -> None:
        queries = {
            MediaQuery.create(name="Dune", year=2021),
            MediaQuery.create(name=" dune ", year=2021),
        }

        assert len(queries) == 2


names = st.text(min_size=1, max_size=200).filter(lambda s: s.strip() and s.isprintable())

# Case folding is not round-trip stable across all of Unicode -- "\u0131".upper() is "I",
# which casefolds to "i" rather than back to "\u0131". The key folds case, it does not
# perform Unicode case *normalization*, so the identity property is stated over an
# alphabet where upper() is reversible.
case_stable_names = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), max_codepoint=0x7F),
    min_size=1,
    max_size=200,
).filter(lambda s: s.strip())
seasons = st.one_of(st.none(), st.integers(min_value=0, max_value=99))
episodes = st.one_of(st.none(), st.integers(min_value=0, max_value=9999))
years = st.one_of(st.none(), st.integers(min_value=EARLIEST_FILM_YEAR, max_value=2030))


class TestProperties:
    @given(name=names, season=seasons, episode=episodes, year=years)
    def test_any_accepted_input_produces_a_usable_query(
        self, name: str, season: int | None, episode: int | None, year: int | None
    ) -> None:
        query = MediaQuery.create(name=name, season=season, episode=episode, year=year)

        assert query.name.strip() == query.name
        assert query.key
        assert query.search_terms().startswith(query.name)

    @given(name=names, season=seasons, episode=episodes, year=years)
    def test_normalization_is_idempotent(
        self, name: str, season: int | None, episode: int | None, year: int | None
    ) -> None:
        once = MediaQuery.create(name=name, season=season, episode=episode, year=year)
        twice = MediaQuery.create(
            name=once.name, season=once.season, episode=once.episode, year=once.year
        )

        assert once == twice

    @given(name=case_stable_names, season=seasons, episode=episodes, year=years)
    def test_the_key_determines_identity(
        self, name: str, season: int | None, episode: int | None, year: int | None
    ) -> None:
        query = MediaQuery.create(name=name, season=season, episode=episode, year=year)
        same = MediaQuery.create(name=name.upper(), season=season, episode=episode, year=query.year)

        assert query.key == same.key
