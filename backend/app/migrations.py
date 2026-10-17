"""Small, safe schema updates applied at start-up.

db.create_all() only creates missing tables; it never adds columns to tables that already
exist in Supabase. These ALTER statements add the resource-pool columns if they are missing,
so an existing database keeps working without running SQL by hand. Every statement is
idempotent (ADD COLUMN IF NOT EXISTS) and only runs on PostgreSQL.
"""
from sqlalchemy import text

NEW_COLUMNS = {
    "node": [
        ("owner_user_id", "INTEGER"),
        ("device_key_hash", "VARCHAR(64)"),
        ("lan_ip", "VARCHAR(64)"),
        ("public_ip", "VARCHAR(64)"),
        ("os_name", "VARCHAR(120)"),
        ("cpu_model", "VARCHAR(160)"),
        ("cpu_ghz", "DOUBLE PRECISION"),
        ("total_storage_gb", "DOUBLE PRECISION"),
        ("free_storage_gb", "DOUBLE PRECISION"),
        ("share_cores", "DOUBLE PRECISION"),
        ("share_ram_mb", "DOUBLE PRECISION"),
        ("share_storage_gb", "DOUBLE PRECISION"),
        ("paused", "BOOLEAN DEFAULT FALSE"),
        ("allow_light_sandbox", "BOOLEAN DEFAULT FALSE"),
        ("sandbox_mode", "VARCHAR(20)"),
        ("agent_version", "VARCHAR(20)"),
        ("used_cores", "DOUBLE PRECISION DEFAULT 0 NOT NULL"),
        ("used_ram_mb", "DOUBLE PRECISION DEFAULT 0 NOT NULL"),
        ("reserved_cores", "DOUBLE PRECISION DEFAULT 0 NOT NULL"),
        ("reserved_ram_mb", "DOUBLE PRECISION DEFAULT 0 NOT NULL"),
    ],
    "task_request": [
        ("required_disk_mb", "DOUBLE PRECISION"),
        ("job_runtime", "VARCHAR(20)"),
        ("job_entry", "VARCHAR(255)"),
        ("job_args", "VARCHAR(500)"),
        ("input_blob_id", "INTEGER"),
        ("result_blob_id", "INTEGER"),
        ("exit_code", "INTEGER"),
        ("output_tail", "TEXT"),
        ("priority", "INTEGER DEFAULT 50"),
        ("job_kind", "VARCHAR(20)"),
        ("allocated_at", "TIMESTAMPTZ"),
        ("result_sha256", "VARCHAR(64)"),
    ],
}

# (table, constraint name, check expression) - added in their own try/except below, after the
# columns above exist. Every existing row already has used/reserved = 0 from the column
# defaults, so the constraint holds immediately; no backfill or NOT VALID needed.
NEW_CHECK_CONSTRAINTS = [
    ("node", "ck_node_cores_ledger", "used_cores + reserved_cores <= total_cores + 0.01"),
    ("node", "ck_node_ram_ledger", "used_ram_mb + reserved_ram_mb <= total_ram_mb + 0.5"),
]


def run_startup_migrations(db):
    engine = db.engine
    if engine.dialect.name != "postgresql":
        return
    with engine.begin() as conn:
        for table, cols in NEW_COLUMNS.items():
            for name, sqltype in cols:
                conn.execute(text(f'ALTER TABLE public."{table}" ADD COLUMN IF NOT EXISTS {name} {sqltype}'))
        # The new tables hold private data; the browser (anon key) must never read them.
        for table in ("device_enrollment", "pool_blob", "pool_file", "pool_chunk", "pool_replica",
                      "node_metrics_history"):
            conn.execute(text(f'ALTER TABLE IF EXISTS public."{table}" ENABLE ROW LEVEL SECURITY'))
    # Two nodes must never share a device key (NULL is fine - unkeyed/legacy nodes have no key).
    # A separate transaction: if duplicate keys already exist from before this was added, log it
    # instead of blocking the app from starting - app.py's device-key lookups already return
    # the first match either way, so this only makes the existing invariant enforced, not new.
    try:
        with engine.begin() as conn:
            conn.execute(text(
                'CREATE UNIQUE INDEX IF NOT EXISTS node_device_key_hash_key ON public."node" (device_key_hash)'
            ))
    except Exception as e:
        print(f"WARNING: could not enforce unique device keys (likely duplicates already exist): {e}")

    # Postgres has no "ADD CONSTRAINT IF NOT EXISTS", and re-adding a constraint that already
    # exists raises - so each one gets its own transaction and is skipped (not fatal) if it's
    # already there or if some row genuinely violates it, same precedent as the index above. A
    # failure here must never stop the backend from booting.
    for table, name, expr in NEW_CHECK_CONSTRAINTS:
        try:
            with engine.begin() as conn:
                exists = conn.execute(text(
                    "SELECT 1 FROM pg_constraint WHERE conname = :name"
                ), {"name": name}).first()
                if not exists:
                    conn.execute(text(f'ALTER TABLE public."{table}" ADD CONSTRAINT {name} CHECK ({expr})'))
        except Exception as e:
            print(f"WARNING: could not add constraint {name} on {table} (existing rows may violate it): {e}")
