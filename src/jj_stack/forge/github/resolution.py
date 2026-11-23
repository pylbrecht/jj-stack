"""Read the GitHub repo that a Git remote names, and the base branch at trunk."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

import jj_stack.ui as ui
from jj_stack.errors import CliError, ErrorHint, ErrorMessage
from jj_stack.forge.models import Repo
from jj_stack.forge.remote_url import parse_remote_url
from jj_stack.identifiers import CommitId
from jj_stack.models.git import GitRemote
from jj_stack.pr_branch_namespace import current_pr_branch_namespace

_PR_URL_PATH_RE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>[0-9]+)/?$")


@dataclass(frozen=True, slots=True)
class GithubRepoAddress:
    """GitHub repo coordinates parsed from a Git remote URL."""

    owner: str
    repo: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    def pr_url(
        self,
        pr_number: int,
    ) -> str:
        return f"https://github.com/{self.full_name}/pull/{pr_number}"

    def head_label(
        self,
        ref: str,
    ) -> str:
        return f"{self.owner}:{ref}"

    def parse_pr_url_path(
        self,
        path: str,
    ) -> tuple[str, int] | None:
        match = _PR_URL_PATH_RE.fullmatch(path)
        if match is None:
            return None

        return f"{match['owner']}/{match['repo']}", int(match["number"])


def parse_github_repo(remote: GitRemote) -> GithubRepoAddress | None:
    """Parse one GitHub repo target from a Git remote's URLs."""

    fetch_repo = _parse_github_url(remote.fetch_url)
    push_repo = _parse_github_url(remote.push_url)
    if fetch_repo != push_repo:
        return None
    return fetch_repo


def _parse_github_url(remote_url: str) -> GithubRepoAddress | None:
    """Parse a GitHub repo target from one Git remote URL."""

    location = parse_remote_url(remote_url)
    if location is None or len(location.path) != 2:
        return None
    owner, repo = location.path
    return GithubRepoAddress(owner=owner, repo=repo)


def github_repo_failure(remote: GitRemote) -> tuple[ErrorMessage, ErrorHint]:
    """Say why a remote does not name one GitHub repo, and what to do about it.

    A remote with a single URL that is not `owner/repo` has nothing to disagree with itself
    about, so it must not be told its fetch and push URLs disagree.
    """

    name = ui.bookmark(remote.name)
    if _parse_github_url(remote.fetch_url) != _parse_github_url(remote.push_url):
        return (
            t"Remote {name}'s fetch and push URLs do not name the same GitHub repo.",
            t"Point both URLs at the same GitHub repo.",
        )
    return (
        t"Could not read a GitHub owner and repo from remote {name}'s URL "
        t"{ui.code(remote.fetch_url)}.",
        t"Point {name} at a GitHub URL such as {ui.code('https://github.com/owner/repo.git')}.",
    )


def resolve_trunk_branch(
    *,
    branches_at_trunk: Sequence[str],
    github_repo_state: Repo,
    remote: GitRemote,
    trunk_commit_id: CommitId,
) -> tuple[str, dict[str, CommitId]]:
    """Resolve the GitHub base branch used for bottom-of-stack pull requests.

    `branches_at_trunk` holds the remote bookmarks jj observes at `trunk()`. A local trunk
    that is merely behind GitHub still carries the default branch, so it resolves normally.
    """

    namespace = current_pr_branch_namespace()
    matches = tuple(
        sorted(branch for branch in branches_at_trunk if not namespace.contains(branch))
    )
    trunk_targets = {branch: trunk_commit_id for branch in matches}
    default_branch = github_repo_state.default_branch
    if default_branch:
        # No match means trunk() is not one of this remote's bookmarks, so the default branch
        # is the only evidence. A match on some *other* branch alone is positive evidence that
        # GitHub's default branch is not the branch jj calls trunk, and basing pull requests
        # on it would be wrong.
        if matches and default_branch not in matches:
            raise CliError(
                t"GitHub's default branch for {ui.bookmark(remote.name)} is "
                t"{ui.bookmark(default_branch)}, but {ui.revset('trunk()')} is "
                t"{ui.join(ui.bookmark, matches)}.",
                hint=(
                    t"Point {ui.revset('trunk()')} at {ui.bookmark(default_branch)}, or change "
                    t"the repo's default branch on GitHub. Both must name the branch your "
                    t"pull requests should target."
                ),
            )
        return default_branch, trunk_targets
    if len(matches) == 1:
        return matches[0], trunk_targets
    if len(matches) > 1:
        raise CliError(
            t"Could not determine the trunk branch because multiple remote branches on "
            t"{ui.bookmark(remote.name)} point at {ui.revset('trunk()')}: "
            t"{ui.join(ui.bookmark, matches)}."
        )
    raise CliError(
        t"Could not determine the trunk branch for remote {ui.bookmark(remote.name)}.",
        hint=(
            t"Set the repo's default branch on GitHub, or create one "
            t"remote branch that points at {ui.revset('trunk()')}."
        ),
    )
