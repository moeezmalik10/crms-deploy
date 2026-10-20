"""Per-student quotas: one source of truth for how many sessions/jobs a student may
have pending or running at once, instead of two separate ad-hoc checks drifting apart.
Limits are read from the environment so an admin can tune them without a code change.
"""
import os
from datetime import datetime, timezone

from app import db
from app.models import StudentQuota, TaskRequest, User

# A task counts against a quota from the moment it's queued until it finishes - not just
# while it's actually running - otherwise two requests submitted back to back would both
# get through before either is allocated.
PENDING_OR_ACTIVE = ("pending", "allocated", "starting", "running")

MAX_ACTIVE_SESSIONS = int(os.environ.get("QUOTA_MAX_SESSIONS", "1"))
MAX_ACTIVE_JOBS = int(os.environ.get("QUOTA_MAX_JOBS", "3"))


def active_session_count(user_id):
    """Physical/remote/VM sessions (not pool jobs) currently queued or running for this user."""
    return TaskRequest.query.filter(
        TaskRequest.user_id == user_id,
        TaskRequest.status.in_(PENDING_OR_ACTIVE),
        TaskRequest.mode.notin_(["job", "job_group"]),
    ).count()


def active_job_count(user_id):
    """Pool jobs (single or split) currently queued or running for this user. A split job's
    children all share one parent_task_id, so the group counts once, not once per part."""
    singles = TaskRequest.query.filter(
        TaskRequest.user_id == user_id, TaskRequest.mode == "job",
        TaskRequest.parent_task_id.is_(None),
        TaskRequest.status.in_(PENDING_OR_ACTIVE),
    ).count()
    groups = sum(
        1 for g in TaskRequest.query.filter_by(user_id=user_id, mode="job_group").all()
        if any(c.status in PENDING_OR_ACTIVE for c in g.sub_tasks)
    )
    return singles + groups


def _lock_user(user_id):
    """Lock the requesting student's own User row for the rest of this transaction - the
    Unified Quota Ledger's "quota row of the requesting student" (there's no separate
    per-student table; the user's own row stands in for it). Without this, two submissions
    from the same student arriving together can each COUNT() before either commits and both
    read the same under-the-limit number, so both pass. The caller's route must go on to
    commit (or roll back) this same transaction promptly - holding the lock any longer than
    that blocks the student's own next request, not anyone else's, since each student only
    ever locks their own row."""
    User.query.with_for_update().get(user_id)


def refresh_student_quota(user_id):
    """Persists the proposal's literal per-student quota ledger row - a read-only snapshot,
    never consulted by session_quota_message/job_quota_message themselves (see StudentQuota's
    docstring). Does not commit: on the "allowed" path the caller's own eventual commit (after
    creating the new TaskRequest) carries this write along in the same transaction; on the
    "blocked" path the caller below commits it directly, since nothing else is pending."""
    sessions = active_session_count(user_id)
    jobs = active_job_count(user_id)
    row = StudentQuota.query.get(user_id)
    if row is None:
        row = StudentQuota(user_id=user_id)
        db.session.add(row)
    row.active_sessions = sessions
    row.active_jobs = jobs
    row.max_sessions = MAX_ACTIVE_SESSIONS
    row.max_jobs = MAX_ACTIVE_JOBS
    row.updated_at = datetime.now(timezone.utc)
    return sessions, jobs


def session_quota_message(user_id):
    """None if the user may start another session, otherwise an error string."""
    _lock_user(user_id)
    sessions, _ = refresh_student_quota(user_id)
    if sessions >= MAX_ACTIVE_SESSIONS:
        db.session.commit()
        return f"You already have an active session (limit {MAX_ACTIVE_SESSIONS})."
    return None


def job_quota_message(user_id):
    """None if the user may submit another job, otherwise an error string."""
    _lock_user(user_id)
    _, jobs = refresh_student_quota(user_id)
    if jobs >= MAX_ACTIVE_JOBS:
        db.session.commit()
        return f"You already have {MAX_ACTIVE_JOBS} jobs waiting or running."
    return None
