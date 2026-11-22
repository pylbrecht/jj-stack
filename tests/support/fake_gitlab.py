"""An in-process fake GitLab host, reached through the `glab api` command runner."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from http import HTTPStatus

import httpx2
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from jj_stack.forge.gitlab.transport import CommandRun, CommandRunner


@dataclass(slots=True)
class FakeGitlabHost:
    """The GitLab host state that the fake API serves."""

    hostname: str = "gitlab.test"
    username: str = "octocat"
    version: str = "19.5.4-ee"
    login_valid: bool = True
    commands: list[tuple[str, ...]] = field(default_factory=list)


def create_gitlab_app(
    host: FakeGitlabHost,
) -> FastAPI:
    """Serve the GitLab REST endpoints that jj-stack uses. Knows nothing about glab."""

    app = FastAPI()

    @app.middleware("http")
    async def require_login(
        request: Request,
        call_next,
    ):
        if not host.login_valid:
            return JSONResponse({"message": "401 Unauthorized"}, status_code=401)

        return await call_next(request)

    @app.get("/api/v4/user")
    async def current_user() -> dict[str, object]:
        return {"id": 1, "username": host.username}

    @app.get("/api/v4/version")
    async def version() -> dict[str, object]:
        return {"version": host.version, "revision": "0123abcd"}

    return app


def glab_runner(
    app: FastAPI,
    host: FakeGitlabHost,
) -> CommandRunner:
    """Run `<command> api ...` argument lists against `app` and print what `glab api -i` would.

    This is the only glab-aware test code: it turns glab's arguments into an HTTP request and
    renders the answer in the `-i` format.
    """

    async def run(
        argv: Sequence[str],
        *,
        stdin: bytes | None,
        env: Mapping[str, str],
    ) -> CommandRun:
        host.commands.append(tuple(argv))
        args = list(argv[argv.index("api") + 1 :])
        path = args.pop(0)
        options: dict[str, str] = {}
        remaining = iter(args)
        for arg in remaining:
            if arg in {"--hostname", "-X"}:
                options[arg] = next(remaining)

        if options.get("--hostname") != host.hostname or "-i" not in args:
            # glab without a known `--hostname` falls back to another host and its login page.
            return CommandRun(
                returncode=0,
                stdout=b"HTTP/2.0 200 OK\nContent-Type: text/html\r\n\r\n<html>",
                stderr=b"",
            )

        async with httpx2.AsyncClient(
            base_url=f"https://{host.hostname}/api/v4/",
            transport=httpx2.ASGITransport(app=app),
        ) as client:
            response = await client.request(options.get("-X", "GET"), path)
        failed = response.status_code >= 300
        return CommandRun(
            returncode=int(failed),
            stdout=_include_output(response),
            stderr=f"glab: HTTP {response.status_code}".encode() if failed else b"",
        )

    return run


def _include_output(
    response: httpx2.Response,
) -> bytes:
    reason = HTTPStatus(response.status_code).phrase
    headers = "".join(
        f"{name.title()}: {value}\r\n" for name, value in sorted(response.headers.items())
    )
    return f"HTTP/2.0 {response.status_code} {reason}\n{headers}\r\n".encode() + response.content
