"""Linear operations used by Phase 5, with two guards:

- Autopilot may move issues only into non-approval states (it can never "approve" itself);
- in test-only mode (default on the Mac) writes are allowed only on issues whose title starts with "[TEST]".
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
        if self.test_only and not issue.title.startswith("[TEST]"):
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

    def comment(self, issue, body: str) -> str:
        self._guard(issue)
        return self.linear.create_comment(issue.id, body)
