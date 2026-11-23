from __future__ import annotations

import asyncio
import json

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


class _RecordingRunner:
    """Answer every command with 200, and record each argument list and peak concurrency."""

    def __init__(self) -> None:
        self.argvs: list[tuple[str, ...]] = []
        self.stdins: list[bytes | None] = []
        self.running = 0
        self.peaks: list[int] = []

    async def __call__(
        self,
        argv,
        *,
        stdin,
        env,
    ) -> CommandRun:
        self.argvs.append(tuple(argv))
        self.stdins.append(stdin)
        self.running += 1
        self.peaks.append(self.running)
        await asyncio.sleep(0)
        self.running -= 1
        return CommandRun(
            returncode=0,
            stdout=b"HTTP/2.0 200 OK\nContent-Type: application/json\r\n\r\n{}",
            stderr=b"",
        )


def test_query_parameters_are_sent_url_encoded_in_the_path() -> None:
    runner = _RecordingRunner()
    transport = GlabTransport(command=("glab",), hostname="gitlab.test", runner=runner)

    asyncio.run(
        transport.request(
            "GET",
            "projects/1/merge_requests",
            query=(("source_branch", "jj-stack/a b"), ("iids[]", "3")),
        )
    )

    assert runner.argvs[0][2] == (
        "projects/1/merge_requests?source_branch=jj-stack%2Fa+b&iids%5B%5D=3"
    )


def test_a_request_body_is_sent_as_json_on_stdin() -> None:
    runner = _RecordingRunner()
    transport = GlabTransport(command=("glab",), hostname="gitlab.test", runner=runner)

    asyncio.run(transport.request("POST", "projects/1/merge_requests", body={"title": "Ünï"}))

    argv = runner.argvs[0]
    assert argv[argv.index("--input") + 1] == "-"
    assert argv[argv.index("-H") + 1] == "Content-Type: application/json"
    assert json.loads(runner.stdins[0] or b"") == {"title": "Ünï"}


def test_the_first_request_runs_alone_before_later_requests_run_together() -> None:
    runner = _RecordingRunner()
    transport = GlabTransport(command=("glab",), hostname="gitlab.test", runner=runner)

    async def requests() -> None:
        await asyncio.gather(*(transport.request("GET", f"path/{n}") for n in range(3)))

    asyncio.run(requests())

    assert runner.peaks[0] == 1
    assert max(runner.peaks[1:]) == 2
