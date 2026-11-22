"""The records the merge command shares with a forge's merge strategy."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal, Protocol

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.forge.github.resolution import GithubRepoAddress
from jj_stack.forge.models import PullRequest
from jj_stack.identifiers import ChangeId, CommitId
from jj_stack.models.tracking import PRIdentity
from jj_stack.ui import Message

_CHECK_POLL_INTERVAL_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class MergeAction:
    """One planned, applied, or blocked merge action."""

    kind: str
    body: Message
    status: Literal["applied", "blocked", "planned"]


@dataclass(frozen=True, slots=True)
class MergeResult:
    """Rendered merge result for one selected local stack."""

    actions: tuple[MergeAction, ...]
    pending: Literal["pending", "enqueued"] | None = None
    final_trunk_commit_id: CommitId | None = None
    merged_change_ids: tuple[ChangeId, ...] = ()

    @property
    def applied(self) -> bool:
        return any(action.status == "applied" for action in self.actions)

    @property
    def blocked(self) -> bool:
        return any(action.status == "blocked" for action in self.actions)


@dataclass(frozen=True, slots=True)
class MergeExecutionInputs:
    """Mutation dependencies independent of normal stack/status preparation."""

    repo: GithubRepoAddress
    selected_head: str
    sync_head: str
    trunk_branch: str

    @property
    def merge_command(self) -> Message:
        return ui.cmd(f"jj-stack merge {self.selected_head}")

    @property
    def sync_command(self) -> Message:
        return ui.cmd(f"jj-stack sync {self.sync_head}")

    @property
    def sync_after_github(self) -> Message:
        return t"Run {self.sync_command} after GitHub finishes."


@dataclass(frozen=True, slots=True)
class MergeChange:
    """One selected change plus its GitHub link."""

    base_ref: str
    change_id: ChangeId
    commit_id: CommitId
    identity: PRIdentity
    # The PR as observed while planning, which binds a later mutation to that version. Its
    # live progress changes between plans, so plans compare by the identity above.
    pr: PullRequest = field(compare=False)


@dataclass(frozen=True, slots=True)
class MergePlan:
    """Resolved merge plan for the selected stack."""

    boundary_action: MergeAction | None
    planned_changes: tuple[MergeChange, ...]
    linked_changes: tuple[MergeChange, ...]


@dataclass(frozen=True, slots=True)
class ChecksPending:
    changes: tuple[MergeChange, ...]
    progress: tuple[str, ...]

    async def wait(self) -> None:
        """Wait without holding the repo lock; the caller must replan before mutating."""

        with console.spinner(description="Waiting for required checks"):
            await asyncio.sleep(_CHECK_POLL_INTERVAL_SECONDS)


class PendingMerge(Protocol):
    """A merge request the forge accepted but has not finished."""

    @property
    def pr_number(self) -> int: ...

    @property
    def status(self) -> Literal["pending", "enqueued"]: ...

    def result(self) -> MergeResult:
        """Report the request as still pending."""
        ...

    async def wait(self) -> MergeResult:
        """Wait for the forge to finish the request, then report its outcome."""
        ...


class StackMergeRequest(Protocol):
    """The forge's merge of a planned stack prefix, ready to preview or submit."""

    def preview(self) -> MergeResult: ...

    async def submit(
        self,
        *,
        wait_for_checks: bool,
    ) -> MergeResult | PendingMerge | ChecksPending: ...
