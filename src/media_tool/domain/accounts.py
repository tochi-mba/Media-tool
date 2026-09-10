"""Who a job, a file, or a request belongs to.

An account id arrives as the ``sub`` claim of a token keyring signed, and from there it
becomes a directory name on disk, a bucket key in the job store, and a field in every log
record. That is why it is a parsed type rather than a bare string: the parse is the one
place that decides what an account is allowed to be called, and every layer downstream
gets to assume it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from media_tool.domain.errors import InvalidAccountIdError

MAX_ACCOUNT_ID_LENGTH = 128
"""Generous next to keyring's ``acct_`` plus a uuid4 hex, and short enough to be a
filename on every filesystem this service is likely to meet."""

_SAFE_ID = re.compile(r"\A[A-Za-z0-9_-]+\Z")
"""One path segment, and an unremarkable one.

Not a check that the id looks like keyring's -- the id is opaque to us, and pinning its
spelling would make keyring's next id format our outage. This is the narrower question of
whether the value can be used as a directory name without meaning something else: no
separators, no ``.`` or ``..``, no whitespace, no control characters, nothing a shell or
a URL would reinterpret.
"""


@dataclass(frozen=True, slots=True, repr=False)
class AccountId:
    """An opaque, validated account identifier.

    Deliberately not a :class:`str` subclass. Equality with a bare string would let one
    be passed wherever an :class:`AccountId` is required, which is precisely the mistake
    this type exists to catch.
    """

    value: str

    @classmethod
    def parse(cls, raw: object) -> AccountId:
        """Validate an untrusted identifier.

        Takes an :class:`object` rather than a :class:`str` because the value it is given
        is a JWT claim -- whatever JSON the signer put there, which is not necessarily a
        string at all.

        Raises:
            InvalidAccountIdError: if the value is not a string, is empty, is longer than
                :data:`MAX_ACCOUNT_ID_LENGTH`, or is not a single safe path segment.
        """
        if not isinstance(raw, str):
            msg = "an account id must be a string"
            raise InvalidAccountIdError(msg)
        if len(raw) > MAX_ACCOUNT_ID_LENGTH:
            msg = f"an account id must be at most {MAX_ACCOUNT_ID_LENGTH} characters"
            raise InvalidAccountIdError(msg)
        if _SAFE_ID.match(raw) is None:
            msg = "an account id must be letters, digits, hyphens or underscores only"
            raise InvalidAccountIdError(msg)
        return cls(raw)

    def __str__(self) -> str:
        return self.value

    def __repr__(self) -> str:
        return f"AccountId({self.value!r})"


__all__ = ["MAX_ACCOUNT_ID_LENGTH", "AccountId"]
