-- =====================================================================
-- CRMS database setup for YOUR OWN Supabase project
-- Recreates the original HRL CRM tables exactly, plus the two IDS tables,
-- the "datasets" storage bucket and the two starter accounts.
-- HOW TO RUN: Supabase Dashboard -> SQL Editor -> New query -> paste -> Run.
-- Safe to run again (it never deletes data; it resets the two starter passwords).
-- =====================================================================

CREATE TABLE IF NOT EXISTS public."user" (
  id serial NOT NULL,
  username character varying(80) NOT NULL,
  email character varying(120) NOT NULL,
  password character varying(200) NOT NULL,
  role character varying(20) NULL,
  created_at timestamp with time zone NULL,
  CONSTRAINT user_pkey PRIMARY KEY (id),
  CONSTRAINT user_email_key UNIQUE (email),
  CONSTRAINT user_username_key UNIQUE (username)
);

CREATE TABLE IF NOT EXISTS public.node (
  id serial NOT NULL,
  name character varying(100) NOT NULL,
  ip_address character varying(50) NULL,
  status character varying(20) NULL,
  total_cores integer NULL,
  total_ram_mb double precision NULL,
  last_heartbeat timestamp with time zone NULL,
  is_active boolean NULL,
  created_at timestamp with time zone NULL,
  node_type character varying(20) NULL,
  is_busy boolean NULL,
  CONSTRAINT node_pkey PRIMARY KEY (id),
  CONSTRAINT node_name_key UNIQUE (name)
);

CREATE TABLE IF NOT EXISTS public.node_metrics (
  id serial NOT NULL,
  node_id integer NOT NULL,
  cpu_used double precision NULL,
  cpu_free double precision NULL,
  memory_total_mb double precision NULL,
  memory_used_mb double precision NULL,
  memory_free_mb double precision NULL,
  storage_total_gb double precision NULL,
  storage_used_gb double precision NULL,
  storage_free_gb double precision NULL,
  raw_payload json NULL,
  "timestamp" timestamp with time zone NULL,
  CONSTRAINT node_metrics_pkey PRIMARY KEY (id),
  CONSTRAINT node_metrics_node_id_fkey FOREIGN KEY (node_id) REFERENCES public.node (id)
);

CREATE TABLE IF NOT EXISTS public.task_request (
  id serial NOT NULL,
  user_id integer NULL,
  assigned_node_id integer NULL,
  task_type character varying(100) NULL,
  required_cpu integer NULL,
  required_ram_mb double precision NULL,
  mode character varying(20) NULL,
  duration_minutes integer NULL,
  status character varying(20) NULL,
  assigned_pc character varying(100) NULL,
  link character varying(255) NULL,
  vm_username character varying(100) NULL,
  vm_password character varying(100) NULL,
  start_time timestamp with time zone NULL,
  expiry_time timestamp with time zone NULL,
  created_at timestamp with time zone NULL,
  completed_at timestamp with time zone NULL,
  parent_task_id integer NULL,
  message text NULL,
  dataset_url character varying(500) NULL,
  model_type character varying(50) NULL,
  validation_type character varying(50) NULL,
  chunk_id integer NULL,
  start_row integer NULL,
  end_row integer NULL,
  CONSTRAINT task_request_pkey PRIMARY KEY (id),
  CONSTRAINT task_request_assigned_node_id_fkey FOREIGN KEY (assigned_node_id) REFERENCES public.node (id),
  CONSTRAINT task_request_parent_task_id_fkey FOREIGN KEY (parent_task_id) REFERENCES public.task_request (id),
  CONSTRAINT task_request_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user" (id)
);

-- task_id must allow NULL (as in the original database), otherwise every ML job fails
CREATE TABLE IF NOT EXISTS public.task_execution_log (
  id serial NOT NULL,
  task_id integer NULL,
  node_id integer NULL,
  status character varying(20) NULL,
  message text NULL,
  "timestamp" timestamp with time zone NULL,
  CONSTRAINT task_execution_log_pkey PRIMARY KEY (id),
  CONSTRAINT task_execution_log_node_id_fkey FOREIGN KEY (node_id) REFERENCES public.node (id),
  CONSTRAINT task_execution_log_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.task_request (id)
);
ALTER TABLE public.task_execution_log ALTER COLUMN task_id DROP NOT NULL;

CREATE TABLE IF NOT EXISTS public.ml_results (
  id serial NOT NULL,
  task_id integer NULL,
  accuracy double precision NULL,
  precision double precision NULL,
  recall double precision NULL,
  f1_score double precision NULL,
  feature_importance text NULL,
  created_at timestamp without time zone NULL,
  CONSTRAINT ml_results_pkey PRIMARY KEY (id),
  CONSTRAINT ml_results_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.task_request (id)
);

