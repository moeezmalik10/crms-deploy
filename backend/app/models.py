from datetime import datetime, timezone
from app import db
from sqlalchemy import CheckConstraint, event

# =========================
# USERS (Frontend login & history)
# =========================
class User(db.Model):
    __tablename__ = "user"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False) 
    password = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), default="student")
    
    # Using timezone=True to ensure database compatibility with UTC logic
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    tasks = db.relationship("TaskRequest", backref="user", lazy=True)

@event.listens_for(User.email, 'set')
def update_username(target, value, oldvalue, initiator):
    if value and "@uog.edu.pk" in value:
        target.username = value.split('@')[0]

# =========================
# NODES (Agent machines)
# =========================
class Node(db.Model):
    __tablename__ = "node"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False) 
    ip_address = db.Column(db.String(50))
    status = db.Column(db.String(20), default="offline")
    
    total_cores = db.Column(db.Integer, default=0)
    total_ram_mb = db.Column(db.Float, default=0.0)
    
    # Timezone aware for accurate 'Ghost Node' detection
    last_heartbeat = db.Column(db.DateTime(timezone=True))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    node_type = db.Column(db.String(20), default="remote")
    is_busy = db.Column(db.Boolean, default=False) 

    # ---- Resource pool (contributed devices) ----
    owner_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)  # student who contributed it
    device_key_hash = db.Column(db.String(64))      # sha256 of the device key; agents must present the key
    lan_ip = db.Column(db.String(64))
    public_ip = db.Column(db.String(64))
    os_name = db.Column(db.String(120))
    cpu_model = db.Column(db.String(160))
    cpu_ghz = db.Column(db.Float)
    total_storage_gb = db.Column(db.Float)
    free_storage_gb = db.Column(db.Float)
    share_cores = db.Column(db.Float)               # how much the owner lends (None = everything free)
    share_ram_mb = db.Column(db.Float)
    share_storage_gb = db.Column(db.Float)
    paused = db.Column(db.Boolean, default=False)
    allow_light_sandbox = db.Column(db.Boolean, default=False)  # run jobs without Docker (less isolation)
    sandbox_mode = db.Column(db.String(20))         # docker / light / none, reported by the agent
    agent_version = db.Column(db.String(20))

    # ---- Unified Quota Ledger (see app.allocation_engine.refresh_ledger) ----
    # "used" = booked by tasks that are actually running; "reserved" = promised to tasks that
    # are allocated/starting but not running yet. Kept as a persisted, DB-constrained mirror of
    # the live computation in compute_node_usage() - never written anywhere else, so the two
    # can't drift apart. A physical session books the node's FULL total here (Rule A gives it
    # the whole device), not just its own modest profile.
    used_cores = db.Column(db.Float, default=0.0, nullable=False)
    used_ram_mb = db.Column(db.Float, default=0.0, nullable=False)
    reserved_cores = db.Column(db.Float, default=0.0, nullable=False)
    reserved_ram_mb = db.Column(db.Float, default=0.0, nullable=False)

    # ---- Quarantine (PCs that fail repeatedly are temporarily taken out of the pool) ----
    # consecutive_failures counts heartbeat-timeout failures not yet "forgiven" by a stability
    # window of healthy heartbeats (app.allocation_engine.register_node_failure/
    # clear_node_failure); once it crosses the threshold, quarantined_until holds a cooldown
    # timestamp that lock_online_nodes()/fitting_device_count() exclude the node until, even if
    # it starts heartbeating again in the meantime - a flapping PC shouldn't re-enter scheduling
    # the instant one heartbeat lands. Null quarantined_until means "not quarantined."
    # last_failure_at anchors the stability window: see clear_node_failure for why resetting the
    # counter on every heartbeat (instead of after sustained health) would make the threshold
    # unreachable.
    consecutive_failures = db.Column(db.Integer, default=0, nullable=False)
    last_failure_at = db.Column(db.DateTime(timezone=True), nullable=True)
    quarantined_until = db.Column(db.DateTime(timezone=True), nullable=True)

    metrics = db.relationship("NodeMetrics", backref="node", lazy=True)
    tasks = db.relationship("TaskRequest", backref="assigned_node", lazy=True)
    # Link for easier log auditing
    logs = db.relationship("TaskExecutionLog", backref="node", lazy=True)

    __table_args__ = (
        # A small epsilon absorbs float rounding, not real over-booking. Defined here (not only
        # in the Postgres migration) so SQLite test databases enforce it too - a constraint only
        # a live migration adds is a constraint the test suite can never actually exercise.
        CheckConstraint("used_cores + reserved_cores <= total_cores + 0.01", name="ck_node_cores_ledger"),
        CheckConstraint("used_ram_mb + reserved_ram_mb <= total_ram_mb + 0.5", name="ck_node_ram_ledger"),
    )

