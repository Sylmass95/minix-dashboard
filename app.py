import os
import re
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

HIDDEN = {
    "minix-dashboard",
    "storyboardgenerator-nginx-1",
    "annoncesgen-frontend-1",
    "annoncesgen-nginx-1",
    "annoncesgen-db-1",
    "annoncesgen-ollama-1",
}

SERVICE_URLS = {
    "homeassistant": "http://192.168.0.30:8123",
    "npm-portduckdns-npm-1": "http://192.168.0.30:81",
    "voicebox": "http://192.168.0.30:17493",
    "web-video-downloader-1": "http://192.168.0.30:8742",
    "frigate": "http://192.168.0.30:5000",
    "portainer": "https://192.168.0.30:9443",
    "storyboardgenerator-app-1": "http://192.168.0.30:3232",
    "annoncesgen-backend-1": "http://192.168.0.30:3333",
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
    "storyboardgenerator-app-1": "Storyboard Generator",
    "annoncesgen-backend-1": "AnnoncesGen",
}

UPDATABLE = {"voicebox", "web-video-downloader-1", "storyboardgenerator-app-1", "annoncesgen-backend-1"}
SHOW_LOGS = {"voicebox", "web-video-downloader-1", "storyboardgenerator-app-1", "annoncesgen-backend-1"}

GIT_REPOS = {
    "voicebox": "/home/sylvain/Téléchargements/SOFT/Voicebox-fork",
    "web-video-downloader-1": "/home/sylvain/Téléchargements/SOFT/VideoDL/web",
    "storyboardgenerator-app-1": "/home/sylvain/Téléchargements/SOFT/StoryboardGenerator",
    "annoncesgen-backend-1": "/home/sylvain/Téléchargements/SOFT/AnnoncesGen",
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
    "storyboardgenerator-app-1": "services",
    "annoncesgen-backend-1": "services",
}

ENV_FILES = {
    "voicebox": [
        "/home/sylvain/Téléchargements/SOFT/Voicebox-fork/.env",
        "/home/sylvain/Téléchargements/SOFT/Voicebox-fork/voicebox.env",
    ],
    "web-video-downloader-1": [
        "/home/sylvain/Téléchargements/SOFT/VideoDL/web/.env",
    ],
    "storyboardgenerator-app-1": [
        "/home/sylvain/Téléchargements/SOFT/StoryboardGenerator/.env",
    ],
    "annoncesgen-backend-1": [
        "/home/sylvain/Téléchargements/SOFT/AnnoncesGen/.env",
    ],
}

RENDER_URL = "http://192.168.0.82:17494"
RENDER_TOKEN = os.environ.get("GPU_WORKER_TOKEN", "")
VIDEODL_URL = "http://192.168.0.30:8742"
VIDEODL_ENV = "/home/sylvain/Téléchargements/SOFT/VideoDL/web/.env"
STORYBOARD_URL = "http://192.168.0.30:3232"
STORYBOARD_ENV = "/home/sylvain/Téléchargements/SOFT/StoryboardGenerator/.env"
VOICEBOX_URL = "http://192.168.0.30:17493"
VOICEBOX_ENV = "/home/sylvain/Téléchargements/SOFT/Voicebox-fork/.env"
ANNONCESGEN_URL = "http://192.168.0.30:3333"
ANNONCESGEN_ENV = "/home/sylvain/Téléchargements/SOFT/AnnoncesGen/.env"
DOWNLOADS_PATH = "/home/sylvain/Téléchargements/SOFT/VideoDL/web/downloads"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")


