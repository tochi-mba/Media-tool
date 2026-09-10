"""What the caller asked for.

A :class:`MediaQuery` is the normalized, validated form of one item in a request. It is
the only representation of "what to fetch" that travels past the API boundary, and it
carries everything the browser adapter needs to go looking: a clean title, a
search string, and a canonical key for deduplication.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from media_tool.domain.errors import InvalidMediaQueryError

EARLIEST_FILM_YEAR = 1878
"""Roughly Muybridge's galloping horse. Anything earlier is a typo, not a film."""

YEARS_OF_LOOKAHEAD = 5
"""Titles are announced years ahead, so the ceiling sits above the current year."""

MAX_NAME_LENGTH = 200


class MediaKind(StrEnum):
    """What sort of thing was asked for, inferred from which fields were supplied."""

    SERIES = "series"
    MOVIE = "movie"
    UNKNOWN = "unknown"
    """Only a name was given. The provider decides what to do with that."""


def _year_ceiling() -> int:
    """Highest acceptable year.

    This is the one place the domain reads the wall clock. It is a validation bound
    rather than orchestration timing, so it does not take an injected clock -- every
    component whose *behaviour* depends on time does.
    """
    return datetime.now(UTC).year + YEARS_OF_LOOKAHEAD


@dataclass(frozen=True, slots=True)
class MediaQuery:
    """A single normalized request for one piece of media.

    Build these with :meth:`create`, which is where normalization and validation live;
    constructing one directly bypasses both.
    """

    name: str
    """Display title, whitespace-normalized but with its original casing intact."""

    season: int | None = None
    episode: int | None = None
    year: int | None = None

    @classmethod
    def create(
        cls,
        *,
        name: str,
        season: int | None = None,
        episode: int | None = None,
        year: int | None = None,
    ) -> MediaQuery:
        """Normalize and validate raw input.

        Raises:
            InvalidMediaQueryError: if the name is empty, too long, or contains control
                characters, or if a number falls outside its plausible range.
        """
        clean_name = " ".join(name.split())

        if not clean_name:
            msg = "name must not be empty"
            raise InvalidMediaQueryError(msg)
        if len(clean_name) > MAX_NAME_LENGTH:
            msg = f"name must be at most {MAX_NAME_LENGTH} characters, got {len(clean_name)}"
            raise InvalidMediaQueryError(msg)
        if not clean_name.isprintable():
            msg = "name must not contain control characters"
            raise InvalidMediaQueryError(msg)

        if season is not None and season < 0:
            msg = f"season must not be negative, got {season}"
            raise InvalidMediaQueryError(msg)
        if episode is not None and episode < 0:
            msg = f"episode must not be negative, got {episode}"
            raise InvalidMediaQueryError(msg)
        if year is not None and not EARLIEST_FILM_YEAR <= year <= _year_ceiling():
            msg = f"year must be between {EARLIEST_FILM_YEAR} and {_year_ceiling()}, got {year}"
            raise InvalidMediaQueryError(msg)

        return cls(name=clean_name, season=season, episode=episode, year=year)

    @property
    def kind(self) -> MediaKind:
        """Infer series vs film from which fields are present.

        An episode with no season is a series too: anime is routinely numbered
        absolutely.
        """
        if self.season is not None or self.episode is not None:
            return MediaKind.SERIES
        if self.year is not None:
            return MediaKind.MOVIE
        return MediaKind.UNKNOWN

    @property
    def key(self) -> str:
        """A canonical identity for deduplication.

        Case and spacing are folded away so ``"  the   WIRE "`` and ``"The Wire"`` are
        one download. A year is deliberately excluded from series keys: the same episode
        submitted with and without a year is still the same episode.
        """
        slug = "-".join(self.name.casefold().split())
        parts = [self.kind.value, slug]

        if self.kind is MediaKind.SERIES:
            if self.season is not None:
                parts.append(f"s{self.season}")
            if self.episode is not None:
                parts.append(f"e{self.episode}")
        elif self.year is not None:
            parts.append(str(self.year))

        return ":".join(parts)

    def episode_tag(self) -> str | None:
        """Render the season/episode in the ``S01E03`` form sites use, or ``None``.

        Seasons and episodes are zero-padded to two digits and grow past that when they
        need to, matching the convention used in release names.
        """
        season = f"S{self.season:02d}" if self.season is not None else ""
        episode = f"E{self.episode:02d}" if self.episode is not None else ""
        return f"{season}{episode}" or None

    def search_terms(self) -> str:
        """The string to type into a site's search box.

        The episode tag is the strongest signal available, so it wins over the year when
        both are present.
        """
        if (tag := self.episode_tag()) is not None:
            return f"{self.name} {tag}"
        if self.year is not None:
            return f"{self.name} {self.year}"
        return self.name
