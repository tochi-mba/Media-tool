"""The accounts the suite uses.

Two of them, because one can never demonstrate isolation: every test that matters here
is of the form "Alice cannot see Bob's", and that needs a Bob.
"""

from __future__ import annotations

from media_tool.domain.accounts import AccountId

ACCOUNT_ID = "acct_alice"
OTHER_ACCOUNT_ID = "acct_bob"

ALICE = AccountId.parse(ACCOUNT_ID)
BOB = AccountId.parse(OTHER_ACCOUNT_ID)
