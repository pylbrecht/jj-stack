from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence

import pytest

from jj_stack.forge.gitlab.client import GitlabClient
from jj_stack.forge.gitlab.forge import GitlabForge
from jj_stack.forge.gitlab.resolution import GitlabProjectAddress
from jj_stack.forge.gitlab.transport import GitlabResponse
from jj_stack.forge.models import PullRequest

_PROJECT = GitlabProjectAddress(host="gitlab.test", path="platform/tools/stacked-mrs")


class _ListTransport:
    """Answer every request with one page that holds `merge_requests`."""

    def __init__(
        self,
        merge_requests: list[dict[str, object]],
    ) -> None:
        self._body = json.dumps(merge_requests).encode()
        self.paths: list[str] = []

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: Sequence[tuple[str, str]] = (),
    ) -> GitlabResponse:
        self.paths.append(path)
        return GitlabResponse(
            status=200,
            headers={"content-type": "application/json"},
            body=self._body,
        )


def _merge_request(
    iid: int,
    **fields: object,
) -> dict[str, object]:
    return {
        "iid": iid,
        "title": f"MR {iid}",
        "description": "body",
        "state": "opened",
        "draft": False,
        "source_branch": f"jj-stack/change-{iid}",
        "target_branch": "main",
        "sha": f"{iid:040x}",
        "source_project_id": 7,
        "target_project_id": 7,
        "web_url": f"https://gitlab.test/platform/tools/stacked-mrs/-/merge_requests/{iid}",
        "merge_commit_sha": None,
        "squash_commit_sha": None,
        "detailed_merge_status": "mergeable",
        **fields,
    }


def _read(
    *merge_requests: dict[str, object],
) -> dict[int, PullRequest | None]:
    forge = GitlabForge(GitlabClient(_ListTransport(list(merge_requests))), project=_PROJECT)
    numbers = [int(str(merge_request["iid"])) for merge_request in merge_requests]
    return asyncio.run(forge.get_prs_by_numbers(pr_numbers=(*numbers, 99)))


def test_merge_requests_map_to_pull_requests_by_iid() -> None:
    prs = _read(
        _merge_request(1, state="merged", squash_commit_sha="a" * 40),
        _merge_request(2, state="locked", draft=True),
        _merge_request(3, source_project_id=8),
    )

    merged, locked, fork = prs[1], prs[2], prs[3]
    assert merged is not None and locked is not None and fork is not None
    assert (merged.state, merged.merge_commit_sha) == ("merged", "a" * 40)
    assert (locked.state, locked.is_draft) == ("open", True)
    assert merged.head.label == "platform/tools/stacked-mrs:jj-stack/change-1"
    assert fork.head.label != "platform/tools/stacked-mrs:jj-stack/change-3"
    assert prs[99] is None


@pytest.mark.parametrize(
    ("status", "ready", "conflicts", "behind_base", "blocked"),
    (
        ("mergeable", True, False, False, False),
        ("conflict", False, True, False, False),
        ("need_rebase", False, False, True, False),
        ("not_approved", False, False, False, True),
        ("checking", None, False, False, False),
    ),
)
def test_detailed_merge_status_decides_readiness_and_merge_warnings(
    status: str,
    ready: bool | None,
    conflicts: bool,
    behind_base: bool,
    blocked: bool,
) -> None:
    pr = _read(_merge_request(1, detailed_merge_status=status))[1]

    assert pr is not None and pr.progress is not None
    progress = pr.progress
    assert (progress.ready, progress.conflicts, progress.behind_base, progress.blocked) == (
        ready,
        conflicts,
        behind_base,
        blocked,
    )
    assert progress.merge_status == status


def test_reading_no_merge_requests_sends_no_request() -> None:
    transport = _ListTransport([_merge_request(1)])
    forge = GitlabForge(GitlabClient(transport), project=_PROJECT)

    prs = asyncio.run(forge.get_prs_by_numbers(pr_numbers=()))

    assert prs == {}
    assert transport.paths == []
