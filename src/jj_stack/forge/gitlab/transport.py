"""Send GitLab API requests and return the raw HTTP answer.

`GitlabTransport` is the seam between the GitLab adapter and the way requests reach the host.
`GlabTransport` sends them through `glab api`; status mapping and retries stay above it, so
another transport can replace it without changing the adapter.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlencode

from jj_stack.forge.errors import ForgeError
from jj_stack.timing import timed

# Each glab call otherwise posts a telemetry event to the GitLab host and, once a day, may print
# an update notice.
_GLAB_ENV = {"GLAB_SEND_TELEMETRY": "0", "GLAB_CHECK_UPDATE": "false"}


@dataclass(frozen=True, slots=True)
class GitlabResponse:
    """One HTTP answer from the GitLab host. Header names are lowercase."""

    status: int
    headers: Mapping[str, str]
    body: bytes


def parse_glab_include_output(
    stdout: bytes,
) -> GitlabResponse | None:
    """Read the status, headers, and body that `glab api -i` writes to stdout.

    Return `None` when stdout holds no complete HTTP response, such as when the command failed
    before it reached the host.
    """

    status_line, newline, rest = stdout.partition(b"\n")
    parts = status_line.split()
    if not newline or len(parts) < 2 or not parts[0].startswith(b"HTTP/"):
        return None

    try:
        status = int(parts[1])
    except ValueError:
        return None

    header_block, separator, body = rest.partition(b"\r\n\r\n")
    if not separator:
        if not rest.startswith(b"\r\n"):
            return None
        header_block, body = b"", rest.removeprefix(b"\r\n")

    headers: dict[str, str] = {}
    for line in header_block.decode(errors="replace").split("\r\n"):
        if not line:
            continue

        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()

    return GitlabResponse(status=status, headers=headers, body=body)


class GitlabTransport(Protocol):
    """Deliver one GitLab API request and return its HTTP answer, whatever the status.

    `path` is relative to the host's `/api/v4/` and may already carry a query string, as the
    `next` link of a paged answer does. `body`, when given, is sent as JSON. A transport raises
    `ForgeError` only when no HTTP answer arrived.
    """

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: Sequence[tuple[str, str]] = (),
        body: Mapping[str, object] | None = None,
    ) -> GitlabResponse: ...


@dataclass(frozen=True, slots=True)
class CommandRun:
    """The outcome of one finished command."""

    returncode: int
    stdout: bytes
    stderr: bytes


class CommandRunner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes | None,
        env: Mapping[str, str],
    ) -> Awaitable[CommandRun]: ...


async def run_subprocess(
    argv: Sequence[str],
    *,
    stdin: bytes | None,
    env: Mapping[str, str],
) -> CommandRun:
    """Run `argv` with `stdin` and `env`, and collect its output. Raise if it cannot start."""

    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(env),
    )
    stdout, stderr = await process.communicate(stdin)
    return CommandRun(returncode=process.returncode or 0, stdout=stdout, stderr=stderr)


@dataclass(slots=True)
class _WarmUp:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    done: bool = False


@dataclass(frozen=True, slots=True)
class GlabTransport:
    """Send requests as `<command> api` calls, such as `glab api` or a wrapper around it.

    `hostname` is the Git remote's host, which is also the key of the host in glab's config.
    It is always passed, because glab otherwise guesses the host from a `.git` directory that
    a non-colocated jj repo lacks, and falls back to another host without saying so.

    The first request runs alone: glab may refresh and save its login during a call, and
    parallel calls that each do so can overwrite each other's saved login.
    """

    command: Sequence[str]
    hostname: str
    runner: CommandRunner
    _warm_up: _WarmUp = field(default_factory=_WarmUp, init=False)

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: Sequence[tuple[str, str]] = (),
        body: Mapping[str, object] | None = None,
    ) -> GitlabResponse:
        if query:
            path = f"{path}?{urlencode(query)}"

        stdin = None if body is None else json.dumps(body).encode()
        if not self._warm_up.done:
            async with self._warm_up.lock:
                if not self._warm_up.done:
                    response = await self._send(method, path, stdin=stdin)
                    self._warm_up.done = True
                    return response

        return await self._send(method, path, stdin=stdin)

    async def _send(
        self,
        method: str,
        path: str,
        *,
        stdin: bytes | None,
    ) -> GitlabResponse:
        argv = [*self.command, "api", path, "--hostname", self.hostname, "-i", "-X", method]
        if stdin is not None:
            argv += ["--input", "-", "-H", "Content-Type: application/json"]

        program = self.command[0]
        try:
            with timed("gitlab", f"{method} {path}"):
                run = await self.runner(argv, stdin=stdin, env={**os.environ, **_GLAB_ENV})
        except FileNotFoundError as error:
            raise ForgeError(
                f"GitLab command {program} was not found; install it or set "
                f"jj-stack.gitlab.command",
                kind="unavailable",
            ) from error
        except OSError as error:
            raise ForgeError(
                f"could not run GitLab command {program}: {error.strerror or error}",
                kind="unavailable",
            ) from error

        response = parse_glab_include_output(run.stdout)
        if response is None:
            detail = run.stderr.decode(errors="replace").strip() or f"exit code {run.returncode}"
            raise ForgeError(
                f"request failed ({program} returned no HTTP answer: {detail})",
                kind="unavailable",
            )

        return response
