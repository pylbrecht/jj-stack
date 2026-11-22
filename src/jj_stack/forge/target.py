"""Select the Git remote that stack commands use, and read the forge repo it names."""

from __future__ import annotations

from dataclasses import dataclass

import jj_stack.forge.github.resolution as github_resolution
import jj_stack.ui as ui
from jj_stack.errors import CliError, ErrorMessage, error_message
from jj_stack.forge.github.resolution import GithubRepoAddress
from jj_stack.forge.protocol import RepoAddress
from jj_stack.forge.vocabulary import forge_name
from jj_stack.models.git import GitRemote
from jj_stack.ui import Message


@dataclass(frozen=True, slots=True)
class ForgeTarget:
    """A fully resolved forge target: the selected Git remote and its repo."""

    remote: GitRemote
    repo: GithubRepoAddress

    # A resolved target carries no diagnostics. These mirror UnresolvedForgeTarget so
    # degraded-mode consumers can read errors off either arm without narrowing.
    @property
    def remote_error(self) -> None:
        return None

    @property
    def repo_error(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class UnresolvedForgeTarget:
    """A forge target that could not be fully resolved.

    Encodes three degraded states:

    - no Git remotes exist at all: every field is None
    - remote selection failed: only `remote_error` is set
    - a remote resolved but does not name a forge repo: `remote` and `repo_error` are set
    """

    remote: GitRemote | None = None
    remote_error: ErrorMessage | None = None
    repo_error: ErrorMessage | None = None


def select_submit_remote(remotes: tuple[GitRemote, ...]) -> GitRemote:
    """Resolve the Git remote used by stack commands."""

    remotes_by_name = {remote.name: remote for remote in remotes}
    if "origin" in remotes_by_name:
        return remotes_by_name["origin"]
    if len(remotes) == 1:
        return remotes[0]
    raise CliError(
        "Could not determine which Git remote to use.",
        hint=t"Run {ui.cmd('jj git remote list')} to check the remotes. Name the intended "
        t"remote {ui.bookmark('origin')}, or configure exactly one remote.",
    )


def resolve_forge_target(
    remotes: tuple[GitRemote, ...],
) -> ForgeTarget | UnresolvedForgeTarget:
    """Resolve the optional remote and forge target used by read-mostly commands."""

    if not remotes:
        return UnresolvedForgeTarget()
    try:
        remote = select_submit_remote(remotes)
    except CliError as error:
        return UnresolvedForgeTarget(remote_error=error_message(error))

    repo = github_resolution.parse_github_repo(remote)
    if repo is None:
        message, hint = github_resolution.github_repo_failure(remote)
        return UnresolvedForgeTarget(remote=remote, repo_error=(message, " ", hint))
    return ForgeTarget(remote=remote, repo=repo)


def require_forge_target(target: ForgeTarget | UnresolvedForgeTarget) -> ForgeTarget:
    """Stop with the resolution error when the repo has no usable forge remote."""

    if isinstance(target, ForgeTarget):
        return target
    raise CliError(
        target.remote_error or target.repo_error or "No Git remote is configured.",
        hint=t"Configure one {forge_name()} remote, then rerun. "
        t"{ui.cmd('jj-stack doctor')} reports what it found.",
    )


def forge_unavailable_messages(
    *,
    forge_error: Message | None,
    repo: RepoAddress | None,
    remote: GitRemote | None,
    remote_error: Message | None,
) -> tuple[Message, ...]:
    """Render the repo-level warning lines for an unavailable remote or forge target."""

    messages: list[Message] = []
    if remote is None:
        messages.append("No Git remote is configured." if remote_error is None else remote_error)
    if forge_error is not None:
        unavailable = f"{forge_name()} unavailable"
        if repo is None:
            messages.append((f"{unavailable}: ", forge_error))
        else:
            messages.append((f"{unavailable} for ", ui.code(repo.full_name), ": ", forge_error))
    return tuple(messages)
