import base64
import json

import pytest

from ledger_agent.ledger import Unauthorized
from ledger_agent.usage import user_id_from


def token_for(claims: object) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


class TestGivenATokenLedgerApiAccepted:
    def test_it_reads_the_user_from_the_claims(self) -> None:
        assert user_id_from(token_for({"sub": "01JBXALICE", "role": "customer"})) == "01JBXALICE"


class TestGivenATokenWithNoUserInIt:
    @pytest.mark.parametrize(
        "token",
        [
            pytest.param("customer-token", id="not a JWT"),
            pytest.param("header.not-base64!.signature", id="an unreadable payload"),
            pytest.param(token_for(["sub"]), id="a payload that isn't an object"),
            pytest.param(token_for({"role": "customer"}), id="no subject"),
            pytest.param(token_for({"sub": ""}), id="an empty subject"),
        ],
    )
    def test_it_is_unauthorized(self, token: str) -> None:
        with pytest.raises(Unauthorized):
            user_id_from(token)
