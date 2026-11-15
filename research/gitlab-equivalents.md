# GitLab equivalents of the GitHub features in use

Research for [issue 4](https://github.com/pylbrecht/jj-stack/issues/4), part of the "GitLab
support via glab" effort ([map](https://github.com/pylbrecht/jj-stack/issues/1)).

Scope: self-managed GitLab, reached only through `glab api` (REST v4 or GraphQL). The reference
instance reports `19.5.4-ee` (`GET /version`). The GitHub side is the feature set that
`src/jj_stack/github/client.py` uses. Merge and GitHub stack objects are out of scope; their
equivalents are listed briefly at the end because later work depends on them.

Sources: docs.gitlab.com pages (cited per section) and GitLab source at `master`
(`gitlab-org/gitlab`, read through the `gitlabhq/gitlabhq` mirror). "Observed" means a read-only
`glab api` GET on the reference instance confirmed the shape or behavior.

## Summary

- Nearly every feature has a direct equivalent. The data model differs in four places that matter
  for stacked MRs: draft state lives in the title, push history is a list of diff versions,
  required checks are one pipeline gate per project, and a push can mark an MR **merged**.
- No clean equivalent: named per-branch required checks, team reviewers, a viewer-specific branch
  rule view, a test-merge commit on Free tier, and native stack objects with an API.
- GitLab retargets dependents after a merge only when the merge itself deletes the source branch,
  and at most 4 dependents per merge.
- A push to a target branch that contains an open MR's head SHA marks that MR merged. This is the
  GitLab form of the auto-close hazard that `submit` already avoids by a temporary retarget, but
  the result is irreversible.

## MR create, update, retarget

GitHub: `createPullRequest`, `updatePullRequest` (base, body, title), `PATCH issues/:n` to close.

GitLab:

- Create: `POST /projects/:id/merge_requests` with `source_branch`, `target_branch`, `title`,
  `description`, `labels`, `reviewer_ids`, `remove_source_branch`
  ([create](https://docs.gitlab.com/api/merge_requests/#create-a-merge-request)). GraphQL
  `mergeRequestCreate` also exists.
- Update and retarget: `PUT /projects/:id/merge_requests/:iid` with `target_branch`, `title`,
  `description`, `state_event=close|reopen`, `add_labels`
  ([update](https://docs.gitlab.com/api/merge_requests/#update-a-merge-request)). GraphQL
  `mergeRequestUpdate` takes `targetBranch`, `title`, `description`, `state`.
- MRs are addressed by project plus `iid`, not by a global node ID. GraphQL batch reads work with
  `project(fullPath:) { mergeRequests(iids: [...]) }` or
  `mergeRequests(sourceBranches: [...], targetBranches: [...], state: all)` (observed).

Semantic differences for stacked MRs (from
[`update_service.rb`](https://gitlab.com/gitlab-org/gitlab/-/blob/master/app/services/merge_requests/update_service.rb)):

- A retarget of a **closed** MR is silently dropped: the service deletes `target_branch` from the
  params for closed or merged MRs and returns success. GitHub rejects the call instead. A client
  must compare the returned `target_branch` with the request.
- A retarget writes a system note ("changed target branch from X to Y"), aborts auto-merge,
  and starts new MR pipelines. An EE hook (`delete_approvals_on_target_branch_change`) can also
  drop approvals; its exact condition was not checked.
- A retarget creates a new diff version with the same head SHA and a new `start_commit_sha`
  (observed on MR 36728 and 36785). See [push history](#push-and-version-history-of-the-mr-head).
- No GitLab source path rejects a retarget to a branch that already contains the head. GitHub
  does. Not tested here; the MR probably shows an empty diff.
- Close: `state_event=close`. A closed MR can be reopened only while its source branch exists.

## Draft state

GitHub: `isDraft`, `convertPullRequestToDraft`, `markPullRequestReadyForReview`, `draft` on
create.

GitLab ([drafts](https://docs.gitlab.com/user/project/merge_requests/drafts/)):

- Read: `draft` boolean in REST and GraphQL. `work_in_progress` is deprecated.
- Write: GraphQL `mergeRequestSetDraft(projectPath, iid, draft)`
  ([source](https://gitlab.com/gitlab-org/gitlab/-/blob/master/app/graphql/mutations/merge_requests/set_draft.rb)).
  REST create and update have **no** `draft` parameter; the only REST way is a `Draft:`,
  `[Draft]`, or `(Draft)` title prefix.
- Draft state is stored as the title prefix. `mergeRequestSetDraft` rewrites the title. So the
  title GitLab returns contains `Draft: ` for a draft, and a title written by jj-stack must
  preserve or strip that prefix on purpose, or a title update toggles draft state.
- A push of a commit whose subject starts with `draft:`, `Draft:`, `fixup!`, or `Fixup!` marks
  the MR draft; this is not undone by a later commit
  ([`refresh_service.rb`](https://gitlab.com/gitlab-org/gitlab/-/blob/master/app/services/merge_requests/refresh_service.rb)
  `mark_mr_as_draft_from_commits`). jj changes with a `fixup!` description flip their MR to draft.
- List filter `draft=true|false` since 19.0 (replaces `wip`).

## MR description templates

GitHub: `pullRequestTemplates` with the owner's `.github` fallback.

GitLab ([description templates](https://docs.gitlab.com/user/project/description_templates/)):

- Files live in `.gitlab/merge_request_templates/*.md`. The default is `Default.md`
  (case-insensitive). Priority: project setting text (Premium), then parent-group `Default.md`
  (Premium group template repository), then the project's `Default.md`.
- Read: `GET /projects/:id/templates/merge_requests` lists names; `GET
  /projects/:id/templates/merge_requests/Default` returns `content`, already resolved through
  the group and instance template projects
  ([project templates API](https://docs.gitlab.com/api/project_templates/)). Observed: the
  reference project lists `Default` and `Squad3`. The project-settings default is the project
  attribute `merge_requests_template` (empty on the reference project).
- Templates can hold variables such as `%{all_commits}` and `%{source_branch}`. GitLab expands
  them only for the default template in the UI build path; the templates API returns them raw.
- The REST create path does not apply the default template (`CreateService` does not use
  `BuildService`, which owns `assign_description_from_repository_template`). jj-stack must read
  and fill the template itself, as it does on GitHub.

## Notes for managed and overview comments

GitHub: issue comments (create, update, delete), searched by a body marker.

GitLab ([notes API](https://docs.gitlab.com/api/notes/#merge-requests)):

- `GET|POST /projects/:id/merge_requests/:iid/notes`, `PUT|DELETE .../notes/:note_id`. Sort with
  `sort=asc|desc` and `order_by=created_at|updated_at`; the default order is newest first.
- The list includes system notes (`"system": true`): pushes, retargets, approvals. Filter them
  out before a body-marker search. There is no server-side body search.
- A top-level note is an "individual note" discussion. It is not resolvable, so a managed comment
  never blocks a "threads must be resolved" check.
- GraphQL `MergeRequest.notes` also exists (Noteable interface) and allows batched reads across
  MRs with aliases, like the current GitHub history query.

## Pipelines and required checks

GitHub: `statusCheckRollup`, named required contexts from branch protection and rulesets.

GitLab:

- Per-MR state: `head_pipeline` on `GET .../merge_requests/:iid` (REST) or `headPipeline`
  (GraphQL), with `status` and `sha`. All MR pipelines: `GET .../merge_requests/:iid/pipelines`
  ([pipelines](https://docs.gitlab.com/api/merge_requests/#list-merge-request-pipelines)).
- The gate is one project setting, `only_allow_merge_if_pipeline_succeeds` ("Pipelines must
  succeed"), plus `allow_merge_on_skipped_pipeline`
  ([auto-merge](https://docs.gitlab.com/user/project/merge_requests/auto_merge/)). It is per
  project, not per target branch, and it gates the whole latest pipeline. The pipeline decides
  which jobs count (`allow_failure`). There is no list of named required checks.
- Named external checks: external status checks, Ultimate only, per project with an optional
  protected-branch filter (`GET /projects/:id/merge_requests/:iid/status_checks`,
  [status checks API](https://docs.gitlab.com/api/status_checks/)).
- The overall verdict is `detailed_merge_status`
  ([merge status](https://docs.gitlab.com/api/merge_requests/#merge-status)) with values such as
  `mergeable`, `ci_must_pass`, `ci_still_running`, `discussions_not_resolved`, `draft_status`,
  `not_approved`, `requested_changes`, `need_rebase`, `conflict`, `merge_request_blocked`. It is
  computed asynchronously; `unchecked`, `checking`, and `preparing` are transient. This replaces
  GitHub's `mergeStateStatus` and `mergeable` (since 15.6).
- After an automatic retarget, GitLab sets `head_pipeline_id` to null and starts no new pipeline.
  With "Pipelines must succeed", the MR is blocked until a push or an explicit
  `POST .../merge_requests/:iid/pipelines`.

## Approvals and review threads

GitHub: `latestOpinionatedReviews`, `reviewDecision`, review requests (users and teams),
re-request, `reviewThreads`, required conversation resolution.

GitLab:

- Approve state: `GET .../merge_requests/:iid/approvals` (all tiers) and `.../approval_state`
  (Premium) ([approvals API](https://docs.gitlab.com/api/merge_request_approvals/)). GraphQL
  `approved` and `approvedBy`. Required approval counts and rules are Premium; on Free an approval
  is advisory.
- Reviewer state: `GET .../merge_requests/:iid/reviewers` returns per-user `state` (for example
  `unreviewed`, `reviewed`, `requested_changes`, `approved`). "Request changes" blocks the merge
  until that reviewer approves. There is no review object with history like GitHub's reviews list.
- Request reviewers: REST `reviewer_ids` on update **replaces** the set. Use GraphQL
  `mergeRequestSetReviewers(reviewerUsernames, operationMode: APPEND)` for additive requests.
  Re-request: GraphQL `mergeRequestReviewerRereview(userId)`. There are no team reviewers.
- Labels: REST `add_labels` or GraphQL `mergeRequestSetLabels(operationMode: APPEND)` are
  additive.
- Threads: `GET .../merge_requests/:iid/discussions`; each note has `resolvable` and `resolved`
  ([discussions API](https://docs.gitlab.com/api/discussions/#merge-requests)). GraphQL gives
  `resolvableDiscussionsCount` and `resolvedDiscussionsCount`, and REST gives
  `blocking_discussions_resolved`.
- The rule "all threads must be resolved" is the project setting
  `only_allow_merge_if_all_discussions_are_resolved` (true on the reference project), not a
  branch rule.
- "Remove all approvals when commits are added to the source branch" is a project approval
  setting ([settings](https://docs.gitlab.com/user/project/merge_requests/approvals/settings/)).
  Every jj-stack rebase push can reset approvals.

## Push and version history of the MR head

GitHub: `headRefOid`, `HeadRefForcePushedEvent` with `beforeCommit` and `afterCommit`.

GitLab:

- Current head: `sha` and `diff_refs.head_sha` (REST), `diffHeadSha` (GraphQL). `diff_refs` is
  empty right after create and fills asynchronously.
- History: `GET .../merge_requests/:iid/versions`, newest first, each with `head_commit_sha`,
  `base_commit_sha`, `start_commit_sha`, `created_at`, `patch_id_sha`
  ([versions API](https://docs.gitlab.com/api/merge_requests/#retrieve-merge-request-diff-versions)).
  REST only; the GraphQL `MergeRequest` type has no versions field.
- A version is a diff reload, not a push, and not only a force push. Observed causes: a push to
  the source branch, a retarget (same head, new start SHA), and a push to the target branch that
  carries commits the MR already contains (the stacked case; see `batched_diff_ids_to_reload` in
  `refresh_service.rb`). One push often yields two versions with the same head seconds apart
  (observed). To get GitHub-like before/after pairs, collapse consecutive versions with equal
  `head_commit_sha`; the "before" is the previous distinct head.
- Each source push also writes a system note "added N commits" with a compare range.
- After merge, GitLab deletes the diff files of non-latest versions but keeps the version rows
  and SHAs (`DeleteNonLatestDiffsService`; observed on merged MRs).

## Merged and closed state, merge commit

- `state` is `opened`, `closed`, `locked`, or `merged`. `merge_commit_sha` is set after a merge
  commit; `squash_commit_sha` after a squash; both are null for fast-forward merges, in which case
  `sha` is on the target branch. `merged_at` is set on merge
  ([MR API](https://docs.gitlab.com/api/merge_requests/#retrieve-a-merge-request)). Observed on
  the reference instance.
- **Merged by push.** A push to a branch marks every open MR that targets it as merged if the
  MR's `diff_head_sha` is among the pushed commits (`post_merge_manually_merged` in
  `refresh_service.rb`). GitHub auto-closes in the analogous case, and design.md has `submit`
  retarget to avoid it. On GitLab the result is `merged`, which cannot be reopened or retargeted.
  A stack rewrite that moves a parent branch onto a commit that contains a child's old head
  must retarget the child first, as on GitHub.
- **Closed by branch deletion.** Deleting a branch by push closes every open MR that uses it as
  source (`close_upon_missing_source_branch_ref`). Same outcome as GitHub.

## Target branch merged or deleted (automatic retarget)

From the [MR docs](https://docs.gitlab.com/user/project/merge_requests/#update-merge-requests-when-target-branch-merges),
[stacked MRs](https://docs.gitlab.com/user/project/merge_requests/reviews/stacked_merge_requests/),
and source
([`retarget_chain_service.rb`](https://gitlab.com/gitlab-org/gitlab/-/blob/master/app/services/merge_requests/retarget_chain_service.rb),
[`delete_source_branch_worker.rb`](https://gitlab.com/gitlab-org/gitlab/-/blob/master/app/workers/merge_requests/delete_source_branch_worker.rb)):

- When MR A merges **and the merge deletes A's source branch** (`should_remove_source_branch` or
  the project's `remove_source_branch_after_merge`), GitLab retargets open MRs that target that
  branch to A's target. The system note reads "deleted the `X` branch. This merge request now
  targets the `main` branch" (observed).
- Limits: at most 4 open dependents per merge (`MAX_RETARGET_MERGE_REQUESTS`), same project
  only, and only those the merging user may update.
- A branch deleted after the merge, or deleted without a merge, triggers no retarget. Dependents
  stay open and target a missing branch (`target_branch_exists: false` in GraphQL). GitHub, in
  contrast, closes PRs whose base branch is deleted.
- The retargeted MR gets a new diff version, aborted auto-merge, and no head pipeline (see
  pipelines). The GraphQL `retargeted` field read `false` on observed retargeted MRs, so the
  system note is the only durable trace.
- The merge API refuses while the target branch waits for deletion: "The target branch is
  scheduled for deletion. Please wait for the branch to be retargeted."

## Branch protection relevant to pushes

GitHub: `refUpdateRule` (viewer-specific), ruleset rules.

GitLab ([protected branches API](https://docs.gitlab.com/api/protected_branches/)):

- `GET /projects/:id/protected_branches` and `.../protected_branches/:name` give
  `push_access_levels`, `merge_access_levels`, `allow_force_push`, and
  `code_owner_approval_required` (Premium). Names can be wildcards such as `release/*`. jj-stack
  needs force push on its own branches, so a wildcard that matches its namespace without
  `allow_force_push` breaks rebased pushes.
- There is no viewer-specific "can I push" view like `refUpdateRule`; the client must compare
  access levels with its own role, or let `git push` fail.
- Push rules (Premium: branch-name regex, commit-message regex, signed commits) reject pushes at
  the Git layer, before any API call.

## Merge-related equivalents (out of scope, for later work)

- Merge with lease: `PUT .../merge_requests/:iid/merge` with `sha` returns 409 on mismatch.
  Since 19.2, a group or instance setting can make `sha` required. `auto_merge=true` replaces
  `merge_when_pipeline_succeeds` (deprecated in 17.11).
- Merge queue: merge trains (Premium). Since 19.1, `auto_merge` routes to the train when trains
  are on. Disabled on the reference project.
- Test merge (`potentialMergeCommit`): merged results pipelines (Premium) and
  `GET .../merge_requests/:iid/merge_ref` (the `refs/merge-requests/:iid/merge` ref).
- Server-side rebase: `PUT .../merge_requests/:iid/rebase`.
- Stack objects: GitLab 19.1 shows stacked MRs in the UI, inferred from target chains, same
  author only (19.2), at most 20. There is no stack object or API. MR dependencies
  (`.../blocks`, Premium) block merges but form no stack.
- Merge method and squash: project `merge_method` (`merge`, `rebase_merge`, `ff`) and
  `squash_option`. There is no per-request method choice.

## No clean GitLab equivalent

- Named per-branch required checks: GitLab gates on the whole latest pipeline per project;
  named checks exist only as external status checks (Ultimate).
- Team reviewers (`team_reviewers`): no team review request.
- A viewer-specific branch rule view (`refUpdateRule`): must be derived from access levels.
- Force-push timeline events: versions are an approximation that also records retargets and
  target-side reloads, and need deduplication.
- Draft as a field independent of the title: GitLab stores draft state in the title.
- Test-merge commit on Free: merged results pipelines need Premium.
- Native stack objects with an API: none.
- Reliable retarget error on a closed MR: GitLab drops the change and returns success.
