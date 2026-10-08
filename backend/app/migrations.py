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
    ],
}


def run_startup_migrations(db):
    engine = db.engine
    if engine.dialect.name != "postgresql":
        return
    with engine.begin() as conn:
        for table, cols in NEW_COLUMNS.items():
            for name, sqltype in cols:
                conn.execute(text(f'ALTER TABLE public."{table}" ADD COLUMN IF NOT EXISTS {name} {sqltype}'))
        # The new tables hold private data; the browser (anon key) must never read them.
        for table in ("device_enrollment", "pool_blob", "pool_file", "pool_chunk", "pool_replica"):
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
