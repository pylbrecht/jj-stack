"""Forge-neutral core of the fake forge servers used by local integration tests.

The core owns the backing bare Git repo and the pull request records: merges, branch advances,
head rewrites, and the detection of heads that their base already contains. A forge's route
layer subclasses it and adds that forge's extras and payload shapes.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

_FAKE_FORGE_GIT_ENV = {
    "GIT_AUTHOR_EMAIL": "fake-forge@example.com",
    "GIT_AUTHOR_NAME": "Fake Forge",
    "GIT_COMMITTER_EMAIL": "fake-forge@example.com",
    "GIT_COMMITTER_NAME": "Fake Forge",
}


@dataclass(slots=True, kw_only=True)
class FakePullRequest:
    """Mutable pull request state that every fake forge shares."""

    base_ref: str
    body: str
    head_ref: str
    head_sha: str
    number: int
    title: str
    merge_commit_sha: str | None = None
    merged_at: str | None = None
    state: str = "open"


@dataclass(slots=True, frozen=True)
class FakePullRequestEvent:
    """Observable pull request mutation recorded by the fake forge."""

    kind: str
    pr_number: int


@dataclass(slots=True, kw_only=True)
class FakeForgeRepo[PR: FakePullRequest]:
    """Pull request records plus their backing bare Git repo."""

    git_dir: Path
    default_branch: str = "main"
    # A forge eventually detects a pushed head that its base already contains and marks the
    # pull request merged. Tests that need the head to stay open flip this off.
    auto_merge_reachable_heads: bool = True
    # The "delete head branches on merge" setting: a merged pull request's branch goes away once
    # the open pull requests based on it have moved to the merged pull request's base.
    delete_branch_on_merge: bool = False
    next_pr_number: int = 1
    pr_events: list[FakePullRequestEvent] = field(default_factory=list)
    pr_force_pushes: dict[int, list[tuple[str, str]]] = field(default_factory=dict)
    prs: dict[int, PR] = field(default_factory=dict)

    def squash_commit_message(
        self,
        pr: PR,
    ) -> str:
        return pr.title

    def refresh_head(
        self,
        pr: PR,
    ) -> str | None:
        """Track the head branch; like a forge, keep the last sha once the branch is gone."""

        current_head = self.ref_target(pr.head_ref)
        if current_head is not None:
            if current_head != pr.head_sha and not self.is_ancestor(pr.head_sha, current_head):
                self.pr_force_pushes.setdefault(pr.number, []).append((pr.head_sha, current_head))

            pr.head_sha = current_head

        return current_head

    def refresh_pr_state(
        self,
        pr: PR,
        *,
        branch_heads: dict[str, str] | None = None,
    ) -> None:
        # A forge eventually detects a pushed head reachable from its existing base. This fake
        # observes it synchronously; base edits separately reject an already-reachable head.
        if not self.auto_merge_reachable_heads or pr.state != "open":
            return
        if branch_heads is None:
            branch_heads = self.branch_heads()
        base_commit = branch_heads.get(pr.base_ref)
        head_commit = branch_heads.get(pr.head_ref)
        if base_commit is None or head_commit is None:
            return
        if not self.is_ancestor(head_commit, base_commit):
            return
        if pr.merged_at is None:
            pr.merged_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        pr.merge_commit_sha = head_commit
        self.update_pr_state(
            pr,
            state="closed",
        )

    def refresh_prs(
        self,
        prs: Iterable[PR],
    ) -> None:
        """Refresh many pull requests off one shared branch-head snapshot."""

        to_refresh = [candidate for candidate in prs if candidate.state == "open"]
        if not to_refresh:
            return
        branch_heads = self.branch_heads()
        for pr in to_refresh:
            self.refresh_pr_state(pr, branch_heads=branch_heads)

    def update_pr_base(
        self,
        pr: PR,
        *,
        base_ref: str,
    ) -> None:
        if pr.base_ref == base_ref:
            return
        pr.base_ref = base_ref
        self.pr_events.append(
            FakePullRequestEvent(
                kind="base",
                pr_number=pr.number,
            )
        )

    def update_pr_state(
        self,
        pr: PR,
        *,
        state: str,
    ) -> None:
        if pr.state == state:
            return
        pr.state = state
        self.pr_events.append(
            FakePullRequestEvent(
                kind="state",
                pr_number=pr.number,
            )
        )

    def ref_target(self, branch: str) -> str | None:
        return self.branch_heads().get(branch)

    def _record_merge(self, pr: PR, merge_commit: str) -> None:
        """Mark the PR merged; a repo that deletes head branches also drops its branch.

        The forge first retargets open PRs based on that branch to the merged PR's base, so no
        open PR is left pointing at a missing base.
        """

        pr.merged_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        pr.merge_commit_sha = merge_commit
        self.update_pr_state(pr, state="closed")
        if not self.delete_branch_on_merge:
            return
        for dependent in self.prs.values():
            if dependent.state == "open" and dependent.base_ref == pr.head_ref:
                self.update_pr_base(dependent, base_ref=pr.base_ref)
        self._run_backing_git("update-ref", "-d", f"refs/heads/{pr.head_ref}")

    def apply_squash_merge(self, pr: PR) -> str:
        """Squash-merge the PR's head into its base on the backing Git repo.

        A real forge computes a three-way merge before squashing, so the squash keeps whatever
        else the base gained since the PR's merge base. Copying the head commit's tree instead
        would silently revert an unrelated merge that landed first.
        """

        heads = self.branch_heads()
        head_commit = heads[pr.head_ref]
        base_commit = heads[pr.base_ref]
        tree = self._run_backing_git(
            "merge-tree", "--write-tree", base_commit, head_commit
        ).splitlines()[0]
        squash_commit = self._run_backing_git(
            "commit-tree",
            tree,
            "-p",
            base_commit,
            "-m",
            self.squash_commit_message(pr),
            env=_FAKE_FORGE_GIT_ENV,
        )
        self._run_backing_git(
            "update-ref",
            f"refs/heads/{pr.base_ref}",
            squash_commit,
        )
        self._record_merge(pr, squash_commit)
        return squash_commit

    def apply_pr_merge(
        self,
        pr: PR,
        *,
        merge_method: str,
    ) -> str:
        """Apply one ordinary PR merge using the requested commit shape."""

        if merge_method == "merge":
            return self.apply_merge_commit((pr,))
        if merge_method == "rebase":
            return self.apply_rebase_merge(pr)
        if merge_method == "squash":
            return self.apply_squash_merge(pr)
        raise AssertionError(f"Unknown fake forge merge method {merge_method!r}.")

    def apply_rebase_merge(self, pr: PR) -> str:
        """Replay the PR's commits onto its base, preserving their messages.

        Like `git rebase`, commits whose changes the base already has are skipped.
        """

        heads = self.branch_heads()
        rebase_commit = heads[pr.base_ref]
        for commit in self._run_backing_git(
            "rev-list",
            "--reverse",
            "--right-only",
            "--cherry-pick",
            "--no-merges",
            f"{heads[pr.base_ref]}...{heads[pr.head_ref]}",
        ).split():
            rebase_commit = self._replay_commit(
                commit_id=commit,
                extra_header="x-fake-rebase-merge true",
                parent_commit_id=rebase_commit,
            )
        self._run_backing_git(
            "update-ref",
            f"refs/heads/{pr.base_ref}",
            rebase_commit,
        )
        self._record_merge(pr, rebase_commit)
        return rebase_commit

    def apply_merge_commit(
        self,
        prs: tuple[PR, ...],
    ) -> str:
        """Merge one PR, or a chain of PRs from the bottom up, through a shared merge commit."""

        if not prs:
            raise AssertionError("A merge commit requires at least one pull request.")
        heads = self.branch_heads()
        base_ref = prs[0].base_ref
        base_commit = heads[base_ref]
        head_commit = heads[prs[-1].head_ref]
        tree = self._run_backing_git(
            "merge-tree", "--write-tree", base_commit, head_commit
        ).splitlines()[0]
        merge_commit = self._run_backing_git(
            "commit-tree",
            tree,
            "-p",
            base_commit,
            "-p",
            head_commit,
            "-m",
            f"Merge through pull request {prs[-1].number}",
            env=_FAKE_FORGE_GIT_ENV,
        )
        self._run_backing_git(
            "update-ref",
            f"refs/heads/{base_ref}",
            merge_commit,
        )
        for pr in prs:
            self._record_merge(pr, merge_commit)
        return merge_commit

    def rewrite_pr_onto_base(
        self,
        pr: PR,
        *,
        base_ref: str,
    ) -> str:
        """Model the forge changing an unmerged PR's base and rebasing its commit."""

        heads = self.branch_heads()
        rewritten = self._replay_commit(
            commit_id=heads[pr.head_ref],
            extra_header="x-fake-stack-rewrite true",
            parent_commit_id=heads[base_ref],
        )
        self._run_backing_git(
            "update-ref",
            f"refs/heads/{pr.head_ref}",
            rewritten,
        )
        self.update_pr_base(
            pr,
            base_ref=base_ref,
        )
        pr.head_sha = rewritten
        return rewritten

    def advance_branch(
        self,
        branch: str,
        *,
        path: str,
        contents: str,
        message: str = "advance trunk for stack rebase",
    ) -> str:
        """Add one file in a new commit on a backing branch."""

        parent = self.ref_target(branch)
        if parent is None:
            raise AssertionError(f"Missing fake forge branch {branch}")
        parent_tree = self._run_backing_git("rev-parse", f"{parent}^{{tree}}")
        tree = self._tree_with_file(parent_tree, path=path, contents=contents)
        commit = self._run_backing_git(
            "commit-tree",
            tree,
            "-p",
            parent,
            "-m",
            message,
            env=_FAKE_FORGE_GIT_ENV,
        )
        self._run_backing_git("update-ref", f"refs/heads/{branch}", commit)
        return commit

    def replace_pr_head_contents(
        self,
        pr: PR,
        *,
        path: str,
        contents: str,
    ) -> str:
        """Replace one rewritten PR head with a same-parent commit containing another file."""

        head = self.ref_target(pr.head_ref)
        if head is None:
            raise AssertionError(f"Missing fake forge branch {pr.head_ref}")
        parent = self._run_backing_git("rev-parse", f"{head}^")
        original_tree = self._run_backing_git("rev-parse", f"{head}^{{tree}}")
        tree = self._tree_with_file(original_tree, path=path, contents=contents)
        rewritten = self._replay_commit(
            commit_id=head,
            drop_change_id=True,
            extra_header="x-fake-content-edit true",
            parent_commit_id=parent,
            tree_id=tree,
        )
        self._run_backing_git(
            "update-ref",
            f"refs/heads/{pr.head_ref}",
            rewritten,
        )
        pr.head_sha = rewritten
        return rewritten

    def _tree_with_file(self, tree: str, *, path: str, contents: str) -> str:
        blob = self._run_backing_git("hash-object", "-w", "--stdin", stdin=contents)
        entries = "\n".join(
            entry
            for entry in self._run_backing_git("ls-tree", tree).splitlines()
            if entry.partition("\t")[2] != path
        )
        return self._run_backing_git(
            "mktree",
            stdin=f"{entries}\n100644 blob {blob}\t{path}\n",
        )

    def force_push_pr_head(self, pr: PR) -> str:
        """Rewrite one PR head externally while preserving its jj change ID."""

        head = self.ref_target(pr.head_ref)
        if head is None:
            raise AssertionError(f"Missing fake forge branch {pr.head_ref}")
        parent = self._run_backing_git("rev-parse", f"{head}^")
        rewritten = self._replay_commit(
            commit_id=head,
            extra_header="x-fake-force-push true",
            message_suffix="\nexternal rewrite",
            parent_commit_id=parent,
        )
        self._run_backing_git(
            "update-ref",
            f"refs/heads/{pr.head_ref}",
            rewritten,
        )
        pr.head_sha = rewritten
        return rewritten

    def _replay_commit(
        self,
        *,
        commit_id: str,
        drop_change_id: bool = False,
        extra_header: str,
        message_suffix: str = "",
        parent_commit_id: str,
        tree_id: str | None = None,
    ) -> str:
        """Replay the commit's delta onto its new parent unless a replacement tree is supplied."""

        raw_commit = self._run_backing_git("cat-file", "commit", commit_id)
        headers, separator, message = raw_commit.partition("\n\n")
        if tree_id is None:
            old_parent = next(
                line.removeprefix("parent ")
                for line in headers.splitlines()
                if line.startswith("parent ")
            )
            tree_id = self._run_backing_git(
                "merge-tree",
                "--write-tree",
                f"--merge-base={old_parent}",
                parent_commit_id,
                commit_id,
            ).splitlines()[0]
        rewritten_headers = [
            f"tree {tree_id}" if line.startswith("tree ") else line
            for line in headers.splitlines()
            if not line.startswith("parent ")
            and not (drop_change_id and line.startswith("change-id "))
        ]
        rewritten_headers.insert(1, f"parent {parent_commit_id}")
        rewritten_headers.append(extra_header)
        return self._run_backing_git(
            "hash-object",
            "-t",
            "commit",
            "-w",
            "--stdin",
            stdin=f"{'\n'.join(rewritten_headers)}{separator}{message}{message_suffix}\n",
        )

    def _run_backing_git(
        self,
        *args: str,
        env: dict[str, str] | None = None,
        stdin: str | None = None,
    ) -> str:
        completed = subprocess.run(
            ["git", "--git-dir", str(self.git_dir), *args],
            capture_output=True,
            check=False,
            env=None if env is None else {**os.environ, **env},
            input=None if stdin is None else stdin.encode("utf-8"),
        )
        stdout = completed.stdout.decode("utf-8")
        stderr = completed.stderr.decode("utf-8")
        if completed.returncode != 0:
            raise AssertionError(
                f"fake forge git {args} failed:\nstdout={stdout}\nstderr={stderr}"
            )
        return stdout.strip()

    def branch_heads(self) -> dict[str, str]:
        """Read every branch target in one git invocation."""

        completed = subprocess.run(
            ["git", "--git-dir", str(self.git_dir), "show-ref", "--heads"],
            capture_output=True,
            check=False,
            text=True,
        )
        if completed.returncode != 0:
            return {}
        heads: dict[str, str] = {}
        for line in completed.stdout.splitlines():
            commit_id, _, ref_name = line.partition(" ")
            if ref_name.startswith("refs/heads/"):
                heads[ref_name.removeprefix("refs/heads/")] = commit_id
        return heads

    def is_ancestor(self, ancestor_commit: str, descendant_commit: str) -> bool:
        completed = subprocess.run(
            [
                "git",
                "--git-dir",
                str(self.git_dir),
                "merge-base",
                "--is-ancestor",
                ancestor_commit,
                descendant_commit,
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        return completed.returncode == 0


def initialize_bare_git_dir(
    root_dir: Path,
    *,
    namespace: str,
    name: str,
) -> Path:
    """Create the bare Git repo that a fake forge serves, at `<namespace>/<name>.git`."""

    namespace_dir = root_dir / namespace
    namespace_dir.mkdir(parents=True, exist_ok=True)
    git_dir = namespace_dir / f"{name}.git"

    subprocess.run(
        ["git", "init", "--bare", str(git_dir)],
        capture_output=True,
        check=True,
        text=True,
    )
    subprocess.run(
        ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
        capture_output=True,
        check=True,
        cwd=git_dir,
        text=True,
    )

    return git_dir
