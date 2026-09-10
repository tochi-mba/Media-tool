"""The keyring client: who is calling, and what credential do they have.

media-tool stores no passwords and mints no tokens. It verifies the tokens keyring signs
and forwards the caller's own token when it needs a credential on their behalf.
"""
