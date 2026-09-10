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


class InvalidAccountIdError(DomainError, ValueError):
    """An account identifier is not one this service is willing to use.

    It becomes a directory name and a log field, so the rules are about what is safe to
    write down rather than about what keyring happens to issue.
    """


class AuthenticationError(DomainError):
    """A request could not be attributed to an account.

    Deliberately carries one message for every cause. Which check failed -- expiry,
    audience, signature -- is nobody's business, and a distinct message per failure is a
    probe for what an acceptable token looks like.
    """


class KeyringUnavailableError(DomainError):
    """Keyring could not be reached, so identity could not be established.

    Kept separate from :class:`AuthenticationError` on purpose: a caller holding a
    perfectly good token must not be told their token is bad because a dependency is
    down. One is a 401, the other a 503, and conflating them makes an outage
    undiagnosable from the outside.
    """


class KeyringRejectedError(DomainError):
    """Keyring refused a call this service made on somebody's behalf.

    The transport's faithful report of a 401 and nothing more. Keyring answers 401 both
    for a service token it does not recognise and for a user token it will not accept,
    and the wire does not distinguish them -- so the interpretation happens here, where
    this service knows whether the user token it forwarded was still good.
    """


class ReauthenticationRequiredError(DomainError):
    """The person's token expired while their work was still running.

    User tokens are short-lived by design and a download can outlast one. This is a
    distinct, actionable outcome -- "log in again and resubmit" -- rather than a generic
    provider failure, because the two need entirely different things from the caller.
    """


class CredentialNotFoundError(DomainError):
    """Keyring holds no credential for that profile and service.

    Not a failure of authentication: the caller is who they say they are and simply has
    not connected that account yet.
    """
