#!/usr/bin/env python3
"""MCPanel v3 - Minecraft Server Management Panel Backend
Multi-server, multi-user management panel with SQLite database.
Features: Analytics, Alerts, 2FA, AI Assistant, MOTD Preview, Settings, Chat Bridge, Sessions."""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import threading
import urllib.parse
import urllib.request
import hashlib
import math
import secrets
import hmac
import base64
import struct
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

# ── Configuration ──────────────────────────────────────────────────────────
PANEL_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(PANEL_DIR, "..", ".."))
DEFAULT_SERVER_DIR = os.path.abspath(os.path.join(PROJECT_DIR, "minecraft-server"))
HTML_FILE = os.path.join(PANEL_DIR, "index.html")
PANEL_PORT = int(os.environ.get("PANEL_PORT", "8080"))
DB_PATH = os.path.join(PANEL_DIR, "panel.db")

# ── Role hierarchy ────────────────────────────────────────────────────────
ROLE_LEVELS = {"viewer": 0, "moderator": 1, "admin": 2, "owner": 3}
TOKEN_EXPIRY_SECONDS = 24 * 60 * 60  # 24 hours

# ── Global state ──────────────────────────────────────────────────────────
valid_tokens = {}  # {token: {user_id, username, role, expires}}
token_lock = threading.Lock()
db_lock = threading.Lock()
schedule_running = True


# ═══════════════════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════════════════

def get_db():
    """Get a thread-local database connection."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Initialize database tables and seed default data."""
    conn = get_db()
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT DEFAULT 'viewer',
            created_at TEXT DEFAULT (datetime('now')),
            last_login TEXT
        );

        CREATE TABLE IF NOT EXISTS servers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            path TEXT NOT NULL,
            port INTEGER DEFAULT 25565,
            screen_name TEXT UNIQUE NOT NULL,
            jar_name TEXT DEFAULT 'server.jar',
            ram_min TEXT DEFAULT '2G',
            ram_max TEXT DEFAULT '6G',
            status TEXT DEFAULT 'stopped',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id INTEGER REFERENCES servers(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            command TEXT NOT NULL,
            interval_seconds INTEGER DEFAULT 300,
            enabled INTEGER DEFAULT 1,
            last_run TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            action TEXT NOT NULL,
            details TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS analytics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id INTEGER,
            players INTEGER DEFAULT 0,
            tps REAL DEFAULT 20.0,
            cpu REAL DEFAULT 0.0,
            ram_used INTEGER DEFAULT 0,
            ram_total INTEGER DEFAULT 4096,
            timestamp TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id INTEGER,
            alert_type TEXT NOT NULL,
            threshold REAL DEFAULT 0,
            enabled INTEGER DEFAULT 1,
            cooldown_seconds INTEGER DEFAULT 300,
            last_triggered TEXT
        );

        CREATE TABLE IF NOT EXISTS alert_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id INTEGER,
            alert_type TEXT,
            message TEXT,
            triggered_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            token TEXT UNIQUE NOT NULL,
            ip TEXT DEFAULT '',
            user_agent TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT DEFAULT (datetime('now', '+24 hours'))
        );
    """)
    conn.commit()

    # Add TOTP columns to users table (safe ALTER - ignore if exists)
    for col_sql in [
        "ALTER TABLE users ADD COLUMN totp_secret TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN totp_enabled INTEGER DEFAULT 0",
    ]:
        try:
            c.execute(col_sql)
            conn.commit()
        except Exception:
            pass

    # Seed default settings if empty
    row_settings = c.execute("SELECT COUNT(*) as cnt FROM settings").fetchone()
    if row_settings["cnt"] == 0:
        c.executemany(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            [
                ("theme", "dark"),
                ("panel_name", "MCPanel"),
                ("alerts_enabled", "true"),
            ],
        )
        conn.commit()
        print("[DB] Created default settings")

    # Seed default alerts for server 1 if no alerts exist
    row_alerts = c.execute("SELECT COUNT(*) as cnt FROM alerts").fetchone()
    if row_alerts["cnt"] == 0:
        c.executemany(
            "INSERT OR IGNORE INTO alerts (server_id, alert_type, threshold, enabled, cooldown_seconds) VALUES (?, ?, ?, ?, ?)",
            [
                (1, "cpu_high", 90, 1, 300),
                (1, "ram_high", 90, 1, 300),
                (1, "tps_low", 15, 1, 300),
            ],
        )
        conn.commit()
        print("[DB] Created default alerts for server 1")

    # Create default admin user if no users exist
    row = c.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
    if row["cnt"] == 0:
        pw_hash = hash_password("admin123")
        c.execute(
            "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
            ("admin", pw_hash, "owner"),
        )
        conn.commit()
        print("[DB] Created default owner user: admin / admin123")

    conn.close()


def hash_password(password):
    """Hash a password with SHA-256."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def verify_password(password, pw_hash):
    """Verify a password against its SHA-256 hash."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest() == pw_hash


def add_audit_log(username, action, details=""):
    """Insert an entry into the audit_log table."""
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO audit_log (username, action, details) VALUES (?, ?, ?)",
            (username, action, details),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_server_by_id(server_id):
    """Fetch a server row by ID, or None."""
    try:
        conn = get_db()
        row = conn.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None


def get_all_servers():
    """Fetch all server rows."""
    try:
        conn = get_db()
        rows = conn.execute("SELECT * FROM servers ORDER BY id").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_default_server_id():
    """Return the ID of the 'main' server or the first server, or None."""
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT id FROM servers WHERE name = 'main' ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            row = conn.execute("SELECT id FROM servers ORDER BY id LIMIT 1").fetchone()
        conn.close()
        return row["id"] if row else None
    except Exception:
        return None


def auto_discover_servers():
    """Create the 'main' server entry if minecraft-server/ dir exists with a .jar."""
    try:
        conn = get_db()
        row = conn.execute("SELECT id FROM servers WHERE name = 'main'").fetchone()
        if row:
            conn.close()
            return
        if os.path.isdir(DEFAULT_SERVER_DIR):
            has_jar = any(
                f.endswith(".jar") for f in os.listdir(DEFAULT_SERVER_DIR)
                if os.path.isfile(os.path.join(DEFAULT_SERVER_DIR, f))
            )
            if has_jar:
                conn.execute(
                    "INSERT INTO servers (name, path, port, screen_name, jar_name) VALUES (?, ?, ?, ?, ?)",
                    ("main", DEFAULT_SERVER_DIR, 25565, "minecraft", "server.jar"),
                )
                conn.commit()
                print("[DB] Auto-discovered 'main' server at", DEFAULT_SERVER_DIR)
        conn.close()
    except Exception as e:
        print(f"[DB] Auto-discover error: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# UTILITY HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def format_bytes(b):
    """Format bytes to human-readable string."""
    if not b:
        return "0 B"
    units = ["B", "KB", "MB", "GB"]
    i = int(math.floor(math.log(b, 1024))) if b > 0 else 0
    if i >= len(units):
        i = len(units) - 1
    return f"{b / (1024 ** i):.1f} {units[i]}"


def get_dir_size(path):
    """Calculate total size of a directory (top-level only for speed)."""
    total = 0
    if not os.path.exists(path):
        return 0
    try:
        for entry in os.scandir(path):
            try:
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
                elif entry.is_dir(follow_symlinks=False):
                    # Quick top-level scan (max 500 files per dir)
                    sub_count = 0
                    for root, dirs, files in os.walk(entry.path):
                        for f in files:
                            if sub_count > 500:
                                break
                            fp = os.path.join(root, f)
                            try:
                                total += os.path.getsize(fp)
                                sub_count += 1
                            except OSError:
                                pass
                        if sub_count > 500:
                            break
            except OSError:
                pass
    except OSError:
        pass
    return total


def safe_path(base_dir, rel_path):
    """Resolve a relative path inside base_dir. Returns absolute path or None."""
    if not rel_path or rel_path == "/":
        return os.path.abspath(base_dir)
    clean = rel_path.lstrip("/")
    joined = os.path.join(base_dir, clean)
    abs_joined = os.path.abspath(joined)
    abs_base = os.path.abspath(base_dir) + os.sep
    if not abs_joined.startswith(abs_base) and abs_joined != os.path.abspath(base_dir):
        return None
    return abs_joined


def parse_server_id(path):
    """Extract server_id from URL path like /api/servers/3/status -> 3.
    Returns (server_id_int, rest_of_path) or (None, path) if not found."""
    m = re.match(r"^/api/servers/(\d+)(.*)", path)
    if m:
        return int(m.group(1)), m.group(2)
    return None, path


# ═══════════════════════════════════════════════════════════════════════════
# SERVER OPERATIONS (parameterised by server_dir / screen_name)
# ═══════════════════════════════════════════════════════════════════════════

def send_screen_cmd(cmd, screen_name):
    """Send a command to a Minecraft server via screen."""
    try:
        subprocess.run(
            ["screen", "-S", screen_name, "-X", "stuff", f"{cmd}\r"],
            capture_output=True, timeout=5,
        )
        return True
    except Exception:
        return False


def is_server_running(screen_name):
    """Check if the Minecraft server screen session is running."""
    try:
        result = subprocess.run(
            ["screen", "-list"], capture_output=True, text=True, timeout=5,
        )
        # Match exact session name with a tab or dot separator
        pattern = re.compile(r"[\t.]" + re.escape(screen_name) + r"[\t\s]")
        return bool(pattern.search(result.stdout))
    except Exception:
        return False


def get_server_uptime(screen_name):
    """Get server uptime from screen session."""
    try:
        result = subprocess.run(
            ["screen", "-list"], capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.split("\n"):
            if screen_name in line:
                parts = line.split("\t")
                if len(parts) >= 2:
                    time_str = parts[-1].strip().strip("()")
                    return time_str if time_str else None
        return None
    except Exception:
        return None


def get_tps(server_dir, screen_name):
    """Estimate TPS from server logs."""
    if not is_server_running(screen_name):
        return 20.0
    try:
        log_file = os.path.join(server_dir, "logs", "latest.log")
        if os.path.exists(log_file):
            result = subprocess.run(
                ["tail", "-100", log_file],
                capture_output=True, text=True, timeout=5,
            )
            for line in reversed(result.stdout.split("\n")):
                if "TPS from last" in line or "tps" in line.lower():
                    try:
                        parts = line.split(":")
                        if len(parts) >= 2:
                            nums = parts[-1].strip().split(",")
                            if nums:
                                return round(float(nums[0].strip()), 1)
                    except (ValueError, IndexError):
                        pass
        try:
            cpu = get_cpu_usage()
            if cpu < 50:
                return 20.0
            elif cpu < 80:
                return 19.5
            else:
                return max(10.0, 20.0 - (cpu - 50) * 0.3)
        except Exception:
            return 20.0
    except Exception:
        return 20.0


def get_cpu_usage():
    """Get CPU usage percentage."""
    try:
        result = subprocess.run(
            ["top", "-bn1"], capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.split("\n"):
            if "%Cpu(s):" in line:
                parts = line.split(",")
                for p in parts:
                    p = p.strip()
                    if p.endswith("id"):
                        idle = float(p.replace("id", "").strip())
                        return round(100.0 - idle, 1)
        return 0.0
    except Exception:
        return 0.0


def get_ram_usage(server_dir):
    """Get RAM usage of Java process running a .jar (fixed: no longer requires 'paper' in line)."""
    try:
        result = subprocess.run(
            ["ps", "aux"], capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.split("\n"):
            if "java" in line.lower() and ".jar" in line:
                # If we have a server_dir, check if that appears in the cmdline
                if server_dir:
                    abs_dir = os.path.abspath(server_dir)
                    if abs_dir not in line:
                        continue
                parts = line.split()
                if len(parts) >= 6:
                    rss_mb = int(parts[5]) / 1024
                    return round(rss_mb)
        return 0
    except Exception:
        return 0


def get_ram_total():
    """Get total system RAM in MB."""
    try:
        result = subprocess.run(
            ["free", "-m"], capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.split("\n"):
            if line.startswith("Mem:"):
                parts = line.split()
                return int(parts[1])
        return 4096
    except Exception:
        return 4096


def get_disk_usage(server_dir):
    """Get disk usage of server_dir."""
    try:
        total, used, free = shutil.disk_usage(server_dir)
        return round(used / (1024 ** 3), 2), round(total / (1024 ** 3), 2)
    except Exception:
        return 0.0, 100.0


def get_java_version():
    """Get Java version."""
    try:
        result = subprocess.run(
            ["java", "-version"], capture_output=True, text=True, timeout=5,
        )
        return result.stderr.split("\n")[0].strip() if result.stderr else "Unknown"
    except Exception:
        return "Not found"


def get_online_players(server_dir):
    """Parse online players from server logs."""
    players = []
    try:
        log_file = os.path.join(server_dir, "logs", "latest.log")
        if not os.path.exists(log_file):
            return players
        result = subprocess.run(
            ["tail", "-500", log_file],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.split("\n"):
            if "joined the game" in line:
                try:
                    name = line.split("]: ")[1].split(" joined")[0].strip()
                    for code in ["\u00a7" + c for c in "0123456789abcdefklmnor"]:
                        name = name.replace(code, "")
                    name = name.split("\u00a7")[0].strip()
                    if name and name not in players:
                        players.append(name)
                except (IndexError, AttributeError):
                    pass
            if "left the game" in line:
                try:
                    name = line.split("]: ")[1].split(" left")[0].strip()
                    for code in ["\u00a7" + c for c in "0123456789abcdefklmnor"]:
                        name = name.replace(code, "")
                    name = name.split("\u00a7")[0].strip()
                    if name in players:
                        players.remove(name)
                except (IndexError, AttributeError):
                    pass
    except Exception:
        pass
    return players


def get_stats(server_dir):
    """Parse server statistics from logs."""
    stats = {"total_joins": 0, "deaths": 0, "advancements": 0, "chat_messages": 0}
    try:
        log_file = os.path.join(server_dir, "logs", "latest.log")
        if not os.path.exists(log_file):
            return stats
        for pattern, key in [
            ("joined the game", "total_joins"),
            ("death.message.", "deaths"),
            ("has made the advancement", "advancements"),
            ("<", "chat_messages"),
        ]:
            try:
                result = subprocess.run(
                    ["grep", "-c", pattern, log_file],
                    capture_output=True, text=True, timeout=10,
                )
                stats[key] = int(result.stdout.strip() or 0)
            except Exception:
                pass
    except Exception:
        pass
    return stats


def read_properties(server_dir):
    """Read server.properties file."""
    props = {}
    prop_file = os.path.join(server_dir, "server.properties")
    if not os.path.exists(prop_file):
        return props
    try:
        with open(prop_file, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, val = line.split("=", 1)
                    props[key.strip()] = val.strip()
    except Exception:
        pass
    return props


def save_properties_file(server_dir, props):
    """Save server.properties file."""
    prop_file = os.path.join(server_dir, "server.properties")
    try:
        lines = [f"{k}={v}" for k, v in props.items()]
        with open(prop_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return True
    except Exception:
        return False


def get_console_logs(server_dir, lines=200):
    """Get recent console output from server log."""
    log_file = os.path.join(server_dir, "logs", "latest.log")
    if not os.path.exists(log_file):
        return "No log file found."
    try:
        result = subprocess.run(
            ["tail", f"-{lines}", log_file],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout
    except Exception:
        return "Error reading log file."


def get_server_version(server_dir):
    """Extract server version from logs."""
    try:
        log_file = os.path.join(server_dir, "logs", "latest.log")
        if os.path.exists(log_file):
            result = subprocess.run(
                ["head", "-50", log_file],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.split("\n"):
                if "version" in line.lower() and (
                    "minecraft" in line.lower()
                    or "paper" in line.lower()
                    or "running" in line.lower()
                ):
                    return line.strip()
            for line in result.stdout.split("\n"):
                if "Paper" in line or "Minecraft" in line:
                    return line.strip()
    except Exception:
        pass
    return "Unknown"


def get_paper_version(server_dir):
    """Try to detect Paper version from jar file."""
    try:
        if not os.path.isdir(server_dir):
            return "Unknown"
        for f in os.listdir(server_dir):
            if f.lower().startswith("paper") and f.endswith(".jar"):
                parts = f.replace("paper", "").replace("Paper", "").replace(".jar", "").split("-")
                if len(parts) >= 2:
                    return f"Paper {parts[1]} (MC {parts[0].lstrip('-')})"
                return f
        # Fall back to any .jar if no paper jar found
        for f in os.listdir(server_dir):
            if f.endswith(".jar"):
                return f
        return "Not found"
    except Exception:
        return "Unknown"


def get_world_sizes(server_dir):
    """Get sizes of world directories."""
    worlds = {}
    for name in ["world", "world_nether", "world_the_end"]:
        path = os.path.join(server_dir, name)
        if os.path.exists(path):
            worlds[name] = format_bytes(get_dir_size(path))
        else:
            worlds[name] = "N/A"
    return worlds


def get_installed_plugins(server_dir):
    """Get list of installed plugin jar files."""
    plugins_dir = os.path.join(server_dir, "plugins")
    if not os.path.exists(plugins_dir):
        return []
    plugins = []
    for f in os.listdir(plugins_dir):
        if f.endswith(".jar"):
            plugins.append(f.replace(".jar", ""))
    return sorted(plugins)


def start_server(server_id):
    """Start a server by ID. Returns (success, message)."""
    srv = get_server_by_id(server_id)
    if not srv:
        return False, "Server not found"
    server_dir = srv["path"]
    screen_name = srv["screen_name"]
    jar_name = srv["jar_name"]
    ram_min = srv["ram_min"]
    ram_max = srv["ram_max"]

    if not os.path.isdir(server_dir):
        return False, f"Server directory does not exist: {server_dir}"

    if is_server_running(screen_name):
        return False, "Server is already running"

    # Find jar file if jar_name is generic
    jar_path = os.path.join(server_dir, jar_name)
    if not os.path.isfile(jar_path):
        # Try to find any .jar that looks like a server jar
        found = None
        for f in os.listdir(server_dir):
            if f.endswith(".jar") and (
                "paper" in f.lower()
                or "spigot" in f.lower()
                or "craftbukkit" in f.lower()
                or "purpur" in f.lower()
                or "pufferfish" in f.lower()
                or f == "server.jar"
            ):
                found = f
                break
        if found:
            jar_path = os.path.join(server_dir, found)
        else:
            # Use any jar
            jars = [f for f in os.listdir(server_dir) if f.endswith(".jar")]
            if jars:
                jar_path = os.path.join(server_dir, jars[0])
            else:
                return False, "No .jar file found in server directory"

    jar_basename = os.path.basename(jar_path)
    try:
        subprocess.Popen(
            [
                "screen", "-dmS", screen_name,
                "java", f"-Xms{ram_min}", f"-Xmx{ram_max}",
                "-jar", jar_basename, "--nogui",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=server_dir,
        )
        # Update status in DB
        with db_lock:
            conn = get_db()
            conn.execute("UPDATE servers SET status = 'starting' WHERE id = ?", (server_id,))
            conn.commit()
            conn.close()
        return True, "Server starting..."
    except Exception as e:
        return False, str(e)


def stop_server(server_id):
    """Stop a server by ID. Returns (success, message)."""
    srv = get_server_by_id(server_id)
    if not srv:
        return False, "Server not found"
    screen_name = srv["screen_name"]
    if not is_server_running(screen_name):
        return False, "Server is not running"
    send_screen_cmd("stop", screen_name)
    with db_lock:
        conn = get_db()
        conn.execute("UPDATE servers SET status = 'stopping' WHERE id = ?", (server_id,))
        conn.commit()
        conn.close()
    return True, "Stopping server..."


def restart_server(server_id):
    """Restart a server by ID. Returns (success, message)."""
    srv = get_server_by_id(server_id)
    if not srv:
        return False, "Server not found"
    screen_name = srv["screen_name"]
    if not is_server_running(screen_name):
        return False, "Server is not running"
    send_screen_cmd("stop", screen_name)
    time.sleep(5)
    return start_server(server_id)


# ═══════════════════════════════════════════════════════════════════════════
# SCHEDULE RUNNER
# ═══════════════════════════════════════════════════════════════════════════

def schedule_runner():
    """Background thread that runs scheduled tasks from the DB."""
    global schedule_running
    while schedule_running:
        try:
            conn = get_db()
            rows = conn.execute(
                "SELECT s.*, srv.screen_name, srv.name as server_name "
                "FROM schedules s JOIN servers srv ON s.server_id = srv.id "
                "WHERE s.enabled = 1"
            ).fetchall()
            conn.close()

            now = time.time()
            for row in rows:
                s = dict(row)
                last_run = s.get("last_run")
                interval = s.get("interval_seconds", 300)

                if last_run:
                    try:
                        last_ts = time.mktime(
                            datetime.strptime(last_run, "%Y-%m-%d %H:%M:%S").timetuple()
                        )
                    except (ValueError, TypeError):
                        last_ts = 0
                else:
                    last_ts = 0

                if now - last_ts >= interval:
                    cmd = s.get("command", "")
                    screen_name = s.get("screen_name", "")
                    if cmd and is_server_running(screen_name):
                        print(f"[Schedule] Running '{s['name']}' on {s['server_name']}: {cmd}")
                        send_screen_cmd(cmd, screen_name)
                        # Update last_run
                        with db_lock:
                            conn2 = get_db()
                            conn2.execute(
                                "UPDATE schedules SET last_run = ? WHERE id = ?",
                                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), s["id"]),
                            )
                            conn2.commit()
                            conn2.close()
        except Exception as e:
            pass
        time.sleep(10)


# ═══════════════════════════════════════════════════════════════════════════
# MARKETPLACE (Modrinth API)
# ═══════════════════════════════════════════════════════════════════════════

def modrinth_search(query, offset=0, limit=20):
    """Search Modrinth for mods/plugins."""
    try:
        import urllib.parse as up
        q = up.quote(query)
        facets = up.quote('[["project_type:mod"]]')
        url = f"https://api.modrinth.com/v2/search?query={q}&facets={facets}&limit={limit}&offset={offset}"
        req = urllib.request.Request(url, headers={"User-Agent": "MCPanel/2.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        hits = []
        for h in data.get("hits", []):
            hits.append({
                "title": h.get("title", ""),
                "description": h.get("description", "")[:200],
                "slug": h.get("slug", ""),
                "icon_url": h.get("icon_url", ""),
                "download_count": h.get("downloads", 0),
                "author": h.get("author", ""),
                "project_id": h.get("project_id", ""),
                "page_url": f"https://modrinth.com/mod/{h.get('slug', '')}",
                "date_modified": h.get("date_modified", "")[:10] if h.get("date_modified") else "",
            })
        return {"hits": hits, "results": hits, "total_hits": data.get("total_hits", 0)}
    except Exception as e:
        return {"hits": [], "results": [], "error": str(e)}


def modrinth_featured():
    """Get popular/featured plugins from Modrinth."""
    try:
        import urllib.parse as up
        facets = up.quote('[["project_type:mod"],["server_side:required"]]')
        url = f"https://api.modrinth.com/v2/search?facets={facets}&sort=downloads&limit=12"
        req = urllib.request.Request(url, headers={"User-Agent": "MCPanel/2.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        hits = []
        for h in data.get("hits", []):
            hits.append({
                "title": h.get("title", ""),
                "description": h.get("description", "")[:200],
                "slug": h.get("slug", ""),
                "icon_url": h.get("icon_url", ""),
                "download_count": h.get("downloads", 0),
                "author": h.get("author", ""),
                "project_id": h.get("project_id", ""),
                "page_url": f"https://modrinth.com/mod/{h.get('slug', '')}",
                "date_modified": h.get("date_modified", "")[:10] if h.get("date_modified") else "",
            })
        return {"hits": hits, "results": hits, "total_hits": data.get("total_hits", 0)}
    except Exception as e:
        return {"hits": [], "results": [], "error": str(e)}


def modrinth_versions(slug):
    """Get available versions for a Modrinth project."""
    try:
        import urllib.parse as up
        url = f"https://api.modrinth.com/v2/project/{up.quote(slug)}/version"
        req = urllib.request.Request(url, headers={"User-Agent": "MCPanel/2.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        versions = []
        for v in data[:10]:
            files = v.get("files", [])
            primary = files[0] if files else None
            if primary:
                versions.append({
                    "version": v.get("version_number", ""),
                    "name": v.get("name", ""),
                    "url": primary.get("url", ""),
                    "size": primary.get("size", 0),
                    "downloads": v.get("downloads", 0),
                })
        return {"versions": versions}
    except Exception as e:
        return {"versions": [], "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# TOTP (Pure Python - no pyotp dependency)
# ═══════════════════════════════════════════════════════════════════════════

def generate_totp_secret():
    """Generate a new base32-encoded TOTP secret."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')