def read_env_var(env_path, var_name):
    try:
        updater = client.containers.get("docker-updater")
        r = updater.exec_run(["sh", "-c", f"cat '{env_path}'"])
        for line in r.output.decode().splitlines():
            line = line.strip()
            if line.startswith(f"{var_name}="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return None


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


_repo_url_cache = {}

def get_github_repo_url(name):
    """Extract 'owner/repo' from git remote origin, cached indefinitely."""
    if name in _repo_url_cache:
        return _repo_url_cache[name]
    repo = GIT_REPOS.get(name)
    if not repo:
        return None
    try:
        updater = client.containers.get("docker-updater")
        result = updater.exec_run(["sh", "-c", f"git -C '{repo}' remote get-url origin"])
        url = result.output.decode("utf-8", errors="replace").strip()
        # SSH: git@github.com:owner/repo.git  |  HTTPS: https://github.com/owner/repo.git
        m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
        if m:
            _repo_url_cache[name] = m.group(1)
            return _repo_url_cache[name]
    except Exception:
        pass
    return None


def get_remote_version(name):
    if not GITHUB_TOKEN:
        return None
    repo_slug = get_github_repo_url(name)
    if not repo_slug:
        return None
    c = cached(f"remote_{name}", VERSION_TTL)
    if c:
        return c
    try:
        resp = http_requests.get(
            f"https://api.github.com/repos/{repo_slug}/commits/main",
            headers={
                "Accept": "application/vnd.github.v3+json",
                "Authorization": f"Bearer {GITHUB_TOKEN}",
            },
            timeout=5,
        )
        if resp.status_code != 200:
            return None
        gh = resp.json()
        from datetime import datetime, timezone
        utc_date = datetime.strptime(
            gh["commit"]["committer"]["date"], "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
        local_date = utc_date.astimezone()
        data = {
            "sha": gh["sha"][:7],
            "date": local_date.strftime("%Y-%m-%d %H:%M"),
        }
        set_cache(f"remote_{name}", data)
        return data
    except Exception:
        return None


def get_docker_image_size(container_name):
    try:
        c = client.containers.get(container_name)
        size_bytes = c.image.attrs.get("Size", 0)
        if size_bytes >= 1_073_741_824:
            return f"{size_bytes / 1_073_741_824:.1f} Go"
        return f"{size_bytes / 1_048_576:.0f} Mo"
    except Exception:
        return "?"


def count_docker_errors(container_name, tail=500):
    try:
        c = client.containers.get(container_name)
        logs = c.logs(tail=tail, timestamps=False).decode("utf-8", errors="replace")
        keywords = ["error", "exception", "traceback", "fatal"]
        return sum(1 for line in logs.splitlines()
                   if any(kw in line.lower() for kw in keywords))
    except Exception:
        return 0


def check_gemini_quota(env_path):
    try:
        api_key = read_env_var(env_path, "GEMINI_API_KEY")
        if not api_key:
            return {"status": "no_key"}
        r = http_requests.get(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash?key={api_key}",
            timeout=5
        )
        if r.status_code == 429:
            return {"status": "exhausted", "key": True}
        if r.status_code != 200:
            return {"status": "error", "key": True}
        remaining = r.headers.get("x-ratelimit-remaining-requests")
        limit = r.headers.get("x-ratelimit-limit-requests")
        return {"status": "ok", "key": True, "remaining": remaining, "limit": limit}
    except Exception:
        return {"status": "error"}


def container_info(c):
    name = c.name
    category = CATEGORIES.get(name, "system")
    logs = ""
    if name in SHOW_LOGS:
        try:
            logs = c.logs(tail=20, timestamps=False).decode("utf-8", errors="replace")
        except Exception:
            pass
    local_ver = get_git_version(name)
    remote_ver = get_remote_version(name) if name in UPDATABLE else None
    update_available = False
    if local_ver and remote_ver and local_ver.get("sha") and remote_ver.get("sha"):
        update_available = local_ver["sha"] != remote_ver["sha"]
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
        "version": local_ver,
        "remote_version": remote_ver,
        "update_available": update_available,
        "mini_logs": logs,
        "has_env": name in ENV_FILES,
    }


_login_attempts = {}
LOGIN_MAX_ATTEMPTS = 5
LOGIN_BLOCK_SECONDS = 300


def _get_client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()


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
    ip = _get_client_ip()
    entry = _login_attempts.get(ip, {"count": 0, "blocked_until": 0})
    if request.method == "POST":
        if time.time() < entry.get("blocked_until", 0):
            remaining = int(entry["blocked_until"] - time.time())
            error = f"Trop de tentatives. Réessayez dans {remaining}s"
        elif request.form.get("pin") == DASHBOARD_PIN:
            _login_attempts.pop(ip, None)
            session["authenticated"] = True
            session.permanent = True
            return redirect(url_for("index"))
        else:
            entry["count"] = entry.get("count", 0) + 1
            if entry["count"] >= LOGIN_MAX_ATTEMPTS:
                entry["blocked_until"] = time.time() + LOGIN_BLOCK_SECONDS
                entry["count"] = 0
                error = f"Trop de tentatives. Bloqué {LOGIN_BLOCK_SECONDS // 60} min"
            else:
                error = f"Code incorrect ({LOGIN_MAX_ATTEMPTS - entry['count']} essais restants)"
            _login_attempts[ip] = entry
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/api/dashboard-version")
@login_required
def api_dashboard_version():
    c = cached("dashboard_version", VERSION_TTL)
    if c:
        return jsonify(c)
    try:
        headers = {"Accept": "application/vnd.github.v3+json"}
        if GITHUB_TOKEN:
            headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
        gh = http_requests.get(
            "https://api.github.com/repos/Sylmass95/minix-dashboard/commits/main",
            headers=headers,
            timeout=5
        ).json()
        from datetime import datetime, timezone
        utc_date = datetime.strptime(gh["commit"]["committer"]["date"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        local_date = utc_date.astimezone()
        data = {
            "sha": gh["sha"][:7],
            "date": local_date.strftime("%Y-%m-%d %H:%M")
        }
        set_cache("dashboard_version", data)
        return jsonify(data)
    except Exception:
        return jsonify({"sha": None, "date": None})


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
        for name in UPDATABLE:
            _cache.pop(f"git_{name}", None)
            _cache.pop(f"remote_{name}", None)
        _cache.pop("dashboard_version", None)
        return jsonify({
            "ok": exec_result.exit_code == 0,
            "stdout": output,
            "stderr": "",
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/containers/web-video-downloader-1/toggle-auth", methods=["POST"])
@login_required
def api_toggle_auth():
    try:
        updater = client.containers.get("docker-updater")
        env_path = ENV_FILES["web-video-downloader-1"][0]
        r = updater.exec_run(["sh", "-c", f"cat '{env_path}'"])
        content = r.output.decode("utf-8", errors="replace")
        lines = content.splitlines()
        new_mode = "password"
        new_lines = []
        for line in lines:
            if line.startswith("AUTH_MODE="):
                old_mode = line.split("=", 1)[1].strip()
                new_mode = "password" if old_mode == "accounts" else "accounts"
                new_lines.append(f"AUTH_MODE={new_mode}")
            else:
                new_lines.append(line)
        new_content = "\n".join(new_lines) + "\n"
        import base64 as b64mod
        encoded = b64mod.b64encode(new_content.encode()).decode()
        updater.exec_run(["sh", "-c", f"echo '{encoded}' | base64 -d > '{env_path}'"])
        container = client.containers.get("web-video-downloader-1")
        container.restart(timeout=10)
        _cache.pop("stats_videodl", None)
        return jsonify({"ok": True, "auth_mode": new_mode})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/voicebox-admin-token")
@login_required
def api_voicebox_admin_token():
    try:
        admin_user = read_env_var(VOICEBOX_ENV, "VOICEBOX_ADMIN_USER") or "admin"
        admin_pwd = read_env_var(VOICEBOX_ENV, "VOICEBOX_ADMIN_PASSWORD")
        if not admin_pwd:
            return jsonify({"ok": False, "error": "VOICEBOX_ADMIN_PASSWORD non trouvé dans .env"}), 500
        r = http_requests.post(
            f"{VOICEBOX_URL}/auth/login",
            json={"username": admin_user, "password": admin_pwd},
            timeout=5
        )
        if r.status_code != 200:
            return jsonify({"ok": False, "error": f"Login échoué ({r.status_code})"}), 500
        data = r.json()
        return jsonify({"ok": True, "token": data.get("token"), "user": data.get("user")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/videodl-admin-token")
@login_required
def api_videodl_admin_token():
    try:
        admin_pwd = read_env_var(VIDEODL_ENV, "ADMIN_PASSWORD")
        if not admin_pwd:
            return jsonify({"ok": False, "error": "ADMIN_PASSWORD non trouvé dans .env"}), 500
        r = http_requests.post(
            f"{VIDEODL_URL}/api/auth/login",
            json={"username": "admin", "password": admin_pwd},
            timeout=5
        )
        if r.status_code != 200:
            return jsonify({"ok": False, "error": f"Login échoué ({r.status_code})"}), 500
        data = r.json()
        return jsonify({"ok": True, "token": data.get("token"), "user": data.get("user")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/storyboard-admin-token")
@login_required
def api_storyboard_admin_token():
    try:
        admin_user = read_env_var(STORYBOARD_ENV, "ADMIN_USER") or "admin"
        admin_pwd = read_env_var(STORYBOARD_ENV, "ADMIN_PASSWORD")
        if not admin_pwd:
            return jsonify({"ok": False, "error": "ADMIN_PASSWORD non trouvé dans .env"}), 500
        r = http_requests.post(
            f"{STORYBOARD_URL}/api/auth/login",
            json={"username": admin_user, "password": admin_pwd},
            timeout=5
        )
        if r.status_code == 401:
            http_requests.post(
                f"{STORYBOARD_URL}/api/auth/register",
                json={"username": admin_user, "password": admin_pwd},
                timeout=5
            )
            r = http_requests.post(
                f"{STORYBOARD_URL}/api/auth/login",
                json={"username": admin_user, "password": admin_pwd},
                timeout=5
            )
        data = r.json()
        return jsonify({"ok": True, "token": data.get("token")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/annoncesgen-admin-token")
@login_required
def api_annoncesgen_admin_token():
    try:
        admin_email = read_env_var(ANNONCESGEN_ENV, "ADMIN_EMAIL")
        admin_pwd = read_env_var(ANNONCESGEN_ENV, "ADMIN_PASSWORD")
        if not admin_email or not admin_pwd:
            return jsonify({"ok": False, "error": "ADMIN_EMAIL/ADMIN_PASSWORD non trouvé dans .env"}), 500
        r = http_requests.post(
            f"{ANNONCESGEN_URL}/api/auth/login",
            json={"email": admin_email, "password": admin_pwd},
            timeout=5
        )
        if r.status_code != 200:
            return jsonify({"ok": False, "error": f"Login échoué ({r.status_code})"}), 500
        data = r.json()
        return jsonify({
            "ok": True,
            "access_token": data.get("access_token"),
            "refresh_token": data.get("refresh_token"),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/containers/annoncesgen-backend-1/toggle-auth", methods=["POST"])
@login_required
def api_toggle_annoncesgen_auth():
    try:
        updater = client.containers.get("docker-updater")
        env_path = ENV_FILES["annoncesgen-backend-1"][0]
        r = updater.exec_run(["sh", "-c", f"cat '{env_path}'"])
        content = r.output.decode("utf-8", errors="replace")
        lines = content.splitlines()
        new_mode = "password"
        found = False
        new_lines = []
        for line in lines:
            if line.startswith("AUTH_MODE="):
                old_mode = line.split("=", 1)[1].strip()
                new_mode = "password" if old_mode == "accounts" else "accounts"
                new_lines.append(f"AUTH_MODE={new_mode}")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"AUTH_MODE={new_mode}")
        new_content = "\n".join(new_lines) + "\n"
        import base64 as b64mod
        encoded = b64mod.b64encode(new_content.encode()).decode()
        updater.exec_run(["sh", "-c", f"echo '{encoded}' | base64 -d > '{env_path}'"])
        container = client.containers.get("annoncesgen-backend-1")
        container.restart(timeout=10)
        _cache.pop("stats_annoncesgen", None)
        return jsonify({"ok": True, "auth_mode": new_mode})
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
        admin_pwd = read_env_var(VIDEODL_ENV, "ADMIN_PASSWORD") or ""
        login_r = session.post(
            f"{VIDEODL_URL}/api/auth/login",
            json={"username": "admin", "password": admin_pwd}, timeout=3
        )
        if login_r.status_code == 200:
            jwt = login_r.json().get("token", "")
            session.headers["Authorization"] = f"Bearer {jwt}"

        ver = session.get(f"{VIDEODL_URL}/api/version", timeout=3).json()
        data["ytdlp_version"] = ver.get("version", "?")

        debug = session.get(
            f"{VIDEODL_URL}/api/debug",
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

    try:
        updater = client.containers.get("docker-updater")
        env_path = ENV_FILES["web-video-downloader-1"][0]
        r = updater.exec_run(["sh", "-c", f"cat '{env_path}'"])
        for line in r.output.decode().splitlines():
            if line.startswith("AUTH_MODE="):
                data["auth_mode"] = line.split("=", 1)[1].strip()
                break
    except Exception:
        pass

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

    data["docker_size"] = get_docker_image_size("voicebox")
    data["error_count"] = count_docker_errors("voicebox")
    set_cache("stats_voicebox", data)
    return jsonify(data)


@app.route("/api/stats/storyboard")
@login_required
def api_stats_storyboard():
    c = cached("stats_storyboard")
    if c:
        return jsonify(c)
    data = {}
    try:
        sb = client.containers.get("storyboardgenerator-app-1")
        script = (
            "import sqlite3\n"
            "c=sqlite3.connect('/app/data/app.db')\n"
            "print(c.execute('SELECT COUNT(*) FROM users').fetchone()[0])\n"
            "print(c.execute('SELECT COUNT(*) FROM projects').fetchone()[0])\n"
            "print(c.execute(\"SELECT COUNT(DISTINCT user_id) FROM projects WHERE created_at > datetime('now','-1 day')\").fetchone()[0])\n"
        )
        r = sb.exec_run(["python3", "-c", script])
        lines = r.output.decode().strip().split("\n")
        if len(lines) >= 3:
            data["users"] = int(lines[0])
            data["projects"] = int(lines[1])
            data["active_users"] = int(lines[2])
    except Exception:
        pass
    data["docker_size"] = get_docker_image_size("storyboardgenerator-app-1")
    data["gemini_quota"] = check_gemini_quota(STORYBOARD_ENV)
    set_cache("stats_storyboard", data)
    return jsonify(data)


@app.route("/api/stats/annoncesgen")
@login_required
def api_stats_annoncesgen():
    c = cached("stats_annoncesgen")
    if c:
        return jsonify(c)
    data = {}
    try:
        auth_mode = read_env_var(ANNONCESGEN_ENV, "AUTH_MODE") or "accounts"
        data["auth_mode"] = auth_mode
    except Exception:
        pass

    try:
        db = client.containers.get("annoncesgen-db-1")
        sql = (
            "SELECT "
            "(SELECT COUNT(*) FROM users),"
            "(SELECT COUNT(*) FROM users WHERE is_active = true "
            "AND id IN (SELECT DISTINCT user_id FROM ad_history "
            "WHERE created_at > NOW() - INTERVAL '1 day')),"
            "(SELECT COUNT(*) FROM ad_history)"
        )
        r = db.exec_run(["psql", "-U", "postgres", "-d", "annoncesgen", "-t", "-c", sql])
        parts = r.output.decode().strip().split("|")
        if len(parts) >= 3:
            data["users_total"] = int(parts[0].strip())
            data["users_active"] = int(parts[1].strip())
            data["generations"] = int(parts[2].strip())
    except Exception:
        pass

    data["docker_size"] = get_docker_image_size("annoncesgen-backend-1")
    data["error_count"] = count_docker_errors("annoncesgen-backend-1")
    data["gemini_quota"] = check_gemini_quota(ANNONCESGEN_ENV)
    set_cache("stats_annoncesgen", data)
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
        data["sleep_paused"] = health.get("sleep_paused", False)
        data["sleep_remaining_min"] = health.get("sleep_remaining_min", -1)
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

    try:
        lr = http_requests.get(f"{RENDER_URL}/logs", params={"tail": 20}, timeout=3).json()
        data["mini_logs"] = lr.get("logs", "")
    except Exception:
        data["mini_logs"] = ""

    try:
        vr = http_requests.get(f"{RENDER_URL}/version", timeout=3).json()
        if vr.get("sha"):
            data["version"] = {"sha": vr["sha"], "date": vr.get("date")}
    except Exception:
        pass

    set_cache("stats_render", data)
    return jsonify(data)


@app.route("/api/render/logs")
@login_required
def api_render_logs():
    try:
        r = http_requests.get(f"{RENDER_URL}/logs", params={"tail": 150}, timeout=5).json()
        return jsonify({"ok": True, "logs": r.get("logs", "")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route("/api/render/engines/<name>/vram-check")
@login_required
def api_render_engine_vram_check(name):
    try:
        h = {"Authorization": f"Bearer {RENDER_TOKEN}"} if RENDER_TOKEN else {}
        r = http_requests.get(f"{RENDER_URL}/engines/{name}/vram-check", headers=h, timeout=5)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/render/engines/<name>/load", methods=["POST"])
@login_required
def api_render_engine_load(name):
    try:
        h = {"Authorization": f"Bearer {RENDER_TOKEN}"} if RENDER_TOKEN else {}
        body = request.get_json(silent=True) or {}
        r = http_requests.post(
            f"{RENDER_URL}/engines/{name}/load", headers=h, json=body, timeout=60,
        )
        _cache.pop("stats_render", None)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/render/engines/<name>/unload", methods=["POST"])
@login_required
def api_render_engine_unload(name):
    try:
        h = {"Authorization": f"Bearer {RENDER_TOKEN}"} if RENDER_TOKEN else {}
        r = http_requests.post(f"{RENDER_URL}/engines/{name}/unload", headers=h, timeout=10)
        _cache.pop("stats_render", None)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/render/sleep-pause", methods=["POST"])
@login_required
def api_render_sleep_pause():
    try:
        r = http_requests.post(f"{RENDER_URL}/system/sleep-pause", timeout=5)
        _cache.pop("stats_render", None)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/render/restart", methods=["POST"])
@login_required
def api_render_restart():
    try:
        r = http_requests.post(f"{RENDER_URL}/system/restart", timeout=5)
        _cache.pop("stats_render", None)
        return jsonify(r.json())
    except Exception:
        _cache.pop("stats_render", None)
        return jsonify({"ok": True, "message": "Restart en cours..."})


RENDER_MAC = "0C:9D:92:84:CC:C0"


def send_wol(mac_address):
    mac = mac_address.replace(":", "").replace("-", "")
    if len(mac) != 12:
        raise ValueError("Adresse MAC invalide")
    magic = b'\xff' * 6 + bytes.fromhex(mac) * 16
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(magic, ("255.255.255.255", 9))


@app.route("/api/render/wol", methods=["POST"])
@login_required
def api_render_wol():
    try:
        send_wol(RENDER_MAC)
        return jsonify({"ok": True, "message": "Magic packet envoyé"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/render/env")
@login_required
def api_render_env_get():
    try:
        r = http_requests.get(f"{RENDER_URL}/system/env", timeout=5).json()
        return jsonify({"ok": True, "files": [{"path": ".env", "content": r.get("content", "")}]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route("/api/render/env", methods=["POST"])
@login_required
def api_render_env_save():
    data = request.get_json()
    content = data.get("content", "")
    try:
        r = http_requests.post(f"{RENDER_URL}/system/env", json={"content": content}, timeout=5).json()
        return jsonify(r)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9090, threaded=True)
