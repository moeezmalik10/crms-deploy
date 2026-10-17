import common
common.configure_env()

from app import create_app

app = create_app()

if __name__ == "__main__":
    print(f"Serving on {common.BASE_URL} (db: {common.DB_PATH})")
    app.run(host=common.SERVER_HOST, port=common.SERVER_PORT, threaded=True, use_reloader=False)
