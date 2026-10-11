"""Phase 10: evidence-grounded research planning (10A) and autonomous research orchestration (10B).

An extension of the Phase 5 engine, not a second orchestrator: a research-objective task is planned by the
Research Planner, approved per research scope (versioned ``ResearchAuthorization``), and then executed in
cycles through the Phase 6B parent DAG runtime, with the Research Evaluator / Replanner deciding each next step
inside the approved scope.
"""
