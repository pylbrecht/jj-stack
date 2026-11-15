# GitHub coupling inventory

Ticket: https://github.com/pylbrecht/jj-stack/issues/2 (map: issue 1, "GitLab support via glab").
Snapshot: `main` at `a520798b`. Paths are relative to `src/jj_stack/` unless they start with
`docs/`.

This file lists where GitHub leaks out of `github/`. It does not propose a seam. Each item has
one tag:

- **[generic]**: the concept exists on any forge (PR create/update, head/base, state, comments,
  reviewers, labels). Only the API shape is GitHub's.
- **[GitHub-only]**: the concept has no direct forge-neutral equivalent (stack objects, merge
  queue, GraphQL timeline, node IDs, check-rollup enums).
- **[mixed]**: a generic need that the code satisfies with a GitHub-only mechanism or field.

`merge` is out of scope for the effort; its items are listed for completeness and marked
"(merge)".

## Summary

- `GithubClient` (`github/client.py`) has 31 public methods. 7 are GitHub stack objects or
  async stack merge, 1 is merge-queue detection. The other 23 are generic in intent.
- The client is passed by its concrete type everywhere (`GithubClient` appears 71 times in
  `stack/` and `commands/`). There is no protocol. `CommandContext.open_github_client`
  (`bootstrap.py:50`) is the only factory and the only injection point tests use.
- `GithubPR` (`models/github.py:134`) is the PR model used everywhere (98 references in
  `stack/` and `commands/`). It mixes generic fields with GitHub-only ones (`node_id`,
  `merge_queue_entry`, `stack_base_ref`, `merge_state_status`, `check_rollup_status`,
  `review_decision`, `head.label`).
- `GithubStack` reaches 20 files in `stack/` and `commands/`. Stack membership is part of the
  sync and cleanup safety rules, not only of `submit` and `unstack`.
- `refs/pull/N/head` is **not used**. PR heads are always same-repo branches under the
  reserved namespace; commits are fetched by commit ID or through `refs/heads/...`.
- Tracking (`models/tracking.py`) is forge-neutral: a PR number and a head branch name.

## 1. Client construction, auth, and repo address

- `github/resolution.py`: `GithubRepoAddress(owner, repo)`, `parse_github_repo`,
  `resolve_github_target`, `require_github_repo`. The URL parser accepts exactly two path
  segments (`resolution.py:103`). **[mixed]**: a repo address is generic, but GitLab project
  paths can have nested groups (`group/sub/project`), which this parser rejects.
- Callers of `resolve_github_target` / `require_github_target`: `stack/preparation.py:43`,
  `commands/list_.py:117`, `commands/cleanup/command.py:279`, `commands/sync.py:120`,
  `commands/unstack.py:104`, `commands/merge/command.py:134`. Callers of
  `open_github_client`: `stack/status.py:179`, `commands/relink.py:120`,
  `commands/doctor.py:291`, `commands/checkout.py:133`, `commands/cleanup/command.py:372`,
  `commands/sync.py:121`, `commands/unstack.py:108`, `commands/merge/command.py:135`,
  `commands/submit/command.py:265`. `GithubRepoAddress` appears in 13 files outside `github/`.
- `commands/github_run.py`: `GithubRun` and `ObservedTrunk` bundle the client, target, and
  `GithubRepo` for one run (used by `sync`, `sync_apply`, `sync_prs`, `cleanup`, `merge`,
  `submit`). **[generic]** in intent.
- `github/auth.py`: token from `GITHUB_TOKEN`/`GH_TOKEN`, else `gh auth token`.
  `commands/doctor.py:272` calls `github_token()` directly. **[GitHub-only]** mechanism; the
  map already decides GitLab auth goes through the configured `glab` command.
- `GITHUB_API_BASE_URL = "https://api.github.com"` (`client.py:41`), REST under
  `/repos/{owner}/{repo}`, GraphQL at `/graphql`, httpx transport with GitHub rate-limit
  headers (`client.py:1188-1270`). **[GitHub-only]**.
- Hard-coded `https://github.com/...` URLs outside `github/`: `formatting.py:27` (PR link
  fallback), `commands/submit/revision_comments.py:106-130` (compare and commit links),
  `commands/merge/wait.py:155` (commit link, merge). **[mixed]**: links are generic, the host
  and path layout are GitHub's.

## 2. Errors

- `GithubClientError` (`client.py:93`) is caught in 18 files in `stack/` and `commands/`.
  `errors.EXIT_GITHUB = 4` is its exit code. **[generic]** need.