def get_totp_code(secret, period=30):
    """Generate a TOTP code from a base32 secret."""
    key = base64.b32decode(secret + '=' * ((8 - len(secret)) % 8))
    t = int(time.time()) // period
    msg = struct.pack('>Q', t)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0f
    code = struct.unpack('>I', h[offset:offset+4])[0] & 0x7fffffff
    return str(code % 1000000).zfill(6)

def verify_totp(secret, code, period=30, window=1):
    """Verify a TOTP code against a secret with a time window."""
    for i in range(-window, window + 1):
        t = int(time.time()) // period + i
        msg = struct.pack('>Q', t)
        h = hmac.new(base64.b32decode(secret + '=' * ((8 - len(secret)) % 8)), msg, hashlib.sha1).digest()
        offset = h[-1] & 0x0f
        generated = str(struct.unpack('>I', h[offset:offset+4])[0] & 0x7fffffff % 1000000).zfill(6)
        if generated == code:
            return True
    return False


# ═══════════════════════════════════════════════════════════════════════════
# ANALYTICS COLLECTOR
# ═══════════════════════════════════════════════════════════════════════════

def analytics_collector():
    """Background thread that snapshots server stats every 60 seconds."""
    while schedule_running:
        try:
            servers = get_all_servers()
            conn = get_db()
            for srv in servers:
                sid = srv["id"]
                sdir = srv["path"]
                sname = srv["screen_name"]
                running = is_server_running(sname)
                players = len(get_online_players(sdir)) if running else 0
                tps = get_tps(sdir, sname) if running else 20.0
                cpu = get_cpu_usage() if running else 0.0
                ram_used = get_ram_usage(sdir) if running else 0
                ram_total = get_ram_total()
                conn.execute(
                    "INSERT INTO analytics (server_id, players, tps, cpu, ram_used, ram_total) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (sid, players, tps, cpu, ram_used, ram_total),
                )

                # Check alerts for this server
                try:
                    alerts_rows = conn.execute(
                        "SELECT * FROM alerts WHERE server_id = ? AND enabled = 1",
                        (sid,),
                    ).fetchall()
                    for alert in alerts_rows:
                        a = dict(alert)
                        triggered = False
                        msg = ""
                        atype = a["alert_type"]
                        threshold = a["threshold"]
                        cooldown = a.get("cooldown_seconds", 300)
                        last_trig = a.get("last_triggered")

                        if atype == "cpu_high" and cpu > threshold:
                            triggered = True
                            msg = f"CPU usage {cpu:.1f}% exceeds threshold {threshold}%"
                        elif atype == "ram_high" and ram_total > 0 and (ram_used / ram_total * 100) > threshold:
                            triggered = True
                            msg = f"RAM usage {ram_used}MB ({ram_used/ram_total*100:.1f}%) exceeds threshold {threshold}%"
                        elif atype == "tps_low" and running and tps < threshold:
                            triggered = True
                            msg = f"TPS {tps:.1f} is below threshold {threshold}"

                        if triggered:
                            # Check cooldown
                            should_fire = True
                            if last_trig:
                                try:
                                    last_ts = time.mktime(
                                        datetime.strptime(last_trig, "%Y-%m-%d %H:%M:%S").timetuple()
                                    )
                                    if time.time() - last_ts < cooldown:
                                        should_fire = False
                                except (ValueError, TypeError):
                                    pass
                            if should_fire:
                                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                conn.execute(
                                    "INSERT INTO alert_history (server_id, alert_type, message) VALUES (?, ?, ?)",
                                    (sid, atype, msg),
                                )
                                conn.execute(
                                    "UPDATE alerts SET last_triggered = ? WHERE id = ?",
                                    (now_str, a["id"]),
                                )
                                conn.commit()
                                print(f"[ALERT] Server {srv['name']}: {msg}")
                except Exception as alert_err:
                    pass

            # Cleanup old analytics (keep 7 days)
            conn.execute("DELETE FROM analytics WHERE timestamp < datetime('now', '-7 days')")
            conn.commit()
            conn.close()
        except Exception as e:
            pass
        time.sleep(60)


# ═══════════════════════════════════════════════════════════════════════════
# AI ASSISTANT (Keyword-based)
# ═══════════════════════════════════════════════════════════════════════════