-- IDS tables
CREATE TABLE IF NOT EXISTS public.intrusion_logs (
  id serial PRIMARY KEY,
  email text NULL,
  attack_type text NULL,
  ip_address text NULL,
  country text NULL,
  city text NULL,
  created_at timestamp with time zone NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS public.blacklisted_ips (
  id serial PRIMARY KEY,
  ip_address text NOT NULL UNIQUE,
  created_at timestamp with time zone NOT NULL DEFAULT now()
);

-- Access rules: the backend (database login) and the IDS (service_role key) bypass these.
-- The browser (anon key) may only READ intrusion_logs for the Security Dashboard.
ALTER TABLE public."user"             ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.node               ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.node_metrics       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.task_request       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.task_execution_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ml_results         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.intrusion_logs     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.blacklisted_ips    ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Dashboard can read intrusion logs" ON public.intrusion_logs;
CREATE POLICY "Dashboard can read intrusion logs" ON public.intrusion_logs
  FOR SELECT TO anon, authenticated USING (true);

-- Storage bucket for ML datasets (public, as in the original project)
INSERT INTO storage.buckets (id, name, public) VALUES ('datasets', 'datasets', true)
ON CONFLICT (id) DO NOTHING;

-- Starter accounts: admin@uog.edu.pk / Admin@12345   and   student1@uog.edu.pk / Student@12345
UPDATE public."user" SET role = 'admin',
  password = 'scrypt:32768:8:1$lCVisqKDgiL8NE50$ffe0ff301d33a6411b72d655b16e20c599d0f1c8f3d31a3ac99f7d9587014cc4c016f351908709290a6fb5901cd93ed7fc85cbff56b088dbd0698b5786df4e37'
WHERE lower(trim(email)) = 'admin@uog.edu.pk';
INSERT INTO public."user" (username, email, password, role, created_at)
SELECT 'admin', 'admin@uog.edu.pk',
  'scrypt:32768:8:1$lCVisqKDgiL8NE50$ffe0ff301d33a6411b72d655b16e20c599d0f1c8f3d31a3ac99f7d9587014cc4c016f351908709290a6fb5901cd93ed7fc85cbff56b088dbd0698b5786df4e37',
  'admin', now()
WHERE NOT EXISTS (SELECT 1 FROM public."user" WHERE lower(trim(email)) = 'admin@uog.edu.pk');

UPDATE public."user" SET role = 'student',
  password = 'scrypt:32768:8:1$3SBFhxfobKwUncO5$c387f1b33f7f71f32c67f309bd288dd6ad140dedddbf66c842c760bd061fd2c09cff63ffd2674d53703bb3c1eeb521f77f34fcc48168c1ed63cc931ccfba7adc'
WHERE lower(trim(email)) = 'student1@uog.edu.pk';
INSERT INTO public."user" (username, email, password, role, created_at)
SELECT 'student1', 'student1@uog.edu.pk',
  'scrypt:32768:8:1$3SBFhxfobKwUncO5$c387f1b33f7f71f32c67f309bd288dd6ad140dedddbf66c842c760bd061fd2c09cff63ffd2674d53703bb3c1eeb521f77f34fcc48168c1ed63cc931ccfba7adc',
  'student', now()
WHERE NOT EXISTS (SELECT 1 FROM public."user" WHERE lower(trim(email)) = 'student1@uog.edu.pk');

SELECT id, username, email, role FROM public."user" ORDER BY id;

-- ===== Resource pool (added Oct 2026) =====
-- The backend also applies these automatically when it starts; running them here is optional.
ALTER TABLE public."node" ADD COLUMN IF NOT EXISTS owner_user_id INTEGER;
ALTER TABLE public."node" ADD COLUMN IF NOT EXISTS device_key_hash VARCHAR(64);
ALTER TABLE public."node" ADD COLUMN IF NOT EXISTS lan_ip VARCHAR(64);
ALTER TABLE public."node" ADD COLUMN IF NOT EXISTS public_ip VARCHAR(64);
ALTER TABLE public."node" ADD COLUMN IF NOT EXISTS os_name VARCHAR(120);
ALTER TABLE public."node" ADD COLUMN IF NOT EXISTS cpu_model VARCHAR(160);
ALTER TABLE public."node" ADD COLUMN IF NOT EXISTS cpu_ghz DOUBLE PRECISION;
ALTER TABLE public."node" ADD COLUMN IF NOT EXISTS total_storage_gb DOUBLE PRECISION;
ALTER TABLE public."node" ADD COLUMN IF NOT EXISTS free_storage_gb DOUBLE PRECISION;
ALTER TABLE public."node" ADD COLUMN IF NOT EXISTS share_cores DOUBLE PRECISION;
ALTER TABLE public."node" ADD COLUMN IF NOT EXISTS share_ram_mb DOUBLE PRECISION;
ALTER TABLE public."node" ADD COLUMN IF NOT EXISTS share_storage_gb DOUBLE PRECISION;
ALTER TABLE public."node" ADD COLUMN IF NOT EXISTS paused BOOLEAN DEFAULT FALSE;
ALTER TABLE public."node" ADD COLUMN IF NOT EXISTS allow_light_sandbox BOOLEAN DEFAULT FALSE;
ALTER TABLE public."node" ADD COLUMN IF NOT EXISTS sandbox_mode VARCHAR(20);
ALTER TABLE public."node" ADD COLUMN IF NOT EXISTS agent_version VARCHAR(20);
ALTER TABLE public."task_request" ADD COLUMN IF NOT EXISTS required_disk_mb DOUBLE PRECISION;
ALTER TABLE public."task_request" ADD COLUMN IF NOT EXISTS job_runtime VARCHAR(20);
ALTER TABLE public."task_request" ADD COLUMN IF NOT EXISTS job_entry VARCHAR(255);
ALTER TABLE public."task_request" ADD COLUMN IF NOT EXISTS job_args VARCHAR(500);
ALTER TABLE public."task_request" ADD COLUMN IF NOT EXISTS input_blob_id INTEGER;
ALTER TABLE public."task_request" ADD COLUMN IF NOT EXISTS result_blob_id INTEGER;
ALTER TABLE public."task_request" ADD COLUMN IF NOT EXISTS exit_code INTEGER;
ALTER TABLE public."task_request" ADD COLUMN IF NOT EXISTS output_tail TEXT;
-- Tables device_enrollment, pool_blob, pool_file, pool_chunk, pool_replica are created by the backend at start-up.
