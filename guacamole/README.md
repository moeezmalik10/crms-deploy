# R4 Guacamole gateway - local prototype

**Not run or tested here.** This dev machine has no Docker installed, so none of this has
actually been started, and nothing below has been confirmed to work end to end. It was written
against Apache Guacamole's own documentation (linked in `docker-compose.yml` and
`backend/app/guacamole_gateway.py`'s comments) and `backend/app/guacamole_gateway.py`'s token
format was cross-verified against the real `openssl` reference commands - see
`backend/tests/test_r4_guacamole_gateway.py`. Everything else here needs someone with Docker to
actually run it before it's trusted.

## What this is, and isn't

A **local, single-machine prototype** of Component 11's remote-access gateway (see
`docs/handoff/R4_sessions_gateway_plan.md` items 2f and Part 3) - standing up Guacamole in
Docker and getting one real SSH connection working through it, gated by a token the backend
issues, before touching production routing. It is deliberately **not**:

- A production deployment. It doesn't run on Render/Vercel/Supabase or any campus server, and
  shouldn't until the infrastructure question in the plan doc is actually decided.
- A replacement for the existing Cloudflare Quick Tunnel path. That's still how students reach
  their code-server workspace today (`node-agent/pool_runtime.py::start_workspace`) - this
  stack is a second, SSH-only path running alongside it, not instead of it.
- A solution to reaching a lab PC with no inbound ports open. `guacd` here reaches the
  workspace container directly over this compose file's own Docker network, which only works
  because both are assumed to be on the same machine. A lab PC on a different machine, with no
  inbound ports, would need an outbound tunnel to wherever this stack runs - not built.

## One-time setup

1. **Generate a secret key** and put it in two places (they must match exactly):
   ```
   openssl rand -hex 16
   ```
   - Copy `guacamole-home/guacamole.properties.example` to `guacamole-home/guacamole.properties`
     (gitignored) and set `json-secret-key` to the generated value.
   - Set the same value as `GUACAMOLE_JSON_SECRET_KEY` wherever the backend runs (an environment
     variable, not a file - see `backend/app/guacamole_gateway.py`).

2. **Get the `guacamole-auth-json` extension jar.** It is not bundled in the official
   `guacamole/guacamole` Docker image (unlike JDBC/LDAP, which that image can enable just from
   environment variables) - download the matching version from
   https://guacamole.apache.org/releases/ (binary release, `guacamole-auth-json-1.5.5.jar`) and
   place it at `guacamole-home/extensions/guacamole-auth-json-1.5.5.jar` (gitignored - this is a
   binary you fetch yourself, not something to vendor into the repo).

3. **Bring the stack up and initialize the Postgres schema** (first run only):
   ```
   docker compose up -d
   ```
   The schema files are copied out of the `guacamole` image into the shared `guac-jdbc-init`
   volume on its first start, and Postgres runs them from `/docker-entrypoint-initdb.d` on its
   own first start - both should happen automatically on the first `up`. If the Guacamole web
   UI can't log in to the database afterward, it's worth checking `docker compose logs postgres`
   for whether the schema actually loaded; if not, try `docker compose down -v && docker compose
   up -d` once more (removes the data volume, be sure that's fine before running it).

4. **Point guacd at a real workspace container's SSH port.** The prototype config above assumes
   `127.0.0.1` is reachable from `guacd` (set via `GUACAMOLE_PROTOTYPE_TARGET_HOST` on the
   backend, see `guacamole_gateway.py`) - true only if this compose stack and the node-agent
   running the workspace container are on the same machine. Start a real `remote`-mode session
   through the app first, so a workspace container with sshd actually exists to connect to (see
   `node-agent/pool_runtime.py::WORKSPACE_DOCKERFILE` - this also hasn't been built/tested here).

## Using a token

The backend's `GET /tasks/<task_id>/guacamole-token` (added in `backend/app/student_routes.py`)
returns a token for the requesting student's own running session. Guacamole's own client reads
it as a `data` query parameter, URL-encoded:

```
http://localhost:8080/guacamole/#/?data=<url-encoded token>
```

or POST it to `/api/tokens` directly (see `json-auth.html`'s own curl example) to get a session
back from Guacamole's REST API instead of using the browser UI.

## What's still open after this prototype works

- The outbound-tunnel problem for a real, physically separate lab PC (see "What this is, and
  isn't" above) - the actual production blocker.
- Idle detection and connect/disconnect logging - Guacamole's own connection history (under the
  JDBC auth module, once a real user database exists here, not just JSON auth) may cover some of
  this; not yet checked against this specific setup.
- Whatever UI change is needed so students actually see a "connect via SSH" option instead of
  (or alongside) their existing workspace link.
