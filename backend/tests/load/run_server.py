"""Runs the real Flask app against the seeded throwaway db, with the scheduler's background
jobs (process_queue, cleanup_system) actually running - unlike the pytest harness, which sets
DISABLE_SCHEDULER=true, this run needs the queue to actually drain over time as fake agents
free up capacity. threaded=True so Locust's concurrent virtual users are handled as concurrent
requests, not serialized one at a time.
"""
import common
common.configure_env()

from app import create_app

app = create_app()

if __name__ == "__main__":
    print(f"Serving on {common.BASE_URL} (db: {common.DB_PATH})")
    app.run(host=common.SERVER_HOST, port=common.SERVER_PORT, threaded=True, use_reloader=False)
