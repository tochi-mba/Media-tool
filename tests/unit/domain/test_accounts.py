"""An account id names whose work a job or a file is.

Its rules matter more than they look: this value becomes a directory name on disk and a
field in every log record, so what it is allowed to contain decides whether one account
can be made to read another's files.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from media_tool.domain.accounts import MAX_ACCOUNT_ID_LENGTH, AccountId
from media_tool.domain.errors import InvalidAccountIdError


class TestAcceptance:
    def test_the_shape_keyring_actually_issues_is_accepted(self) -> None:
        raw = "acct_9f2c4b1e8d7a4c3fb0a1e2d3c4b5a697"

        assert str(AccountId.parse(raw)) == raw

    @pytest.mark.parametrize("raw", ["a", "A1", "acct_1", "01H8XGJWBWBAQ4V2S1M9T0K3AB", "a-b_c"])
    def test_other_opaque_identifier_shapes_are_accepted(self, raw: str) -> None:
        # Deliberately not pinned to keyring's current `acct_<hex>`: an id is opaque to
        # us, and pinning its spelling would make keyring's next id format our outage.
        assert str(AccountId.parse(raw)) == raw

    def test_an_id_of_exactly_the_limit_is_accepted(self) -> None:
        raw = "a" * MAX_ACCOUNT_ID_LENGTH

        assert str(AccountId.parse(raw)) == raw


class TestRejection:
    """Each of these is a path segment we refuse to let an account choose."""

    @pytest.mark.parametrize(
        "raw",
        [
            "..",
            ".",
            "../acct_other",
            "acct/other",
            "acct\\other",
            "acct\x00other",
            "acct other",
            "acct.other",
            "",
            "   ",
        ],
    )
    def test_ids_that_are_not_a_single_safe_path_segment_are_rejected(self, raw: str) -> None:
        with pytest.raises(InvalidAccountIdError):
            AccountId.parse(raw)

    def test_an_overlong_id_is_rejected(self) -> None:
        with pytest.raises(InvalidAccountIdError, match=str(MAX_ACCOUNT_ID_LENGTH)):
            AccountId.parse("a" * (MAX_ACCOUNT_ID_LENGTH + 1))

    def test_a_non_string_is_rejected(self) -> None:
        # The value arrives as a JWT claim, which is whatever JSON the signer put there.
        with pytest.raises(InvalidAccountIdError):
            AccountId.parse(12345)


class TestIdentity:
    def test_two_ids_with_the_same_text_are_equal(self) -> None:
        assert AccountId.parse("acct_a") == AccountId.parse("acct_a")

    def test_ids_differing_only_in_case_are_different_accounts(self) -> None:
        # keyring's ids are hex, so this cannot bite today. Folding case here would be a
        # silent merge of two accounts if it ever issued something case-sensitive.
        assert AccountId.parse("acct_a") != AccountId.parse("acct_A")

    def test_an_id_can_key_a_dictionary(self) -> None:
        # The job store buckets by account, so this has to hold.
        assert {AccountId.parse("acct_a"): 1}[AccountId.parse("acct_a")] == 1

    def test_an_id_is_not_equal_to_its_own_text(self) -> None:
        # A domain type that compares equal to a bare string invites passing the string
        # where the type is required, which is exactly the mistake it exists to prevent.
        text: object = "acct_a"

        assert AccountId.parse("acct_a") != text

    def test_the_text_is_what_goes_on_disk_and_into_logs(self) -> None:
        assert f"{AccountId.parse('acct_a')}" == "acct_a"

    def test_the_repr_shows_the_id(self) -> None:
        assert repr(AccountId.parse("acct_a")) == "AccountId('acct_a')"


class TestProperties:
    @given(
        st.text(
            alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), max_codepoint=127),
            min_size=1,
            max_size=MAX_ACCOUNT_ID_LENGTH,
        )
    )
    def test_every_accepted_id_round_trips_unchanged(self, raw: str) -> None:
        assert str(AccountId.parse(raw)) == raw

    @given(st.text(min_size=1, max_size=80))
    def test_an_accepted_id_is_always_a_single_harmless_path_segment(self, raw: str) -> None:
        # The property that matters: whatever gets through cannot climb out of the
        # directory it is about to name.
        try:
            account = AccountId.parse(raw)
        except InvalidAccountIdError:
            return

        text = str(account)
        assert "/" not in text
        assert "\\" not in text
        assert text not in {".", ".."}
        assert text.strip() == text
