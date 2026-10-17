"""Reads the db left behind by a load-test run and reports against the proposal's Objective 2
success criterion: "No over-allocation of any PC under load test; Jain's fairness index of 0.9
or higher under contention."

Fairness is measured as each student's count of jobs that reached "completed" during the
run - the system's observable share handed to that student while every student was competing
for the same scarce, deliberately-undersized pool. Jain's index:
    J(x1..xn) = (sum(xi))^2 / (n * sum(xi^2))
1.0 is perfectly equal shares; 1/n is the worst case (one student got everything).

Over-allocation is checked directly against the Unified Quota Ledger columns this session
added (Node.used_cores/reserved_cores/used_ram_mb/reserved_ram_mb, guarded by a DB CHECK
constraint) - if the scheduler had ever over-committed a node, either the CHECK constraint
would have raised during the run (and the run would have crashed, not produced this report) or
it would show up directly in this final snapshot.
"""
import common
common.configure_env()

from app import create_app, db
from app.models import Node, TaskRequest, User

app = create_app()
with app.app_context():
    students = User.query.filter(User.username.like("load%")).order_by(User.id).all()
    per_student = {}
    for u in students:
        completed = TaskRequest.query.filter_by(user_id=u.id, mode="job", status="completed").count()
        submitted = TaskRequest.query.filter_by(user_id=u.id, mode="job").count()
        per_student[u.username] = {"completed": completed, "submitted": submitted}

    xs = [v["completed"] for v in per_student.values()]
    n = len(xs)
    total_completed = sum(xs)
    total_submitted = sum(v["submitted"] for v in per_student.values())

    if total_completed == 0:
        jain = None
    else:
        jain = (sum(xs) ** 2) / (n * sum(x * x for x in xs))

    print(f"Students: {n}")
    print(f"Total jobs submitted: {total_submitted}, completed: {total_completed}")
    print(f"Per-student completions: min={min(xs)}, max={max(xs)}, "
         f"students with zero completions: {sum(1 for x in xs if x == 0)}")
    print()
    if jain is None:
        print("Jain's fairness index: N/A (no job completed during the run)")
    else:
        verdict = "PASS" if jain >= 0.9 else "BELOW TARGET"
        print(f"Jain's fairness index: {jain:.4f}  (target >= 0.90)  -> {verdict}")

    print()
    nodes = Node.query.filter(Node.name.like("LOAD-NODE-%")).all()
    violations = [n for n in nodes
                 if (n.used_cores or 0) + (n.reserved_cores or 0) > (n.total_cores or 0) + 0.01
                 or (n.used_ram_mb or 0) + (n.reserved_ram_mb or 0) > (n.total_ram_mb or 0) + 0.5]
    if violations:
        print(f"OVER-ALLOCATION DETECTED on {len(violations)} node(s): {[v.name for v in violations]}")
    else:
        print(f"No over-allocation: all {len(nodes)} node(s)' used+reserved stayed within total "
             f"(checked against the Unified Quota Ledger columns; also DB-enforced live by the CHECK constraint).")
