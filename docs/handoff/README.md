# Team handoff plans

Three planning documents, one per remaining component from the updated proposal's component
breakdown that isn't R3's (Muhammad Moeez's) own scope:

- [`R1_resilience_plan.md`](R1_resilience_plan.md) — Areej Fatima, PC-level self-healing
  (Windows-service agent, watchdog, log rotation, node quarantine).
- [`R2_explainable_ids_plan.md`](R2_explainable_ids_plan.md) — Laiba Azhar, request-level
  features + SHAP explanations for the intrusion detection model.
- [`R4_sessions_gateway_plan.md`](R4_sessions_gateway_plan.md) — Maryam Tanveer, VM slot pool,
  one-time credentials, the Guacamole remote-access gateway, and pool consolidation (built under
  R3, credited to R4 in the updated proposal — see
  https://github.com/moeezmalik10/crms-deploy/issues/1).

**What these are:** a grounded starting point — what the proposal actually asks for, what's
already built (so nobody duplicates work), what's genuinely missing, and a suggested order. Each
one cites specific files and functions in the current codebase rather than speaking in the
abstract.

**What these are not:** implementations. No code was written against these plans — that's
intentional. R1/R2/R4 are individually graded components under this team's own proposal, and
having an AI build them instead of the people assigned to them would misrepresent whose work is
being evaluated. These plans exist so each person can start from an accurate picture of the
codebase instead of from zero, not so the work can be skipped.
