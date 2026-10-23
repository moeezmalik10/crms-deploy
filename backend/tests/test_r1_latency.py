"""R1 item 4 (docs/handoff/R1_resilience_plan.md): the detection-to-recovery latency budget is
(heartbeat timeout) + (next cleanup_system tick) + (next process_queue tick). The 60s heartbeat
timeout is deliberately untouched (shrinking it risks reintroducing false failure alarms - see
app.tasks's own comment); both tick intervals were cut from 30s/15s to 5s instead. Locks that in
so a future change doesn't silently widen the latency budget back out without anyone noticing.
"""
from app import scheduler


def test_cleanup_system_ticks_every_five_seconds(app):
    job = scheduler.get_job("cleanup_system")
    assert job is not None
    assert job.trigger.interval.total_seconds() == 5


def test_process_queue_ticks_every_five_seconds(app):
    job = scheduler.get_job("process_queue")
    assert job is not None
    assert job.trigger.interval.total_seconds() == 5
