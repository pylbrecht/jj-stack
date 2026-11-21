"""The requests that commands and stack code make of a forge, independent of its API."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from jj_stack.forge.models import (
    PRComment,
    PRProgress,
    PRReview,
    PRRevision,
    PullRequest,
    Repo,
)
from jj_stack.identifiers import CommitId


class RepoAddress(Protocol):
    """Where the forge's repo lives, as far as messages and links need it."""

    @property
    def full_name(self) -> str: ...


class Forge(Protocol):
    """One forge repo that receives the stack's pull requests.

    Mutations take the observed `Repo` or `PullRequest`, so an adapter can bind the write to the
    version it read.
    """

    @property
    def repo(self) -> RepoAddress: ...

    async def get_repo(self) -> Repo: ...

    async def get_pr_template(self) -> str: ...

    async def get_branch_targets(
        self,
        *,
        branches: Sequence[str],
    ) -> dict[str, CommitId]: ...

    async def get_publication_branches(
        self,
        *,
        branches: Sequence[str],
        include_pr_template: bool,
    ) -> tuple[dict[str, CommitId], str]: ...

    async def get_publication_branches_by_suffix(
        self,
        *,
        branch_prefix: str,
        suffixes: Sequence[str],
        include_pr_template: bool,
    ) -> tuple[dict[str, CommitId], str]: ...

    async def get_pr(
        self,
        *,
        pr_number: int,
    ) -> PullRequest: ...

    async def get_prs_by_numbers(
        self,
        *,
        pr_numbers: Sequence[int],
    ) -> dict[int, PullRequest | None]: ...

    async def get_open_prs_by_head_refs(
        self,
        *,
        head_refs: Sequence[str],
    ) -> dict[str, tuple[PullRequest, ...]]: ...

    async def get_prs_by_base_refs(
        self,
        *,
        base_refs: Sequence[str],
    ) -> dict[str, tuple[PullRequest, ...]]: ...

    async def create_pr(
        self,
        *,
        base: str,
        body: str,
        draft: bool,
        head: str,
        repo: Repo,
        title: str,
    ) -> PullRequest: ...

    async def list_pr_reviews(
        self,
        *,
        pr_number: int,
    ) -> tuple[PRReview, ...]: ...

    async def find_issue_comments_by_body_marker(
        self,
        *,
        body_marker: str,
        pr_numbers: Sequence[int],
    ) -> dict[int, PRComment | None]: ...

    async def find_issue_comments_and_revisions(
        self,
        *,
        body_markers: Sequence[str],
        pr_numbers: Sequence[int],
        revision_limit: int | None,
    ) -> tuple[
        dict[str, dict[int, PRComment | None]],
        dict[int, tuple[PRRevision, ...]],
    ]: ...

    async def get_pr_merge_details(
        self,
        *,
        prs: Sequence[PullRequest],
    ) -> dict[int, PRProgress | None]: ...

    async def get_pr_progress(
        self,
        *,
        prs: Sequence[PullRequest],
    ) -> dict[int, PRProgress]: ...

    async def create_issue_comment(
        self,
        *,
        issue_number: int,
        body: str,
    ) -> None: ...

    async def update_issue_comment(
        self,
        *,
        comment_id: int,
        body: str,
    ) -> None: ...

    async def delete_issue_comment(
        self,
        *,
        comment_id: int,
    ) -> None: ...

    async def request_reviewers(
        self,
        *,
        pr_number: int,
        reviewers: list[str],
        team_reviewers: list[str],
    ) -> None: ...

    async def add_labels(
        self,
        *,
        issue_number: int,
        labels: list[str],
    ) -> None: ...

    async def update_pr(
        self,
        *,
        pr: PullRequest,
        base: str | None = None,
        body: str | None = None,
        title: str | None = None,
        draft: bool | None = None,
    ) -> PullRequest: ...

    async def close_pr(
        self,
        *,
        pr_number: int,
    ) -> None: ...
