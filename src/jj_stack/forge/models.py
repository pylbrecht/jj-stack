"""Forge-neutral repo and pull request models that commands and stack code read."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from jj_stack.identifiers import CommitId

CheckRollupStatus = Literal["failed", "passed", "pending"]
PRState = Literal["open", "closed", "merged"]
ReviewDecision = Literal["approved", "changes_requested", "review_required"]


class BranchRef(BaseModel):
    """A branch a pull request names, such as its base."""

    ref: str


class PRHead(BaseModel):
    """PR head branch and commit, with the owner label when available."""

    label: str | None = None
    ref: str
    sha: CommitId


class ReviewThread(BaseModel):
    """One review thread, with its first comment."""

    is_resolved: bool = Field(exclude=True)
    is_outdated: bool
    path: str
    line: int | None = None
    body: str = ""
    url: str | None = None


class Check(BaseModel):
    """The latest result of one check, by the forge's own state name."""

    name: str
    state: str
    url: str | None = None
    is_required: bool = Field(default=False, exclude=True)


class MergeDetails(BaseModel):
    """Live review and check evidence for detailed inspection, never saved in tracking."""

    # The forge's own mergeability answer, kept only for display.
    mergeable: str | None = None
    required_checks: tuple[str, ...] = ()
    resolve_threads: bool = False
    unresolved_threads: tuple[ReviewThread, ...] = ()
    checks: tuple[Check, ...] = ()
    merge_checks: tuple[Check, ...] = ()

    @property
    def missing_checks(self) -> tuple[str, ...]:
        reported = {check.name for check in (*self.checks, *self.merge_checks)}
        return tuple(name for name in self.required_checks if name not in reported)


class PRProgress(BaseModel):
    """How far an open pull request is from merging, as far as it was looked up."""

    check_rollup_status: CheckRollupStatus | None = None
    review_decision: ReviewDecision | None = None
    # Whether the forge would merge the PR now; None while it has no answer.
    ready: bool | None = None
    conflicts: bool = False
    behind_base: bool = False
    # The forge refuses the merge for a reason that only the merge details explain.
    blocked: bool = False
    # The forge's own merge status, kept only for display.
    merge_status: str | None = None
    # The branch the PR lands on, which a forge stack can make differ from its base.
    landing_ref: str | None = None
    # Approvals from reviewers with write access, when looked up.
    approvals: int | None = None
    # How many commits the landing branch has that this PR's head lacks, when looked up.
    behind: int | None = None
    merge_details: MergeDetails | None = None


class PullRequest(BaseModel):
    """One pull request as observed on the forge."""

    base: BranchRef
    body: str | None = None
    head: PRHead
    head_branch_exists: bool = True
    html_url: str
    is_draft: bool = False
    merge_commit_sha: CommitId | None = None
    number: int
    # Whether the PR waits in the forge's merge queue.
    queued: bool = False
    state: PRState
    title: str
    # None when the read that returned the PR does not observe it.
    progress: PRProgress | None = None


class RepoPermissions(BaseModel):
    """The token's permissions on the repo, which a forge reports only to signed-in requests."""

    push: bool


class Repo(BaseModel):
    """The repo that receives the pull requests, as observed on the forge."""

    allow_merge_commit: bool | None = None
    allow_rebase_merge: bool | None = None
    allow_squash_merge: bool | None = None
    default_branch: str | None
    full_name: str
    permissions: RepoPermissions | None = None


class PRComment(BaseModel):
    """One comment on a pull request's conversation."""

    body: str
    id: int


class PRRevision(BaseModel):
    """One available pull request revision observed from a force push."""

    before_commit_id: CommitId
    commit_id: CommitId
    is_current: bool
    version: int


class PRReviewAuthor(BaseModel):
    """The account that left a review."""

    login: str


class PRReview(BaseModel):
    """One review of a pull request."""

    id: int
    state: str
    user: PRReviewAuthor | None = None
