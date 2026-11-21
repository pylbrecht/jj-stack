"""Shared reporting meaning for classified changes; commands only format and aggregate it."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

import jj_stack.ui as ui
from jj_stack.forge.models import CheckRollupStatus, PullRequest
from jj_stack.identifiers import ChangeId, short_change_id
from jj_stack.stack.change_state import (
    BranchClaimed,
    BranchDisagrees,
    BranchMissing,
    ChangeState,
    Closed,
    CompetingOpenPR,
    Edited,
    Landed,
    LookupFailed,
    Merged,
    NotInspected,
    PRAmbiguous,
    PRHeadMoved,
    PRIdentityMismatch,
    PRMissing,
    Published,
    PushedUnrecorded,
    Queued,
    Rewritten,
    Stop,
    Unpublished,
    UntrackedPRExists,
    WithPR,
)

type ReportStatus = Literal[
    "unsubmitted",
    "submitted",
    "open",
    "queued",
    "draft",
    "approved",
    "changes_requested",
    "review_required",
    "merged",
    "closed",
    "missing",
    "ambiguous",
    "link_mismatch",
    "branch_moved",
    "divergent",
    "unknown",
]


@dataclass(frozen=True, slots=True)
class ChangeReport:
    lifecycle: ReportStatus
    problem: ReportStatus | None
    divergent: bool
    needs_sync: bool
    needs_submit: bool
    checks: CheckRollupStatus | None
    merge_warnings: tuple[str, ...]
    # Whether GitHub would merge the open PR now; None while it has no answer.
    ready: bool | None
    approvals: int | None
    reason: ui.Message | None
    repair: ui.Message | None

    @property
    def status(self) -> ReportStatus:
        return "divergent" if self.divergent else self.problem or self.lifecycle


def report_change(state: ChangeState) -> ChangeReport:
    """Interpret every classified state without falling back to a healthy PR for a stop."""

    problem: ReportStatus | None = None
    match state:
        case Unpublished() | UntrackedPRExists() | BranchClaimed():
            lifecycle: ReportStatus = "unsubmitted"
        case NotInspected():
            lifecycle = "submitted"
        case LookupFailed():
            lifecycle = "submitted" if state.tracked is not None else "unsubmitted"
            problem = "unknown"
        case PRMissing() | PRAmbiguous():
            lifecycle = "submitted"
            problem = "missing" if isinstance(state, PRMissing) else "ambiguous"
        case Landed() | Merged():
            lifecycle = "merged"
        case (
            Published()
            | Edited()
            | PushedUnrecorded()
            | Rewritten()
            | Queued()
            | Closed()
            | PRIdentityMismatch()
            | CompetingOpenPR()
            | PRHeadMoved()
            | BranchMissing()
            | BranchDisagrees()
        ):
            lifecycle = _pr_status(state.pr)
            if isinstance(state, PRIdentityMismatch):
                problem = "link_mismatch"
            elif isinstance(state, CompetingOpenPR):
                problem = "ambiguous" if state.ambiguous else "link_mismatch"
            elif isinstance(state, (PRHeadMoved, BranchDisagrees)):
                problem = "branch_moved"
            elif isinstance(state, BranchMissing):
                problem = "link_mismatch"
    needs_sync = isinstance(state, (Landed, Merged))
    divergent = state.divergent and not needs_sync
    return ChangeReport(
        lifecycle=lifecycle,
        problem=problem,
        divergent=divergent,
        needs_sync=needs_sync,
        needs_submit=(
            isinstance(state, (Edited, PushedUnrecorded))
            and state.has_local_edits
            and not divergent
        ),
        checks=(
            state.pr.progress.check_rollup_status
            if isinstance(state, WithPR) and state.pr.state == "open" and state.pr.progress
            else None
        ),
        merge_warnings=(
            _merge_warnings(state.pr)
            if isinstance(state, WithPR) and state.pr.state == "open"
            else ()
        ),
        ready=_ready(state.pr) if isinstance(state, WithPR) and problem is None else None,
        approvals=state.pr.progress.approvals
        if isinstance(state, WithPR) and state.pr.progress
        else None,
        reason=state.reason if isinstance(state, Stop) else None,
        repair=_repair(state),
    )


def _repair(state: ChangeState) -> ui.Message | None:
    if isinstance(state, PRHeadMoved) and not state.rewrite_checked:
        # Inspection never fetches the head, so sync tells GitHub's rewrite from other work.
        sync = ui.cmd(f"jj-stack sync {short_change_id(state.change_id)}")
        return (moved_pr_branch_advice(t"Run {sync}", command="jj-stack sync"), ".")
    return state.repair if isinstance(state, Stop) else None


def moved_pr_branch_advice(step: ui.Message, *, command: str) -> ui.Message:
    """Explain what the command named in `step` does with a PR branch that moved."""

    return (
        t"{step}. If GitHub rewrote a PR branch while merging or rebasing the stack, "
        t"{ui.cmd(command)} updates it; if someone else pushed work to it, "
        t"{ui.cmd(command)} stops and explains what to do"
    )


def submittable_edits(reports: Mapping[ChangeId, ChangeReport]) -> tuple[ChangeId, ...]:
    """Changes a `submit` would refresh; none when anything in the stack would stop `submit`."""

    blocked = any(
        report.repair is not None or report.divergent or report.lifecycle in ("closed", "queued")
        for report in reports.values()
    )
    if blocked:
        return ()
    return tuple(change_id for change_id, report in reports.items() if report.needs_submit)


def stack_behind(states: Iterable[ChangeState]) -> tuple[int, str] | None:
    """How many commits the landing branch has that the stack's bottom PR lacks, and its name.

    The count describes the submitted PR, so it is left out once the change moved on locally.
    """

    for state in states:
        if not isinstance(state, WithPR) or (progress := state.pr.progress) is None:
            continue
        if progress.behind:
            if isinstance(state, Edited):
                return None
            return progress.behind, progress.landing_ref or state.pr.base.ref
    return None


def approval_count(count: int) -> str:
    return f"{count} approval{'' if count == 1 else 's'}"


READY_MARK = ui.semantic_text("✓", "signature status good")
NOT_READY_MARK = ui.semantic_text("✗", "error heading")


def _ready(pr: PullRequest) -> bool | None:
    if pr.state != "open" or pr.is_draft or pr.queued or pr.progress is None:
        return None
    return pr.progress.ready


def _pr_status(pr: PullRequest) -> ReportStatus:
    if pr.state == "merged":
        return "merged"
    if pr.state == "closed":
        return "closed"
    if pr.queued:
        return "queued"
    if pr.is_draft:
        return "draft"
    decision = pr.progress.review_decision if pr.progress is not None else None
    return decision or "open"


_STATUS_LABELS: dict[ReportStatus, tuple[str, str, str | None]] = {
    "unsubmitted": ("not submitted", "not submitted", None),
    "submitted": ("submitted", "submitted", None),
    "open": ("open", "open", None),
    "queued": ("queued", "queued", "hint"),
    "draft": ("draft", "drafts", "hint"),
    "approved": ("approved", "approved", "hint"),
    "changes_requested": ("changes requested", "changes requested", "warning"),
    "review_required": ("needs review", "need review", "hint"),
    "merged": ("sync needed", "merged, sync needed", "warning"),
    "closed": ("closed", "closed", "warning"),
    "missing": ("missing PR", "missing PRs", "warning"),
    "ambiguous": ("ambiguous PR", "ambiguous PRs", "warning"),
    "link_mismatch": ("saved PR needs repair", "saved PRs need repair", "warning"),
    "branch_moved": ("PR branch moved", "PR branches moved", "warning"),
    "divergent": ("divergent", "divergent changes", "warning"),
    "unknown": ("GitHub lookup failed", "GitHub lookups failed", "warning"),
}


def status_label(status: ReportStatus, *, count: int = 1) -> str | ui.SemanticText:
    singular, plural, severity = _STATUS_LABELS[status]
    label = singular if count == 1 else f"{count} {plural}"
    return label if severity is None else ui.semantic_text(label, severity, "heading")


def _merge_warnings(pr: PullRequest) -> tuple[str, ...]:
    """Report observed reasons; a forge's generic blocked answer is not itself a reason."""

    progress = pr.progress
    if progress is None:
        return ()
    warnings: list[str] = []
    details = progress.merge_details
    if progress.conflicts:
        warnings.append("merge conflicts")
    if progress.behind_base:
        warnings.append("behind base")
    if details is not None:
        if details.resolve_threads and details.unresolved_threads:
            warnings.append("unresolved review threads")
        warnings.extend(f"missing required check: {name}" for name in details.missing_checks)
        for check in details.merge_checks:
            if check.state not in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
                warnings.append(
                    f"merge check {check.state.lower().replace('_', ' ')}: {check.name}"
                )
    return tuple(warnings)
