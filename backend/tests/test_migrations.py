"""app.migrations._run_ddl - found live in production: run_startup_migrations used to run
every ALTER TABLE in one shared transaction with no error handling, so a single transient
failure (a real psycopg2.errors.DeadlockDetected against the previous instance's scheduler
still holding row locks during a deploy's rollover) crashed the whole boot, even though the
column it was adding already existed and the statement was a no-op either way. These tests
don't touch a real database (run_startup_migrations itself early-returns for sqlite, which is
all the rest of this suite uses) - they exercise _run_ddl's retry/non-fatal behavior directly
against a fake engine that fails on demand.
"""
from contextlib import contextmanager

from app.migrations import NEW_COLUMNS, _run_ddl, run_startup_migrations


class _FakeConn:
    def execute(self, *a, **kw):
        pass


class _FakeEngine:
    """engine.begin() is a context manager; dialect.name gates run_startup_migrations itself."""
    def __init__(self, fail_times=0, dialect_name="postgresql"):
        self.fail_times = fail_times
        self.attempts = 0
        self.dialect = type("D", (), {"name": dialect_name})()

    @contextmanager
    def begin(self):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise Exception("psycopg2.errors.DeadlockDetected: deadlock detected")
        yield _FakeConn()


def test_run_ddl_retries_once_on_a_transient_failure(monkeypatch):
    monkeypatch.setattr("app.migrations.time.sleep", lambda s: None)
    engine = _FakeEngine(fail_times=1)
    _run_ddl(engine, "ALTER TABLE whatever", "do a thing")
    assert engine.attempts == 2   # failed once, retried, succeeded


def test_run_ddl_never_raises_even_if_every_attempt_fails(monkeypatch, capsys):
    monkeypatch.setattr("app.migrations.time.sleep", lambda s: None)
    engine = _FakeEngine(fail_times=99)
    _run_ddl(engine, "ALTER TABLE whatever", "do a thing")   # must not raise
    assert engine.attempts == 2   # one try + one retry, then gives up and logs instead
    assert "WARNING" in capsys.readouterr().out


def test_run_ddl_succeeds_immediately_with_no_retry_needed(monkeypatch):
    monkeypatch.setattr("app.migrations.time.sleep", lambda s: None)
    engine = _FakeEngine(fail_times=0)
    _run_ddl(engine, "ALTER TABLE whatever", "do a thing")
    assert engine.attempts == 1


def test_run_startup_migrations_does_not_crash_boot_when_every_statement_fails(monkeypatch):
    """The actual bug: one deadlocked ALTER TABLE used to abort the single shared transaction
    everything else lived in too, crashing the whole boot. Now every statement is independently
    wrapped, so even a database where literally every DDL call fails must still let the app
    finish starting."""
    monkeypatch.setattr("app.migrations.time.sleep", lambda s: None)
    engine = _FakeEngine(fail_times=10_000)
    fake_db = type("DB", (), {"engine": engine})()

    run_startup_migrations(fake_db)   # must not raise

    # Columns, RLS statements and the unique index all go through _run_ddl (try + 1 retry each).
    # The 2 CHECK constraints below them use their own single try/except with no retry - a
    # pre-existing, lower-risk path (see run_startup_migrations) left as-is here.
    retried_statements = sum(len(cols) for cols in NEW_COLUMNS.values()) + 6 + 1
    assert engine.attempts == retried_statements * 2 + 2


def test_run_startup_migrations_skips_entirely_for_non_postgres(monkeypatch):
    engine = _FakeEngine(dialect_name="sqlite")
    fake_db = type("DB", (), {"engine": engine})()
    run_startup_migrations(fake_db)
    assert engine.attempts == 0