- Callers branch on GitHub HTTP status or message text:
  - `stack/status.py:257-258`: 401/403/404/5xx classify a failed lookup.
  - `commands/unstack.py:166`: 404 on `get_stack`. **[GitHub-only]** (stacks).
  - `stack/github_stack_safety.py:94`: 422 on `unstack`. **[GitHub-only]**.
  - `commands/submit/github_stack.py:124`: message contains "auto-merge" on stack create.
    **[GitHub-only]**.
  - `commands/merge/github_stack.py:165`: 400/409 with "head" in the message (merge).
  - `github/overview_comments.py:24`: 404 on comment delete is "already gone". **[generic]**.
  - `github/stack_availability.py:22`: 404 on `list_stacks` means the Stacks API is off.
    **[GitHub-only]**.
  - `user_facing_reason()` and `github_message()` are used for display in `doctor`,
    `sync_apply`, `cleanup/actions`, `status`.

## 3. `GithubClient` methods and callers

Callers are outside `github/client.py`. "self" means the method is only reused inside the
client.

### Repo and branch reads

- `get_repo` -> `stack/pr_facts.py:87`, `commands/doctor.py:293`,
  `github/error_messages.py:40`. Returns `GithubRepo` (`default_branch`, `node_id`,
  `permissions.push`, `allow_*_merge`). **[mixed]**: default branch and push permission are
  generic; `node_id` exists only to feed `create_pr(repository_id=...)`; `allow_*_merge` is
  used by `merge`.
- `get_branch_targets` -> `stack/pr_facts.py:74`, `stack/convergence_observation.py:72`,
  `stack/pr_rewrites.py:86`, `commands/relink.py:127`, `commands/checkout.py:217`,
  `commands/submit/publication.py:113`. **[generic]** (branch name -> commit ID). Implemented
  via GraphQL aliases.
- `get_publication_branches` -> `commands/submit/command.py:478`. Branch targets plus the
  default PR template in one GraphQL request. **[mixed]**: the bundling is a GraphQL
  optimisation.
- `get_publication_branches_by_suffix` -> `commands/submit/command.py:487`. First-submit
  recovery: branches with the namespace prefix ending in a short change ID, with the template.
  **[generic]** need; uses GraphQL `refs(query:)` paging.
- `get_pr_template` -> `commands/sync_prs.py:60`. GitHub's default template, with the owner's
  `.github` repo fallback. **[mixed]**: see section 7.

### PR reads

- `get_pr` -> `github/pr_refs.py:22` (`load_pr`, used by `relink`, `checkout`,
  `submit --base-pull-request`). **[generic]**.
- `get_prs_by_numbers` -> `stack/status.py:293`, `stack/pr_facts.py:77`,
  `commands/checkout.py:465`, `commands/merge/wait.py:121` (merge, with `merge_progress=True`).
  **[generic]**.
- `get_open_prs_by_head_refs` -> `stack/status.py:252`, `stack/pr_facts.py:70`,
  `stack/convergence_observation.py:73`, `commands/checkout.py:353`. **[generic]**.
- `get_prs_by_base_refs` -> `stack/pr_facts.py:82` (cleanup dependents). **[generic]**.
- `get_pr_merge_details` -> `stack/status.py:228` (`view`/`list` for blocked PRs),
  `commands/merge/checks.py:52` (merge). Required checks from branch rules, unresolved review
  threads, check runs. **[mixed]**: see section 6.
- `get_pr_progress` -> `stack/status.py:231`. Approvals by writers and "behind" count against
  the landing branch (`stack_base_ref or base.ref`). **[mixed]**.
- `list_pr_reviews` -> `commands/submit/prs.py:45` (`--re-request`). REST
  `/pulls/N/reviews`. **[generic]**.

### PR writes

- `create_pr` -> `commands/submit/prs.py:92`. Needs `repository_id` = `GithubRepo.node_id`
  (`commands/submit/publication.py:230`). **[generic]** need, **[GitHub-only]** argument.
- `update_pr` -> `commands/submit/prs.py:105`, `commands/submit/auto_close.py:77`,
  `commands/cleanup/actions.py:71`, `commands/merge/github_stack.py:137` (merge). Keyed by
  `pr.node_id`, not by number. **[generic]** need, **[GitHub-only]** key.
- `set_pr_draft` -> `commands/submit/prs.py:154`. Keyed by `node_id`. **[generic]**.
- `close_pr` -> `commands/sync_apply.py:71`, `commands/cleanup/actions.py:78`. REST issue
  PATCH. **[generic]**.
- `request_reviewers` -> `commands/submit/prs.py:192`. Users and teams. **[mixed]**: team
  reviewers are a GitHub concept.
