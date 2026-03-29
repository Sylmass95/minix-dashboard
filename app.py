import os
import time
import secrets
import requests as http_requests
from flask import Flask, jsonify, render_template, request, session, redirect, url_for
from functools import wraps
import docker

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
client = docker.from_env()

DASHBOARD_PIN = os.environ.get("DASHBOARD_PIN", "1234")
from datetime import timedelta
app.permanent_session_lifetime = timedelta(days=30)

_cache = {}
CACHE_TTL = 30
VERSION_TTL = 120

HIDDEN = {"minix-dashboard"}

SERVICE_URLS = {
    "homeassistant": "http://192.168.0.30:8123",
    "npm-portduckdns-npm-1": "http://192.168.0.30:81",
    "voicebox": "http://192.168.0.30:17493",
    "web-video-downloader-1": "http://192.168.0.30:8742",
    "frigate": "http://192.168.0.30:5000",
    "portainer": "https://192.168.0.30:9443",
}

FRIENDLY_NAMES = {
    "homeassistant": "Home Assistant",
    "npm-portduckdns-npm-1": "Nginx Proxy Manager",
    "voicebox": "Voicebox",
    "web-video-downloader-1": "Video Downloader",
    "frigate": "Frigate",
    "mqtt": "MQTT (Mosquitto)",
    "portainer": "Portainer",
    "docker-updater": "Docker Updater",
}

UPDATABLE = {"voicebox", "web-video-downloader-1"}
SHOW_LOGS = {"voicebox", "web-video-downloader-1"}

GIT_REPOS = {
    "voicebox": "/home/sylvain/Téléchargements/SOFT/Voicebox-fork",
    "web-video-downloader-1": "/home/sylvain/Téléchargements/SOFT/VideoDL/web",
}

CATEGORIES = {
    "homeassistant": "home",
    "frigate": "home",
    "voicebox": "services",
    "web-video-downloader-1": "services",
    "mqtt": "system",
    "npm-portduckdns-npm-1": "system",
    "portainer": "system",
    "docker-updater": "system",
}

ENV_FILES = {
    "voicebox": [
        "/home/sylvain/Téléchargements/SOFT/Voicebox-fork/.env",
        "/home/sylvain/Téléchargements/SOFT/Voicebox-fork/voicebox.env",
    ],
    "web-video-downloader-1": [
        "/home/sylvain/Téléchargements/SOFT/VideoDL/web/.env",
    ],
}

RENDER_URL = "http://192.168.0.82:17494"
VIDEODL_URL = "http://192.168.0.30:8742"
VIDEODL_ADMIN_PWD = "666"
VOICEBOX_URL = "http://192.168.0.30:17493"
DOWNLOADS_PATH = "/home/sylvain/Téléchargements/SOFT/VideoDL/web/downloads"


def cached(key, ttl=CACHE_TTL):
    entry = _cache.get(key)
    if entry and time.time() - entry["ts"] < ttl:
        return entry["data"]
    return None


def set_cache(key, data):
    _cache[key] = {"ts": time.time(), "data": data}


def get_git_version(name):
    repo = GIT_REPOS.get(name)
    if not repo:
        return None
    c = cached(f"git_{name}", VERSION_TTL)
    if c:
        return c
    try:
        updater = client.containers.get("docker-updater")
        cmd = f"git -C '{repo}' log -1 --format='%h|%ai|%s'"
        result = updater.exec_run(["sh", "-c", cmd])
        line = result.output.decode("utf-8", errors="replace").strip()
        if "|" in line:
            sha, date, msg = line.split("|", 2)
            data = {"sha": sha, "date": date[:16], "message": msg[:60]}
            set_cache(f"git_{name}", data)
            return data
    except Exception:
        pass
    return None


