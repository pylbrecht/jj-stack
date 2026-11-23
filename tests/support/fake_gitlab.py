"""An in-process fake GitLab host, reached through the `glab api` command runner."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Annotated

import httpx2
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from jj_stack.forge.gitlab.transport import CommandRun, CommandRunner

from .fake_forge import FakeForgeRepo, FakePullRequest


@dataclass(slots=True, kw_only=True)
class FakeMergeRequest(FakePullRequest):
    """A merge request with the GitLab extras that inspection reads."""

    draft: bool = False
    detailed_merge_status: str = "mergeable"
    # The head pipeline's status, such as `success`; None when the MR has no pipeline.
    pipeline_status: str | None = None
    approved_by: list[str] = field(default_factory=list)
    approvals_required: int = 0


@dataclass(slots=True, kw_only=True)
class FakeGitlabProject(FakeForgeRepo[FakeMergeRequest]):
    """A GitLab project: merge requests on top of the fake forge core."""

    id: int = 7
    path: str = "platform/tools/stacked-mrs"

    def create_merge_request(
        self,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
    ) -> FakeMergeRequest:
        head_sha = self.ref_target(source_branch)
        if head_sha is None:
            raise AssertionError(f"fake GitLab project has no branch {source_branch}")

        merge_request = FakeMergeRequest(
            base_ref=target_branch,
            body="",
            head_ref=source_branch,
            head_sha=head_sha,
            number=self.next_pr_number,
            title=title,
        )
        self.prs[merge_request.number] = merge_request
        self.next_pr_number += 1
        return merge_request


@dataclass(slots=True)
class FakeGitlabHost:
    """The GitLab host state that the fake API serves."""

    hostname: str = "gitlab.test"
    username: str = "octocat"
    version: str = "19.5.4-ee"
    login_valid: bool = True
    commands: list[tuple[str, ...]] = field(default_factory=list)
    project: FakeGitlabProject | None = None


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

    def require_project(
        path: str,
    ) -> FakeGitlabProject:
        if host.project is None or host.project.path != path:
            raise _NotFound("404 Project Not Found")

        return host.project

    def require_merge_request(
        project: FakeGitlabProject,
        iid: int,
    ) -> FakeMergeRequest:
        merge_request = project.prs.get(iid)
        if merge_request is None:
            raise _NotFound("404 Not found")

        project.refresh_head(merge_request)
        return merge_request

    @app.exception_handler(_NotFound)
    async def not_found(
        request: Request,
        error: _NotFound,
    ) -> JSONResponse:
        return JSONResponse({"message": str(error)}, status_code=404)

    @app.get("/api/v4/projects/{project_path:path}/merge_requests/{iid:int}/approvals")
    async def approvals(
        project_path: str,
        iid: int,
    ) -> dict[str, object]:
        merge_request = require_merge_request(require_project(project_path), iid)
        left = max(merge_request.approvals_required - len(merge_request.approved_by), 0)
        return {
            "approved": left == 0,
            "approvals_required": merge_request.approvals_required,
            "approvals_left": left,
            "approved_by": [{"user": {"username": name}} for name in merge_request.approved_by],
        }

    @app.get("/api/v4/projects/{project_path:path}/merge_requests/{iid:int}")
    async def merge_request(
        project_path: str,
        iid: int,
    ) -> dict[str, object]:
        project = require_project(project_path)
        merge_request = require_merge_request(project, iid)
        pipeline = (
            None
            if merge_request.pipeline_status is None
            else {
                "status": merge_request.pipeline_status,
                "web_url": f"{_web_url(host, project)}/-/pipelines/{iid}",
            }
        )
        behind = project._run_backing_git(
            "rev-list", "--count", f"{merge_request.head_sha}..{merge_request.base_ref}"
        )
        return {
            **_merge_request_json(host, project, merge_request),
            "head_pipeline": pipeline,
            "diverged_commits_count": int(behind),
        }

    @app.get("/api/v4/projects/{project_path:path}/merge_requests")
    async def merge_requests(
        project_path: str,
        iids: Annotated[list[int] | None, Query(alias="iids[]")] = None,
        state: str | None = None,
        source_branch: str | None = None,
    ) -> list[dict[str, object]]:
        project = require_project(project_path)
        listed = []
        for merge_request in project.prs.values():
            project.refresh_head(merge_request)
            payload = _merge_request_json(host, project, merge_request)
            if iids is not None and merge_request.number not in iids:
                continue
            if state is not None and payload["state"] != state:
                continue
            if source_branch is not None and merge_request.head_ref != source_branch:
                continue
            listed.append(payload)
        return listed

    @app.get("/api/v4/projects/{project_path:path}/repository/branches/{branch:path}")
    async def branch(
        project_path: str,
        branch: str,
    ) -> dict[str, object]:
        target = require_project(project_path).ref_target(branch)
        if target is None:
            raise _NotFound("404 Branch Not Found")

        return {"name": branch, "commit": {"id": target}}

    @app.get("/api/v4/projects/{project_path:path}")
    async def project(
        project_path: str,
    ) -> dict[str, object]:
        project = require_project(project_path)
        return {
            "id": project.id,
            "path_with_namespace": project.path,
            "default_branch": project.default_branch,
            "web_url": _web_url(host, project),
        }

    return app


class _NotFound(Exception):
    pass


def _web_url(
    host: FakeGitlabHost,
    project: FakeGitlabProject,
) -> str:
    return f"https://{host.hostname}/{project.path}"


def _merge_request_json(
    host: FakeGitlabHost,
    project: FakeGitlabProject,
    merge_request: FakeMergeRequest,
) -> dict[str, object]:
    if merge_request.state == "open":
        state = "opened"
    else:
        state = "merged" if merge_request.merged_at is not None else "closed"
    return {
        "iid": merge_request.number,
        "title": merge_request.title,
        "description": merge_request.body,
        "state": state,
        "draft": merge_request.draft,
        "source_branch": merge_request.head_ref,
        "target_branch": merge_request.base_ref,
        "sha": merge_request.head_sha,
        "source_project_id": project.id,
        "target_project_id": project.id,
        "web_url": f"{_web_url(host, project)}/-/merge_requests/{merge_request.number}",
        "merge_commit_sha": merge_request.merge_commit_sha,
        "squash_commit_sha": None,
        "detailed_merge_status": merge_request.detailed_merge_status,
    }


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
