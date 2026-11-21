"""GitHub API response models."""

from typing import Literal, get_args

from pydantic import AliasPath, BaseModel, Field, field_validator, model_validator

from jj_stack.forge.github.details import GithubMergeQueueEntry
from jj_stack.forge.models import (
    CheckRollupStatus,
    PRHead,
    PRProgress,
    PullRequest,
    Repo,
    ReviewDecision,
)
from jj_stack.identifiers import CommitId

DEFAULT_PR_TEMPLATE_PATHS = tuple(
    f"{directory}{name}"
    for directory in (".github/", "", "docs/")
    for name in ("PULL_REQUEST_TEMPLATE.md", "pull_request_template.md")
)

_CHECK_ROLLUP_STATUSES: dict[str, CheckRollupStatus] = {
    "ERROR": "failed",
    "EXPECTED": "pending",
    "FAILURE": "failed",
    "PENDING": "pending",
    "SUCCESS": "passed",
}
_READY_MERGE_STATES = {"CLEAN", "HAS_HOOKS", "UNSTABLE"}
_UNREADY_MERGE_STATES = {"BLOCKED", "BEHIND", "DIRTY"}


class GithubRepo(Repo):
    """A repo read from GitHub, with the ID that pull request creation needs."""

    node_id: str


class GithubStackPR(BaseModel):
    """Pull request state embedded in a GitHub stack response."""

    head: PRHead
    number: int
    merged_at: str | None = None

    @property
    def is_historical(self) -> bool:
        return self.merged_at is not None


class GithubStack(BaseModel):
    """Ordered pull requests in one GitHub stack."""

    number: int
    prs: tuple[GithubStackPR, ...] = Field(alias="pull_requests", min_length=1)

    @property
    def pr_numbers(self) -> tuple[int, ...]:
        return tuple(pr.number for pr in self.prs)

    @property
    def historical_prs(self) -> tuple[GithubStackPR, ...]:
        return tuple(pr for pr in self.prs if pr.is_historical)

    @property
    def active_pr_numbers(self) -> tuple[int, ...]:
        return tuple(pr.number for pr in self.prs if not pr.is_historical)

    @property
    def has_merged_prefix(self) -> bool:
        """Whether every merged member sits below every active one."""

        return self.prs[: len(self.historical_prs)] == self.historical_prs


class GithubStackMergeDetails(BaseModel):
    """Details returned by GitHub's asynchronous stack merge endpoint."""

    expected_head_sha: CommitId | None = None
    merge_action: str | None = None
    merge_method: str | None = None
    message: str | None = None
    sha: CommitId | None = None
    uuid: str | None = None


class GithubStackMerge(BaseModel):
    """Pending or terminal asynchronous stack merge state."""

    details: GithubStackMergeDetails
    status: Literal["enqueued", "failed", "merged", "pending"]


class GithubStackMergeSubmission(BaseModel):
    """Typed submit response for an asynchronous merge request.

    `already_pending` reports GitHub's 409, which means an operation for this pull request is
    already in flight. It does not mean the merge conflicts; a conflict comes back later as a
    failed terminal status.
    """

    already_pending: bool
    result: GithubStackMerge


class GithubPullRequest(PullRequest):
    """A pull request read from GitHub, with the facts only the GitHub adapter uses."""

    node_id: str
    merge_queue_entry: GithubMergeQueueEntry | None = None
    queue_removal_reason: str | None = None
    queue_test_commit: CommitId | None = None
    # The branch the PR's GitHub stack lands on, when the PR is in one.
    stack_base_ref: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _from_graphql(cls, value: object) -> object:
        # A model built by field name has no GraphQL `baseRefName`.
        if not isinstance(value, dict) or "baseRefName" not in value:
            return value
        return _GraphqlPullRequest.model_validate(value).fields()


class _GraphqlHeadOwner(BaseModel):
    login: str


class _GraphqlPullRequest(BaseModel):
    """Pull request fields read from GitHub's GraphQL API."""

    base_ref: str = Field(alias="baseRefName")
    body: str | None = None
    check_rollup_status: CheckRollupStatus | None = Field(
        default=None, validation_alias=AliasPath("statusCheckRollup", "state")
    )
    head_ref: str = Field(alias="headRefName")
    head_sha: CommitId = Field(alias="headRefOid")
    head_owner: _GraphqlHeadOwner | None = Field(default=None, alias="headRepositoryOwner")
    # GitHub reports a null `headRef` once the head branch is deleted.
    head_branch_exists: bool = Field(default=True, validation_alias="headRef")
    html_url: str = Field(validation_alias="url")
    is_draft: bool = Field(default=False, validation_alias="isDraft")
    merge_queue_entry: GithubMergeQueueEntry | None = Field(
        default=None, validation_alias="mergeQueueEntry"
    )
    queue_removal_reason: str | None = Field(
        default=None, validation_alias=AliasPath("timelineItems", "nodes", 0, "reason")
    )
    queue_test_commit: CommitId | None = Field(
        default=None,
        validation_alias=AliasPath("timelineItems", "nodes", 0, "beforeCommit", "oid"),
    )
    merge_commit_sha: CommitId | None = Field(
        default=None, validation_alias=AliasPath("mergeCommit", "oid")
    )
    merge_state_status: str | None = Field(default=None, validation_alias="mergeStateStatus")
    node_id: str = Field(validation_alias="id")
    number: int
    review_decision: ReviewDecision | None = Field(
        default=None, validation_alias="reviewDecision"
    )
    stack_base_ref: str | None = Field(
        default=None, validation_alias=AliasPath("stack", "baseRefName")
    )
    state: str
    title: str

    def fields(self) -> dict[str, object]:
        status = self.merge_state_status
        ready = (
            True
            if status in _READY_MERGE_STATES
            else False
            if status in _UNREADY_MERGE_STATES
            else None
        )
        label = None if self.head_owner is None else f"{self.head_owner.login}:{self.head_ref}"
        return {
            "base": {"ref": self.base_ref},
            "body": self.body,
            "head": {"label": label, "ref": self.head_ref, "sha": self.head_sha},
            "head_branch_exists": self.head_branch_exists,
            "html_url": self.html_url,
            "is_draft": self.is_draft,
            "merge_commit_sha": self.merge_commit_sha,
            "merge_queue_entry": self.merge_queue_entry,
            "node_id": self.node_id,
            "number": self.number,
            "progress": PRProgress(
                check_rollup_status=self.check_rollup_status,
                review_decision=self.review_decision,
                ready=ready,
                conflicts=status == "DIRTY",
                behind_base=status == "BEHIND",
                blocked=status == "BLOCKED",
                merge_status=status,
                landing_ref=self.stack_base_ref or self.base_ref,
            ),
            "queue_removal_reason": self.queue_removal_reason,
            "queue_test_commit": self.queue_test_commit,
            "queued": self.merge_queue_entry is not None,
            "stack_base_ref": self.stack_base_ref,
            "state": self.state.lower(),
            "title": self.title,
        }

    @field_validator("check_rollup_status", mode="before")
    @classmethod
    def _normalize_check_rollup(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        status = _CHECK_ROLLUP_STATUSES.get(value, value)
        # GitHub may add states; an unknown one reads as no status rather than a failure.
        return status if status in get_args(CheckRollupStatus) else None

    @field_validator("head_branch_exists", mode="before")
    @classmethod
    def _head_ref_exists(cls, value: object) -> object:
        return value if isinstance(value, bool) else value is not None

    @field_validator("review_decision", mode="before")
    @classmethod
    def _normalize_review_decision(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        decision = value.lower()
        return decision if decision in get_args(ReviewDecision) else None
