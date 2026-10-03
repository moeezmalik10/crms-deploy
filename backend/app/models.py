from datetime import datetime, timezone
from app import db
from sqlalchemy import event

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

    metrics = db.relationship("NodeMetrics", backref="node", lazy=True)
    tasks = db.relationship("TaskRequest", backref="assigned_node", lazy=True)
    # Link for easier log auditing
    logs = db.relationship("TaskExecutionLog", backref="node", lazy=True)

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

    status = db.Column(db.String(20), default="pending") 
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
