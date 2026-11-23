"""The `Forge` requests of commands and stack code, answered by one GitLab project.

A GitLab merge request (MR) is the forge's pull request, and its project-scoped `iid` is the PR
number. GitLab keeps the draft state in the title: a `Draft:` prefix marks a draft MR, so the
adapter removes the prefix from the title it reports and adds it to the titles it writes.
A request that no supported command sends yet stops before it sends anything.
"""

from __future__ import annotations

import asyncio
import itertools
import re
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
    PRReviewAuthor,
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
# GitLab's own rule for the title prefixes that mark an MR as a draft.
_DRAFT_PREFIX_RE = re.compile(r"^\s*(?:(?:\[draft\]|\(draft\)|draft:)\s*)+", re.IGNORECASE)
# The reviewer states that count as a review, by the names that re-request reads.
_REVIEW_STATES = {"approved": "APPROVED", "requested_changes": "CHANGES_REQUESTED"}
_SET_DRAFT = """
mutation($projectPath: ID!, $iid: String!, $draft: Boolean!) {
  mergeRequestSetDraft(input: {projectPath: $projectPath, iid: $iid, draft: $draft}) {
    errors
  }
}
"""
# APPEND keeps the reviewers that the MR has; the REST `reviewer_ids` field replaces them.
_SET_REVIEWERS = """
mutation($projectPath: ID!, $iid: String!, $usernames: [String!]!) {
  mergeRequestSetReviewers(
    input: {
      projectPath: $projectPath, iid: $iid, reviewerUsernames: $usernames, operationMode: APPEND
    }
  ) {
    errors
  }
}
"""
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
    name: str
    commit: _GitlabCommit


class _GitlabTemplate(BaseModel):
    content: str


class _GitlabUser(BaseModel):
    username: str


class _GitlabReviewer(BaseModel):
    user: _GitlabUser
    state: str


class _GitlabNote(BaseModel):
    id: int
    body: str
    # GitLab writes system notes itself, such as "changed the description".
    system: bool = False


