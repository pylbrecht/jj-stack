from __future__ import annotations

import asyncio
import json

import pytest

from jj_stack.forge.errors import ForgeError, ForgeErrorKind
from jj_stack.forge.gitlab.client import GitlabClient
from jj_stack.forge.gitlab.transport import GitlabResponse


class _ScriptedTransport:
    """Answer each request with the next scripted response."""

    def __init__(
        self,
        *responses: GitlabResponse,
    ) -> None:
        self._responses = list(responses)
        self.requests: list[tuple[str, str]] = []

    async def request(
        self,
        method: str,
        path: str,
    ) -> GitlabResponse:
        self.requests.append((method, path))
        return self._responses.pop(0)


def _json_response(
    status: int,
    payload: object,
    **headers: str,
) -> GitlabResponse:
    return GitlabResponse(
        status=status,
        headers={"content-type": "application/json", **headers},
        body=json.dumps(payload).encode(),
    )


_VERSION = _json_response(200, {"version": "19.5.4-ee", "revision": "abc"})


@pytest.mark.parametrize(
    ("response", "kind", "message"),
    (
        (_json_response(401, {"message": "401 Unauthorized"}), "auth", "authentication failed"),
        (_json_response(403, {"message": "403 Forbidden"}), "auth", "access denied"),
        (_json_response(404, {"message": "404 Not Found"}), "not_found", "404 Not Found"),
        (_json_response(502, {"message": "Bad Gateway"}), "unavailable", "GitLab 502"),
        (
            GitlabResponse(
                status=200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=b"<html>Sign in with Cloudflare Access</html>",
            ),
            "unavailable",
            "not a GitLab API answer",
        ),
    ),
)
def test_failed_or_unusable_answers_become_forge_errors(
    response: GitlabResponse,
    kind: ForgeErrorKind,
    message: str,
) -> None:
    client = GitlabClient(_ScriptedTransport(response))

    with pytest.raises(ForgeError) as raised:
        asyncio.run(client.get_version())

    assert raised.value.kind == kind
    assert message in raised.value.message


def test_rate_limited_request_is_retried_after_the_requested_wait() -> None:
    limited = GitlabResponse(
        status=429,
        headers={"content-type": "text/plain", "retry-after": "0"},
        body=b"Retry later",
    )
    transport = _ScriptedTransport(limited, _VERSION)
    client = GitlabClient(transport)

    version = asyncio.run(client.get_version())

    assert version == "19.5.4-ee"
    assert transport.requests == [("GET", "version"), ("GET", "version")]


def test_rate_limit_that_outlasts_the_retries_stops_as_rate_limited() -> None:
    limited = GitlabResponse(
        status=429,
        headers={"content-type": "text/plain", "retry-after": "0"},
        body=b"Retry later",
    )
    client = GitlabClient(_ScriptedTransport(*(limited for _ in range(4))))

    with pytest.raises(ForgeError) as raised:
        asyncio.run(client.get_version())

    assert raised.value.kind == "rate_limited"
