"""Linear operations used by Phase 5, with two guards:

- Autopilot may move issues only into non-approval states (it can never "approve" itself);
- in test-only mode (default on the Mac) writes are allowed only on issues whose title starts with "[TEST]" or "[E2E]".
Every status change is recorded so a later history entry can be recognized as Autopilot's own.
"""

from dataclasses import dataclass

from research_autopilot.storage.db import Store

AWAITING = "Awaiting Approval"
APPROVED = "Approved"
REVISION = "Revision Requested"
REJECTED = "Canceled"
AUTOPILOT_STATES = {"Todo", "In Progress", AWAITING, "Done"}
HUMAN_DECISION_STATES = {APPROVED: "approve", REVISION: "revise", REJECTED: "reject"}


TEST_PREFIXES = ("[TEST]", "[E2E]")  # issues that test-only mode may write to

class WriteRefused(Exception):
    pass


@dataclass
class LinearOps:
    linear: object  # LinearWriter (or fake)
    store: Store
    test_only: bool = True
    write_enabled: bool = False

    def _guard(self, issue) -> None:
        if not self.write_enabled:
            raise WriteRefused("Linear writes disabled (RA_AUTOPILOT_WRITE=false)")
        if self.test_only and not issue.title.startswith(TEST_PREFIXES):
            raise WriteRefused(f"test-only mode: refusing to write to non-test issue {issue.identifier}")

    def missing_states(self, team_id: str) -> list[str]:
        have = set(self.linear.team_states(team_id))
        return [s for s in (AWAITING, APPROVED, REVISION, REJECTED) if s not in have]

    def set_state(self, issue, name: str, reason: str) -> None:
        if name not in AUTOPILOT_STATES:
            raise WriteRefused(f"Autopilot may not move issues to {name!r}")
        self._guard(issue)
        if issue.state_name == name:
            return
        states = self.linear.team_states(issue.team_id)
        if name not in states:
            raise WriteRefused(f"Linear workflow state {name!r} does not exist")
        state_id, state_type = states[name]
        if name == "Done" and state_type != "completed":
            raise WriteRefused("'Done' is not a completed state")
        self.store.record_linear_write(issue.identifier, name, reason)
        self.linear.set_state(issue.id, state_id)
        issue.state_name, issue.state_type = name, state_type

    def reflect_verified_decision(self, issue, name: str, reason: str) -> None:
        """Reflect a controller-verified Discord rejection/revision in Linear.

        This is deliberately separate from ``set_state`` so ordinary Autopilot
        code still cannot manufacture a human approval state.
        """
        if name not in {REVISION, REJECTED}:
            raise WriteRefused(f"not a reflectable human decision state: {name!r}")
        self._guard(issue)
        if issue.state_name == name:
            return
        states = self.linear.team_states(issue.team_id)
        if name not in states:
            raise WriteRefused(f"Linear workflow state {name!r} does not exist")
        state_id, state_type = states[name]
        self.store.record_linear_write(issue.identifier, name, reason)
        self.linear.set_state(issue.id, state_id)
        issue.state_name, issue.state_type = name, state_type

    def cancel_by_human(self, issue, actor: str, reason: str) -> None:
        """Move an issue to Canceled for a verified human Cancel (Discord button of an authorized user / CLI).

        ``set_state`` refuses 'Canceled' on purpose (Autopilot never rejects on its own); only the research
        Cancel control calls this, with the verified actor, and every write is audited like any other.
        """
        if not actor or actor.startswith("auto:"):
            raise WriteRefused("Canceled requires a verified human actor")
        self._guard(issue)
        if issue.state_name == REJECTED or issue.state_type in ("completed", "canceled"):
            return
        states = self.linear.team_states(issue.team_id)
        if REJECTED not in states:
            raise WriteRefused(f"Linear workflow state {REJECTED!r} does not exist")
        state_id, state_type = states[REJECTED]
        self.store.record_linear_write(issue.identifier, REJECTED, f"human cancel by {actor}: {reason}"[:500])
        self.linear.set_state(issue.id, state_id)
        issue.state_name, issue.state_type = REJECTED, state_type

    def comment(self, issue, body: str) -> str:
        self._guard(issue)
        return self.linear.create_comment(issue.id, body)
