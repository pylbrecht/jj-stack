from __future__ import annotations

import asyncio

import pytest

from jj_stack.forge.errors import ForgeError
from jj_stack.forge.gitlab.transport import (
    CommandRun,
    CommandRunner,
    GitlabResponse,
    GlabTransport,
    parse_glab_include_output,
)

# `glab api -i` output as glab 1.128.0 prints it: a status line ending in `\n`, sorted headers
# each ending in `\r\n`, an empty `\r\n` line, then the body as GitLab sent it.
_RECORDED_NOT_FOUND = (
    b"HTTP/2.0 404 Not Found\n"
    b"Cache-Control: no-cache\r\n"
    b"Content-Type: application/json\r\n"
    b"Retry-After: 7\r\n"
    b"\r\n"
    b'{"message":"404 Project Not Found"}'
)


@pytest.mark.parametrize(
    ("stdout", "expected"),
    (
        (
            _RECORDED_NOT_FOUND,
            GitlabResponse(
                status=404,
                headers={
                    "cache-control": "no-cache",
                    "content-type": "application/json",
                    "retry-after": "7",
                },
                body=b'{"message":"404 Project Not Found"}',
            ),
        ),
        (
            b"HTTP/1.1 204 No Content\nDate: Sun, 22 Nov 2026 09:00:00 GMT\r\n\r\n",
            GitlabResponse(
                status=204,
                headers={"date": "Sun, 22 Nov 2026 09:00:00 GMT"},
                body=b"",
            ),
        ),
    ),
)
def test_glab_include_output_parses_into_status_headers_and_body(
    stdout: bytes,
    expected: GitlabResponse,
) -> None:
    response = parse_glab_include_output(stdout)

    assert response == expected


@pytest.mark.parametrize(
    "stdout",
    (
        b"",
        b'{"message":"no status line"}',
        b"HTTP/2.0 200 OK\nContent-Type: application/json\r\n",
    ),
)
def test_glab_output_without_a_complete_http_response_is_not_a_response(
    stdout: bytes,
) -> None:
    response = parse_glab_include_output(stdout)

    assert response is None


async def _missing_command(
    argv,
    *,
    stdin,
    env,
) -> CommandRun:
    raise FileNotFoundError(argv[0])


async def _login_needed(
    argv,
    *,
    stdin,
    env,
) -> CommandRun:
    return CommandRun(
        returncode=1,
        stdout=b"",
        stderr=b"glab-cf: Cloudflare login needed\n",
    )


@pytest.mark.parametrize(
    ("runner", "message"),
    (
        (_missing_command, "GitLab command glab-cf was not found"),
        (_login_needed, "glab-cf: Cloudflare login needed"),
    ),
)
def test_a_command_that_returns_no_http_answer_raises_a_forge_error(
    runner: CommandRunner,
    message: str,
) -> None:
    transport = GlabTransport(
        command=("glab-cf", "--ship"),
        hostname="gitlab.test",
        runner=runner,
    )

    with pytest.raises(ForgeError) as raised:
        asyncio.run(transport.request("GET", "version"))

    assert raised.value.kind == "unavailable"
    assert message in raised.value.message