- `add_labels` -> `commands/submit/prs.py:198`. Uses the issue endpoint. **[generic]**.

### Managed comments and timeline

- `find_issue_comments_and_revisions` -> `commands/submit/publication.py:168`. One GraphQL
  pass returns marker comments and force-push revisions. **[mixed]**.
- `find_issue_comments_by_body_marker` -> `commands/cleanup/command.py:504`. **[generic]**.
- `create_issue_comment`, `update_issue_comment` -> `commands/submit/managed_comments.py:24,
  29`. **[generic]**.
- `delete_issue_comment` -> `github/overview_comments.py:20` (used by
  `commands/cleanup/actions.py:98`, `commands/submit/overview_comments.py:101`).
  **[generic]**.
- Comment IDs are GitHub `databaseId` integers (`GithubIssueComment.id`).

### GitHub stack objects

- `list_stacks` -> `stack/pr_facts.py:167` (`observe_github_stacks`), `commands/doctor.py:339`.
  **[GitHub-only]**.
- `get_stack` -> `commands/unstack.py:164`. **[GitHub-only]**.
- `create_stack`, `append_to_stack` -> `commands/submit/github_stack.py:118, 113`.
  **[GitHub-only]**.
- `unstack` -> `stack/github_stack_safety.py:92` (`dissolve_github_stack`). **[GitHub-only]**.

### Merge (out of scope)

- `base_branch_uses_merge_queue` -> `commands/merge/command.py:331`. **[GitHub-only]**.
- `submit_stack_merge`, `poll_stack_merge` -> `commands/merge/github_stack.py:159`,
  `commands/merge/wait.py:101`. REST `merge-async`. **[GitHub-only]**.

## 4. PR identity, head, and base

- PR number is the identity in tracking (`PRIdentity.pr_number`, `head_ref`). **[generic]**;
  GitLab MR IIDs are also per-project integers.
- `GithubPR.node_id` (GraphQL global ID) is used for every PR mutation: `submit/prs.py`,
  `submit/auto_close.py`, `cleanup/actions.py`, `merge/github_stack.py`, `merge/plan.py`,
  `submit/publication.py`. **[GitHub-only]** key.
- `GithubPR.head.ref`, `head.sha`, `base.ref`: used across `stack/` and `commands/` (about 20
  files). **[generic]**.
- `GithubPR.head.label` (`owner:branch`) is the only fork check: `github/pr_refs.py:31`
  (`pr_head_repo_problem`), reached from `relink`, `checkout`, and `submit
  --base-pull-request`. **[mixed]**: "head branch is in this repo" is generic; GitLab exposes
  it as `source_project_id`.
- `GithubPR.head_branch_exists` (GraphQL `headRef` null) -> `commands/cleanup/actions.py`
  (closed PR still reopenable). **[generic]** need.
- `GithubPR.html_url` -> 21 files, for links only. **[generic]**.
- PR reference parsing: `github/pr_refs.py:17` matches `/{owner}/{repo}/pull/{n}`. Used by
  `relink`, `checkout --pull-request`, `submit --base-pull-request`. **[mixed]**: GitLab uses
  `/-/merge_requests/{n}`.
- Branch name length limit: `pr_branch_namespace.py:13-16` cites GitHub's 255-byte ref limit.
  **[mixed]**.
- `refs/pull/N/head`: not used anywhere in `src/`.

## 5. PR state and draft

- `GithubPR.state` (`open`/`closed`/`merged`, lowercased from GraphQL) -> about 25 files.
  **[generic]**.
- `GithubPR.is_draft` -> `stack/reporting.py`, `stack/status.py`, `commands/view.py`,
  `commands/checkout.py`, `commands/sync_prs.py`, `commands/submit/*`, `commands/merge/plan.py`.
  **[generic]**.
- `GithubPR.merge_commit_sha` -> `stack/pr_facts.py`, `stack/trunk_evidence.py`,
  `commands/merge/wait.py`. Basis of the "rewritten merge result on trunk" evidence. **[generic]**
  need; GitLab has `merge_commit_sha` and `squash_commit_sha`, which can differ.
- `GithubPR.title`, `body` -> `commands/submit/*`, `commands/view_details.py`,
  `commands/checkout.py`. **[generic]**.

## 6. Checks, reviews, and merge readiness (inspection)

All of these feed `view`/`list` through `stack/status.py` and `stack/reporting.py`;
`merge/checks.py` also uses them.

- `GithubPR.check_rollup_status` (from `statusCheckRollup.state` enum) -> `stack/reporting.py`,
  `commands/_json_status.py`. **[mixed]**: a pipeline status is generic, the enum mapping is
  GitHub's.
