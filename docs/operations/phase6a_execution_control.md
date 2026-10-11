# Phase 6A: Explicit Execution Control

Research Tasks remain the scientific-specification SSOT.  Linear `Status` remains
the task-status SSOT and its existing mirror is not used by this feature.  PostgreSQL
is the SSOT for execution request/run state; Notion is an idempotent projection.

## One-time Notion setup

The verified data source has `Run Autopilot` (button), `Run Requested`
(checkbox), `Execution Status` (status), `Last Run` (URL), and `Execution Error`
(text). The API documents Button as read-only (`button: {}`): it cannot
configure or press a Button. A workspace owner configures it in the Notion UI:

1. In Research Tasks, add a **Button** property named `Run Autopilot`.
2. Configure its action to edit the current page: set `Run Requested` to checked
   and `Execution Status` to `Requested`.
3. Ensure `Execution Status` has exactly these options: `Idle`, `Requested`,
   `Accepted`, `Running`, `Awaiting Approval`, `Blocked`, `Completed`, `Failed`,
   `Canceled`.
4. Grant the Research Autopilot integration **Update content** capability for this
   data source; read-only capability cannot acknowledge requests or project state.

The button was verified against the test H2 task on 2026-10-11.

## Consistency contract

`Run Requested=true` represents at most one unaccepted request. The poller stores
the request/run before clearing it. A failed acknowledgement is safe: the request's
unique `page_id:last_edited_time` key makes the next poll reuse the accepted run
and retry the acknowledgement. A pending new request suppresses delayed projection
from an older run. Notion exposes `last_edited_time`, but no
compare-and-swap precondition for `PATCH /pages`; therefore ambiguous races are
blocked/reviewed rather than inferred.  Delayed projections are derived from the
latest PostgreSQL execution run and never use Notion's editable status as authority.

## State mapping

`Accepted` is a persisted request/run before workflow start. `Running` begins when
the Phase 5 workflow is created, not at the first scientific calculation.
`awaiting_authorization` and `awaiting_approval` map to `Awaiting Approval`;
`done`, `blocked`, `cancelled`, and `dead_letter` map to `Completed`, `Blocked`,
`Canceled`, and `Failed`. Completion retains the existing Phase 5 completion gate.
