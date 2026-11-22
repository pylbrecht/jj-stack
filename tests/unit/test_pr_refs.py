from __future__ import annotations

import pytest

from jj_stack.errors import CliError
from jj_stack.forge.github.resolution import GithubRepoAddress
from jj_stack.forge.pr_refs import parse_repo_pr_reference


def test_parse_repo_pr_reference_accepts_matching_url_on_any_host() -> None:
    assert (
        parse_repo_pr_reference(
            reference="https://pr.example/octo-org/stacked-prs/pull/17",
            repo=GithubRepoAddress(owner="octo-org", repo="stacked-prs"),
        )
        == 17
    )


def test_parse_repo_pr_reference_rejects_non_pr_urls_and_other_repos() -> None:
    github_repo = GithubRepoAddress(owner="octo-org", repo="stacked-prs")
    with pytest.raises(CliError, match="is not a pull request number"):
        parse_repo_pr_reference(
            reference="https://github.com/octo-org/stacked-prs/issues/17",
            repo=github_repo,
        )
    with pytest.raises(CliError, match="does not match configured repo"):
        parse_repo_pr_reference(
            reference="https://github.com/other-org/stacked-prs/pull/17",
            repo=github_repo,
        )