- `GithubPR.merge_state_status` (`CLEAN`, `BLOCKED`, `BEHIND`, `DIRTY`, `HAS_HOOKS`,
  `UNSTABLE`) -> `stack/reporting.py:201-261`, `stack/status.py`, `commands/_json_status.py`.
  **[GitHub-only]** enum; GitLab has `detailed_merge_status` with other values.
- `GithubPR.review_decision` -> `stack/reporting.py`, `commands/view_details.py`. **[mixed]**.
- `GithubPR.approvals`, `behind` (from `get_pr_progress`) -> `stack/reporting.py`,
  `commands/view.py`, `commands/list_.py`. **[generic]** need.
- `GithubPRMergeDetails`, `GithubCheck`, `GithubReviewThread`, `GithubCheckCounts`
  (`models/github_details.py`) -> `stack/status.py`, `stack/reporting.py`,
  `commands/view.py`, `commands/view_details.py`, `commands/_json_status.py`,
  `commands/merge/checks.py`. Required checks come from GitHub branch rules
  (`_GraphqlBranchRules`, `client.py:236`); "latest check run" dedupe uses check-suite app and
  workflow IDs. **[mixed]**: unresolved threads and required checks are generic ideas; the
  rule source and dedupe keys are GitHub's.

## 7. Templates and derived text

- `DEFAULT_PR_TEMPLATE_PATHS` (`models/github.py:20`): `.github/`, root, `docs/` with
  `PULL_REQUEST_TEMPLATE.md`. Used by `commands/submit/descriptions.py:146`
  (`read_pr_template`, local file) and by the GraphQL template read. **[GitHub-only]** paths;
  GitLab uses `.gitlab/merge_request_templates/*.md`.
- Default PR body rules (`commands/submit/descriptions.py`) are forge-neutral Markdown.

## 8. Managed comments and force-push timeline

- Markers: `STACK_OVERVIEW_COMMENT_MARKER` (`github/overview_comments.py:9`, used by
  `commands/submit/publication.py`, `commands/submit/overview_comments.py`,
  `commands/cleanup/command.py`) and `REVISION_HISTORY_COMMENT_MARKER`
  (`commands/submit/revision_comments.py:14`). HTML comment markers in a PR comment body.
  **[generic]**; GitLab MR notes support the same.
- `GithubIssueComment` -> `commands/submit/managed_comments.py`,
  `commands/submit/overview_comments.py`, `commands/submit/revision_comments.py`,
  `commands/cleanup/*`. **[generic]**.
- `GithubPRRevision` (from GraphQL `HeadRefForcePushedEvent` timeline,
  `client.py:310`, `client.py:1818`) -> `commands/submit/revision_comments.py`,
  `commands/submit/publication.py`. Revision comment links use `github.com/.../compare/a..b`.
  **[GitHub-only]** source; GitLab has MR versions (`/merge_requests/:iid/versions`) as a
  possible counterpart.

## 9. GitHub stack objects

- Models: `GithubStack`, `GithubStackPR` (`models/github.py:67-102`), `GithubPR.stack_base_ref`.
  **[GitHub-only]**.
- Observation: `observe_github_stacks` (`stack/pr_facts.py:163`) is called by `sync`
  (`commands/sync.py:311`), `sync --all` (`stack/global_convergence.py:102`), `submit`
  (`commands/submit/command.py:505`), `checkout` (`commands/checkout.py:461`), `cleanup`
  (`commands/cleanup/command.py:237, 502`), `unstack` (`commands/unstack.py:135`), `merge`
  (`commands/merge/command.py:350`). `doctor` calls `list_stacks` for its availability check.
  Every command except `view`/`list`/`relink` therefore needs the Stacks API.
- Planning and safety: `stack/github_stack_safety.py` (`require_merged_prefix`,
  `selected_github_stack`, `dissolve_github_stack`), `stack/convergence.py:319-337`,
  `stack/convergence_observation.py:30-38`, `stack/global_convergence.py:291-298`,
  `commands/submit/github_stack.py` (`GithubStackPlan`), `commands/cleanup/actions.py:184-223`,
  `commands/checkout.py:515-538` (picker), `commands/sync_apply.py:86`,
  `commands/sync_prs.py:29`, `commands/submit/render.py`, `commands/merge/github_stack.py`.
- `stack_base_ref` decides which branch's rules and "behind" count apply
  (`client.py:694-790`, `stack/reporting.py`).
