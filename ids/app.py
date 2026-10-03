import os
from flask import Flask
from flask_cors import CORS
from routes.detect import detect_bp
from routes.blacklist import blacklist_bp

app = Flask(__name__)

CORS(app, resources={
    r"/*": {
        "origins": "*"
    }
})

app.register_blueprint(detect_bp)
app.register_blueprint(blacklist_bp)


@app.route("/health")
def health():
    return {"status": "ok"}


@app.route("/health/supabase")
def health_supabase():
    # Shows whether SUPABASE_URL / SUPABASE_KEY on Render work. Never shows the key.
    from ids.settings import diagnose
    return diagnose()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5001)))