# =========================
# NODE METRICS (Heartbeat history for Agent)
# =========================
class NodeMetrics(db.Model):
    __tablename__ = "node_metrics"

    id = db.Column(db.Integer, primary_key=True)
    node_id = db.Column(db.Integer, db.ForeignKey("node.id"), nullable=False)

    cpu_used = db.Column(db.Float)
    cpu_free = db.Column(db.Float)
    memory_total_mb = db.Column(db.Float)
    memory_used_mb = db.Column(db.Float)
    memory_free_mb = db.Column(db.Float)
    storage_total_gb = db.Column(db.Float)
    storage_used_gb = db.Column(db.Float)
    storage_free_gb = db.Column(db.Float)

    raw_payload = db.Column(db.JSON)
    timestamp = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class NodeMetricsHistory(db.Model):
    """A sampled trail of NodeMetrics over time - NodeMetrics itself holds exactly one row per
    node (each heartbeat updates it in place), so it has no history to show on a usage chart.
    Appended to at most every few minutes per node (see agent_heartbeat) and trimmed after 7
    days (see app.tasks.trim_metrics_history)."""
    __tablename__ = "node_metrics_history"

    id = db.Column(db.Integer, primary_key=True)
    node_id = db.Column(db.Integer, db.ForeignKey("node.id"), nullable=False)

    cpu_used = db.Column(db.Float)
    memory_total_mb = db.Column(db.Float)
    memory_free_mb = db.Column(db.Float)
    storage_total_gb = db.Column(db.Float)
    storage_free_gb = db.Column(db.Float)

    timestamp = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)

# =========================
# TASK REQUESTS (CORE TABLE)
# =========================
class TaskRequest(db.Model):
    __tablename__ = "task_request"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    assigned_node_id = db.Column(db.Integer, db.ForeignKey("node.id"), nullable=True)

    task_type = db.Column(db.String(100)) 
    
    # Allow a parent task to easily find all its children
    parent_task_id = db.Column(db.Integer, db.ForeignKey('task_request.id'), nullable=True)
    # Update the relationship to explicitly tell SQLAlchemy how to join
    sub_tasks = db.relationship(
        "TaskRequest", 
        backref=db.backref('parent', remote_side=[id]), 
        lazy=True,
        primaryjoin="TaskRequest.id == TaskRequest.parent_task_id",
        cascade="all, delete-orphan"
    )
    dataset_url = db.Column(db.String(500))        # Shared link for all agents
    model_type = db.Column(db.String(50))          # "random_forest", "decision_tree", etc.
    validation_type = db.Column(db.String(50))     # "kfold", "none"

    # Specific instructions for this node
    chunk_id = db.Column(db.Integer)
    start_row = db.Column(db.Integer)
    end_row = db.Column(db.Integer)

    required_cpu = db.Column(db.Integer)
    required_ram_mb = db.Column(db.Float)

    mode = db.Column(db.String(20))          # physical / remote
    duration_minutes = db.Column(db.Integer)

    # Lower runs first when the pool is contended: interactive sessions (10) ahead of
    # batch jobs (50). See app.allocation_engine.default_priority().
    priority = db.Column(db.Integer, default=50)

    status = db.Column(db.String(20), default="pending")
    # Set each time this task (re-)enters "allocated"; cleared on requeue. Unlike created_at
    # (fixed at submission), this tracks the current attempt, so the stale-"starting"/
    # stale-"allocated" sweeps in app.tasks don't mistake "submitted a while ago" for
    # "stuck on this attempt" and bounce a freshly-requeued task straight back to pending.
    allocated_at = db.Column(db.DateTime(timezone=True))
    assigned_pc = db.Column(db.String(100))
    link = db.Column(db.String(255))    
    vm_username = db.Column(db.String(100)) 
    vm_password = db.Column(db.String(100)) 
    message = db.Column(db.Text) # To store the ML JSON metrics/logs
    
    # Explicit timezone support for expiry calculations
    start_time = db.Column(db.DateTime(timezone=True))
    expiry_time = db.Column(db.DateTime(timezone=True))    
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    completed_at = db.Column(db.DateTime(timezone=True))

    execution_logs = db.relationship("TaskExecutionLog", backref="task", lazy=True, cascade="all, delete-orphan")

    # ---- Pool compute jobs (mode = "job") ----
    required_disk_mb = db.Column(db.Float)
    job_runtime = db.Column(db.String(20))          # python / cpp
    job_entry = db.Column(db.String(255))           # file to run inside the uploaded zip
    job_args = db.Column(db.String(500))
    input_blob_id = db.Column(db.Integer)
    result_blob_id = db.Column(db.Integer)
    exit_code = db.Column(db.Integer)
    output_tail = db.Column(db.Text)
    # The checkpoint record Component 5 describes: "unit ID, status and a SHA-256 hash of the
    # output" - unit ID and status are this row's own id/status; this is the hash, taken over
    # the result blob's raw bytes the moment a unit finishes (app.pool_agent_routes.job_result),
    # and re-checked whenever that result is served back out (app.pool_routes), so silent
    # corruption between the two is caught rather than handed to the student unnoticed.
    result_sha256 = db.Column(db.String(64))
    # None = run-arbitrary-code job (today's behaviour). "sweep" = parameter sweep: each part
    # gets its own argument set, the group's result is the part with the best score.
    job_kind = db.Column(db.String(20))