class _GitlabDiffVersion(BaseModel):
    head_commit_sha: CommitId


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
                "title": _DRAFT_PREFIX_RE.sub("", self.title, count=1)
                if self.draft
                else self.title,
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

    async def get_pr_template(self) -> str:
        """Read the project's `Default` merge request description template, if it has one."""

        try:
            template = await self._client.get(
                f"{self._path}/templates/merge_requests/Default", model=_GitlabTemplate
            )
        except ForgeError as error:
            if error.kind == "not_found":
                return ""
            raise
        return template.content

    async def get_publication_branches(
        self,
        *,
        branches: Sequence[str],
        include_pr_template: bool,
    ) -> tuple[dict[str, CommitId], str]:
        return await self._with_template(
            self.get_branch_targets(branches=branches), include_pr_template=include_pr_template
        )

    async def get_publication_branches_by_suffix(
        self,
        *,
        branch_prefix: str,
        suffixes: Sequence[str],
        include_pr_template: bool,
    ) -> tuple[dict[str, CommitId], str]:
        async def ending_in(
            suffix: str,
        ) -> dict[str, CommitId]:
            # GitLab's branch search takes either a `^` prefix or a `$` suffix anchor.
            branches = await self._client.get_pages(
                f"{self._path}/repository/branches",
                model=_GitlabBranch,
                query=(("search", f"{suffix}$"),),
            )
            return {
                branch.name: branch.commit.id
                for branch in branches
                if branch.name.startswith(branch_prefix) and branch.name.endswith(suffix)
            }

        async def targets() -> dict[str, CommitId]:
            found = await _read_all({suffix: ending_in(suffix) for suffix in set(suffixes)})
            return {name: target for page in found.values() for name, target in page.items()}

        return await self._with_template(targets(), include_pr_template=include_pr_template)

    async def _with_template(
        self,
        targets: Awaitable[dict[str, CommitId]],
        *,
        include_pr_template: bool,
    ) -> tuple[dict[str, CommitId], str]:
        targets_task = asyncio.ensure_future(targets)
        template_task = asyncio.ensure_future(
            self.get_pr_template() if include_pr_template else asyncio.sleep(0, result="")
        )
        await wait_for_read_tasks(targets_task, template_task)
        return targets_task.result(), template_task.result()

    async def get_prs_by_base_refs(
        self,
        *,
        base_refs: Sequence[str],
    ) -> dict[str, tuple[PullRequest, ...]]:
        async def targeting(
            ref: str,
        ) -> tuple[PullRequest, ...]:
            merge_requests = await self._client.get_pages(
                f"{self._path}/merge_requests",
                model=_GitlabMergeRequest,
                query=(("target_branch", ref),),
            )
            # As on GitHub, a merged MR no longer needs its target branch.
            return tuple(
                mr.pull_request(self._project) for mr in merge_requests if mr.state != "merged"
            )

        return await _read_all({ref: targeting(ref) for ref in set(base_refs)})

    async def create_pr(
        self,
        *,
        base: str,
        body: str,
        draft: bool,
        head: str,
        repo: Repo,  # noqa: ARG002 - the project path already names the GitLab project.
        title: str,
    ) -> PullRequest:
        merge_request = await self._client.send(
            "POST",
            f"{self._path}/merge_requests",
            body={
                "source_branch": head,
                "target_branch": base,
                "title": _titled(title, draft=draft),
                "description": body,
            },
            model=_GitlabMergeRequest,
        )
        return merge_request.pull_request(self._project)

    async def update_pr(
        self,
        *,
        pr: PullRequest,
        base: str | None = None,
        body: str | None = None,
        title: str | None = None,
        draft: bool | None = None,
    ) -> PullRequest:
        """Apply the given fields, then the draft state, which GitLab keeps in the title."""

        fields = {
            "target_branch": base,
            "description": body,
            "title": None if title is None else _titled(title, draft=pr.is_draft),
        }
        changed = {name: value for name, value in fields.items() if value is not None}
        if changed:
            merge_request = await self._client.send(
                "PUT",
                self._merge_request_path(pr.number),
                body=changed,
                model=_GitlabMergeRequest,
            )
            pr = merge_request.pull_request(self._project)

        if draft is None:
            return pr

        await self._client.mutate(
            _SET_DRAFT,
            mutation="mergeRequestSetDraft",
            variables={"projectPath": self._project.path, "iid": str(pr.number), "draft": draft},
        )
        return pr.model_copy(update={"is_draft": draft})

    async def list_pr_reviews(
        self,
        *,
        pr_number: int,
    ) -> tuple[PRReview, ...]:
        """Each reviewer's current review state; GitLab keeps no history of earlier reviews."""

        reviewers = await self._client.get_pages(
            f"{self._merge_request_path(pr_number)}/reviewers", model=_GitlabReviewer
        )
        return tuple(
            PRReview(
                id=index,
                state=_REVIEW_STATES.get(reviewer.state, reviewer.state.upper()),
                user=PRReviewAuthor(login=reviewer.user.username),
            )
            for index, reviewer in enumerate(reviewers)
        )

    async def find_issue_comments_and_revisions(
        self,
        *,
        body_markers: Sequence[str],
        pr_numbers: Sequence[int],
        revision_limit: int | None,
    ) -> tuple[dict[str, dict[int, PRComment | None]], dict[int, tuple[PRRevision, ...]]]:
        """Each MR's first note with each marker, and its pushed versions as revisions."""

        async def notes(
            number: int,
        ) -> list[_GitlabNote]:
            return await self._client.get_pages(
                f"{self._merge_request_path(number)}/notes",
                model=_GitlabNote,
                query=(("sort", "asc"), ("order_by", "created_at")),
            )

        async def revisions(
            number: int,
        ) -> tuple[PRRevision, ...]:
            if revision_limit is None:
                return ()

            versions = await self._client.get_pages(
                f"{self._merge_request_path(number)}/versions", model=_GitlabDiffVersion
            )
            return _revisions(versions)[-revision_limit:]

        numbers = set(pr_numbers)
        notes_task = asyncio.ensure_future(_read_all({n: notes(n) for n in numbers}))
        revisions_task = asyncio.ensure_future(_read_all({n: revisions(n) for n in numbers}))
        await wait_for_read_tasks(notes_task, revisions_task)
        notes_by_pr = notes_task.result()
        comments = {
            marker: {
                number: next(
                    (
                        PRComment(id=note.id, body=note.body)
                        for note in pr_notes
                        if not note.system and marker in note.body
                    ),
                    None,
                )
                for number, pr_notes in notes_by_pr.items()
            }
            for marker in body_markers
        }
        return comments, revisions_task.result()

    async def create_issue_comment(
        self,
        *,
        issue_number: int,
        body: str,
    ) -> None:
        await self._client.send(
            "POST",
            f"{self._merge_request_path(issue_number)}/notes",
            body={"body": body},
            model=_GitlabNote,
        )

    async def update_issue_comment(
        self,
        *,
        issue_number: int,
        comment_id: int,
        body: str,
    ) -> None:
        await self._client.send(
            "PUT",
            f"{self._merge_request_path(issue_number)}/notes/{comment_id}",
            body={"body": body},
            model=_GitlabNote,
        )

    async def delete_issue_comment(
        self,
        *,
        issue_number: int,
        comment_id: int,
    ) -> None:
        await self._client.delete(f"{self._merge_request_path(issue_number)}/notes/{comment_id}")

    async def request_reviewers(
        self,
        *,
        pr_number: int,
        reviewers: list[str],
        team_reviewers: list[str],
    ) -> None:
        # Submit stops on team reviewers before it changes anything, so this is a safeguard.
        if team_reviewers:
            raise ForgeError("GitLab has no team reviewers", kind="invalid")

        await self._client.mutate(
            _SET_REVIEWERS,
            mutation="mergeRequestSetReviewers",
            variables={
                "projectPath": self._project.path,
                "iid": str(pr_number),
                "usernames": reviewers,
            },
        )

    async def add_labels(
        self,
        *,
        issue_number: int,
        labels: list[str],
    ) -> None:
        await self._client.send(
            "PUT",
            self._merge_request_path(issue_number),
            body={"add_labels": ",".join(labels)},
            model=_GitlabMergeRequest,
        )

    def _merge_request_path(
        self,
        iid: int,
    ) -> str:
        return f"{self._path}/merge_requests/{iid}"

    # The requests that only `sync` and `cleanup` send arrive with those commands.

    async def find_issue_comments_by_body_marker(
        self,
        **_: object,
    ) -> dict[int, PRComment | None]:
        _unsupported()

    async def close_pr(
        self,
        **_: object,
    ) -> NoReturn:
        _unsupported()


def _titled(
    title: str,
    *,
    draft: bool,
) -> str:
    return f"Draft: {title}" if draft else title


def _revisions(
    versions: Sequence[_GitlabDiffVersion],
) -> tuple[PRRevision, ...]:
    """Turn MR versions, newest first, into one revision per change of the head.

    GitLab also adds a version when the target branch changes, with the same head as before.
    """

    heads: list[CommitId] = []
    for version in reversed(versions):
        if not heads or heads[-1] != version.head_commit_sha:
            heads.append(version.head_commit_sha)
    return tuple(
        PRRevision(
            before_commit_id=before,
            commit_id=after,
            is_current=index == len(heads) - 1,
            version=index + 1,
        )
        for index, (before, after) in enumerate(itertools.pairwise(heads), start=1)
    )


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
