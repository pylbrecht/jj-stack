"""The `Forge` requests of commands and stack code, answered by one GitLab project.

A GitLab merge request (MR) is the forge's pull request, and its project-scoped `iid` is the PR
number. Only reads are supported so far; a mutation stops before it sends anything.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Mapping, Sequence
from types import TracebackType
from typing import NoReturn, Self
from urllib.parse import quote

from pydantic import BaseModel

from jj_stack.concurrency import wait_for_read_tasks
from jj_stack.forge.errors import ForgeError
from jj_stack.forge.gitlab.client import GitlabClient
from jj_stack.forge.gitlab.resolution import GitlabProjectAddress
from jj_stack.forge.models import (
    Check,
    CheckRollupStatus,
    MergeDetails,
    PRComment,
    PRProgress,
    PRReview,
    PRRevision,
    PullRequest,
    Repo,
    ReviewDecision,
)
from jj_stack.identifiers import CommitId

# GitLab is still computing these, so whether it would merge the MR has no answer yet.
_UNDECIDED_MERGE_STATUSES = {"approvals_syncing", "checking", "preparing", "unchecked"}
# Statuses that a merge warning or the draft state already explains.
_EXPLAINED_MERGE_STATUSES = {"conflict", "draft_status", "mergeable", "need_rebase", "not_open"}
# GitLab locks an open MR for the short time it takes to merge it.
_PR_STATES = {"opened": "open", "locked": "open", "merged": "merged", "closed": "closed"}
_REVIEW_DECISIONS: dict[str, ReviewDecision] = {
    "not_approved": "review_required",
    "requested_changes": "changes_requested",
}
_PIPELINE_ROLLUPS: dict[str, CheckRollupStatus] = {
    "success": "passed",
    "skipped": "passed",
    "failed": "failed",
    "canceled": "failed",
}


class _GitlabProject(BaseModel):
    path_with_namespace: str
    default_branch: str | None = None


class _GitlabCommit(BaseModel):
    id: CommitId


class _GitlabBranch(BaseModel):
    commit: _GitlabCommit


class _GitlabPipeline(BaseModel):
    status: str
    web_url: str


class _GitlabMergeRequestDetails(BaseModel):
    """The fields that only the single merge request read returns."""

    sha: CommitId
    target_branch: str
    detailed_merge_status: str | None = None
    head_pipeline: _GitlabPipeline | None = None
    diverged_commits_count: int | None = None


class _GitlabApprovals(BaseModel):
    approvals_required: int = 0
    approvals_left: int = 0
    approved_by: list[dict[str, object]] = []


class _GitlabMergeRequest(BaseModel):
    iid: int
    title: str
    description: str | None = None
    state: str
    draft: bool = False
    source_branch: str
    target_branch: str
    sha: CommitId
    source_project_id: int
    target_project_id: int
    web_url: str
    merge_commit_sha: CommitId | None = None
    squash_commit_sha: CommitId | None = None
    detailed_merge_status: str | None = None

    def pull_request(
        self,
        project: GitlabProjectAddress,
    ) -> PullRequest:
        # An MR from a fork has its head in another project, which only its id names.
        label = (
            project.head_label(self.source_branch)
            if self.source_project_id == self.target_project_id
            else f"project {self.source_project_id}:{self.source_branch}"
        )
        status = self.detailed_merge_status
        return PullRequest.model_validate(
            {
                "base": {"ref": self.target_branch},
                "body": self.description,
                "head": {"label": label, "ref": self.source_branch, "sha": self.sha},
                "html_url": self.web_url,
                "is_draft": self.draft,
                "merge_commit_sha": self.merge_commit_sha or self.squash_commit_sha,
                "number": self.iid,
                "state": _PR_STATES[self.state],
                "title": self.title,
                "progress": PRProgress(
                    ready=None if status in _UNDECIDED_MERGE_STATUSES else status == "mergeable",
                    conflicts=status == "conflict",
                    behind_base=status == "need_rebase",
                    blocked=status is not None
                    and status not in _UNDECIDED_MERGE_STATUSES | _EXPLAINED_MERGE_STATUSES,
                    merge_status=status,
                    landing_ref=self.target_branch,
                    review_decision=_REVIEW_DECISIONS.get(status or ""),
                ),
            }
        )


class GitlabForge:
    """One GitLab project that receives the stack's merge requests."""

    def __init__(
        self,
        client: GitlabClient,
        *,
        project: GitlabProjectAddress,
    ) -> None:
        self._client = client
        self._project = project
        self._path = f"projects/{quote(project.path, safe='')}"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    @property
    def repo(self) -> GitlabProjectAddress:
        return self._project

    @property
    def stacks(self) -> None:
        # GitLab has no server-side stack object, so stack membership is always empty.
        return None

    async def get_repo(self) -> Repo:
        project = await self._client.get(self._path, model=_GitlabProject)
        return Repo(default_branch=project.default_branch, full_name=project.path_with_namespace)

    async def get_branch_targets(
        self,
        *,
        branches: Sequence[str],
    ) -> dict[str, CommitId]:
        async def target(branch: str) -> CommitId | None:
            try:
                found = await self._client.get(
                    f"{self._path}/repository/branches/{quote(branch, safe='')}",
                    model=_GitlabBranch,
                )
            except ForgeError as error:
                if error.kind == "not_found":
                    return None
                raise
            return found.commit.id

        targets = await _read_all({branch: target(branch) for branch in set(branches)})
        return {branch: commit for branch, commit in targets.items() if commit is not None}

    async def get_pr(
        self,
        *,
        pr_number: int,
    ) -> PullRequest:
        merge_request = await self._client.get(
            f"{self._path}/merge_requests/{pr_number}", model=_GitlabMergeRequest
        )
        return merge_request.pull_request(self._project)

    async def get_prs_by_numbers(
        self,
        *,
        pr_numbers: Sequence[int],
    ) -> dict[int, PullRequest | None]:
        numbers = sorted(set(pr_numbers))
        # Without an `iids[]` filter, GitLab lists every MR of the project.
        if not numbers:
            return {}

        merge_requests = await self._client.get_pages(
            f"{self._path}/merge_requests",
            model=_GitlabMergeRequest,
            query=tuple(("iids[]", str(number)) for number in numbers),
        )
        found = {mr.iid: mr.pull_request(self._project) for mr in merge_requests}
        return {number: found.get(number) for number in numbers}

    async def get_open_prs_by_head_refs(
        self,
        *,
        head_refs: Sequence[str],
    ) -> dict[str, tuple[PullRequest, ...]]:
        async def open_on(ref: str) -> tuple[PullRequest, ...]:
            merge_requests = await self._client.get_pages(
                f"{self._path}/merge_requests",
                model=_GitlabMergeRequest,
                query=(("state", "opened"), ("source_branch", ref)),
            )
            # The source branch filter also matches MRs from forks that use the same name.
            label = self._project.head_label(ref)
            return tuple(
                pr
                for pr in (mr.pull_request(self._project) for mr in merge_requests)
                if pr.head.label == label
            )

        return await _read_all({ref: open_on(ref) for ref in set(head_refs)})

    async def get_pr_progress(
        self,
        *,
        prs: Sequence[PullRequest],
    ) -> dict[int, PRProgress]:
        """Each MR's progress with its head pipeline, approvals, and lag behind its target."""

        async def progress(pr: PullRequest) -> PRProgress:
            details_task = asyncio.create_task(self._details(pr))
            approvals_task = asyncio.create_task(
                self._client.get(
                    f"{self._path}/merge_requests/{pr.number}/approvals",
                    model=_GitlabApprovals,
                )
            )
            await wait_for_read_tasks(details_task, approvals_task)
            details, approvals = details_task.result(), approvals_task.result()
            observed = pr.progress or PRProgress()
            decision = observed.review_decision
            if decision is None and approvals.approvals_required and not approvals.approvals_left:
                decision = "approved"
            pipeline = details.head_pipeline
            return observed.model_copy(
                update={
                    "check_rollup_status": None
                    if pipeline is None
                    else _PIPELINE_ROLLUPS.get(pipeline.status, "pending"),
                    "review_decision": decision,
                    "approvals": len(approvals.approved_by),
                    "behind": details.diverged_commits_count,
                }
            )

        return await _read_all({pr.number: progress(pr) for pr in prs})

    async def get_pr_merge_details(
        self,
        *,
        prs: Sequence[PullRequest],
    ) -> dict[int, PRProgress | None]:
        """Each MR's merge status and head pipeline; None for an MR whose head or target moved."""

        async def merge_details(pr: PullRequest) -> PRProgress | None:
            details = await self._details(pr)
            if details.sha != pr.head.sha or details.target_branch != pr.base.ref:
                return None

            pipeline = details.head_pipeline
            checks = (
                ()
                if pipeline is None
                else (
                    Check(name="pipeline", state=pipeline.status.upper(), url=pipeline.web_url),
                )
            )
            status = details.detailed_merge_status
            return PRProgress(
                conflicts=status == "conflict",
                merge_details=MergeDetails(mergeable=status, checks=checks),
            )

        return await _read_all({pr.number: merge_details(pr) for pr in prs})

    async def _details(
        self,
        pr: PullRequest,
    ) -> _GitlabMergeRequestDetails:
        return await self._client.get(
            f"{self._path}/merge_requests/{pr.number}",
            model=_GitlabMergeRequestDetails,
            query=(("include_diverged_commits_count", "true"),),
        )

    # Mutations and the reads only they need arrive with the commands that send them.

    async def get_pr_template(self) -> NoReturn:
        _unsupported()

    async def get_publication_branches(
        self,
        **_: object,
    ) -> NoReturn:
        _unsupported()

    async def get_publication_branches_by_suffix(
        self,
        **_: object,
    ) -> NoReturn:
        _unsupported()

    async def get_prs_by_base_refs(
        self,
        **_: object,
    ) -> NoReturn:
        _unsupported()

    async def create_pr(
        self,
        **_: object,
    ) -> NoReturn:
        _unsupported()

    async def list_pr_reviews(
        self,
        **_: object,
    ) -> tuple[PRReview, ...]:
        _unsupported()

    async def find_issue_comments_by_body_marker(
        self,
        **_: object,
    ) -> dict[int, PRComment | None]:
        _unsupported()

    async def find_issue_comments_and_revisions(
        self,
        **_: object,
    ) -> tuple[dict[str, dict[int, PRComment | None]], dict[int, tuple[PRRevision, ...]]]:
        _unsupported()

    async def create_issue_comment(
        self,
        **_: object,
    ) -> NoReturn:
        _unsupported()

    async def update_issue_comment(
        self,
        **_: object,
    ) -> NoReturn:
        _unsupported()

    async def delete_issue_comment(
        self,
        **_: object,
    ) -> NoReturn:
        _unsupported()

    async def request_reviewers(
        self,
        **_: object,
    ) -> NoReturn:
        _unsupported()

    async def add_labels(
        self,
        **_: object,
    ) -> NoReturn:
        _unsupported()

    async def update_pr(
        self,
        **_: object,
    ) -> NoReturn:
        _unsupported()

    async def close_pr(
        self,
        **_: object,
    ) -> NoReturn:
        _unsupported()


def _unsupported() -> NoReturn:
    raise ForgeError("this request is not supported on GitLab yet", kind="other")


async def _read_all[KeyT, ValueT](
    reads: Mapping[KeyT, Awaitable[ValueT]],
) -> dict[KeyT, ValueT]:
    """Run independent reads together and return their results by key."""

    tasks = {key: asyncio.ensure_future(read) for key, read in reads.items()}
    if tasks:
        await wait_for_read_tasks(*tasks.values())
    return {key: task.result() for key, task in tasks.items()}