# =========================
# Machine Learning Results (For storing trained models and metadata)
# =========================
class MLResult(db.Model):
    #Stores the combined 'Brain' results for the User to view in the UI 
    __tablename__ = 'ml_results'
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey('task_request.id'))
    
    accuracy = db.Column(db.Float)
    precision = db.Column(db.Float)
    recall = db.Column(db.Float)
    f1_score = db.Column(db.Float)
    
    # Store Feature Importance as JSON
    feature_importance = db.Column(db.Text) 
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

# =========================
# TASK EXECUTION LOG (Agent feedback)
# =========================
class TaskExecutionLog(db.Model):
    __tablename__ = "task_execution_log"

    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("task_request.id"),nullable=False)
    node_id = db.Column(db.Integer, db.ForeignKey("node.id"))

    status = db.Column(db.String(20)) 
    message = db.Column(db.Text)
    timestamp = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


# =========================
# RESOURCE POOL
# =========================
class DeviceEnrollment(db.Model):
    """One-time join code a student creates on the website to add a device to the pool."""
    __tablename__ = "device_enrollment"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    code_hash = db.Column(db.String(64), nullable=False, index=True)
    share_cores = db.Column(db.Float)
    share_ram_mb = db.Column(db.Float)
    share_storage_gb = db.Column(db.Float)
    allow_light_sandbox = db.Column(db.Boolean, default=False)
    expires_at = db.Column(db.DateTime(timezone=True))
    used_at = db.Column(db.DateTime(timezone=True))
    node_id = db.Column(db.Integer, db.ForeignKey("node.id"))
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class PoolBlob(db.Model):
    """Small binary objects kept by the server: job inputs/results and chunks in transit."""
    __tablename__ = "pool_blob"
    id = db.Column(db.Integer, primary_key=True)
    owner_user_id = db.Column(db.Integer)
    kind = db.Column(db.String(20))                 # job_input / job_result / chunk
    name = db.Column(db.String(255))
    size = db.Column(db.Integer)
    data = db.Column(db.LargeBinary)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class PoolFile(db.Model):
    """A file in pooled storage. Its encrypted chunks live on contributors' disks."""
    __tablename__ = "pool_file"
    id = db.Column(db.Integer, primary_key=True)
    owner_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    name = db.Column(db.String(255))
    size = db.Column(db.BigInteger)
    sha256 = db.Column(db.String(64))
    wrapped_key = db.Column(db.Text)                # file key, encrypted with the server master key
    chunk_count = db.Column(db.Integer)
    replicas_wanted = db.Column(db.Integer, default=2)
    status = db.Column(db.String(20), default="replicating")   # replicating / stored / deleting
    download_requested_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    chunks = db.relationship("PoolChunk", backref="file", lazy=True, cascade="all, delete-orphan")


class PoolChunk(db.Model):
    __tablename__ = "pool_chunk"
    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.Integer, db.ForeignKey("pool_file.id"), nullable=False)
    idx = db.Column(db.Integer)
    size = db.Column(db.Integer)                    # encrypted size
    sha256 = db.Column(db.String(64))               # of the encrypted bytes
    tmp_blob_id = db.Column(db.Integer)             # server copy while replicating / downloading
    replicas = db.relationship("PoolReplica", backref="chunk", lazy=True, cascade="all, delete-orphan")


class PoolReplica(db.Model):
    __tablename__ = "pool_replica"
    id = db.Column(db.Integer, primary_key=True)
    chunk_id = db.Column(db.Integer, db.ForeignKey("pool_chunk.id"), nullable=False)
    node_id = db.Column(db.Integer, db.ForeignKey("node.id"), nullable=False)
    status = db.Column(db.String(20), default="pending")       # pending / stored / deleting
    updated_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
