"""Shared GitHub Stacks API availability diagnostics."""

from jj_stack.errors import EXIT_FORGE, CliError
from jj_stack.forge.errors import ForgeError

GITHUB_STACKS_ROLLOUT_URL = "https://gh.io/stacksbeta"


class GithubStacksUnavailableError(CliError):
    """The repo cannot use GitHub's Stacks API."""

    exit_code = EXIT_FORGE


def github_stacks_unavailable_error(
    *,
    error: ForgeError,
    repo: str,
) -> GithubStacksUnavailableError | None:
    """Explain a missing Stacks API after repo access has succeeded."""

    if error.kind != "not_found":
        return None
    return GithubStacksUnavailableError(
        f"GitHub stacked pull requests are unavailable for {repo}.",
        hint=(
            "See GitHub's current availability and requirements at "
            f"{GITHUB_STACKS_ROLLOUT_URL}, then rerun the command."
        ),
    )
