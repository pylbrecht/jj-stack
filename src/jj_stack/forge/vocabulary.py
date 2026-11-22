"""Name the forge and its pull requests in human output the way the configured forge does."""

from __future__ import annotations

from dataclasses import dataclass

from jj_stack.config import ForgeKind


@dataclass(frozen=True, slots=True)
class ForgeVocabulary:
    """The words human output uses for the forge and its pull requests."""

    forge_name: str
    # The forge's short noun for a pull request, such as "MR" on GitLab.
    pr_noun: str
    # What comes before a pull request number in the forge's own references, such as `!12`.
    pr_sigil: str

    def pr_count(
        self,
        count: int,
    ) -> str:
        return f"{count} {self.pr_noun}{'' if count == 1 else 's'}"


_VOCABULARIES: dict[ForgeKind, ForgeVocabulary] = {
    "github": ForgeVocabulary(forge_name="GitHub", pr_noun="PR", pr_sigil="#"),
    "gitlab": ForgeVocabulary(forge_name="GitLab", pr_noun="MR", pr_sigil="!"),
}
_current_vocabulary: ForgeVocabulary | None = None


def install_forge_vocabulary(
    forge: ForgeKind,
) -> None:
    """Install the configured forge's vocabulary for the current CLI invocation."""

    global _current_vocabulary
    _current_vocabulary = _VOCABULARIES[forge]


def forge_vocabulary() -> ForgeVocabulary:
    """Return the vocabulary installed during command bootstrap."""

    if _current_vocabulary is None:
        raise RuntimeError("forge vocabulary has not been installed")
    return _current_vocabulary


def forge_name() -> str:
    return forge_vocabulary().forge_name
