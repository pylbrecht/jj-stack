# GitLab merge mechanics

Research for [issue 11](https://github.com/pylbrecht/jj-stack/issues/11), part of the "GitLab
support via glab" effort ([map](https://github.com/pylbrecht/jj-stack/issues/1)). It builds on
`research/gitlab-equivalents.md` (MR state, pipelines, approvals, retarget, merged-by-push) and
`research/glab-api.md` (transport); facts from those files are not repeated.

Scope: self-managed GitLab reached only through `glab api` (REST v4 or GraphQL). The reference
instance reports `19.5.4-ee`. The GitHub side is what `src/jj_stack/commands/merge/` and
`docs/internals/design.md` ("Merge", "Repo policy") rely on.

Sources: docs.gitlab.com pages (cited per section) and GitLab source at `master`. `SRC` is
`https://gitlab.com/gitlab-org/gitlab/-/blob/master`. No live calls were made for this ticket;
every fact comes from docs or source.

## Summary

- **No stack merge and no async merge operation.** GitHub's `merge-async` takes one request for a
  whole stack prefix, returns `pending`/`enqueued`/`merged`/`failed`, and has an operation UUID to
  poll. GitLab merges one MR per request. A GitLab stack merge is a client-side loop: retarget
  the bottom MR to trunk, merge, retarget or rebase the next one, repeat.
- **REST merge is synchronous.** `PUT .../merge` without `auto_merge` runs the Git merge inside
  the request and returns the merged MR (`state: merged`, merge or squash SHA) or an error. There
  is nothing to poll for an immediate merge.
- **SHA guard exists.** `sha` on `PUT .../merge` returns 409 on mismatch. With auto-merge, the
  SHA is stored and any push of a different head aborts the auto-merge. This matches GitHub's
  `sha` on `merge-async`.
- **Auto-merge is the "wait for checks" primitive** (all tiers). `auto_merge=true` merges now
  when the MR is mergeable and no pipeline runs, otherwise it arms auto-merge. It skips almost
  every check at arm time (draft, approvals, threads, conflicts, CI) and waits. A failed
  pipeline does **not** abort it. A source push, a retarget, and (for `ff`/`rebase_merge`) a
  target push abort it. The reason exists only as a system note.
- **Merge trains are the merge-queue equivalent** (Premium). Since 19.1 `auto_merge=true`
  routes to the train when trains are on, and since 19.3 an enforcement setting makes a direct
  merge return 405. Train state is readable per MR (REST car `status`, GraphQL
  `mergeTrainIndex`); a removal reason exists only as a system note.
- **The merge method is a project setting, not a request field.** `merge`, `rebase_merge`, or
  `ff`, plus `squash_option`. A request can only ask for `squash`. jj-stack's `--method` has no
  GitLab equivalent except squash on or off.
- **Approvals block the merge, they do not reject the request.** An unapproved MR makes a direct
  merge return 405 (`detailed_merge_status: not_approved`); auto-merge waits for approval.
  Required approvals are EE/Premium; on Free and CE an approval is advisory.
- **The merge result commit is not always exposed.** REST and GraphQL expose
  `merge_commit_sha` and `squash_commit_sha`. A fast-forward without squash lands `sha`
  itself. A fast-forward after server-side rebase (automatic rebase, or `ff` merge trains) lands
  new commits whose SHA no API field returns. Trunk ancestry must then be the evidence, as
  design.md already requires.

## Merge API

Docs: [merge a merge request](https://docs.gitlab.com/api/merge_requests/#merge-a-merge-request).
Source: `lib/api/merge_requests.rb` (`put ':id/merge_requests/:merge_request_iid/merge'`,
`execute_merge`, `execute_immediate_merge!`), `lib/api/helpers.rb` (`check_sha_param!`),
`app/services/merge_requests/merge_service.rb`.

- `PUT /projects/:id/merge_requests/:iid/merge`. Parameters: `sha`, `auto_merge`,
  `merge_when_pipeline_succeeds` (deprecated 17.11), `squash`, `squash_commit_message`,
  `merge_commit_message`, `should_remove_source_branch`, and EE `skip_merge_train`.
- No merge-method parameter. The method is the project's `merge_method` (`merge`,
  `rebase_merge`, `ff`; `GET /projects/:id`). `squash=true` is applied only when true (`if
  params[:squash]`), so `squash=false` cannot override an MR that has squash on. With
  `squash_option: always`, a non-squash merge fails ("This project requires squashing commits").
- Status codes (docs and source):
  - 400 "SHA must be provided when merging": `sha` missing and the group or instance setting
    "Require a commit SHA on the merge requests API" is on (19.2, Free;
    [docs](https://docs.gitlab.com/user/group/manage/)). Default for new groups changed back to
    off in 19.2.6 / 19.3.2 / 19.4.
  - 401: the user cannot merge (`can_be_merged_by?`, no merge access to the target branch).
  - 405: not mergeable (any failing merge check), or merge-train enforcement refuses a direct
    merge. The body gives no reason; read `detailed_merge_status`.
  - 409 "SHA does not match HEAD of source branch: <sha>": `sha` differs from `diff_head_sha`.
  - 422 "Branch cannot be merged": the Git merge failed or did not complete in the request
    (for example a lost lease against a concurrent merge). `merge_error` on the MR holds the
    stored message.
- Immediate merge is synchronous: `MergeService#execute` runs in the request and the API
  re-checks `merged?` before it answers. A 200 returns the merged MR. GraphQL
  `mergeRequestAccept` is different: since 16.5 it calls `merge_async`, so its response does not
  yet show `merged` (source: `app/graphql/mutations/merge_requests/accept.rb`). GraphQL requires
  `sha`, takes an explicit `strategy` (`MERGE_WHEN_CHECKS_PASS`, `MERGE_TRAIN`,
  `ADD_TO_MERGE_TRAIN_WHEN_CHECKS_PASS`), and reports failures in the payload `errors`
  ("The merge-head is not at the anticipated SHA", "This branch cannot be merged", "The merge
  request is already scheduled to be merged").
- The merge refuses while the target branch waits for deletion after a lower merge ("The target
  branch is scheduled for deletion. Please wait for the branch to be retargeted.").
- `should_remove_source_branch=true` (or the project's `remove_source_branch_after_merge`) is
  what triggers GitLab's automatic retarget of up to 4 dependents
  (see `research/gitlab-equivalents.md`).

GitHub comparison: `submit_stack_merge` sends `PUT pulls/:n/merge-async` with
`merge_action`, `merge_method`, and `sha`, treats 409 as "an operation is already in flight",
and reads 400/409 with "head" as a head mismatch. On GitLab, 409 means only a SHA mismatch,
there is no in-flight operation identity to compare, and `merge_method` cannot be sent.

Impact on a GitLab `merge`: a direct merge needs no `_request_result` polling loop. Always send
`sha` (required on some instances). Map 405 to "blocked" and read `detailed_merge_status` for
the reason; map 409 to "head changed, run submit". The `--method` choice reduces to the squash
flag, checked against `squash_option`; the method-resolution and signed-stack rules in
design.md need a GitLab form ("the project decides the method").

## Auto-merge (merge when checks pass)

Docs: [auto-merge](https://docs.gitlab.com/user/project/merge_requests/auto_merge/). Source:
`app/services/auto_merge_service.rb`, `app/services/auto_merge/base_service.rb`,
`app/services/auto_merge/merge_when_checks_pass_service.rb`,
`app/models/merge_request.rb` (`skipped_auto_merge_checks`),
`app/services/merge_requests/refresh_service.rb`, `update_service.rb`.

- Tier: all. Current form ("merge when checks pass") since 17.7; `auto_merge` parameter name
  since 17.11 replaces `merge_when_pipeline_succeeds`.
- Set: `PUT .../merge` with `auto_merge=true` (and `sha`). Logic in `execute_merge`: if an
  auto-merge strategy is available, arm it; else if no pipeline runs or the head pipeline
  succeeded, merge immediately; else 405. `MergeWhenChecksPassService` is unavailable when the
  MR is already mergeable and its pipeline is not in progress, so `auto_merge=true` on a ready MR
  merges at once.
- The REST endpoint ignores the strategy's return value. If arming fails (`:failed`), the
  response is still 200 with the open MR. Read `merge_when_pipeline_succeeds` in the response.
- Arming skips the CI, approval, conflict, draft, blocked, threads, status-check,
  requested-changes and other checks (`skipped_auto_merge_checks`). So auto-merge can be armed
  on a draft or unapproved MR and waits. It re-runs all checks before it merges
  (`process` → `mergeable?(use_cache: false)` → `merge_async`).
- See it: REST `merge_when_pipeline_succeeds` and `merge_user`; GraphQL `autoMergeEnabled`,
  `autoMergeStrategy`, `availableAutoMergeStrategies`, `mergeUser`, `mergeOngoing`.
- Aborts (system note "aborted the automatic merge because <reason>"):
  - a push to the source branch whose new head differs from the stored `sha`
    ("the source branch was updated");
  - a retarget ("the target branch was changed");
  - a push to the target branch, for `ff` or `rebase_merge` projects, when the MR now needs a
    rebase ("the target branch was updated"), unless automatic rebase before merge is on
    (`retain_auto_merge_with_automatic_rebase`, 19.3, flag off by default);
  - a failed merge attempt.
- Does **not** abort: a failed pipeline. Per the docs, the author can retry jobs or push; the
  auto-merge stays armed until then. A new unresolved thread blocks but does not abort.
- Cancel: `POST .../cancel_auto_merge` (19.5; 201 with the MR, 409 when not armed, 403) or the
  deprecated `POST .../cancel_merge_when_pipeline_succeeds` (always 201 with a status hash).
- The MR's own fields hold no abort reason. Only the notes list does (`system: true`).

GitHub comparison: jj-stack does not use GitHub auto-merge. It waits for required checks itself
(`checks.py`, stop at the first failure) and then sends one merge request. GitHub's design.md
rule "a merge initiated through auto-merge is supported" refers to merges by others.

Impact on a GitLab `merge`: two designs are possible. (a) Mirror the GitHub flow: poll
`head_pipeline` (status and `sha`) and `detailed_merge_status`, stop on a failed pipeline, then
send an immediate merge with `sha`. (b) Arm auto-merge with `sha` and poll for `merged`. Option
(b) needs its own failure detection, because a failed pipeline leaves the MR armed and open;
jj-stack must read the pipeline and cancel or report. Interrupting a wait in (b) leaves an armed
auto-merge on GitLab, which design.md allows ("Interrupting a wait does not cancel the
request"). The "rerun resumes waiting" rule maps to "MR armed with the same `sha`"; the stored
SHA is visible only in the system note "enabled an automatic merge when all merge checks for
<sha> pass", not as a field.

## Merge trains

Docs: [merge trains](https://docs.gitlab.com/ci/pipelines/merge_trains/),
[merge trains API](https://docs.gitlab.com/api/merge_trains/). Source:
`ee/lib/api/merge_trains.rb`, `ee/app/services/ee/auto_merge_service.rb`,
`ee/app/services/auto_merge/merge_train_service.rb`,
`ee/app/services/auto_merge/add_to_merge_train_when_checks_pass_service.rb`,
`ee/app/models/merge_trains/car.rb`,
`ee/app/services/merge_requests/merge_strategies/from_train_ref.rb`.

- Tier: Premium. Needs merged results pipelines (`merge_pipelines_enabled`) and
  `merge_trains_enabled`. Detect from `GET /projects/:id` (EE fields `merge_trains_enabled`,
  `merge_trains_skip_train_allowed`, `merge_train_enforcement`,
  `max_pipelines_per_merge_train`). A train is per target branch; there is no per-branch rule.
- Entry: `PUT .../merge` with `auto_merge=true` picks the first available strategy in the order
  `merge_train`, `add_to_merge_train_when_checks_pass`, `merge_when_checks_pass`. The merge
  train strategy needs a completed head pipeline; otherwise the MR is armed to join later.
  `merge_when_checks_pass` is unavailable when trains are on. Routing `auto_merge` to the train
  became unconditional in 19.1 (`fix_merge_api_train_bypass` removed).
- Explicit entry: `POST /projects/:id/merge_trains/merge_requests/:iid` with `sha`, `squash`,
  `auto_merge` (false: join now; true: join when checks pass). 201 (car created) or 202 (armed
  to join), body is the whole train; 400, 401, 409.
- Direct merge with trains on: `PUT .../merge` without `auto_merge` merges immediately and
  restarts the train ("merge immediately"), or with `skip_merge_train=true` (experiment) does
  not restart it. Under `merge_train_enforcement` `enforce_for_all_users` or
  `enforce_with_owner_override` (19.2, GA 19.3) it returns 405 "This project requires merge
  requests to go through the merge train. Set auto_merge to true …".
- Status: `GET /projects/:id/merge_trains/merge_requests/:iid` returns the car (404 when not on
  a train) with `status` `idle`, `stale`, `fresh` (active) or `merging`, `merged`,
  `skip_merged` (complete), `pipeline`, `merged_at`. GraphQL `MergeRequest.mergeTrainCar`,
  `mergeTrainIndex`, `mergeTrainsCount`. `GET /projects/:id/merge_trains/:target_branch` lists
  a train.
- Removal: failed train pipeline, MR made draft or closed, source branch updated, failed merge
  check, cannot combine with earlier cars, timeout, error, or manual removal. The car is deleted
  and the reason exists only as a system note ("removed this merge request from the merge train
  because <reason>", or "aborted automatic add to merge train because <reason>"). Since 19.7
  (flag off by default) user removals are rate limited (429 with `Retry-After`).
- Merge: the car's train ref is fast-forwarded onto the target (`FromTrainRef`), so the target
  gets exactly the commit the train pipeline tested. `merge_commit_sha` / `squash_commit_sha` are
  taken from the train ref parameters.

GitHub comparison: jj-stack asks `base_branch_uses_merge_queue` (queue object or `MERGE_QUEUE`
rule), sends `merge_action=merge_queue`, then polls `mergeQueueEntry` (state, position,
estimate, checks) and `REMOVED_FROM_MERGE_QUEUE_EVENT` for a removal reason. GitLab: queue
presence is the project's `merge_trains_enabled`; entry is `auto_merge=true`; position is
`mergeTrainIndex`; removal is "not merged, no car, auto-merge off", with the reason in notes.
GitHub drops a queue entry before it records the merge; on GitLab the car status goes
`merging` → `merged` and the MR becomes `merged`, so the "unexplained removal" grace polls may
still be needed for the short window between car deletion and the note.

Impact on a GitLab `merge`: the queue branch of the flow maps one MR at a time. A queue entry
needs `auto_merge=true` even when checks have passed. Signed-stack and method rules carry over:
the project decides the method.

## Approvals and other blocking checks

Docs: [approvals API](https://docs.gitlab.com/api/merge_request_approvals/),
[merge status](https://docs.gitlab.com/api/merge_requests/#merge-status).

- `GET .../approvals`: on EE (also unlicensed) `approved` is true when the rules are satisfied,
  plus `approvals_required` and `approvals_left`. On CE `approved` only means "at least one
  approval". `GET .../approval_state` (per-rule) is Premium. GraphQL `approved`,
  `approvalsLeft`, `approvalsRequired`, `approvalState`.
- Required approvals block through the merge checks: `detailed_merge_status: not_approved`, a
  direct merge returns 405, and auto-merge waits. `requested_changes` blocks the same way.
- Other blocking values that a GitLab `merge` must report: `ci_must_pass`, `ci_still_running`,
  `discussions_not_resolved`, `draft_status`, `need_rebase`, `conflict`,
  `merge_request_blocked`, `status_checks_must_pass` (Ultimate), `merge_time` (17.6 "merge
  after"), `title_regex`, `locked_paths`, `security_policy_*`. Transient: `unchecked`,
  `checking`, `preparing`, `approvals_syncing`.
- `reset_approvals_on_push` (Premium project setting): every jj-stack rebase push of an MR can
  drop its approvals. A retarget can also drop approvals (EE hook, see earlier research).

GitHub comparison: design.md says jj-stack "leaves merge eligibility to GitHub" and only waits
for required checks. GitLab supports the same split: send the merge, map 405 to the
`detailed_merge_status` reason.

Impact on a GitLab `merge`: before the request, read `detailed_merge_status`. Wait while it is
transient or `ci_still_running`; stop with a reason for the others. For stacked MRs, each
bottom-up step may reset approvals of the remaining MRs (rebase push, retarget), so a stack merge
can stop after one MR waiting for a re-approval.

## Merge result: SHA and final state

Docs: [MR API](https://docs.gitlab.com/api/merge_requests/), [merge
methods](https://docs.gitlab.com/user/project/merge_requests/methods/). Source:
`MergeService#commit`, `MergeStrategies::FromSourceBranch`, `MergeRequest#merged_commit_sha`,
`lib/api/entities/merge_request_basic.rb`, `app/graphql/types/merge_request_type.rb`.

- Final state: `state: merged`, `merged_at`, `merge_user`, and `merge_commit_sha` (merge commit
  methods) and/or `squash_commit_sha` (squash). A failed async merge leaves `state: opened` with
  `merge_error`.
- Per method:
  - `merge`: a new merge commit; `merge_commit_sha` set (also with squash, plus
    `squash_commit_sha`).
  - `rebase_merge` (semi-linear): merge commit, but only when a fast-forward is possible;
    otherwise `need_rebase`.
  - `ff`: target moves to `sha` (no squash) or to a squash commit (`squash_commit_sha`).
    `merge_commit_sha` is null.
  - Automatic rebase before merge (`automatic_rebase_enabled`, GA 19.2, for `ff` and
    `rebase_merge`): GitLab rebases server-side at merge time without updating the source
    branch, drops signatures, and does not re-run CI. The landed commits are new.
- GitLab stores the landed tip as `merged_commit_sha`, but neither REST nor GraphQL exposes it.
  So for `ff` with automatic rebase (and `ff` merge trains) no API field gives the landed SHA;
  `in_progress_merge_commit_sha` exists only during the merge.

GitHub comparison: `merge-async` returns `details.sha` (the trunk commit), and
`_terminal_result` stops if a merged result lacks it. The queue path uses each PR's
`merge_commit_sha`.

Impact on a GitLab `merge`: the "final trunk commit" must come from `merge_commit_sha`, then
`squash_commit_sha`, then `sha` for a plain fast-forward, and otherwise from a fetch of trunk.
design.md already requires ancestry evidence ("a PR's merged state alone does not establish that
its work reached this repo's trunk"); the sync rule "rewritten merge result on trunk" must accept
a GitLab merge with no reported result SHA and match by content instead.

## Stack merge

- GitLab has no stack object and no multi-MR merge
  ([stacked MRs](https://docs.gitlab.com/user/project/merge_requests/reviews/stacked_merge_requests/)
  are a UI view, see earlier research). Each MR is merged alone into its own target.
- A bottom-up loop works per method:
  - `merge` without squash, or `ff` without squash: after the bottom MR merges, the next MR's
    commits already sit on trunk's history, so a retarget to trunk makes it mergeable without a
    rebase. (Inference from the method definitions; not tested.)
  - `rebase_merge`, squash, or automatic rebase: trunk no longer contains the next MR's base, so
    it is `need_rebase` or conflicts until jj-stack syncs and pushes. That push starts a new
    pipeline and may reset approvals.
- GitLab's own retarget after a merge happens only when the merge deletes the source branch,
  and leaves the dependent with no head pipeline; an explicit retarget via `PUT` starts MR
  pipelines and aborts auto-merge.
- A merge train can hold several MRs of one stack only if they all target trunk; each car's
  train ref then contains the lower cars. (Inference; not tested.)

GitHub comparison: one `merge-async` request for the stack prefix, which GitHub stacks merge
atomically and then rebase; `wait.py` retries while GitHub rewrites the remaining PRs.

Impact on a GitLab `merge`: the "one asynchronous merge request for the selected PRs" rule in
design.md has no GitLab form. A multi-MR GitLab merge is a sequence of single merges with a
retarget (and, for some methods, a sync and push) between them. Each step is a separate
irreversible mutation guarded by its own `sha`, and a stop mid-way leaves lower MRs merged, which
the existing "`sync` when a lower PR already merged" next step already covers.

## Version and tier notes

- All tiers: merge API, `sha` guard, auto-merge (17.7 form), `cancel_auto_merge` (19.5), merge
  methods, squash option, automatic rebase before merge (GA 19.2), "require SHA" group setting
  (19.2), `detailed_merge_status` (15.6).
- EE (any license): `approved` reflects rules; `approvals_required`, `approvals_left`.
- Premium: required approval rules, `approval_state`, merge trains, merged results pipelines,
  `reset_approvals_on_push` setting, MR dependencies (`merge_request_blocked`).
- Ultimate: external status checks (`status_checks_must_pass`).
- Version gates that matter for the reference instance (19.5): `auto_merge` routes to trains
  (19.1), merge-train enforcement (GA 19.3), require-SHA (19.2), `cancel_auto_merge` (19.5).
  Older instances need `merge_when_pipeline_succeeds` (pre-17.11 name) and
  `cancel_merge_when_pipeline_succeeds`.
