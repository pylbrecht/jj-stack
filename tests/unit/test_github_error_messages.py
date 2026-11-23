from __future__ import annotations

import asyncio

import httpx2
import pytest

from jj_stack.errors import EXIT_FORGE, CliError, resolve_exit_code
from jj_stack.forge.errors import ForgeError
from jj_stack.forge.github.client import REPO_NOT_FOUND_REASON, GithubClient
from jj_stack.forge.github.error_messages import observe_github_repo, repo_lookup_reason
from jj_stack.forge.github.resolution import GithubRepoAddress
from jj_stack.forge.target import ForgeTarget, UnresolvedForgeTarget, require_forge_target
from jj_stack.models.git import GitRemote
from jj_stack.ui import plain_text


def test_repo_lookup_treats_a_404_as_a_missing_or_hidden_repo() -> None:
    """Only a lookup of the repo itself may read a 404 as "no such repo for this token"."""

    missing = ForgeError("request failed (GitHub 404: Not Found)", kind="not_found")
    refused = ForgeError("request failed (GitHub 500)", kind="unavailable")

    assert repo_lookup_reason(missing) == REPO_NOT_FOUND_REASON
    assert repo_lookup_reason(refused) == "request failed (GitHub 500)"

    def observe(status: int) -> CliError:
        github = GithubClient(
            httpx2.AsyncClient(
                base_url="https://api.github.test",
                transport=httpx2.MockTransport(lambda request: httpx2.Response(status)),
            ),
            repo=GithubRepoAddress(owner="octo-org", repo="repo"),
        )
        with pytest.raises(CliError) as raised:
            asyncio.run(observe_github_repo(github, hint="rerun later"))
        return raised.value

    wrapped = observe(404)
    assert plain_text(wrapped.message) == "Could not inspect GitHub repo octo-org/repo"
    assert wrapped.hint == REPO_NOT_FOUND_REASON
    assert resolve_exit_code(wrapped) == EXIT_FORGE
    assert observe(500).hint == "rerun later"


def test_require_github_target_reports_the_earliest_resolution_failure() -> None:
    remote = GitRemote(name="origin", fetch_url="file:///repo", push_url="file:///repo")
    target = ForgeTarget(remote=remote, repo=GithubRepoAddress(owner="octo-org", repo="repo"))

    assert require_forge_target(target) is target
    with pytest.raises(CliError, match="No Git remote is configured"):
        require_forge_target(UnresolvedForgeTarget())
    with pytest.raises(CliError, match="no usable remote"):
        require_forge_target(UnresolvedForgeTarget(remote_error="no usable remote"))
    with pytest.raises(CliError, match="not a GitHub URL"):
        require_forge_target(UnresolvedForgeTarget(remote=remote, repo_error="not a GitHub URL"))
