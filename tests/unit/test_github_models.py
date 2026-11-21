from __future__ import annotations

import pytest

from jj_stack.forge.github.models import GithubPullRequest, GithubStack
from jj_stack.forge.models import PRProgress


def _graphql_pr_payload(
    review_decision: object,
    *,
    check_rollup_state: object = None,
) -> dict[str, object]:
    return {
        "baseRefName": "main",
        "headRefName": "jj-stack/feature-1",
        "headRefOid": "head-commit-id",
        "headRepositoryOwner": {"login": "octo-org"},
        "mergeQueueEntry": None,
        "id": "PR_1",
        "number": 1,
        "reviewDecision": review_decision,
        "state": "OPEN",
        "statusCheckRollup": (
            None if check_rollup_state is None else {"state": check_rollup_state}
        ),
        "title": "feature 1",
        "url": "https://github.test/octo-org/stacked-prs/pull/1",
    }


def _progress(
    payload: dict[str, object],
) -> PRProgress:
    progress = GithubPullRequest.model_validate(payload).progress
    assert progress is not None
    return progress


def test_graphql_pr_statuses_normalize_known_states_and_drop_unknown() -> None:
    approved = GithubPullRequest.model_validate(
        _graphql_pr_payload("APPROVED", check_rollup_state="SUCCESS")
    )
    changes = _progress(_graphql_pr_payload("CHANGES_REQUESTED", check_rollup_state="FAILURE"))
    errored = _progress(_graphql_pr_payload(None, check_rollup_state="ERROR"))
    pending = _progress(_graphql_pr_payload(None, check_rollup_state="PENDING"))
    expected = _progress(_graphql_pr_payload(None, check_rollup_state="EXPECTED"))
    required = _progress(_graphql_pr_payload("REVIEW_REQUIRED"))
    unknown = _progress(_graphql_pr_payload("FUTURE_STATE", check_rollup_state="FUTURE_STATE"))

    assert approved.progress is not None
    assert approved.progress.review_decision == "approved"
    assert approved.progress.check_rollup_status == "passed"
    assert approved.head.sha == "head-commit-id"
    assert changes.review_decision == "changes_requested"
    assert changes.check_rollup_status == "failed"
    assert errored.check_rollup_status == "failed"
    assert pending.check_rollup_status == "pending"
    assert expected.check_rollup_status == "pending"
    assert required.review_decision == "review_required"
    assert unknown.review_decision is None
    assert unknown.check_rollup_status is None


@pytest.mark.parametrize(
    ("merge_state", "expected"),
    (
        ("CLEAN", (True, False, False, False)),
        ("UNSTABLE", (True, False, False, False)),
        ("BLOCKED", (False, False, False, True)),
        ("BEHIND", (False, False, True, False)),
        ("DIRTY", (False, True, False, False)),
        ("UNKNOWN", (None, False, False, False)),
    ),
)
def test_graphql_pr_merge_state_reads_as_neutral_merge_answers(
    merge_state: str,
    expected: tuple[bool | None, bool, bool, bool],
) -> None:
    payload = {**_graphql_pr_payload(None), "mergeStateStatus": merge_state}

    progress = _progress(payload)

    assert (
        progress.ready,
        progress.conflicts,
        progress.behind_base,
        progress.blocked,
    ) == expected
    assert progress.merge_status == merge_state


def test_github_stack_splits_history_and_reports_a_merged_member_above_an_active_one() -> None:
    historical = {
        "head": {"ref": "jj-stack/one", "sha": "head-one"},
        "merged_at": "2026-07-23T12:00:00Z",
        "number": 1,
    }
    active = {
        "head": {"ref": "jj-stack/two", "sha": "head-two"},
        "number": 2,
    }

    stack = GithubStack.model_validate({"number": 7, "pull_requests": [historical, active]})

    assert stack.active_pr_numbers == (2,)
    assert stack.has_merged_prefix
    reversed_stack = GithubStack.model_validate(
        {"number": 7, "pull_requests": [active, historical]}
    )
    assert not reversed_stack.has_merged_prefix
