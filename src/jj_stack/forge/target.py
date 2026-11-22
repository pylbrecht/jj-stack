"""Select the Git remote that stack commands use, and read the forge repo it names."""

from __future__ import annotations

from dataclasses import dataclass

import jj_stack.forge.github.resolution as github_resolution
import jj_stack.forge.gitlab.resolution as gitlab_resolution
import jj_stack.ui as ui
from jj_stack.config import ForgeKind
from jj_stack.errors import CliError, ErrorHint, ErrorMessage, error_message
from jj_stack.forge.github.resolution import GithubRepoAddress
from jj_stack.forge.gitlab.resolution import GitlabProjectAddress
from jj_stack.forge.protocol import RepoAddress
from jj_stack.forge.vocabulary import forge_name
from jj_stack.models.git import GitRemote
from jj_stack.ui import Message

type ForgeAddress = GithubRepoAddress | GitlabProjectAddress


@dataclass(frozen=True, slots=True)
class ForgeTarget:
    """A fully resolved forge target: the selected Git remote and its repo."""

    remote: GitRemote
    repo: ForgeAddress

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


def parse_forge_repo(
    remote: GitRemote,
    *,
    forge: ForgeKind,
) -> ForgeAddress | None:
    """Return the repo of `forge` that both of the remote's URLs name, if they name one."""

    if forge == "gitlab":
        return gitlab_resolution.parse_gitlab_project(remote)
    return github_resolution.parse_github_repo(remote)


def _repo_failure(
    remote: GitRemote,
    *,
    forge: ForgeKind,
) -> tuple[ErrorMessage, ErrorHint]:
    if forge == "gitlab":
        return gitlab_resolution.gitlab_project_failure(remote)
    return github_resolution.github_repo_failure(remote)


def resolve_forge_target(
    remotes: tuple[GitRemote, ...],
    *,
    forge: ForgeKind,
) -> ForgeTarget | UnresolvedForgeTarget:
    """Resolve the optional remote and forge target used by read-mostly commands."""

    if not remotes:
        return UnresolvedForgeTarget()
    try:
        remote = select_submit_remote(remotes)
    except CliError as error:
        return UnresolvedForgeTarget(remote_error=error_message(error))

    repo = parse_forge_repo(remote, forge=forge)
    if repo is None:
        message, hint = _repo_failure(remote, forge=forge)
        return UnresolvedForgeTarget(remote=remote, repo_error=(message, " ", hint))
    return ForgeTarget(remote=remote, repo=repo)


def require_forge_repo(
    remote: GitRemote,
    *,
    forge: ForgeKind,
) -> ForgeAddress:
    """Parse the remote's forge repo, or stop with what is wrong with its URLs."""

    repo = parse_forge_repo(remote, forge=forge)
    if repo is not None:
        return repo

    message, hint = _repo_failure(remote, forge=forge)
    raise CliError(message, hint=hint)


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
