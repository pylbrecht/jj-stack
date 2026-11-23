from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from urllib.parse import urlencode

import pytest
from pydantic import BaseModel

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
        *,
        query: Sequence[tuple[str, str]] = (),
        body: Mapping[str, object] | None = None,
    ) -> GitlabResponse:
        self.requests.append((method, f"{path}?{urlencode(query)}" if query else path))
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


class _Item(BaseModel):
    iid: int


def test_a_page_with_an_unexpected_item_becomes_a_forge_error() -> None:
    client = GitlabClient(_ScriptedTransport(_json_response(200, [{"iid": 1}, {"id": 2}])))

    with pytest.raises(ForgeError) as raised:
        asyncio.run(client.get_pages("projects/7/merge_requests", model=_Item))

    assert raised.value.kind == "unavailable"
    assert "unexpected GitLab answer" in raised.value.message


def test_a_paged_list_follows_each_next_link_until_the_last_page() -> None:
    next_link = (
        '<https://gitlab.test/api/v4/projects/7/merge_requests?page=2&per_page=100>; rel="next", '
        '<https://gitlab.test/api/v4/projects/7/merge_requests?page=1&per_page=100>; rel="first"'
    )
    transport = _ScriptedTransport(
        _json_response(200, [{"iid": 1}, {"iid": 2}], link=next_link),
        _json_response(200, [{"iid": 3}]),
    )
    client = GitlabClient(transport)

    items = asyncio.run(
        client.get_pages("projects/7/merge_requests", query=(("state", "opened"),), model=_Item)
    )

    assert [item.iid for item in items] == [1, 2, 3]
    assert transport.requests == [
        ("GET", "projects/7/merge_requests?state=opened&per_page=100"),
        ("GET", "projects/7/merge_requests?page=2&per_page=100"),
    ]


@pytest.mark.parametrize(
    "payload",
    (
        {"errors": [{"message": "Field 'drafty' doesn't exist on type 'MergeRequest'"}]},
        {"data": {"mergeRequestSetDraft": {"errors": ["Field 'drafty' doesn't exist"]}}},
    ),
)
def test_graphql_errors_in_a_success_answer_become_forge_errors(
    payload: object,
) -> None:
    client = GitlabClient(_ScriptedTransport(_json_response(200, payload)))

    with pytest.raises(ForgeError) as raised:
        asyncio.run(
            client.mutate(
                "mutation { mergeRequestSetDraft(input: {}) { errors } }",
                mutation="mergeRequestSetDraft",
                variables={},
            )
        )

    assert raised.value.kind == "invalid"
    assert "Field 'drafty' doesn't exist" in raised.value.message