def ai_assistant_response(message):
    """Generate a keyword-based AI assistant response."""
    msg_lower = message.lower().strip()

    if not msg_lower:
        return "Hello! I'm the MCPanel AI assistant. Ask me anything about your server!"

    if any(k in msg_lower for k in ["whitelist", "allow player"]):
        return ("To manage the whitelist, go to your server's console and use:\n"
                "• `whitelist on` - Enable whitelist\n"
                "• `whitelist off` - Disable whitelist\n"
                "• `whitelist add <player>` - Add a player\n"
                "• `whitelist remove <player>` - Remove a player\n"
                "• `whitelist list` - Show whitelisted players")

    if any(k in msg_lower for k in ["op", "admin", "operator"]):
        return ("To manage operators:\n"
                "• `op <player>` - Grant operator status\n"
                "• `deop <player>` - Remove operator status\n"
                "• Operators have full access to server commands.")

    if any(k in msg_lower for k in ["ram", "memory", "allocat"]):
        return ("Memory allocation is set per server in the server settings:\n"
                "• RAM Min (Xms): Initial memory allocation\n"
                "• RAM Max (Xmx): Maximum memory allocation\n"
                "• Recommended: 2-6GB for small servers, 8-16GB for large servers.\n"
                "• Monitor usage on the Status page.")

    if any(k in msg_lower for k in ["backup"]):
        return ("To create a backup:\n"
                "• Go to your server page → click 'Backup'\n"
                "• Backups are saved as .tar.gz in the server directory\n"
                "• Backups exclude logs, jars, and cache for smaller file sizes\n"
                "• You can also restore by extracting the backup manually.")

    if any(k in msg_lower for k in ["plugin", "mod", "install"]):
        return ("To manage plugins:\n"
                "• Go to the Marketplace to browse and install plugins\n"
                "• Plugins are .jar files placed in the `plugins/` directory\n"
                "• Use `plugins` command in console to list loaded plugins\n"
                "• Restart the server after installing new plugins.")

    if any(k in msg_lower for k in ["port", "connect", "join"]):
        return ("Server port configuration:\n"
                "• Default port: 25565\n"
                "• Change in server.properties: `server-port=25565`\n"
                "• Players connect with: `your-ip:port`\n"
                "• Make sure the port is open in your firewall.")

    if any(k in msg_lower for k in ["restart", "stop", "start", "boot"]):
        return ("Server controls:\n"
                "• Start - Launches the server in a screen session\n"
                "• Stop - Gracefully stops the server (saves worlds)\n"
                "• Restart - Stops then starts the server\n"
                "• Use scheduled tasks for automatic restarts.")

    if any(k in msg_lower for k in ["seed", "world", "generate"]):
        return ("World/seed information:\n"
                "• The level-seed is in server.properties\n"
                "• Set `level-seed=` to a specific seed before first run\n"
                "• World files are in `world/`, `world_nether/`, `world_the_end/`\n"
                "• Check world sizes on the Server Info page.")

    if any(k in msg_lower for k in ["error", "crash", "issue", "fix", "troubleshoot"]):
        return ("Common server errors:\n"
                "• OutOfMemoryError - Increase RAM allocation\n"
                "• Port in use - Another process uses the port; kill it or change port\n"
                "• Plugin errors - Check `logs/latest.log` for stack traces\n"
                "• Can't bind to IP - Check firewall and server-ip setting\n"
                "• Use the Console page to view real-time logs.")

    if any(k in msg_lower for k in ["help", "what can", "commands", "feature"]):
        return ("MCPanel features:\n"
                "• Server Start/Stop/Restart with console access\n"
                "• Player management (kick, ban, op, whitelist)\n"
                "• File browser and editor\n"
                "• Plugin marketplace (Modrinth integration)\n"
                "• Scheduled tasks for automation\n"
                "• Server analytics and alerts\n"
                "• Backup creation\n"
                "• Multi-user with role-based access\n"
                "• 2FA support for accounts\n"
                "• Ask me about any specific feature!")

    if any(k in msg_lower for k in ["performance", "lag", "slow", "optimize", "tps"]):
        return ("Performance optimization tips:\n"
                "• Monitor TPS on the Status page (target: 19.5-20)\n"
                "• Allocate enough RAM (6GB+ recommended)\n"
                "• Use Paper server jar for better performance\n"
                "• Install optimization plugins (ClearLagg, etc.)\n"
                "• Reduce view-distance and simulation-distance\n"
                "• Use pre-generated chunks for new worlds")

    if any(k in msg_lower for k in ["2fa", "totp", "authenticator", "security"]):
        return ("To enable 2FA:\n"
                "• Go to Settings → Security → Enable 2FA\n"
                "• Scan the QR code with Google Authenticator or similar app\n"
                "• Enter the 6-digit code to verify and enable\n"
                "• You'll need to enter a code on each login after enabling.")

    return ("I'm not sure about that. Try asking about:\n"
            "• whitelist, op, ram, backup, plugin, port\n"
            "• restart, seed, error, performance, 2fa, help")


# ═══════════════════════════════════════════════════════════════════════════
# MOTD PREVIEW
# ═══════════════════════════════════════════════════════════════════════════

MC_COLORS = {
    '§0': '#000000', '§1': '#0000AA', '§2': '#00AA00', '§3': '#00AAAA',
    '§4': '#AA0000', '§5': '#AA00AA', '§6': '#FFAA00', '§7': '#AAAAAA',
    '§8': '#555555', '§9': '#5555FF', '§a': '#55FF55', '§b': '#55FFFF',
    '§c': '#FF5555', '§d': '#FF55FF', '§e': '#FFFF55', '§f': '#FFFFFF',
    '§k': '', '§l': 'font-weight:bold', '§m': 'text-decoration:line-through',
    '§n': 'text-decoration:underline', '§o': 'font-style:italic', '§r': '',
}

def motd_to_html(motd):
    """Convert Minecraft § color code MOTD string to HTML."""
    if not motd:
        return ""
    html = ""
    i = 0
    current_color = "#FFFFFF"
    current_styles = []
    while i < len(motd):
        if i + 1 < len(motd) and motd[i] == '§':
            code = motd[i:i+2]
            if code in MC_COLORS:
                val = MC_COLORS[code]
                # Flush current span
                if html and not html.endswith(">") and (current_color != "#FFFFFF" or current_styles):
                    pass  # will close later
                # Handle reset
                if code == '§r':
                    if html:
                        # Close any open spans
                        while '<span' in html:
                            html += "</span>"
                            break
                    current_color = "#FFFFFF"
                    current_styles = []
                elif val.startswith('#'):
                    # Color change - close previous spans
                    if '<span' in html:
                        html += "</span>"
                    current_color = val
                    current_styles = []
                elif val:
                    # Style code
                    if val not in current_styles:
                        current_styles.append(val)
                else:
                    pass  # §k (obfuscated) - skip
                i += 2
                continue
        # Regular character
        # Open span if needed
        if not html or html.endswith("</span>"):
            style_parts = []
            if current_color != "#FFFFFF":
                style_parts.append(f"color:{current_color}")
            style_parts.extend(current_styles)
            if style_parts:
                html += f'<span style="{";".join(style_parts)}">'
        html += motd[i]
        i += 1
    # Close any remaining spans
    if '<span' in html and not html.endswith("</span>"):
        html += "</span>"
    return html


# ═══════════════════════════════════════════════════════════════════════════
# CHAT BRIDGE
# ═══════════════════════════════════════════════════════════════════════════

def get_chat_messages(server_dir, lines=50):
    """Parse recent chat messages from server logs."""
    messages = []
    try:
        log_file = os.path.join(server_dir, "logs", "latest.log")
        if not os.path.exists(log_file):
            return messages
        result = subprocess.run(
            ["tail", f"-{lines * 3}", log_file],
            capture_output=True, text=True, timeout=5,
        )
        for line in reversed(result.stdout.split("\n")):
            if len(messages) >= lines:
                break
            # Match lines containing <PlayerName> message
            m = re.search(r'<([^>]+)>\s*(.*)', line)
            if m:
                player = m.group(1).strip()
                msg = m.group(2).strip()
                # Extract timestamp from log line
                ts = ""
                ts_m = re.match(r'\[(\d{2}:\d{2}:\d{2})\]', line)
                if ts_m:
                    ts = ts_m.group(1)
                messages.insert(0, {
                    "player": player,
                    "message": msg,
                    "timestamp": ts,
                })
    except Exception:
        pass
    return messages


# ═══════════════════════════════════════════════════════════════════════════
# HTTP HANDLER
# ═══════════════════════════════════════════════════════════════════════════

class PanelHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        """Override to reduce log noise."""
        pass

    # ── Response helpers ──────────────────────────────────────────────────

    def send_json(self, data, status=200):
        """Send a JSON response."""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode())

    def send_html(self, content, status=200):
        """Send HTML response."""
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(content.encode())

    def send_binary(self, data, filename, content_type="application/octet-stream"):
        """Send binary file download."""
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    # ── Request helpers ───────────────────────────────────────────────────

    def read_body(self):
        """Read and parse JSON request body."""
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except Exception:
            return None

    def read_raw_body(self):
        """Read raw request body bytes."""
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return b""
        return self.rfile.read(length)

    def get_current_user(self):
        """Validate bearer token and return user dict, or None."""
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        token = auth[7:]
        with token_lock:
            info = valid_tokens.get(token)
            if not info:
                return None
            # Check expiry
            if time.time() > info.get("expires", 0):
                del valid_tokens[token]
                return None
            return {
                "id": info["user_id"],
                "username": info["username"],
                "role": info["role"],
            }

    def require_role(self, min_role):
        """Check auth and return user dict if role >= min_role, else send 401/403 and return None."""
        user = self.get_current_user()
        if not user:
            self.send_json({"error": "Unauthorized"}, 401)
            return None
        if ROLE_LEVELS.get(user["role"], 0) < ROLE_LEVELS.get(min_role, 0):
            self.send_json({"error": "Insufficient permissions"}, 403)
            return None
        return user

    def require_auth(self):
        """Check auth and return user dict, or send 401 and return None."""
        user = self.get_current_user()
        if not user:
            self.send_json({"error": "Unauthorized"}, 401)
            return None
        return user

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "*")
        self.end_headers()

    # ── GET routing ──────────────────────────────────────────────────────

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        query = urllib.parse.parse_qs(parsed.query)

        # Serve HTML
        if path in ("", "/index.html"):
            html_path = os.path.join(PANEL_DIR, "index.html")
            if os.path.exists(html_path):
                with open(html_path, "r", encoding="utf-8") as f:
                    self.send_html(f.read())
            else:
                self.send_html("<h1>MCPanel - index.html not found</h1>", 404)
            return

        # ── Auth endpoints (no token required) ─────────────────────────
        if path == "/api/auth/login":
            self.send_json({"error": "Use POST for login"})
            return

        # ── Marketplace endpoints (auth required but any role) ─────────
        if path == "/api/marketplace/search":
            user = self.require_auth()
            if not user:
                return
            q = query.get("query", [""])[0]
            offset = int(query.get("offset", ["0"])[0])
            self.send_json(modrinth_search(q, offset))
            return

        if path == "/api/marketplace/featured":
            user = self.require_auth()
            if not user:
                return
            self.send_json(modrinth_featured())
            return

        if path == "/api/marketplace/versions":
            user = self.require_auth()
            if not user:
                return
            slug = query.get("slug", [""])[0]
            self.send_json(modrinth_versions(slug))
            return

        # ── 2FA setup (auth required) ────────────────────────────────
        if path == "/api/auth/2fa/setup":
            user = self.require_auth()
            if not user:
                return
            secret = generate_totp_secret()
            self.send_json({"secret": secret, "user": user})
            return

        # ── AI Chat (auth required) ──────────────────────────────────
        if path == "/api/ai/chat":
            user = self.require_auth()
            if not user:
                return
            self.send_json({"error": "Use POST for AI chat"})
            return

        # ── Settings (auth required) ─────────────────────────────────
        if path == "/api/settings":
            user = self.require_auth()
            if not user:
                return
            try:
                conn = get_db()
                rows = conn.execute("SELECT key, value, updated_at FROM settings ORDER BY key").fetchall()
                conn.close()
                settings = {r["key"]: {"value": r["value"], "updated_at": r["updated_at"]} for r in rows}
                self.send_json({"settings": settings, "user": user})
            except Exception as e:
                self.send_json({"error": str(e), "user": user})
            return

        # ── Sessions (auth required, admin+) ─────────────────────────
        if path == "/api/auth/sessions":
            user = self.require_role("admin")
            if not user:
                return
            try:
                conn = get_db()
                rows = conn.execute(
                    "SELECT s.*, u.username FROM sessions s LEFT JOIN users u ON s.user_id = u.id "
                    "ORDER BY s.created_at DESC LIMIT 100"
                ).fetchall()
                conn.close()
                sessions = []
                for r in rows:
                    sessions.append({
                        "id": r["id"],
                        "user_id": r["user_id"],
                        "username": r["username"],
                        "ip": r["ip"],
                        "user_agent": r["user_agent"],
                        "created_at": r["created_at"],
                        "expires_at": r["expires_at"],
                    })
                self.send_json({"sessions": sessions, "user": user})
            except Exception as e:
                self.send_json({"error": str(e), "user": user})
            return

        # ── /api/servers/... endpoints ─────────────────────────────────
        server_id, rest = parse_server_id(path)
        if server_id is not None:
            return self._handle_server_get(server_id, rest, query)

        # ── Backwards compat: old /api/* endpoints ────────────────────
        # These operate on the default (first) server
        user = self.require_auth()
        if not user:
            return

        default_id = get_default_server_id()

        if path == "/api/status":
            self._compat_status(default_id, user)
        elif path == "/api/console":
            lines = int(query.get("lines", ["200"])[0])
            self._compat_console(default_id, lines, user)
        elif path == "/api/players":
            self._compat_players(default_id, user)
        elif path == "/api/stats":
            self._compat_stats(default_id, user)
        elif path == "/api/files":
            fp = query.get("path", ["/"])[0]
            self._compat_files(default_id, fp, user)
        elif path == "/api/file/read":
            fp = query.get("path", [""])[0]
            self._compat_file_read(default_id, fp, user)
        elif path == "/api/file/download":
            fp = query.get("path", [""])[0]
            self._compat_file_download(default_id, fp, user)
        elif path == "/api/properties":
            self._compat_properties(default_id, user)
        elif path == "/api/ops":
            self._compat_ops(default_id, user)
        elif path == "/api/banned":
            self._compat_banned(default_id, user)
        elif path == "/api/plugins":
            self._compat_plugins(default_id, user)
        elif path == "/api/schedules":
            self._compat_schedules(default_id, user)
        elif path == "/api/logs":
            search = query.get("search", [""])[0]
            lines = int(query.get("lines", ["200"])[0])
            self._compat_logs(default_id, search, lines, user)
        elif path == "/api/server-info":
            self._compat_server_info(default_id, user)
        elif path == "/api/auth/me":
            self.send_json({"user": user})
        elif path == "/api/users":
            self._handle_list_users(user)
        elif path == "/api/servers":
            self._handle_list_servers(user)
        else:
            self.send_json({"error": "Not found"}, 404)

    # ── POST routing ────────────────────────────────────────────────────

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        # Auth login (no token required)
        if path == "/api/auth/login":
            self._handle_login()
            return

        # Auth register (admin+ required)
        if path == "/api/auth/register":
            user = self.require_role("admin")
            if not user:
                return
            self._handle_register(user)
            return

        # 2FA endpoints
        if path == "/api/auth/2fa/enable":
            user = self.require_auth()
            if not user:
                return
            self._handle_2fa_enable(user)
            return

        if path == "/api/auth/2fa/verify":
            self._handle_2fa_verify()
            return

        if path == "/api/auth/2fa/disable":
            user = self.require_auth()
            if not user:
                return
            self._handle_2fa_disable(user)
            return

        # Sessions delete
        if path == "/api/auth/sessions/delete":
            user = self.require_role("admin")
            if not user:
                return
            self._handle_session_delete(user)
            return

        # AI Chat
        if path == "/api/ai/chat":
            user = self.require_auth()
            if not user:
                return
            self._handle_ai_chat(user)
            return

        # ── /api/servers/... POST endpoints ────────────────────────────
        server_id, rest = parse_server_id(path)
        if server_id is not None:
            return self._handle_server_post(server_id, rest)

        # ── User management ────────────────────────────────────────────
        user = self.require_auth()
        if not user:
            return

        if path == "/api/users/role":
            self._handle_update_user_role(user)
            return

        # ── Backwards compat: old POST /api/* endpoints ─────────────────
        # These operate on the default (first) server
        default_id = get_default_server_id()

        compat_post_routes = {
            "/api/start": "_compat_post_start",
            "/api/stop": "_compat_post_stop",
            "/api/restart": "_compat_post_restart",
            "/api/command": "_compat_post_command",
            "/api/backup": "_compat_post_backup",
            "/api/say": "_compat_post_say",
            "/api/kick": "_compat_post_kick",
            "/api/ban": "_compat_post_ban",
            "/api/unban": "_compat_post_unban",
            "/api/op": "_compat_post_op",
            "/api/deop": "_compat_post_deop",
            "/api/whitelist": "_compat_post_whitelist",
            "/api/gamemode": "_compat_post_gamemode",
            "/api/tp": "_compat_post_tp",
            "/api/properties": "_compat_post_save_properties",
            "/api/file/save": "_compat_post_file_save",
            "/api/file/create": "_compat_post_file_create",
            "/api/file/mkdir": "_compat_post_file_mkdir",
            "/api/file/delete": "_compat_post_file_delete",
            "/api/file/rename": "_compat_post_file_rename",
            "/api/file/upload": "_compat_post_file_upload",
            "/api/plugins/delete": "_compat_post_plugin_delete",
            "/api/schedules": "_compat_post_schedule_create",
            "/api/schedules/delete": "_compat_post_schedule_delete",
            "/api/schedules/toggle": "_compat_post_schedule_toggle",
        }

        if path in compat_post_routes:
            handler_name = compat_post_routes[path]
            handler = getattr(self, handler_name, None)
            if handler:
                handler(default_id, user)
                return

        self.send_json({"error": "Not found"}, 404)

    # ── PUT routing ─────────────────────────────────────────────────────

    def do_PUT(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        user = self.require_auth()
        if not user:
            return

        # /api/settings (PUT)
        if path == "/api/settings":
            body = self.read_body()
            if not body or "key" not in body or "value" not in body:
                self.send_json({"error": "Missing key/value"}, 400)
                return
            try:
                with db_lock:
                    conn = get_db()
                    conn.execute(
                        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, datetime('now')) "
                        "ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = datetime('now')",
                        (body["key"], body["value"], body["value"]),
                    )
                    conn.commit()
                    conn.close()
                add_audit_log(user["username"], "update_setting", f"{body['key']} = {body['value']}")
                self.send_json({"success": True, "message": f"Setting '{body['key']}' updated"})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        # /api/servers/{id}
        server_id, rest = parse_server_id(path)
        if server_id is not None and rest == "":
            if ROLE_LEVELS.get(user["role"], 0) < ROLE_LEVELS["admin"]:
                self.send_json({"error": "Insufficient permissions"}, 403)
                return
            body = self.read_body()
            if not body:
                self.send_json({"error": "Missing body"}, 400)
                return
            try:
                with db_lock:
                    conn = get_db()
                    sets = []
                    vals = []
                    for key in ("name", "path", "port", "screen_name", "jar_name", "ram_min", "ram_max"):
                        if key in body:
                            sets.append(f"{key} = ?")
                            vals.append(body[key])
                    if not sets:
                        conn.close()
                        self.send_json({"error": "No fields to update"}, 400)
                        return
                    vals.append(server_id)
                    conn.execute(f"UPDATE servers SET {', '.join(sets)} WHERE id = ?", vals)
                    conn.commit()
                    conn.close()
                add_audit_log(user["username"], "update_server", f"Updated server {server_id}")
                self.send_json({"success": True, "message": "Server updated"})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        # /api/users/{id}/role
        m = re.match(r"^/api/users/(\d+)/role$", path)
        if m:
            target_id = int(m.group(1))
            if ROLE_LEVELS.get(user["role"], 0) < ROLE_LEVELS["owner"]:
                self.send_json({"error": "Owner only"}, 403)
                return
            body = self.read_body()
            role = body.get("role", "") if body else ""
            if role not in ROLE_LEVELS:
                self.send_json({"error": "Invalid role"}, 400)
                return
            try:
                with db_lock:
                    conn = get_db()
                    conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, target_id))
                    conn.commit()
                    conn.close()
                add_audit_log(user["username"], "update_user_role", f"User {target_id} -> {role}")
                self.send_json({"success": True, "message": f"User role updated to {role}"})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        self.send_json({"error": "Not found"}, 404)

    # ── DELETE routing ──────────────────────────────────────────────────

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        user = self.require_auth()
        if not user:
            return

        # /api/servers/{id}
        server_id, rest = parse_server_id(path)
        if server_id is not None and rest == "":
            if ROLE_LEVELS.get(user["role"], 0) < ROLE_LEVELS["owner"]:
                self.send_json({"error": "Owner only"}, 403)
                return
            srv = get_server_by_id(server_id)
            if not srv:
                self.send_json({"error": "Server not found"}, 404)
                return
            # Stop server if running
            if is_server_running(srv["screen_name"]):
                send_screen_cmd("stop", srv["screen_name"])
                time.sleep(3)
            # Delete from DB
            with db_lock:
                conn = get_db()
                conn.execute("DELETE FROM schedules WHERE server_id = ?", (server_id,))
                conn.execute("DELETE FROM servers WHERE id = ?", (server_id,))
                conn.commit()
                conn.close()
            add_audit_log(user["username"], "delete_server", f"Deleted server: {srv['name']}")
            self.send_json({"success": True, "message": f"Deleted server: {srv['name']}"})
            return

        # /api/users/{id}
        m = re.match(r"^/api/users/(\d+)$", path)
        if m:
            target_id = int(m.group(1))
            if ROLE_LEVELS.get(user["role"], 0) < ROLE_LEVELS["owner"]:
                self.send_json({"error": "Owner only"}, 403)
                return
            if target_id == user["id"]:
                self.send_json({"error": "Cannot delete yourself"}, 400)
                return
            try:
                with db_lock:
                    conn = get_db()
                    conn.execute("DELETE FROM users WHERE id = ?", (target_id,))
                    conn.commit()
                    conn.close()
                add_audit_log(user["username"], "delete_user", f"Deleted user {target_id}")
                self.send_json({"success": True, "message": "User deleted"})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        self.send_json({"error": "Not found"}, 404)

    # ═════════════════════════════════════════════════════════════════════
    # AUTH HANDLERS
    # ═════════════════════════════════════════════════════════════════════

    def _handle_login(self):
        body = self.read_body()
        if not body:
            self.send_json({"error": "Missing request body"}, 400)
            return
        username = body.get("username", "").strip()
        password = body.get("password", "")
        if not username or not password:
            self.send_json({"error": "Username and password required"}, 400)
            return
        try:
            conn = get_db()
            row = conn.execute(
                "SELECT id, username, password, role, totp_enabled FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            conn.close()
        except Exception:
            self.send_json({"error": "Database error"}, 500)
            return

        if not row or not verify_password(password, row["password"]):
            add_audit_log(username, "login_failed", "Invalid credentials")
            self.send_json({"success": False, "error": "Invalid username or password"}, 401)
            return

        # Check if 2FA is enabled
        if row["totp_enabled"]:
            # Generate a short-lived temp token
            temp_token = secrets.token_hex(32)
            with token_lock:
                valid_tokens[temp_token] = {
                    "user_id": row["id"],
                    "username": row["username"],
                    "role": row["role"],
                    "expires": time.time() + 300,  # 5 minutes
                    "is_2fa_pending": True,
                    "real_user_id": row["id"],
                }
            add_audit_log(username, "login_2fa_pending", "2FA verification required")
            self.send_json({
                "success": True,
                "requires_2fa": True,
                "temp_token": temp_token,
            })
            return

        # Generate token
        token = secrets.token_hex(32)
        expires = time.time() + TOKEN_EXPIRY_SECONDS
        with token_lock:
            valid_tokens[token] = {
                "user_id": row["id"],
                "username": row["username"],
                "role": row["role"],
                "expires": expires,
            }

        # Update last login
        try:
            conn = get_db()
            conn.execute(
                "UPDATE users SET last_login = datetime('now') WHERE id = ?",
                (row["id"],),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

        # Create session
        try:
            client_ip = self.client_address[0] if self.client_address else ""
            client_ua = self.headers.get("User-Agent", "")[:200]
            with db_lock:
                conn = get_db()
                conn.execute(
                    "INSERT INTO sessions (user_id, token, ip, user_agent) VALUES (?, ?, ?, ?)",
                    (row["id"], token, client_ip, client_ua),
                )
                conn.commit()
                conn.close()
        except Exception:
            pass

        add_audit_log(username, "login", "Successful login")
        self.send_json({
            "success": True,
            "token": token,
            "user": {
                "id": row["id"],
                "username": row["username"],
                "role": row["role"],
            },
        })

    def _handle_2fa_enable(self, user):
        """POST /api/auth/2fa/enable - Verify code and enable 2FA."""
        body = self.read_body()
        if not body:
            self.send_json({"error": "Missing body"}, 400)
            return
        secret = body.get("secret", "")
        code = str(body.get("code", ""))
        if not secret or not code:
            self.send_json({"error": "Secret and code required"}, 400)
            return
        if not verify_totp(secret, code):
            self.send_json({"error": "Invalid code"}, 401)
            return
        try:
            with db_lock:
                conn = get_db()
                conn.execute(
                    "UPDATE users SET totp_secret = ?, totp_enabled = 1 WHERE id = ?",
                    (secret, user["id"]),
                )
                conn.commit()
                conn.close()
            add_audit_log(user["username"], "2fa_enable", "2FA enabled")
            self.send_json({"success": True, "message": "2FA enabled"})
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def _handle_2fa_verify(self):
        """POST /api/auth/2fa/verify - Verify 2FA code with temp token."""
        body = self.read_body()
        if not body:
            self.send_json({"error": "Missing body"}, 400)
            return
        temp_token = body.get("temp_token", "")
        code = str(body.get("code", ""))
        if not temp_token or not code:
            self.send_json({"error": "Temp token and code required"}, 400)
            return
        # Validate temp token
        with token_lock:
            info = valid_tokens.get(temp_token)
            if not info:
                self.send_json({"error": "Invalid or expired temp token"}, 401)
                return
            if time.time() > info.get("expires", 0):
                del valid_tokens[temp_token]
                self.send_json({"error": "Temp token expired"}, 401)
                return
            if not info.get("is_2fa_pending"):
                self.send_json({"error": "Not a 2FA pending token"}, 400)
                return
            user_id = info["real_user_id"]
            username = info["username"]
            role = info["role"]
            # Remove temp token
            del valid_tokens[temp_token]

        # Get user's TOTP secret
        try:
            conn = get_db()
            row = conn.execute(
                "SELECT totp_secret FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            conn.close()
        except Exception:
            self.send_json({"error": "Database error"}, 500)
            return

        if not row or not row["totp_secret"]:
            self.send_json({"error": "2FA not configured for this user"}, 400)
            return

        if not verify_totp(row["totp_secret"], code):
            add_audit_log(username, "2fa_failed", "Invalid 2FA code")
            self.send_json({"error": "Invalid code"}, 401)
            return

        # Generate real token
        token = secrets.token_hex(32)
        expires = time.time() + TOKEN_EXPIRY_SECONDS
        with token_lock:
            valid_tokens[token] = {
                "user_id": user_id,
                "username": username,
                "role": role,
                "expires": expires,
            }

        # Update last login
        try:
            conn = get_db()
            conn.execute(
                "UPDATE users SET last_login = datetime('now') WHERE id = ?",
                (user_id,),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

        # Create session
        try:
            with db_lock:
                conn = get_db()
                conn.execute(
                    "INSERT INTO sessions (user_id, token, ip, user_agent) VALUES (?, ?, ?, ?)",
                    (user_id, token, "", ""),
                )
                conn.commit()
                conn.close()
        except Exception:
            pass

        add_audit_log(username, "login", "Successful login with 2FA")
        self.send_json({
            "success": True,
            "token": token,
            "user": {
                "id": user_id,
                "username": username,
                "role": role,
            },
        })

    def _handle_2fa_disable(self, user):
        """POST /api/auth/2fa/disable - Disable 2FA."""
        body = self.read_body()
        if not body:
            self.send_json({"error": "Missing body"}, 400)
            return
        code = str(body.get("code", ""))
        if not code:
            self.send_json({"error": "Code required"}, 400)
            return
        # Get current TOTP secret
        try:
            conn = get_db()
            row = conn.execute(
                "SELECT totp_secret FROM users WHERE id = ?",
                (user["id"],),
            ).fetchone()
            conn.close()
        except Exception:
            self.send_json({"error": "Database error"}, 500)
            return

        if not row or not row["totp_secret"]:
            self.send_json({"error": "2FA not enabled"}, 400)
            return

        if not verify_totp(row["totp_secret"], code):
            add_audit_log(user["username"], "2fa_disable_failed", "Invalid code")
            self.send_json({"error": "Invalid code"}, 401)
            return

        try:
            with db_lock:
                conn = get_db()
                conn.execute(
                    "UPDATE users SET totp_secret = '', totp_enabled = 0 WHERE id = ?",
                    (user["id"],),
                )
                conn.commit()
                conn.close()
            add_audit_log(user["username"], "2fa_disable", "2FA disabled")
            self.send_json({"success": True, "message": "2FA disabled"})
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def _handle_session_delete(self, user):
        """POST /api/auth/sessions/delete - Kill a session."""
        body = self.read_body()
        if not body:
            self.send_json({"error": "Missing body"}, 400)
            return
        session_id = body.get("id")
        if not session_id:
            self.send_json({"error": "Session id required"}, 400)
            return
        try:
            with db_lock:
                conn = get_db()
                # Get session token to invalidate
                row = conn.execute(
                    "SELECT token FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if row and row["token"]:
                    # Remove from valid_tokens
                    with token_lock:
                        valid_tokens.pop(row["token"], None)
                conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                conn.commit()
                conn.close()
            add_audit_log(user["username"], "session_delete", f"Deleted session {session_id}")
            self.send_json({"success": True, "message": "Session deleted"})
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def _handle_ai_chat(self, user):
        """POST /api/ai/chat - AI assistant."""
        body = self.read_body()
        if not body or "message" not in body:
            self.send_json({"error": "Missing message"}, 400)
            return
        response = ai_assistant_response(body["message"])
        self.send_json({"response": response, "user": user})

    def _handle_register(self, actor):
        """Register a new user. Requires admin+ role."""
        body = self.read_body()
        if not body:
            self.send_json({"error": "Missing body"}, 400)
            return
        username = body.get("username", "").strip()
        password = body.get("password", "")
        role = body.get("role", "viewer")
        if not username or not password:
            self.send_json({"error": "Username and password required"}, 400)
            return
        if role not in ROLE_LEVELS:
            self.send_json({"error": f"Invalid role. Must be one of: {', '.join(ROLE_LEVELS)}"}, 400)
            return
        # Non-owners cannot create owner/admin
        if actor["role"] != "owner" and ROLE_LEVELS.get(role, 0) >= ROLE_LEVELS["admin"]:
            self.send_json({"error": "Only owners can create admin/owner accounts"}, 403)
            return
        pw_hash = hash_password(password)
        try:
            with db_lock:
                conn = get_db()
                conn.execute(
                    "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
                    (username, pw_hash, role),
                )
                conn.commit()
                conn.close()
            add_audit_log(actor["username"], "register_user", f"Created user {username} with role {role}")
            self.send_json({"success": True, "message": f"User '{username}' created"})
        except sqlite3.IntegrityError:
            self.send_json({"error": "Username already exists"}, 409)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def _handle_list_users(self, actor):
        """List all users. Requires admin+."""
        if ROLE_LEVELS.get(actor["role"], 0) < ROLE_LEVELS["admin"]:
            self.send_json({"error": "Insufficient permissions"}, 403)
            return
        try:
            conn = get_db()
            rows = conn.execute(
                "SELECT id, username, role, created_at, last_login FROM users ORDER BY id"
            ).fetchall()
            conn.close()
            users = []
            for r in rows:
                users.append({
                    "id": r["id"],
                    "username": r["username"],
                    "role": r["role"],
                    "created_at": r["created_at"],
                    "last_login": r["last_login"],
                })
            self.send_json({"users": users})
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def _handle_update_user_role(self, actor):
        """Update user role via POST /api/users/role."""
        body = self.read_body()
        if not body:
            self.send_json({"error": "Missing body"}, 400)
            return
        target_id = body.get("id")
        role = body.get("role", "")
        if not target_id or role not in ROLE_LEVELS:
            self.send_json({"error": "Valid user id and role required"}, 400)
            return
        if ROLE_LEVELS.get(actor["role"], 0) < ROLE_LEVELS["owner"]:
            self.send_json({"error": "Owner only"}, 403)
            return
        try:
            with db_lock:
                conn = get_db()
                conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, target_id))
                conn.commit()
                conn.close()
            add_audit_log(actor["username"], "update_user_role", f"User {target_id} -> {role}")
            self.send_json({"success": True, "message": f"User {target_id} role updated to {role}"})
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    # ═════════════════════════════════════════════════════════════════════
    # SERVER LIST HANDLERS
    # ═════════════════════════════════════════════════════════════════════

    def _handle_list_servers(self, user):
        """GET /api/servers - List all servers."""
        servers = get_all_servers()
        result = []
        for srv in servers:
            running = is_server_running(srv["screen_name"])
            status = "running" if running else "stopped"
            # Update DB status
            if status != srv.get("status"):
                try:
                    with db_lock:
                        conn = get_db()
                        conn.execute(
                            "UPDATE servers SET status = ? WHERE id = ?", (status, srv["id"])
                        )
                        conn.commit()
                        conn.close()
                except Exception:
                    pass
            result.append({
                "id": srv["id"],
                "name": srv["name"],
                "path": srv["path"],
                "port": srv["port"],
                "screen_name": srv["screen_name"],
                "jar_name": srv["jar_name"],
                "ram_min": srv["ram_min"],
                "ram_max": srv["ram_max"],
                "status": status,
                "created_at": srv["created_at"],
            })
        self.send_json({"servers": result})

    # ═════════════════════════════════════════════════════════════════════
    # SERVER-SPECIFIC GET HANDLERS
    # ═════════════════════════════════════════════════════════════════════

    def _handle_server_get(self, server_id, rest, query):
        """Route GET /api/servers/{id}/..."""
        # Allow viewer role for read endpoints
        user = self.require_auth()
        if not user:
            return

        srv = get_server_by_id(server_id)
        if not srv:
            self.send_json({"error": "Server not found"}, 404)
            return

        server_dir = srv["path"]
        screen_name = srv["screen_name"]

        if rest == "":
            # GET /api/servers/{id} - Server details
            running = is_server_running(screen_name)
            status = "running" if running else "stopped"
            self.send_json({
                "id": srv["id"],
                "name": srv["name"],
                "path": srv["path"],
                "port": srv["port"],
                "screen_name": srv["screen_name"],
                "jar_name": srv["jar_name"],
                "ram_min": srv["ram_min"],
                "ram_max": srv["ram_max"],
                "status": status,
                "created_at": srv["created_at"],
                "user": user,
            })

        elif rest == "/status":
            # GET /api/servers/{id}/status
            running = is_server_running(screen_name)
            uptime = get_server_uptime(screen_name) if running else None
            tps = get_tps(server_dir, screen_name) if running else 20.0
            ram_used = get_ram_usage(server_dir)
            ram_total = get_ram_total()
            disk_used, disk_total = get_disk_usage(server_dir) if os.path.isdir(server_dir) else (0, 100)
            self.send_json({
                "running": running,
                "uptime": uptime,
                "cpu": get_cpu_usage() if running else 0,
                "ram_used": ram_used,
                "ram_total": ram_total,
                "disk_used": disk_used,
                "disk_total": disk_total,
                "java_version": get_java_version(),
                "tps": tps,
                "user": user,
            })

        elif rest == "/console":
            # GET /api/servers/{id}/console
            lines = int(query.get("lines", ["200"])[0])
            logs = get_console_logs(server_dir, lines)
            self.send_json({"logs": logs, "user": user})

        elif rest == "/players":
            # GET /api/servers/{id}/players
            players = get_online_players(server_dir)
            self.send_json({"players": players, "count": len(players), "user": user})

        elif rest == "/files":
            # GET /api/servers/{id}/files
            file_path = query.get("path", ["/"])[0]
            self._serve_file_listing(server_dir, file_path, user)

        elif rest == "/file/read":
            # GET /api/servers/{id}/file/read
            file_path = query.get("path", [""])[0]
            self._serve_file_read(server_dir, file_path, user)

        elif rest == "/file/download":
            # GET /api/servers/{id}/file/download
            file_path = query.get("path", [""])[0]
            self._serve_file_download(server_dir, file_path, user)

        elif rest == "/properties":
            # GET /api/servers/{id}/properties
            self.send_json({"properties": read_properties(server_dir), "user": user})

        elif rest == "/plugins":
            # GET /api/servers/{id}/plugins
            plugins_dir = os.path.join(server_dir, "plugins")
            plugins = []
            if os.path.exists(plugins_dir):
                for f in sorted(os.listdir(plugins_dir)):
                    if f.endswith(".jar"):
                        fp = os.path.join(plugins_dir, f)
                        try:
                            stat = os.stat(fp)
                        except OSError:
                            continue
                        plugins.append({
                            "name": f,
                            "size": stat.st_size,
                            "size_formatted": format_bytes(stat.st_size),
                            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                        })
            self.send_json({"plugins": plugins, "user": user})

        elif rest == "/ops":
            # GET /api/servers/{id}/ops
            self._serve_ops(server_dir, user)

        elif rest == "/banned":
            # GET /api/servers/{id}/banned
            self._serve_banned(server_dir, user)

        elif rest == "/schedules":
            # GET /api/servers/{id}/schedules
            self._serve_schedules(server_id, user)

        elif rest == "/logs":
            # GET /api/servers/{id}/logs
            search = query.get("search", [""])[0]
            lines = int(query.get("lines", ["200"])[0])
            self._serve_logs(server_dir, search, lines, user)

        elif rest == "/info":
            # GET /api/servers/{id}/info
            self._serve_server_info(server_dir, user)

        elif rest == "/stats":
            # GET /api/servers/{id}/stats
            self.send_json({**get_stats(server_dir), "user": user})

        elif rest == "/analytics":
            # GET /api/servers/{id}/analytics?hours=24
            hours = int(query.get("hours", ["24"])[0])
            try:
                conn = get_db()
                rows = conn.execute(
                    "SELECT * FROM analytics WHERE server_id = ? "
                    "AND timestamp >= datetime('now', ?) ORDER BY timestamp ASC",
                    (server_id, f'-{hours} hours'),
                ).fetchall()
                conn.close()
                points = []
                for r in rows:
                    points.append({
                        "id": r["id"],
                        "server_id": r["server_id"],
                        "players": r["players"],
                        "tps": r["tps"],
                        "cpu": r["cpu"],
                        "ram_used": r["ram_used"],
                        "ram_total": r["ram_total"],
                        "timestamp": r["timestamp"],
                    })
                self.send_json({"analytics": points, "count": len(points), "user": user})
            except Exception as e:
                self.send_json({"error": str(e), "user": user})

        elif rest == "/alerts":
            # GET /api/servers/{id}/alerts
            try:
                conn = get_db()
                rows = conn.execute(
                    "SELECT * FROM alerts WHERE server_id = ? ORDER BY id",
                    (server_id,),
                ).fetchall()
                conn.close()
                alerts_list = []
                for r in rows:
                    alerts_list.append({
                        "id": r["id"],
                        "server_id": r["server_id"],
                        "alert_type": r["alert_type"],
                        "threshold": r["threshold"],
                        "enabled": bool(r["enabled"]),
                        "cooldown_seconds": r["cooldown_seconds"],
                        "last_triggered": r["last_triggered"],
                    })
                self.send_json({"alerts": alerts_list, "user": user})
            except Exception as e:
                self.send_json({"error": str(e), "user": user})

        elif rest == "/alert-history":
            # GET /api/servers/{id}/alert-history
            try:
                conn = get_db()
                rows = conn.execute(
                    "SELECT * FROM alert_history WHERE server_id = ? ORDER BY id DESC LIMIT 50",
                    (server_id,),
                ).fetchall()
                conn.close()
                history = []
                for r in rows:
                    history.append({
                        "id": r["id"],
                        "server_id": r["server_id"],
                        "alert_type": r["alert_type"],
                        "message": r["message"],
                        "triggered_at": r["triggered_at"],
                    })
                self.send_json({"history": history, "user": user})
            except Exception as e:
                self.send_json({"error": str(e), "user": user})

        elif rest == "/chat":
            # GET /api/servers/{id}/chat?lines=50
            lines = int(query.get("lines", ["50"])[0])
            messages = get_chat_messages(server_dir, lines)
            self.send_json({"messages": messages, "count": len(messages), "user": user})

        elif rest == "/motd/preview":
            # GET /api/servers/{id}/motd/preview
            self.send_json({"error": "Use POST for MOTD preview", "user": user})

        else:
            self.send_json({"error": "Not found"}, 404)

    # ═════════════════════════════════════════════════════════════════════
    # SERVER-SPECIFIC POST HANDLERS
    # ═════════════════════════════════════════════════════════════════════

    def _handle_server_post(self, server_id, rest):
        """Route POST /api/servers/..."""

        # POST /api/servers (create new server)
        if server_id is None and rest == "" or (server_id is not None and rest == ""):
            # This is actually handled by the parent, but just in case:
            # Create server endpoint is POST /api/servers with no ID
            pass

        # If server_id is None, this is POST /api/servers (create)
        if server_id is None:
            user = self.require_role("admin")
            if not user:
                return
            self._handle_create_server(user)
            return

        # For server-specific endpoints, require at least moderator
        user = self.require_role("moderator")
        if not user:
            return

        srv = get_server_by_id(server_id)
        if not srv:
            self.send_json({"error": "Server not found"}, 404)
            return

        server_dir = srv["path"]
        screen_name = srv["screen_name"]

        if rest == "/start":
            ok, msg = start_server(server_id)
            add_audit_log(user["username"], "start_server", f"Server {srv['name']}: {msg}")
            self.send_json({"success": ok, "message": msg, "user": user})

        elif rest == "/stop":
            ok, msg = stop_server(server_id)
            add_audit_log(user["username"], "stop_server", f"Server {srv['name']}: {msg}")
            self.send_json({"success": ok, "message": msg, "user": user})

        elif rest == "/restart":
            ok, msg = restart_server(server_id)
            add_audit_log(user["username"], "restart_server", f"Server {srv['name']}: {msg}")
            self.send_json({"success": ok, "message": msg, "user": user})

        elif rest == "/command":
            # Require moderator+
            body = self.read_body()
            cmd = body.get("command", "") if body else ""
            if not cmd:
                self.send_json({"success": False, "message": "No command", "user": user})
                return
            if send_screen_cmd(cmd, screen_name):
                add_audit_log(user["username"], "server_command", f"Server {srv['name']}: {cmd}")
                self.send_json({"success": True, "message": f"Sent: {cmd}", "user": user})
            else:
                self.send_json({"success": False, "message": "Failed to send command", "user": user})

        elif rest == "/backup":
            try:
                if not os.path.isdir(server_dir):
                    self.send_json({"success": False, "message": "Server directory not found", "user": user})
                    return
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_name = f"backup_{timestamp}.tar.gz"
                subprocess.run(
                    ["tar", "-czf", backup_name,
                     "--exclude=logs", "--exclude=*.jar", "--exclude=cache",
                     "world", "world_nether", "world_the_end",
                     "server.properties", "ops.json", "whitelist.json",
                     "banned-players.json", "banned-ips.json"],
                    capture_output=True, text=True, timeout=300, cwd=server_dir,
                )
                backup_path = os.path.join(server_dir, backup_name)
                size = os.path.getsize(backup_path) if os.path.exists(backup_path) else 0
                add_audit_log(user["username"], "backup", f"Server {srv['name']}: {backup_name}")
                self.send_json({
                    "success": True,
                    "message": f"Backup created: {backup_name} ({format_bytes(size)})",
                    "user": user,
                })
            except Exception as e:
                self.send_json({"success": False, "message": f"Backup failed: {str(e)}", "user": user})

        elif rest == "/say":
            body = self.read_body()
            msg = body.get("message", "") if body else ""
            if not msg:
                self.send_json({"success": False, "message": "No message", "user": user})
                return
            send_screen_cmd(f"say {msg}", screen_name)
            self.send_json({"success": True, "message": "Broadcast sent", "user": user})

        elif rest == "/kick":
            body = self.read_body()
            if not body:
                self.send_json({"success": False, "message": "Missing body", "user": user})
                return
            player = body.get("player", "")
            reason = body.get("reason", "Kicked by admin")
            send_screen_cmd(f"kick {player} {reason}", screen_name)
            add_audit_log(user["username"], "kick", f"Kicked {player}")
            self.send_json({"success": True, "message": f"Kicked {player}", "user": user})

        elif rest == "/ban":
            body = self.read_body()
            if not body:
                self.send_json({"success": False, "message": "Missing body", "user": user})
                return
            player = body.get("player", "")
            reason = body.get("reason", "Banned by admin")
            send_screen_cmd(f"ban {player} {reason}", screen_name)
            add_audit_log(user["username"], "ban", f"Banned {player}")
            self.send_json({"success": True, "message": f"Banned {player}", "user": user})

        elif rest == "/unban":
            body = self.read_body()
            if not body:
                self.send_json({"success": False, "message": "Missing body", "user": user})
                return
            player = body.get("player", "")
            send_screen_cmd(f"pardon {player}", screen_name)
            add_audit_log(user["username"], "unban", f"Unbanned {player}")
            self.send_json({"success": True, "message": f"Unbanned {player}", "user": user})

        elif rest == "/op":
            body = self.read_body()
            if not body:
                self.send_json({"success": False, "message": "Missing body", "user": user})
                return
            player = body.get("player", "")
            send_screen_cmd(f"op {player}", screen_name)
            self.send_json({"success": True, "message": f"Opped {player}", "user": user})

        elif rest == "/deop":
            body = self.read_body()
            if not body:
                self.send_json({"success": False, "message": "Missing body", "user": user})
                return
            player = body.get("player", "")
            send_screen_cmd(f"deop {player}", screen_name)
            self.send_json({"success": True, "message": f"Deopped {player}", "user": user})

        elif rest == "/whitelist":
            body = self.read_body()
            if not body:
                self.send_json({"success": False, "message": "Missing body", "user": user})
                return
            action = body.get("action", "")
            player = body.get("player", "")
            if action == "add":
                send_screen_cmd(f"whitelist add {player}", screen_name)
                self.send_json({"success": True, "message": f"Added {player} to whitelist", "user": user})
            elif action == "remove":
                send_screen_cmd(f"whitelist remove {player}", screen_name)
                self.send_json({"success": True, "message": f"Removed {player} from whitelist", "user": user})
            elif action == "on":
                send_screen_cmd("whitelist on", screen_name)
                self.send_json({"success": True, "message": "Whitelist enabled", "user": user})
            elif action == "off":
                send_screen_cmd("whitelist off", screen_name)
                self.send_json({"success": True, "message": "Whitelist disabled", "user": user})
            else:
                self.send_json({"success": False, "message": "Invalid action", "user": user})

        elif rest == "/gamemode":
            body = self.read_body()
            if not body:
                self.send_json({"success": False, "message": "Missing body", "user": user})
                return
            player = body.get("player", "")
            mode = body.get("mode", "survival")
            send_screen_cmd(f"gamemode {mode} {player}", screen_name)
            self.send_json({"success": True, "message": f"Set {player} to {mode}", "user": user})

        elif rest == "/tp":
            body = self.read_body()
            if not body:
                self.send_json({"success": False, "message": "Missing body", "user": user})
                return
            player = body.get("player", "")
            target = body.get("target", "")
            send_screen_cmd(f"tp {player} {target}", screen_name)
            self.send_json({"success": True, "message": f"Teleported {player}", "user": user})

        elif rest == "/properties":
            body = self.read_body()
            if not body or "properties" not in body:
                self.send_json({"success": False, "message": "Missing properties", "user": user})
                return
            if save_properties_file(server_dir, body["properties"]):
                add_audit_log(user["username"], "save_properties", f"Server {srv['name']}")
                self.send_json({"success": True, "message": "Properties saved", "user": user})
            else:
                self.send_json({"success": False, "message": "Failed to save properties", "user": user})

        elif rest == "/file/save":
            body = self.read_body()
            if not body:
                self.send_json({"success": False, "message": "Missing body", "user": user})
                return
            rel_path = body.get("path", "")
            content = body.get("content", "")
            abs_path = safe_path(server_dir, rel_path)
            if not abs_path:
                self.send_json({"success": False, "message": "Access denied", "user": user})
                return
            try:
                os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                with open(abs_path, "w", encoding="utf-8") as f:
                    f.write(content)
                add_audit_log(user["username"], "file_save", f"Server {srv['name']}: {rel_path}")
                self.send_json({"success": True, "message": "File saved", "user": user})
            except Exception as e:
                self.send_json({"success": False, "message": str(e), "user": user})

        elif rest == "/file/delete":
            body = self.read_body()
            if not body:
                self.send_json({"success": False, "message": "Missing body", "user": user})
                return
            rel_path = body.get("path", "")
            abs_path = safe_path(server_dir, rel_path)
            if not abs_path:
                self.send_json({"success": False, "message": "Access denied", "user": user})
                return
            try:
                if os.path.isdir(abs_path):
                    shutil.rmtree(abs_path)
                else:
                    os.remove(abs_path)
                add_audit_log(user["username"], "file_delete", f"Server {srv['name']}: {rel_path}")
                self.send_json({"success": True, "message": "Deleted", "user": user})
            except Exception as e:
                self.send_json({"success": False, "message": str(e), "user": user})

        elif rest == "/file/rename":
            body = self.read_body()
            if not body:
                self.send_json({"success": False, "message": "Missing body", "user": user})
                return
            old_rel = body.get("old_path", "")
            new_rel = body.get("new_path", "")
            old_abs = safe_path(server_dir, old_rel)
            new_abs = safe_path(server_dir, new_rel)
            if not old_abs or not new_abs:
                self.send_json({"success": False, "message": "Access denied", "user": user})
                return
            try:
                os.rename(old_abs, new_abs)
                add_audit_log(user["username"], "file_rename", f"{old_rel} -> {new_rel}")
                self.send_json({"success": True, "message": "Renamed", "user": user})
            except Exception as e:
                self.send_json({"success": False, "message": str(e), "user": user})

        elif rest == "/file/mkdir":
            body = self.read_body()
            if not body:
                self.send_json({"success": False, "message": "Missing body", "user": user})
                return
            rel_path = body.get("path", "")
            abs_path = safe_path(server_dir, rel_path)
            if not abs_path:
                self.send_json({"success": False, "message": "Access denied", "user": user})
                return
            try:
                os.makedirs(abs_path, exist_ok=True)
                self.send_json({"success": True, "message": "Directory created", "user": user})
            except Exception as e:
                self.send_json({"success": False, "message": str(e), "user": user})

        elif rest == "/file/upload":
            self._handle_file_upload(server_dir, srv["name"], user)

        elif rest == "/plugins/delete":
            body = self.read_body()
            if not body:
                self.send_json({"success": False, "message": "Missing body", "user": user})
                return
            name = body.get("name", "")
            plugin_path = os.path.join(server_dir, "plugins", name)
            if not os.path.exists(plugin_path):
                self.send_json({"success": False, "message": "Plugin not found", "user": user})
                return
            try:
                os.remove(plugin_path)
                add_audit_log(user["username"], "plugin_delete", f"Server {srv['name']}: {name}")
                self.send_json({"success": True, "message": f"Deleted {name}", "user": user})
            except Exception as e:
                self.send_json({"success": False, "message": str(e), "user": user})

        elif rest == "/schedules":
            # POST /api/servers/{id}/schedules - create schedule
            body = self.read_body()
            if not body:
                self.send_json({"success": False, "message": "Missing body", "user": user})
                return
            name = body.get("name", "").strip()
            command = body.get("command", "").strip()
            interval = int(body.get("interval_seconds", 300))
            if not name or not command:
                self.send_json({"success": False, "message": "Name and command required", "user": user})
                return
            try:
                with db_lock:
                    conn = get_db()
                    conn.execute(
                        "INSERT INTO schedules (server_id, name, command, interval_seconds) VALUES (?, ?, ?, ?)",
                        (server_id, name, command, interval),
                    )
                    conn.commit()
                    conn.close()
                self.send_json({"success": True, "message": f"Schedule '{name}' created", "user": user})
            except Exception as e:
                self.send_json({"success": False, "message": str(e), "user": user})

        elif rest == "/schedules/delete":
            body = self.read_body()
            if not body:
                self.send_json({"success": False, "message": "Missing body", "user": user})
                return
            sched_id = body.get("id")
            if not sched_id:
                self.send_json({"success": False, "message": "Schedule id required", "user": user})
                return
            try:
                with db_lock:
                    conn = get_db()
                    conn.execute(
                        "DELETE FROM schedules WHERE id = ? AND server_id = ?",
                        (sched_id, server_id),
                    )
                    conn.commit()
                    conn.close()
                self.send_json({"success": True, "message": "Schedule deleted", "user": user})
            except Exception as e:
                self.send_json({"success": False, "message": str(e), "user": user})

        elif rest == "/schedules/toggle":
            body = self.read_body()
            if not body:
                self.send_json({"success": False, "message": "Missing body", "user": user})
                return
            sched_id = body.get("id")
            if not sched_id:
                self.send_json({"success": False, "message": "Schedule id required", "user": user})
                return
            try:
                with db_lock:
                    conn = get_db()
                    row = conn.execute(
                        "SELECT enabled FROM schedules WHERE id = ? AND server_id = ?",
                        (sched_id, server_id),
                    ).fetchone()
                    if row:
                        new_val = 0 if row["enabled"] == 1 else 1
                        conn.execute(
                            "UPDATE schedules SET enabled = ? WHERE id = ?",
                            (new_val, sched_id),
                        )
                        conn.commit()
                    conn.close()
                self.send_json({"success": True, "message": "Schedule toggled", "user": user})
            except Exception as e:
                self.send_json({"success": False, "message": str(e), "user": user})

        elif rest == "/alerts":
            # POST /api/servers/{id}/alerts - create alert
            body = self.read_body()
            if not body:
                self.send_json({"success": False, "message": "Missing body", "user": user})
                return
            alert_type = body.get("alert_type", "").strip()
            threshold = float(body.get("threshold", 0))
            cooldown = int(body.get("cooldown_seconds", 300))
            if not alert_type:
                self.send_json({"success": False, "message": "Alert type required", "user": user})
                return
            try:
                with db_lock:
                    conn = get_db()
                    conn.execute(
                        "INSERT INTO alerts (server_id, alert_type, threshold, cooldown_seconds) VALUES (?, ?, ?, ?)",
                        (server_id, alert_type, threshold, cooldown),
                    )
                    conn.commit()
                    conn.close()
                add_audit_log(user["username"], "create_alert", f"Server {srv['name']}: {alert_type}")
                self.send_json({"success": True, "message": f"Alert '{alert_type}' created", "user": user})
            except Exception as e:
                self.send_json({"success": False, "message": str(e), "user": user})

        elif rest == "/alerts/delete":
            # POST /api/servers/{id}/alerts/delete - delete alert
            body = self.read_body()
            if not body:
                self.send_json({"success": False, "message": "Missing body", "user": user})
                return
            alert_id = body.get("id")
            if not alert_id:
                self.send_json({"success": False, "message": "Alert id required", "user": user})
                return
            try:
                with db_lock:
                    conn = get_db()
                    conn.execute(
                        "DELETE FROM alerts WHERE id = ? AND server_id = ?",
                        (alert_id, server_id),
                    )
                    conn.commit()
                    conn.close()
                self.send_json({"success": True, "message": "Alert deleted", "user": user})
            except Exception as e:
                self.send_json({"success": False, "message": str(e), "user": user})

        elif rest == "/alerts/toggle":
            # POST /api/servers/{id}/alerts/toggle - toggle alert
            body = self.read_body()
            if not body:
                self.send_json({"success": False, "message": "Missing body", "user": user})
                return
            alert_id = body.get("id")
            if not alert_id:
                self.send_json({"success": False, "message": "Alert id required", "user": user})
                return
            try:
                with db_lock:
                    conn = get_db()
                    row = conn.execute(
                        "SELECT enabled FROM alerts WHERE id = ? AND server_id = ?",
                        (alert_id, server_id),
                    ).fetchone()
                    if row:
                        new_val = 0 if row["enabled"] == 1 else 1
                        conn.execute(
                            "UPDATE alerts SET enabled = ? WHERE id = ?",
                            (new_val, alert_id),
                        )
                        conn.commit()
                    conn.close()
                self.send_json({"success": True, "message": "Alert toggled", "user": user})
            except Exception as e:
                self.send_json({"success": False, "message": str(e), "user": user})

        elif rest == "/motd/preview":
            # POST /api/servers/{id}/motd/preview
            body = self.read_body()
            if not body or "motd" not in body:
                self.send_json({"error": "Missing motd", "user": user})
                return
            html = motd_to_html(body["motd"])
            self.send_json({"html": html, "user": user})

        else:
            self.send_json({"error": "Not found"}, 404)

    # ═════════════════════════════════════════════════════════════════════
    # CREATE SERVER
    # ═════════════════════════════════════════════════════════════════════

    def _handle_create_server(self, user):
        """POST /api/servers - Create a new server."""
        body = self.read_body()
        if not body:
            self.send_json({"error": "Missing body"}, 400)
            return
        name = body.get("name", "").strip()
        if not name:
            self.send_json({"error": "Server name required"}, 400)
            return

        port = int(body.get("port", 25565))
        ram_min = body.get("ram_min", "2G")
        ram_max = body.get("ram_max", "6G")
        jar_name = body.get("jar_name", "server.jar")
        mc_version = body.get("mc_version", "")

        # Generate screen name from name (alphanumeric + underscore)
        screen_name = re.sub(r"[^a-zA-Z0-9_]", "_", name.lower())[:30]
        # Make unique
        existing = get_all_servers()
        existing_screens = {s["screen_name"] for s in existing}
        counter = 1
        base_screen = screen_name
        while screen_name in existing_screens:
            screen_name = f"{base_screen}_{counter}"
            counter += 1

        # Server path
        server_path = os.path.abspath(os.path.join(PROJECT_DIR, name))
        if not os.path.exists(server_path):
            try:
                os.makedirs(server_path, exist_ok=True)
            except Exception as e:
                self.send_json({"error": f"Cannot create directory: {str(e)}"}, 500)
                return

        try:
            with db_lock:
                conn = get_db()
                conn.execute(
                    "INSERT INTO servers (name, path, port, screen_name, jar_name, ram_min, ram_max) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (name, server_path, port, screen_name, jar_name, ram_min, ram_max),
                )
                conn.commit()
                conn.close()
            add_audit_log(user["username"], "create_server", f"Created server: {name} at {server_path}")
            self.send_json({
                "success": True,
                "message": f"Server '{name}' created",
                "server": {
                    "name": name,
                    "path": server_path,
                    "port": port,
                    "screen_name": screen_name,
                    "jar_name": jar_name,
                    "ram_min": ram_min,
                    "ram_max": ram_max,
                },
            })
        except sqlite3.IntegrityError as e:
            if "name" in str(e):
                self.send_json({"error": "Server name already exists"}, 409)
            elif "screen_name" in str(e):
                self.send_json({"error": "Screen name conflict"}, 409)
            else:
                self.send_json({"error": str(e)}, 500)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    # ═════════════════════════════════════════════════════════════════════
    # FILE SERVING HELPERS
    # ═════════════════════════════════════════════════════════════════════

    def _serve_file_listing(self, server_dir, rel_path, user):
        """List files in a directory for a server."""
        if not os.path.isdir(server_dir):
            self.send_json({"error": "Server directory not found", "user": user})
            return
        abs_path = safe_path(server_dir, rel_path)
        if not abs_path:
            self.send_json({"error": "Access denied", "user": user}, 403)
            return
        if not os.path.exists(abs_path) or not os.path.isdir(abs_path):
            self.send_json({"error": "Directory not found", "user": user})
            return

        files = []
        try:
            for entry in sorted(os.listdir(abs_path)):
                full = os.path.join(abs_path, entry)
                try:
                    stat = os.stat(full)
                except OSError:
                    continue
                is_dir = os.path.isdir(full)
                if is_dir:
                    size = get_dir_size(full)
                else:
                    size = stat.st_size
                rel = os.path.relpath(full, server_dir)
                files.append({
                    "name": entry,
                    "path": "/" + rel.replace(os.sep, "/"),
                    "is_dir": is_dir,
                    "size": size,
                    "size_formatted": format_bytes(size),
                    "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                })
        except PermissionError:
            self.send_json({"error": "Permission denied", "user": user}, 403)
            return
        self.send_json({"files": files, "user": user})

    def _serve_file_read(self, server_dir, rel_path, user):
        """Read a file from a server directory."""
        if not os.path.isdir(server_dir):
            self.send_json({"error": "Server directory not found", "user": user})
            return
        abs_path = safe_path(server_dir, rel_path)
        if not abs_path:
            self.send_json({"error": "Access denied", "user": user}, 403)
            return
        if not os.path.exists(abs_path) or os.path.isdir(abs_path):
            self.send_json({"error": "File not found", "user": user})
            return
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(100000)  # Limit to 100KB
            self.send_json({"content": content, "path": rel_path, "user": user})
        except Exception as e:
            self.send_json({"error": str(e), "user": user})

    def _serve_file_download(self, server_dir, rel_path, user):
        """Download a file from a server directory."""
        if not os.path.isdir(server_dir):
            self.send_json({"error": "Server directory not found", "user": user})
            return
        abs_path = safe_path(server_dir, rel_path)
        if not abs_path:
            self.send_json({"error": "Access denied", "user": user}, 403)
            return
        if not os.path.exists(abs_path):
            self.send_json({"error": "File not found", "user": user})
            return
        try:
            with open(abs_path, "rb") as f:
                data = f.read(50 * 1024 * 1024)  # Max 50MB
            filename = os.path.basename(abs_path)
            self.send_binary(data, filename)
        except Exception as e:
            self.send_json({"error": str(e), "user": user})

    def _handle_file_upload(self, server_dir, server_name, user):
        """Handle multipart file upload to a server directory."""
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self.send_json({"success": False, "message": "Expected multipart form data", "user": user})
            return

        boundary = content_type.split("boundary=")[-1].strip()
        raw_body = self.read_raw_body()

        # Extract path field and file data
        upload_subdir = ""
        file_data = None
        file_name = None

        text_body = raw_body.decode("utf-8", errors="replace")
        parts = text_body.split(f"--{boundary}")
        for part in parts:
            if 'Content-Disposition' in part:
                name_match = re.search(r'name="([^"]*)"', part)
                if not name_match:
                    continue
                field_name = name_match.group(1)
                if field_name == "file":
                    if 'filename="' in part:
                        file_name = part.split('filename="')[1].split('"')[0]
                    if "\r\n\r\n" in part:
                        file_data = part.split("\r\n\r\n", 1)[1]
                        file_data = file_data.rsplit("\r\n", 1)[0]
                elif field_name == "path":
                    if "\r\n\r\n" in part:
                        path_val = part.split("\r\n\r\n", 1)[1].rsplit("\r\n", 1)[0].strip()
                        if path_val and path_val != "/":
                            upload_subdir = path_val.lstrip("/")

        if not file_name or not file_data:
            self.send_json({"success": False, "message": "No file data", "user": user})
            return

        # Security: validate path
        if upload_subdir:
            target_dir = safe_path(server_dir, upload_subdir)
        else:
            target_dir = server_dir

        if not target_dir:
            self.send_json({"success": False, "message": "Access denied", "user": user})
            return

        try:
            os.makedirs(target_dir, exist_ok=True)
            save_path = os.path.join(target_dir, file_name)
            # Additional security check
            if not os.path.abspath(save_path).startswith(os.path.abspath(server_dir) + os.sep) and \
               os.path.abspath(save_path) != os.path.abspath(server_dir):
                self.send_json({"success": False, "message": "Access denied", "user": user})
                return
            with open(save_path, "wb") as f:
                f.write(file_data.encode("latin-1"))
            add_audit_log(user["username"], "file_upload", f"Server {server_name}: {file_name}")
            self.send_json({"success": True, "message": f"Uploaded {file_name}", "user": user})
        except Exception as e:
            self.send_json({"success": False, "message": str(e), "user": user})

    # ═════════════════════════════════════════════════════════════════════
    # OPS, BANNED, SCHEDULES, LOGS, INFO HELPERS
    # ═════════════════════════════════════════════════════════════════════

    def _serve_ops(self, server_dir, user):
        """Read ops.json from a server directory."""
        ops = []
        ops_file = os.path.join(server_dir, "ops.json")
        if os.path.exists(ops_file):
            try:
                with open(ops_file, "r") as f:
                    data = json.load(f)
                    ops = [entry.get("name", "") for entry in data if entry.get("name")]
            except Exception:
                pass
        self.send_json({"ops": ops, "user": user})

    def _serve_banned(self, server_dir, user):
        """Read banned-players.json from a server directory."""
        banned = []
        ban_file = os.path.join(server_dir, "banned-players.json")
        if os.path.exists(ban_file):
            try:
                with open(ban_file, "r") as f:
                    data = json.load(f)
                    banned = [
                        {"name": e.get("name", ""), "reason": e.get("reason", "")}
                        for e in data if e.get("name")
                    ]
            except Exception:
                pass
        self.send_json({"banned": banned, "user": user})

    def _serve_schedules(self, server_id, user):
        """List schedules for a server."""
        try:
            conn = get_db()
            rows = conn.execute(
                "SELECT * FROM schedules WHERE server_id = ? ORDER BY id",
                (server_id,),
            ).fetchall()
            conn.close()
            schedules = []
            for r in rows:
                schedules.append({
                    "id": r["id"],
                    "server_id": r["server_id"],
                    "name": r["name"],
                    "command": r["command"],
                    "interval_seconds": r["interval_seconds"],
                    "enabled": bool(r["enabled"]),
                    "last_run": r["last_run"] or "Never",
                    "created_at": r["created_at"],
                })
            self.send_json({"schedules": schedules, "user": user})
        except Exception as e:
            self.send_json({"error": str(e), "user": user})

    def _serve_logs(self, server_dir, search, lines, user):
        """Serve server logs with optional search."""
        log_file = os.path.join(server_dir, "logs", "latest.log")
        if not os.path.exists(log_file):
            self.send_json({"lines": [], "total": 0, "search": search, "user": user})
            return
        try:
            result = subprocess.run(
                ["tail", f"-{lines}", log_file],
                capture_output=True, text=True, timeout=10,
            )
            all_lines = result.stdout.split("\n")
            if search:
                filtered = [l for l in all_lines if search.lower() in l.lower()]
            else:
                filtered = all_lines
            self.send_json({"lines": filtered, "total": len(filtered), "search": search, "user": user})
        except Exception as e:
            self.send_json({"error": str(e), "user": user})

    def _serve_server_info(self, server_dir, user):
        """Serve server info: version, paper, worlds, java, plugins, JAR info, scheduled tasks."""
        props = read_properties(server_dir)
        version = get_server_version(server_dir)
        paper_ver = get_paper_version(server_dir)
        world_sizes = get_world_sizes(server_dir) if os.path.isdir(server_dir) else {}
        plugins = get_installed_plugins(server_dir) if os.path.isdir(server_dir) else []

        # Calculate total plugins size
        plugins_dir = os.path.join(server_dir, "plugins")
        total_plugins_size = 0
        if os.path.isdir(plugins_dir):
            for f in os.listdir(plugins_dir):
                if f.endswith(".jar"):
                    fp = os.path.join(plugins_dir, f)
                    try:
                        total_plugins_size += os.path.getsize(fp)
                    except OSError:
                        pass

        # Get JAR file info
        jar_name = props.get("")  # fallback
        jar_size = 0
        if os.path.isdir(server_dir):
            for f in os.listdir(server_dir):
                if f.endswith(".jar"):
                    jar_name = f
                    try:
                        jar_size = os.path.getsize(f)
                    except OSError:
                        pass
                    break
        jar_path_full = os.path.join(server_dir, jar_name) if jar_name else ""
        if jar_path_full and os.path.exists(jar_path_full):
            try:
                jar_size = os.path.getsize(jar_path_full)
            except OSError:
                pass

        # Count scheduled tasks
        scheduled_count = 0
        # We'll pass server_id through a different approach - use srv from caller context
        # For now, count from the first server or use a generic approach
        try:
            conn = get_db()
            # Find the server_id for this server_dir
            row = conn.execute("SELECT id FROM servers WHERE path = ?", (server_dir,)).fetchone()
            if row:
                scheduled_count = conn.execute(
                    "SELECT COUNT(*) as cnt FROM schedules WHERE server_id = ? AND enabled = 1",
                    (row["id"],),
                ).fetchone()["cnt"]
            conn.close()
        except Exception:
            pass

        self.send_json({
            "server_version": version,
            "paper_version": paper_ver,
            "ip": props.get("server-ip", "*"),
            "port": int(props.get("server-port", "25565")),
            "max_players": int(props.get("max-players", "20")),
            "online_mode": props.get("online-mode", "true"),
            "motd": props.get("motd", "A Minecraft Server"),
            "view_distance": props.get("view-distance", "10"),
            "world_sizes": world_sizes,
            "plugins": plugins,
            "plugin_count": len(plugins),
            "plugins_size": total_plugins_size,
            "plugins_size_formatted": format_bytes(total_plugins_size),
            "jar_name": jar_name,
            "jar_size": jar_size,
            "jar_size_formatted": format_bytes(jar_size),
            "scheduled_tasks_count": scheduled_count,
            "java_version": get_java_version(),
            "user": user,
        })

    # ═════════════════════════════════════════════════════════════════════
    # BACKWARDS COMPAT HANDLERS (old /api/* endpoints -> default server)
    # ═════════════════════════════════════════════════════════════════════

    def _get_default_srv(self):
        """Get default server dict or None."""
        sid = get_default_server_id()
        if sid is None:
            return None
        return get_server_by_id(sid)

    def _compat_status(self, default_id, user):
        if default_id is None:
            self.send_json({"error": "No servers configured", "user": user})
            return
        srv = get_server_by_id(default_id)
        if not srv:
            self.send_json({"error": "Default server not found", "user": user})
            return
        running = is_server_running(srv["screen_name"])
        uptime = get_server_uptime(srv["screen_name"]) if running else None
        tps = get_tps(srv["path"], srv["screen_name"]) if running else 20.0
        ram_used = get_ram_usage(srv["path"])
        ram_total = get_ram_total()
        disk_used, disk_total = get_disk_usage(srv["path"]) if os.path.isdir(srv["path"]) else (0, 100)
        self.send_json({
            "running": running,
            "uptime": uptime,
            "cpu": get_cpu_usage() if running else 0,
            "ram_used": ram_used,
            "ram_total": ram_total,
            "disk_used": disk_used,
            "disk_total": disk_total,
            "java_version": get_java_version(),
            "tps": tps,
            "user": user,
        })

    def _compat_console(self, default_id, lines, user):
        if default_id is None:
            self.send_json({"logs": "No servers configured", "user": user})
            return
        srv = get_server_by_id(default_id)
        if not srv:
            self.send_json({"logs": "Default server not found", "user": user})
            return
        self.send_json({"logs": get_console_logs(srv["path"], lines), "user": user})

    def _compat_players(self, default_id, user):
        if default_id is None:
            self.send_json({"players": [], "count": 0, "user": user})
            return
        srv = get_server_by_id(default_id)
        if not srv:
            self.send_json({"players": [], "count": 0, "user": user})
            return
        players = get_online_players(srv["path"])
        self.send_json({"players": players, "count": len(players), "user": user})

    def _compat_stats(self, default_id, user):
        if default_id is None:
            self.send_json({"total_joins": 0, "deaths": 0, "advancements": 0, "chat_messages": 0, "user": user})
            return
        srv = get_server_by_id(default_id)
        if not srv:
            self.send_json({"total_joins": 0, "deaths": 0, "advancements": 0, "chat_messages": 0, "user": user})
            return
        self.send_json({**get_stats(srv["path"]), "user": user})

    def _compat_files(self, default_id, rel_path, user):
        if default_id is None:
            self.send_json({"error": "No servers configured", "user": user})
            return
        srv = get_server_by_id(default_id)
        if not srv:
            self.send_json({"error": "Default server not found", "user": user})
            return
        self._serve_file_listing(srv["path"], rel_path, user)

    def _compat_file_read(self, default_id, rel_path, user):
        if default_id is None:
            self.send_json({"error": "No servers configured", "user": user})
            return
        srv = get_server_by_id(default_id)
        if not srv:
            self.send_json({"error": "Default server not found", "user": user})
            return
        self._serve_file_read(srv["path"], rel_path, user)

    def _compat_file_download(self, default_id, rel_path, user):
        if default_id is None:
            self.send_json({"error": "No servers configured", "user": user})
            return
        srv = get_server_by_id(default_id)
        if not srv:
            self.send_json({"error": "Default server not found", "user": user})
            return
        self._serve_file_download(srv["path"], rel_path, user)

    def _compat_properties(self, default_id, user):
        if default_id is None:
            self.send_json({"properties": {}, "user": user})
            return
        srv = get_server_by_id(default_id)
        if not srv:
            self.send_json({"properties": {}, "user": user})
            return
        self.send_json({"properties": read_properties(srv["path"]), "user": user})

    def _compat_ops(self, default_id, user):
        if default_id is None:
            self.send_json({"ops": [], "user": user})
            return
        srv = get_server_by_id(default_id)
        if not srv:
            self.send_json({"ops": [], "user": user})
            return
        self._serve_ops(srv["path"], user)

    def _compat_banned(self, default_id, user):
        if default_id is None:
            self.send_json({"banned": [], "user": user})
            return
        srv = get_server_by_id(default_id)
        if not srv:
            self.send_json({"banned": [], "user": user})
            return
        self._serve_banned(srv["path"], user)

    def _compat_plugins(self, default_id, user):
        if default_id is None:
            self.send_json({"plugins": [], "user": user})
            return
        srv = get_server_by_id(default_id)
        if not srv:
            self.send_json({"plugins": [], "user": user})
            return
        self._serve_plugins_compat(srv["path"], user)

    def _serve_plugins_compat(self, server_dir, user):
        """List plugins (compat format)."""
        plugins_dir = os.path.join(server_dir, "plugins")
        plugins = []
        if os.path.exists(plugins_dir):
            for f in sorted(os.listdir(plugins_dir)):
                if f.endswith(".jar"):
                    fp = os.path.join(plugins_dir, f)
                    try:
                        stat = os.stat(fp)
                    except OSError:
                        continue
                    plugins.append({
                        "name": f,
                        "size": stat.st_size,
                        "size_formatted": format_bytes(stat.st_size),
                        "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    })
        self.send_json({"plugins": plugins, "user": user})

    def _compat_schedules(self, default_id, user):
        if default_id is None:
            self.send_json({"schedules": [], "user": user})
            return
        self._serve_schedules(default_id, user)

    def _compat_logs(self, default_id, search, lines, user):
        if default_id is None:
            self.send_json({"lines": [], "total": 0, "search": search, "user": user})
            return
        srv = get_server_by_id(default_id)
        if not srv:
            self.send_json({"lines": [], "total": 0, "search": search, "user": user})
            return
        self._serve_logs(srv["path"], search, lines, user)

    def _compat_server_info(self, default_id, user):
        if default_id is None:
            self.send_json({"server_version": "Unknown", "user": user})
            return
        srv = get_server_by_id(default_id)
        if not srv:
            self.send_json({"server_version": "Unknown", "user": user})
            return
        self._serve_server_info(srv["path"], user)


    # ═════════════════════════════════════════════════════════════════════
    # BACKWARDS COMPAT POST HANDLERS
    # ═════════════════════════════════════════════════════════════════════

    def _compat_get_srv(self, default_id):
        if default_id is None:
            return None
        return get_server_by_id(default_id)

    def _compat_post_start(self, default_id, user):
        if default_id is None:
            self.send_json({"success": False, "message": "No servers configured", "user": user})
            return
        ok, msg = start_server(default_id)
        add_audit_log(user["username"], "start_server", f"Compat: {msg}")
        self.send_json({"success": ok, "message": msg, "user": user})

    def _compat_post_stop(self, default_id, user):
        if default_id is None:
            self.send_json({"success": False, "message": "No servers configured", "user": user})
            return
        ok, msg = stop_server(default_id)
        add_audit_log(user["username"], "stop_server", f"Compat: {msg}")
        self.send_json({"success": ok, "message": msg, "user": user})

    def _compat_post_restart(self, default_id, user):
        if default_id is None:
            self.send_json({"success": False, "message": "No servers configured", "user": user})
            return
        ok, msg = restart_server(default_id)
        add_audit_log(user["username"], "restart_server", f"Compat: {msg}")
        self.send_json({"success": ok, "message": msg, "user": user})

    def _compat_post_command(self, default_id, user):
        srv = self._compat_get_srv(default_id)
        if not srv:
            self.send_json({"success": False, "message": "No server", "user": user})
            return
        body = self.read_body()
        cmd = body.get("command", "") if body else ""
        if not cmd:
            self.send_json({"success": False, "message": "No command", "user": user})
            return
        if send_screen_cmd(cmd, srv["screen_name"]):
            self.send_json({"success": True, "message": f"Sent: {cmd}", "user": user})
        else:
            self.send_json({"success": False, "message": "Failed to send command", "user": user})

    def _compat_post_backup(self, default_id, user):
        srv = self._compat_get_srv(default_id)
        if not srv:
            self.send_json({"success": False, "message": "No server", "user": user})
            return
        server_dir = srv["path"]
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_name = f"backup_{timestamp}.tar.gz"
            subprocess.run(
                ["tar", "-czf", backup_name,
                 "--exclude=logs", "--exclude=*.jar", "--exclude=cache",
                 "world", "world_nether", "world_the_end",
                 "server.properties", "ops.json", "whitelist.json",
                 "banned-players.json", "banned-ips.json"],
                capture_output=True, text=True, timeout=300, cwd=server_dir,
            )
            backup_path = os.path.join(server_dir, backup_name)
            size = os.path.getsize(backup_path) if os.path.exists(backup_path) else 0
            add_audit_log(user["username"], "backup", f"Compat: {backup_name}")
            self.send_json({"success": True, "message": f"Backup created: {backup_name} ({format_bytes(size)})", "user": user})
        except Exception as e:
            self.send_json({"success": False, "message": f"Backup failed: {str(e)}", "user": user})

    def _compat_post_say(self, default_id, user):
        srv = self._compat_get_srv(default_id)
        if not srv:
            self.send_json({"success": False, "message": "No server", "user": user})
            return
        body = self.read_body()
        msg = body.get("message", "") if body else ""
        if not msg:
            self.send_json({"success": False, "message": "No message", "user": user})
            return
        send_screen_cmd(f"say {msg}", srv["screen_name"])
        self.send_json({"success": True, "message": "Broadcast sent", "user": user})

    def _compat_post_kick(self, default_id, user):
        srv = self._compat_get_srv(default_id)
        if not srv:
            self.send_json({"success": False, "message": "No server", "user": user})
            return
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body", "user": user})
            return
        player = body.get("player", "")
        reason = body.get("reason", "Kicked by admin")
        send_screen_cmd(f"kick {player} {reason}", srv["screen_name"])
        self.send_json({"success": True, "message": f"Kicked {player}", "user": user})

    def _compat_post_ban(self, default_id, user):
        srv = self._compat_get_srv(default_id)
        if not srv:
            self.send_json({"success": False, "message": "No server", "user": user})
            return
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body", "user": user})
            return
        player = body.get("player", "")
        reason = body.get("reason", "Banned by admin")
        send_screen_cmd(f"ban {player} {reason}", srv["screen_name"])
        self.send_json({"success": True, "message": f"Banned {player}", "user": user})

    def _compat_post_unban(self, default_id, user):
        srv = self._compat_get_srv(default_id)
        if not srv:
            self.send_json({"success": False, "message": "No server", "user": user})
            return
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body", "user": user})
            return
        player = body.get("player", "")
        send_screen_cmd(f"pardon {player}", srv["screen_name"])
        self.send_json({"success": True, "message": f"Unbanned {player}", "user": user})

    def _compat_post_op(self, default_id, user):
        srv = self._compat_get_srv(default_id)
        if not srv:
            self.send_json({"success": False, "message": "No server", "user": user})
            return
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body", "user": user})
            return
        player = body.get("player", "")
        send_screen_cmd(f"op {player}", srv["screen_name"])
        self.send_json({"success": True, "message": f"Opped {player}", "user": user})

    def _compat_post_deop(self, default_id, user):
        srv = self._compat_get_srv(default_id)
        if not srv:
            self.send_json({"success": False, "message": "No server", "user": user})
            return
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body", "user": user})
            return
        player = body.get("player", "")
        send_screen_cmd(f"deop {player}", srv["screen_name"])
        self.send_json({"success": True, "message": f"Deopped {player}", "user": user})

    def _compat_post_whitelist(self, default_id, user):
        srv = self._compat_get_srv(default_id)
        if not srv:
            self.send_json({"success": False, "message": "No server", "user": user})
            return
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body", "user": user})
            return
        action = body.get("action", "")
        player = body.get("player", "")
        if action == "add":
            send_screen_cmd(f"whitelist add {player}", srv["screen_name"])
            self.send_json({"success": True, "message": f"Added {player} to whitelist", "user": user})
        elif action == "remove":
            send_screen_cmd(f"whitelist remove {player}", srv["screen_name"])
            self.send_json({"success": True, "message": f"Removed {player} from whitelist", "user": user})
        else:
            self.send_json({"success": False, "message": "Invalid action", "user": user})

    def _compat_post_gamemode(self, default_id, user):
        srv = self._compat_get_srv(default_id)
        if not srv:
            self.send_json({"success": False, "message": "No server", "user": user})
            return
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body", "user": user})
            return
        player = body.get("player", "")
        mode = body.get("mode", "survival")
        send_screen_cmd(f"gamemode {mode} {player}", srv["screen_name"])
        self.send_json({"success": True, "message": f"Set {player} to {mode}", "user": user})

    def _compat_post_tp(self, default_id, user):
        srv = self._compat_get_srv(default_id)
        if not srv:
            self.send_json({"success": False, "message": "No server", "user": user})
            return
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body", "user": user})
            return
        player = body.get("player", "")
        target = body.get("target", "")
        send_screen_cmd(f"tp {player} {target}", srv["screen_name"])
        self.send_json({"success": True, "message": f"Teleported {player}", "user": user})

    def _compat_post_save_properties(self, default_id, user):
        srv = self._compat_get_srv(default_id)
        if not srv:
            self.send_json({"success": False, "message": "No server", "user": user})
            return
        body = self.read_body()
        if not body or "properties" not in body:
            self.send_json({"success": False, "message": "Missing properties", "user": user})
            return
        if save_properties_file(srv["path"], body["properties"]):
            self.send_json({"success": True, "message": "Properties saved", "user": user})
        else:
            self.send_json({"success": False, "message": "Failed to save properties", "user": user})

    def _compat_post_file_save(self, default_id, user):
        srv = self._compat_get_srv(default_id)
        if not srv:
            self.send_json({"success": False, "message": "No server", "user": user})
            return
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body", "user": user})
            return
        rel_path = body.get("path", "")
        content = body.get("content", "")
        abs_path = safe_path(srv["path"], rel_path)
        if not abs_path:
            self.send_json({"success": False, "message": "Access denied", "user": user})
            return
        try:
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)
            self.send_json({"success": True, "message": "File saved", "user": user})
        except Exception as e:
            self.send_json({"success": False, "message": str(e), "user": user})

    def _compat_post_file_create(self, default_id, user):
        srv = self._compat_get_srv(default_id)
        if not srv:
            self.send_json({"success": False, "message": "No server", "user": user})
            return
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body", "user": user})
            return
        rel_path = body.get("path", "")
        content = body.get("content", "")
        abs_path = safe_path(srv["path"], rel_path)
        if not abs_path:
            self.send_json({"success": False, "message": "Access denied", "user": user})
            return
        try:
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)
            self.send_json({"success": True, "message": "File created", "user": user})
        except Exception as e:
            self.send_json({"success": False, "message": str(e), "user": user})

    def _compat_post_file_mkdir(self, default_id, user):
        srv = self._compat_get_srv(default_id)
        if not srv:
            self.send_json({"success": False, "message": "No server", "user": user})
            return
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body", "user": user})
            return
        rel_path = body.get("path", "")
        abs_path = safe_path(srv["path"], rel_path)
        if not abs_path:
            self.send_json({"success": False, "message": "Access denied", "user": user})
            return
        try:
            os.makedirs(abs_path, exist_ok=True)
            self.send_json({"success": True, "message": "Directory created", "user": user})
        except Exception as e:
            self.send_json({"success": False, "message": str(e), "user": user})

    def _compat_post_file_delete(self, default_id, user):
        srv = self._compat_get_srv(default_id)
        if not srv:
            self.send_json({"success": False, "message": "No server", "user": user})
            return
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body", "user": user})
            return
        rel_path = body.get("path", "")
        abs_path = safe_path(srv["path"], rel_path)
        if not abs_path:
            self.send_json({"success": False, "message": "Access denied", "user": user})
            return
        try:
            if os.path.isdir(abs_path):
                shutil.rmtree(abs_path)
            else:
                os.remove(abs_path)
            self.send_json({"success": True, "message": "Deleted", "user": user})
        except Exception as e:
            self.send_json({"success": False, "message": str(e), "user": user})

    def _compat_post_file_rename(self, default_id, user):
        srv = self._compat_get_srv(default_id)
        if not srv:
            self.send_json({"success": False, "message": "No server", "user": user})
            return
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body", "user": user})
            return
        old_rel = body.get("old_path", "")
        new_rel = body.get("new_path", "")
        old_abs = safe_path(srv["path"], old_rel)
        new_abs = safe_path(srv["path"], new_rel)
        if not old_abs or not new_abs:
            self.send_json({"success": False, "message": "Access denied", "user": user})
            return
        try:
            os.rename(old_abs, new_abs)
            self.send_json({"success": True, "message": "Renamed", "user": user})
        except Exception as e:
            self.send_json({"success": False, "message": str(e), "user": user})

    def _compat_post_file_upload(self, default_id, user):
        srv = self._compat_get_srv(default_id)
        if not srv:
            self.send_json({"success": False, "message": "No server", "user": user})
            return
        self._handle_file_upload(srv["path"], srv["name"], user)

    def _compat_post_plugin_delete(self, default_id, user):
        srv = self._compat_get_srv(default_id)
        if not srv:
            self.send_json({"success": False, "message": "No server", "user": user})
            return
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body", "user": user})
            return
        name = body.get("name", "")
        plugin_path = os.path.join(srv["path"], "plugins", name)
        if not os.path.exists(plugin_path):
            self.send_json({"success": False, "message": "Plugin not found", "user": user})
            return
        try:
            os.remove(plugin_path)
            self.send_json({"success": True, "message": f"Deleted {name}", "user": user})
        except Exception as e:
            self.send_json({"success": False, "message": str(e), "user": user})

    def _compat_post_schedule_create(self, default_id, user):
        if default_id is None:
            self.send_json({"success": False, "message": "No server", "user": user})
            return
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body", "user": user})
            return
        name = body.get("name", "").strip()
        command = body.get("command", "").strip()
        interval_minutes = int(body.get("interval_minutes", 5))
        if not name or not command:
            self.send_json({"success": False, "message": "Name and command required", "user": user})
            return
        try:
            with db_lock:
                conn = get_db()
                conn.execute(
                    "INSERT INTO schedules (server_id, name, command, interval_seconds) VALUES (?, ?, ?, ?)",
                    (default_id, name, command, interval_minutes * 60),
                )
                conn.commit()
                conn.close()
            self.send_json({"success": True, "message": f"Schedule '{name}' created", "user": user})
        except Exception as e:
            self.send_json({"success": False, "message": str(e), "user": user})

    def _compat_post_schedule_delete(self, default_id, user):
        if default_id is None:
            self.send_json({"success": False, "message": "No server", "user": user})
            return
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body", "user": user})
            return
        name = body.get("name", "")
        try:
            with db_lock:
                conn = get_db()
                conn.execute(
                    "DELETE FROM schedules WHERE server_id = ? AND name = ?",
                    (default_id, name),
                )
                conn.commit()
                conn.close()
            self.send_json({"success": True, "message": f"Deleted schedule: {name}", "user": user})
        except Exception as e:
            self.send_json({"success": False, "message": str(e), "user": user})

    def _compat_post_schedule_toggle(self, default_id, user):
        if default_id is None:
            self.send_json({"success": False, "message": "No server", "user": user})
            return
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body", "user": user})
            return
        name = body.get("name", "")
        try:
            with db_lock:
                conn = get_db()
                row = conn.execute(
                    "SELECT enabled FROM schedules WHERE server_id = ? AND name = ?",
                    (default_id, name),
                ).fetchone()
                if row:
                    new_val = 0 if row["enabled"] == 1 else 1
                    conn.execute(
                        "UPDATE schedules SET enabled = ? WHERE server_id = ? AND name = ?",
                        (new_val, default_id, name),
                    )
                    conn.commit()
                conn.close()
            self.send_json({"success": True, "message": f"Toggled schedule: {name}", "user": user})
        except Exception as e:
            self.send_json({"success": False, "message": str(e), "user": user})


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  MCPanel v3 - Minecraft Server Management Panel")
    print("=" * 60)

    # Initialize database
    print("[INIT] Initializing database...")
    init_db()

    # Auto-discover servers
    print("[INIT] Auto-discovering servers...")
    auto_discover_servers()

    # Show configured servers
    servers = get_all_servers()
    print(f"[INIT] {len(servers)} server(s) configured:")
    for srv in servers:
        running = is_server_running(srv["screen_name"])
        status = "RUNNING" if running else "STOPPED"
        print(f"  - {srv['name']} (id={srv['id']}, port={srv['port']}, screen={srv['screen_name']}) [{status}]")

    # Clean expired tokens periodically
    def token_cleaner():
        while True:
            time.sleep(300)  # Every 5 minutes
            with token_lock:
                now = time.time()
                expired = [t for t, info in valid_tokens.items() if now > info.get("expires", 0)]
                for t in expired:
                    del valid_tokens[t]
                if expired:
                    print(f"[AUTH] Cleaned {len(expired)} expired tokens")

    cleaner_thread = threading.Thread(target=token_cleaner, daemon=True)
    cleaner_thread.start()

    # Start schedule runner
    sched_thread = threading.Thread(target=schedule_runner, daemon=True)
    sched_thread.start()
    print("[INIT] Schedule runner started")

    # Start analytics collector
    analytics_thread = threading.Thread(target=analytics_collector, daemon=True)
    analytics_thread.start()
    print("[INIT] Analytics collector started")

    print(f"[INIT] Panel starting on port {PANEL_PORT}...")
    print(f"[INIT] Default login: admin / admin123")
    print(f"[INIT] DB path: {DB_PATH}")
    print(f"[INIT] Project dir: {PROJECT_DIR}")
    print("=" * 60)

    server = HTTPServer(("0.0.0.0", PANEL_PORT), PanelHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[SHUTDOWN] MCPanel shutting down...")
        schedule_running = False
        server.server_close()
