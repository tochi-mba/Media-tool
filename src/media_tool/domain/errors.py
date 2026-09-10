"""Domain error vocabulary.

These describe what went wrong in business terms. Translating them into HTTP status
codes is the API layer's job -- nothing here knows what a status code is.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for every error this package raises deliberately."""


class InvalidMediaQueryError(DomainError, ValueError):
    """A submitted media item cannot be understood.

    Also a :class:`ValueError` so that callers validating input with generic machinery
    (pydantic validators, for one) catch it without importing this module.
    """


class JobNotFoundError(DomainError):
    """No job with the given id exists, or it has passed its retention window."""


class JobItemNotFoundError(DomainError):
    """The job exists but has no item at the given index."""


class InvalidJobTransitionError(DomainError):
    """A job was asked to move to a state it cannot reach from where it is."""


class ArtifactNotFoundError(DomainError):
    """The artifact's metadata is known but its bytes are no longer on disk."""


class ArtifactTooLargeError(DomainError):
    """A download exceeded the configured size ceiling and was discarded."""