- Availability error: `github/stack_availability.py` (404 -> "stacked pull requests are
  unavailable"), used by `stack/pr_facts.py` and `commands/doctor.py:341`.

## 10. Merge queue (mostly merge)

- `GithubPR.merge_queue_entry` / `is_queued` and `GithubMergeQueueEntry` ->
  `stack/change_state.py`, `stack/convergence_observation.py`, `stack/status.py`,
  `stack/reporting.py` (in scope: `submit` and `sync` stop on a queued PR; `view`/`list`
  report it), plus `commands/merge/*`. **[GitHub-only]**; GitLab merge trains are the nearest
  counterpart.
- `queue_removal_reason`, `queue_test_commit` (GraphQL timeline `RemovedFromMergeQueueEvent`)
  -> `commands/merge/wait.py` only. **[GitHub-only]** (merge).

## 11. Rules in `docs/internals/design.md`

Line numbers refer to `docs/internals/design.md` at the snapshot.

Forge-generic rules that name GitHub only as "the forge":

- Summary and one-repo scope (9, 13). Tracking: "GitHub repo is command context" (68),
  identity and snapshot match (71-78).
- PR branches and bases (82-112): branch-based PRs; trunk branch from the reported default
  branch (109-112). **[generic]**.
- Reserved branch namespace (114-143): "fetched GitHub rewrite" (131). **[generic]**.
- Safety rules (212-222): "GitHub cannot undo every remote mutation", "check the target".
  Rule 4 "using GitHub's expected-head check" (219-220) is merge. **[generic]** except rule 4.
- Tracking and recovery (240-245, 258). **[generic]**.
- Selection: "Only GitHub's public API is supported... interpreted as a `github.com` owner and
  repo" (270-271). **[GitHub-only]** statement that the effort must change.
- Identity and mutation preconditions (299-324). **[generic]**, except "native GitHub stack
  rebase" (312-314).
- Submit and branch transport (326-374): atomic leased push, base retargeting to avoid
  auto-close (368-371). **[generic]**; the auto-close behaviour is GitHub's and may not apply
  to GitLab. "Requires ... an available GitHub Stacks API" (331) and the queue stop (373-374)
  are **[GitHub-only]**.
- Repo policy (444-454): external merges supported; eligibility left to the forge.
  **[generic]**; "native GitHub stack through GitHub's UI" (447) is **[GitHub-only]**.
- Trunk evidence and sync (456-551): rewritten merge result on trunk (465-469), replacement
  rule (492-523). **[generic]**, but each references GitHub stacks (463, 468, 475, 478, 508-510,
  516-518). Change-ID header preserved through rebase merges but not squash (548-551):
  forge behaviour; **[mixed]**.
- Derived artifacts (580-617): description refresh, overview comment, draft, reviewers, labels.
  **[generic]**, except "force-push timeline" (594) **[GitHub-only]**, "orphaned PR in the same
  GitHub stack" (589), and `--team-reviewers` (606).
- Unstack and cleanup (619-662): `unstack` removes a GitHub stack **[GitHub-only]**;
  cleanup retargets to trunk so GitHub can reopen a PR (633-637), closed-PR reopen limits
  (653-656), dependents from PR bases (660) are GitHub forge behaviours stated as rules
  **[mixed]**; "every active member of a GitHub stack ... is also cleaned up" (650-651)
  **[GitHub-only]**.
- Adoption and repair (664-695): `--pick` lists GitHub stack resources (667-671)
  **[GitHub-only]**; `doctor` checks "GitHub Stacks API availability" (691) **[GitHub-only]**;
  `relink` same-repo head check **[generic]**.
- Inspection (697-731): merge-queue entry (712), `BLOCKED` label (715), rules "from the
  branch a PR's GitHub stack lands on" (716-717), lazy merge state (722). **[mixed]** /
  **[GitHub-only]** as noted.

GitHub-only sections:

- "GitHub stack objects" (145-158).
- "GitHub stack membership" (553-578).
- "Merge" (376-442), out of scope.

## Observations

- The concrete `GithubClient` type is threaded through `stack/` helpers (`pr_facts`,
  `status`, `convergence_observation`, `pr_rewrites`, `github_stack_safety`), so the coupling
  is not limited to `commands/`.
- Stack membership is woven into sync and cleanup safety (trunk evidence, replacement rule,
  cleanup eligibility), so "GitHub stack objects are out of scope" means those rules need a
  "no stacks" branch, not only that `submit` skips stack calls.
- Most PR mutations key on the GraphQL `node_id`, not the PR number. Tracking keys on the
  number, so the number is already the stable identity.
- `GithubRepoAddress` assumes `owner/repo`; GitLab nested groups do not fit it.