def container_info(c):
    name = c.name
    category = CATEGORIES.get(name, "system")
    logs = ""
    if name in SHOW_LOGS:
        try:
            logs = c.logs(tail=20, timestamps=False).decode("utf-8", errors="replace")
        except Exception:
            pass
    return {
        "id": c.short_id,
        "name": name,
        "display_name": FRIENDLY_NAMES.get(name, name),
        "status": c.status,
        "health": c.attrs.get("State", {}).get("Health", {}).get("Status", ""),
        "started_at": c.attrs.get("State", {}).get("StartedAt", ""),
        "url": SERVICE_URLS.get(name),
        "updatable": name in UPDATABLE,
        "category": category,
        "version": get_git_version(name),
        "mini_logs": logs,
        "has_env": name in ENV_FILES,
    }


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("authenticated"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        if request.form.get("pin") == DASHBOARD_PIN:
            session["authenticated"] = True
            session.permanent = True
            return redirect(url_for("index"))
        error = "Code incorrect"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/api/containers")
@login_required
def api_containers():
    containers = client.containers.list(all=True)
    result = [container_info(c) for c in containers if c.name not in HIDDEN]
    order = {"services": 0, "home": 1, "system": 2}
    result.sort(key=lambda x: (order.get(x["category"], 9), x["display_name"].lower()))
    return jsonify(result)


@app.route("/api/containers/<name>/restart", methods=["POST"])
@login_required
def api_restart(name):
    try:
        c = client.containers.get(name)
        c.restart(timeout=10)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/containers/<name>/stop", methods=["POST"])
@login_required
def api_stop(name):
    try:
        c = client.containers.get(name)
        c.stop(timeout=10)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/containers/<name>/start", methods=["POST"])
@login_required
def api_start(name):
    try:
        c = client.containers.get(name)
        c.start()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/containers/<name>/logs")
@login_required
def api_logs(name):
    try:
        c = client.containers.get(name)
        tail = int(request.args.get("tail", 150))
        logs = c.logs(tail=tail, timestamps=True).decode("utf-8", errors="replace")
        return jsonify({"ok": True, "logs": logs})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/containers/<name>/env")
@login_required
def api_env_get(name):
    paths = ENV_FILES.get(name)
    if not paths:
        return jsonify({"ok": False, "error": "Pas de .env pour ce service"}), 404
    try:
        updater = client.containers.get("docker-updater")
        files = []
        for p in paths:
            result = updater.exec_run(["sh", "-c", f"cat '{p}'"])
            content = result.output.decode("utf-8", errors="replace")
            filename = p.rsplit("/", 1)[-1]
            files.append({"name": filename, "path": p, "content": content})
        return jsonify({"ok": True, "files": files})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/containers/<name>/env", methods=["POST"])
@login_required
def api_env_save(name):
    paths = ENV_FILES.get(name)
    if not paths:
        return jsonify({"ok": False, "error": "Pas de .env pour ce service"}), 404
    try:
        updater = client.containers.get("docker-updater")
        files = request.json.get("files", [])
        for f in files:
            path = f["path"]
            if path not in paths:
                continue
            content = f["content"]
            escaped = content.replace("'", "'\\''")
            updater.exec_run(["sh", "-c", f"printf '%s' '{escaped}' > '{path}'"])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/update", methods=["POST"])
@login_required
def api_update():
    try:
        updater = client.containers.get("docker-updater")
        exec_result = updater.exec_run(
            ["bash", "/home/sylvain/update-docker-apps.sh"],
            workdir="/home/sylvain",
        )
        output = exec_result.output.decode("utf-8", errors="replace")
        return jsonify({
            "ok": exec_result.exit_code == 0,
            "stdout": output,
            "stderr": "",
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# --- Stats routes ---

@app.route("/api/stats/videodl")
@login_required
def api_stats_videodl():
    c = cached("stats_videodl")
    if c:
        return jsonify(c)
    data = {}
    try:
        session = http_requests.Session()
        session.post(
            f"{VIDEODL_URL}/api/site-auth",
            json={"password": VIDEODL_ADMIN_PWD}, timeout=3
        )

        ver = session.get(f"{VIDEODL_URL}/api/version", timeout=3).json()
        data["ytdlp_version"] = ver.get("version", "?")

        debug = session.get(
            f"{VIDEODL_URL}/api/debug",
            headers={"Authorization": f"Bearer {VIDEODL_ADMIN_PWD}"},
            timeout=3
        ).json()
        ffmpeg_raw = debug.get("ffmpeg", "?")
        data["ffmpeg"] = ffmpeg_raw.split(" Copyright")[0] if " Copyright" in ffmpeg_raw else ffmpeg_raw
        data["node"] = debug.get("node", "?")

        progress = session.get(f"{VIDEODL_URL}/api/progress?since=0", timeout=3).json()
        messages = progress.get("messages", [])
        active = [m for m in messages if isinstance(m, dict) and m.get("type") == "progress"]
        done = [m for m in messages if isinstance(m, dict) and m.get("type") == "complete"]
        data["downloads_active"] = len(active)
        data["downloads_done"] = len(done)

        stats = session.get(f"{VIDEODL_URL}/api/stats", timeout=3).json()
        data["total_downloads"] = stats.get("total", 0)
        data["last_24h"] = stats.get("last_24h", 0)
        data["last_7d"] = stats.get("last_7d", 0)
        data["by_site"] = stats.get("by_site", [])
        data["active_users"] = stats.get("active_users", 0)
    except Exception:
        pass

    try:
        updater = client.containers.get("docker-updater")
        r = updater.exec_run(["sh", "-c", f"du -sh '{DOWNLOADS_PATH}' 2>/dev/null"])
        data["downloads_size"] = r.output.decode().split("\t")[0].strip()
    except Exception:
        data["downloads_size"] = "?"

    set_cache("stats_videodl", data)
    return jsonify(data)


@app.route("/api/stats/voicebox")
@login_required
def api_stats_voicebox():
    c = cached("stats_voicebox")
    if c:
        return jsonify(c)
    data = {}
    try:
        health = http_requests.get(f"{VOICEBOX_URL}/health", timeout=3).json()
        data["model_loaded"] = health.get("model_loaded", False)
        data["gpu_available"] = health.get("gpu_available", False)
        data["backend_variant"] = health.get("backend_variant", "?")
        data["model_size"] = health.get("model_size", "?")
    except Exception:
        pass

    try:
        tasks = http_requests.get(f"{VOICEBOX_URL}/tasks/active", timeout=3).json()
        data["active_generations"] = len(tasks.get("generations", []))
        data["active_downloads"] = len(tasks.get("downloads", []))
    except Exception:
        pass

    try:
        vb = client.containers.get("voicebox")
        script = (
            "import sqlite3\n"
            "c=sqlite3.connect('/app/data/voicebox.db')\n"
            "print(c.execute('SELECT COUNT(*) FROM users').fetchone()[0])\n"
            "print(c.execute('SELECT COUNT(*) FROM generations').fetchone()[0])\n"
            "print(c.execute(\"SELECT COUNT(DISTINCT user_id) FROM generations WHERE created_at > datetime('now','-1 day')\").fetchone()[0])\n"
        )
        r = vb.exec_run(["python3", "-c", script])
        lines = r.output.decode().strip().split("\n")
        if len(lines) >= 2:
            data["users"] = int(lines[0])
            data["generations"] = int(lines[1])
        if len(lines) >= 3:
            data["active_users"] = int(lines[2])
    except Exception:
        pass

    set_cache("stats_voicebox", data)
    return jsonify(data)


@app.route("/api/stats/render")
@login_required
def api_stats_render():
    c = cached("stats_render", 15)
    if c:
        return jsonify(c)
    data = {"online": False}
    try:
        health = http_requests.get(f"{RENDER_URL}/health", timeout=3).json()
        data["online"] = True
        data["status"] = health.get("status", "?")
        data["hostname"] = health.get("hostname", "?")
        gpu = health.get("gpu", {})
        data["gpu_name"] = gpu.get("name", "?")
        data["vram_total"] = gpu.get("vram_total_gb", 0)
        data["vram_used"] = gpu.get("vram_used_gb", 0)
        data["engines_loaded"] = health.get("engines", {})
        data["supported_engines"] = health.get("supported_engines", [])
        data["last_seen"] = time.strftime("%d/%m/%Y %H:%M:%S")
    except Exception:
        last = _cache.get("render_last_seen")
        data["last_seen"] = last if last else "jamais"

    if data["online"]:
        _cache["render_last_seen"] = data["last_seen"]

    try:
        engines = http_requests.get(f"{RENDER_URL}/engines", timeout=3).json()
        data["engines_detail"] = engines
    except Exception:
        pass

    set_cache("stats_render", data)
    return jsonify(data)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5555, threaded=True)
