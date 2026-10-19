"""Node quarantine (proposal: "PCs that fail repeatedly are temporarily taken out of the pool").
R1 (Areej)'s scope per the updated proposal's team-area table - see
docs/handoff/R1_resilience_plan.md item 3. Built ahead of time by request; not implemented for
Areej without her knowledge, since she already knows this plan exists and nothing here claims it
as R3's own credit.
"""
from datetime import datetime, timedelta, timezone

from app.allocation_engine import (
    QUARANTINE_FAILURE_THRESHOLD,
    QUARANTINE_STABILITY_WINDOW_SECONDS,
    clear_node_failure,
    fitting_device_count,
    lock_online_nodes,
    register_node_failure,
)
from app.models import Node


def test_failures_below_threshold_do_not_quarantine(app, db, make_node):
    node = make_node("FLAKY-A")
    node_id = node.id

    with app.app_context():
        for _ in range(QUARANTINE_FAILURE_THRESHOLD - 1):
            register_node_failure(Node.query.get(node_id))
        db.session.commit()

    db.session.expire_all()
    n = Node.query.get(node_id)
    assert n.consecutive_failures == QUARANTINE_FAILURE_THRESHOLD - 1
    assert n.quarantined_until is None


def test_crossing_the_threshold_quarantines_the_node(app, db, make_node):
    node = make_node("FLAKY-B")
    node_id = node.id

    with app.app_context():
        for _ in range(QUARANTINE_FAILURE_THRESHOLD):
            register_node_failure(Node.query.get(node_id))
        db.session.commit()

    db.session.expire_all()
    n = Node.query.get(node_id)
    assert n.consecutive_failures == QUARANTINE_FAILURE_THRESHOLD
    assert n.quarantined_until is not None
    # SQLite drops tzinfo on round-trip even for a timezone-aware column; compare naively,
    # the way the rest of the app does for the same reason (see agent_routes._aware).
    assert n.quarantined_until.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)


def test_a_heartbeat_soon_after_a_failure_does_not_erase_the_streak(app, db, make_node):
    """The bug this design fixes: a node is only ever re-flagged as failed once per distinct
    outage (its status already left "online" the moment cleanup_system first caught it), so
    "repeatedly" necessarily spans separate online/offline cycles. If a heartbeat landing right
    after the first failure reset the counter to 0 immediately, a second outage could never add
    to it and the threshold would be unreachable - so a heartbeat inside the stability window
    must leave the streak alone."""
    node = make_node("FLAKY-C")
    node_id = node.id

    with app.app_context():
        n = Node.query.get(node_id)
        register_node_failure(n)
        db.session.commit()

        clear_node_failure(n)   # "the node answered again" - moments later, same outage window
        db.session.commit()

    db.session.expire_all()
    n = Node.query.get(node_id)
    assert n.consecutive_failures == 1


def test_a_heartbeat_after_the_stability_window_does_reset_the_streak(app, db, make_node):
    node = make_node("FLAKY-D")
    node_id = node.id

    with app.app_context():
        n = Node.query.get(node_id)
        register_node_failure(n)
        n.last_failure_at = (datetime.now(timezone.utc)
                              - timedelta(seconds=QUARANTINE_STABILITY_WINDOW_SECONDS + 1))
        db.session.commit()

        clear_node_failure(n)   # genuinely stable since then - safe to forgive
        db.session.commit()

    db.session.expire_all()
    n = Node.query.get(node_id)
    assert n.consecutive_failures == 0


def test_an_active_quarantine_is_not_lifted_by_a_later_heartbeat(app, db, make_node):
    """A quarantine already in effect holds for its full cooldown - it isn't lifted just because
    the flapping PC happened to answer once more during the cooldown window."""
    node = make_node("FLAKY-E")
    node_id = node.id

    with app.app_context():
        n = Node.query.get(node_id)
        for _ in range(QUARANTINE_FAILURE_THRESHOLD):
            register_node_failure(n)
        db.session.commit()
        quarantined_at = n.quarantined_until

        clear_node_failure(n)
        db.session.commit()

    db.session.expire_all()
    n = Node.query.get(node_id)
    assert n.quarantined_until == quarantined_at


def test_a_quarantined_node_is_excluded_from_scheduling(app, db, make_node):
    online_node = make_node("GOOD-NODE", device_key_hash="g1")
    quarantined_node = make_node(
        "QUARANTINED-NODE", device_key_hash="q1",
        quarantined_until=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    with app.app_context():
        candidates = {n.id for n in lock_online_nodes()}
        db.session.commit()  # release the row locks taken above

    assert online_node.id in candidates
    assert quarantined_node.id not in candidates


def test_a_node_whose_cooldown_has_expired_is_eligible_again(app, db, make_node):
    node = make_node(
        "EX-QUARANTINE-NODE", device_key_hash="q2",
        quarantined_until=datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    with app.app_context():
        candidates = {n.id for n in lock_online_nodes()}
        db.session.commit()

    assert node.id in candidates


def test_fitting_device_count_excludes_a_quarantined_node(app, db, make_node):
    make_node("SIZING-GOOD", device_key_hash="s1", sandbox_mode="docker",
              total_cores=8, total_ram_mb=8192)
    make_node(
        "SIZING-QUARANTINED", device_key_hash="s2", sandbox_mode="docker",
        total_cores=8, total_ram_mb=8192,
        quarantined_until=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    with app.app_context():
        count = fitting_device_count(1, 1024)

    assert count == 1


def test_cleanup_system_quarantines_a_node_that_keeps_timing_out(app, db, make_node):
    """Integration path: repeated cleanup_system ticks, each finding the same node still dead,
    cross the threshold and quarantine it - even once it comes back online with a fresh
    heartbeat, it stays excluded from scheduling until the cooldown passes."""
    from app.tasks import cleanup_system

    node = make_node("REAL-FLAKY-NODE", device_key_hash="q3")
    node_id = node.id

    for _ in range(QUARANTINE_FAILURE_THRESHOLD):
        with app.app_context():
            n = Node.query.get(node_id)
            n.status = "online"
            n.last_heartbeat = datetime.now(timezone.utc) - timedelta(seconds=90)
            db.session.commit()
            cleanup_system()
        db.session.expire_all()

    n = Node.query.get(node_id)
    assert n.quarantined_until is not None

    # The node comes back online with a fresh heartbeat, but the quarantine still holds.
    with app.app_context():
        n = Node.query.get(node_id)
        n.status = "online"
        n.last_heartbeat = datetime.now(timezone.utc)
        db.session.commit()
        candidates = {c.id for c in lock_online_nodes()}
        db.session.commit()

    assert node_id not in candidates
