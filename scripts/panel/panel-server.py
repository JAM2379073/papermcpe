#!/usr/bin/env python3
"""MCPanel - Minecraft Server Management Panel Backend"""

import json
import os
import shutil
import subprocess
import sys
import time
import threading
import urllib.parse
import math
import secrets
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

# ── Configuration ──
PANEL_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(PANEL_DIR, "..", ".."))
SERVER_DIR = os.path.abspath(os.path.join(PROJECT_DIR, "minecraft-server"))
HTML_FILE = os.path.join(PANEL_DIR, "index.html")
SCREEN_NAME = "minecraft"
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "admin123")
PANEL_PORT = int(os.environ.get("PANEL_PORT", "8080"))
SCHEDULES_FILE = os.path.join(PANEL_DIR, "schedules.json")

# ── Auth tokens ──
valid_tokens = set()
token_lock = threading.Lock()

# ── Schedule runner state ──
schedule_running = True
last_runs = {}

def get_dir_size(path):
    """Calculate total size of a directory."""
    total = 0
    if not os.path.exists(path):
        return 0
    for dirpath, dirnames, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total

def format_bytes(b):
    """Format bytes to human-readable string."""
    if not b:
        return "0 B"
    units = ["B", "KB", "MB", "GB"]
    i = int(math.floor(math.log(b, 1024)))
    if i >= len(units):
        i = len(units) - 1
    return f"{b / (1024 ** i):.1f} {units[i]}"

def send_screen_cmd(cmd):
    """Send a command to the Minecraft server via screen."""
    try:
        subprocess.run(
            ["screen", "-S", SCREEN_NAME, "-X", "stuff", f"{cmd}\r"],
            capture_output=True, timeout=5
        )
        return True
    except Exception:
        return False

def is_server_running():
    """Check if the Minecraft server screen session is running."""
    try:
        result = subprocess.run(
            ["screen", "-list"], capture_output=True, text=True, timeout=5
        )
        return SCREEN_NAME in result.stdout
    except Exception:
        return False

def get_server_uptime():
    """Get server uptime by checking the screen session's elapsed time."""
    try:
        result = subprocess.run(
            ["screen", "-list"], capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.split("\n"):
            if SCREEN_NAME in line:
                parts = line.split("\t")
                if len(parts) >= 2:
                    time_str = parts[-1].strip().strip("()")
                    return time_str if time_str else None
        return None
    except Exception:
        return None

def get_tps():
    """Estimate TPS from server or return 20 if server is not running."""
    if not is_server_running():
        return 20.0
    try:
        # Try to read from a tps plugin output or estimate
        # Send tps command and check logs
        log_file = os.path.join(SERVER_DIR, "logs", "latest.log")
        if os.path.exists(log_file):
            # Try reading tps from recent log lines
            result = subprocess.run(
                ["tail", "-100", log_file],
                capture_output=True, text=True, timeout=5
            )
            for line in reversed(result.stdout.split("\n")):
                if "TPS from last" in line or "tps" in line.lower():
                    try:
                        # Parse formats like "TPS from last 1m, 5m, 15m: 20.0, 20.0, 20.0"
                        parts = line.split(":")
                        if len(parts) >= 2:
                            nums = parts[-1].strip().split(",")
                            if nums:
                                val = float(nums[0].strip())
                                return round(val, 1)
                    except (ValueError, IndexError):
                        pass
        # If no TPS data available, estimate from CPU
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
            ["top", "-bn1"], capture_output=True, text=True, timeout=5
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

def get_ram_usage():
    """Get RAM usage of Java/Minecraft process."""
    try:
        # Get Java process RAM
        result = subprocess.run(
            ["ps", "aux"], capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.split("\n"):
            if "java" in line and "paper" in line.lower():
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
            ["free", "-m"], capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.split("\n"):
            if line.startswith("Mem:"):
                parts = line.split()
                return int(parts[1])
        return 4096
    except Exception:
        return 4096

def get_disk_usage():
    """Get disk usage of SERVER_DIR."""
    try:
        total, used, free = shutil.disk_usage(SERVER_DIR)
        return round(used / (1024 ** 3), 2), round(total / (1024 ** 3), 2)
    except Exception:
        return 0.0, 100.0

def get_java_version():
    """Get Java version."""
    try:
        result = subprocess.run(
            ["java", "-version"], capture_output=True, text=True, timeout=5
        )
        return result.stderr.split("\n")[0].strip() if result.stderr else "Unknown"
    except Exception:
        return "Not found"

def get_online_players():
    """Parse online players from server logs."""
    players = []
    try:
        log_file = os.path.join(SERVER_DIR, "logs", "latest.log")
        if not os.path.exists(log_file):
            return players
        result = subprocess.run(
            ["tail", "-200", log_file],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.split("\n"):
            if "joined the game" in line:
                # Extract player name: [HH:MM:SS] PlayerName joined the game
                try:
                    name = line.split("]: ")[1].split(" joined")[0].strip()
                    # Remove any color codes
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

def get_stats():
    """Parse server statistics from logs."""
    stats = {"total_joins": 0, "deaths": 0, "advancements": 0, "chat_messages": 0}
    try:
        log_file = os.path.join(SERVER_DIR, "logs", "latest.log")
        if not os.path.exists(log_file):
            return stats
        # Use grep for efficiency
        for pattern, key in [
            ("joined the game", "total_joins"),
            ("death.message.", "deaths"),
            ("has made the advancement", "advancements"),
            ("<", "chat_messages"),
        ]:
            try:
                result = subprocess.run(
                    ["grep", "-c", pattern, log_file],
                    capture_output=True, text=True, timeout=10
                )
                stats[key] = int(result.stdout.strip() or 0)
            except Exception:
                pass
    except Exception:
        pass
    return stats

def read_properties():
    """Read server.properties file."""
    props = {}
    prop_file = os.path.join(SERVER_DIR, "server.properties")
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

def save_properties(props):
    """Save server.properties file."""
    prop_file = os.path.join(SERVER_DIR, "server.properties")
    try:
        lines = [f"{k}={v}" for k, v in props.items()]
        with open(prop_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return True
    except Exception as e:
        return False

def get_console_logs(lines=200):
    """Get recent console output from server log."""
    log_file = os.path.join(SERVER_DIR, "logs", "latest.log")
    if not os.path.exists(log_file):
        return "No log file found."
    try:
        result = subprocess.run(
            ["tail", f"-{lines}", log_file],
            capture_output=True, text=True, timeout=10
        )
        return result.stdout
    except Exception:
        return "Error reading log file."

def load_schedules():
    """Load schedules from JSON file."""
    if os.path.exists(SCHEDULES_FILE):
        try:
            with open(SCHEDULES_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return []

def save_schedules(schedules):
    """Save schedules to JSON file."""
    with open(SCHEDULES_FILE, "w") as f:
        json.dump(schedules, f, indent=2)

def schedule_runner():
    """Background thread that runs scheduled tasks."""
    global schedule_running
    while schedule_running:
        schedules = load_schedules()
        now = time.time()
        for task in schedules:
            if not task.get("enabled", False):
                continue
            name = task.get("name", "")
            interval = task.get("interval_seconds", 300)
            last = last_runs.get(name, 0)
            if now - last >= interval:
                cmd = task.get("command", "")
                if cmd:
                    print(f"[Schedule] Running '{name}': {cmd}")
                    send_screen_cmd(cmd)
                    last_runs[name] = now
        time.sleep(10)

def get_server_version():
    """Extract server version from logs."""
    try:
        log_file = os.path.join(SERVER_DIR, "logs", "latest.log")
        if os.path.exists(log_file):
            result = subprocess.run(
                ["head", "-50", log_file],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.split("\n"):
                if "version" in line.lower() and ("minecraft" in line.lower() or "paper" in line.lower() or "running" in line.lower()):
                    return line.strip()
            for line in result.stdout.split("\n"):
                if "Paper" in line or "Minecraft" in line or "mc" in line.lower():
                    return line.strip()
    except Exception:
        pass
    return "Unknown"

def get_paper_version():
    """Try to detect Paper version from jar file."""
    try:
        for f in os.listdir(SERVER_DIR):
            if f.startswith("paper") and f.endswith(".jar"):
                # Extract version from filename like paper-1.20.4-476.jar
                parts = f.replace("paper", "").replace(".jar", "").split("-")
                if len(parts) >= 2:
                    return f"Paper {parts[1]} (MC {parts[0].lstrip('-')})"
                return f
        return "Not found"
    except Exception:
        return "Unknown"

def get_world_sizes():
    """Get sizes of world directories."""
    worlds = {}
    for name in ["world", "world_nether", "world_the_end"]:
        path = os.path.join(SERVER_DIR, name)
        if os.path.exists(path):
            worlds[name] = format_bytes(get_dir_size(path))
        else:
            worlds[name] = "N/A"
    return worlds

def get_installed_plugins():
    """Get list of installed plugins."""
    plugins_dir = os.path.join(SERVER_DIR, "plugins")
    if not os.path.exists(plugins_dir):
        return []
    plugins = []
    for f in os.listdir(plugins_dir):
        if f.endswith(".jar"):
            plugins.append(f.replace(".jar", ""))
    return sorted(plugins)


# ── HTTP Handler ──
class PanelHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        """Override to reduce log noise."""
        pass

    def send_json(self, data, status=200):
        """Send a JSON response."""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def send_html(self, content, status=200):
        """Send HTML response."""
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(content.encode())

    def read_body(self):
        """Read and parse JSON request body."""
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except Exception:
            return None

    def check_auth(self):
        """Check Bearer token authentication. Returns True if valid."""
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
            with token_lock:
                if token in valid_tokens:
                    return True
        return False

    def do_OPTIONS(self):
        """Handle CORS preflight requests."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "*")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # Serve HTML
        if path == "/" or path == "/index.html":
            html_path = os.path.join(PANEL_DIR, "index.html")
            if os.path.exists(html_path):
                with open(html_path, "r", encoding="utf-8") as f:
                    self.send_html(f.read())
            else:
                self.send_html("<h1>MCPanel - index.html not found</h1>", 404)
            return

        # Auth endpoints (no token needed)
        if path == "/api/auth/login":
            self.send_json({"error": "Use POST for login"})
            return

        # All other API endpoints require auth
        if not self.check_auth():
            self.send_json({"error": "Unauthorized"}, 401)
            return

        # Route API requests
        if path == "/api/status":
            self.handle_status()
        elif path == "/api/console":
            lines = int(query.get("lines", ["200"])[0])
            self.handle_console(lines)
        elif path == "/api/players":
            self.handle_players()
        elif path == "/api/stats":
            self.handle_stats()
        elif path == "/api/files":
            file_path = query.get("path", ["/"])[0]
            self.handle_files(file_path)
        elif path == "/api/file/read":
            file_path = query.get("path", [""])[0]
            self.handle_file_read(file_path)
        elif path == "/api/file/download":
            file_path = query.get("path", [""])[0]
            self.handle_file_download(file_path)
        elif path == "/api/properties":
            self.handle_properties()
        elif path == "/api/ops":
            self.handle_ops()
        elif path == "/api/banned":
            self.handle_banned()
        elif path == "/api/plugins":
            self.handle_plugins()
        elif path == "/api/schedules":
            self.handle_schedules()
        elif path == "/api/logs":
            search = query.get("search", [""])[0]
            lines = int(query.get("lines", ["200"])[0])
            self.handle_logs(search, lines)
        elif path == "/api/server-info":
            self.handle_server_info()
        else:
            self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # Auth login endpoint (no token needed)
        if path == "/api/auth/login":
            self.handle_login()
            return

        # All other API endpoints require auth
        if not self.check_auth():
            self.send_json({"error": "Unauthorized"}, 401)
            return

        if path == "/api/command":
            self.handle_command()
        elif path == "/api/start":
            self.handle_start()
        elif path == "/api/stop":
            self.handle_stop()
        elif path == "/api/restart":
            self.handle_restart()
        elif path == "/api/backup":
            self.handle_backup()
        elif path == "/api/say":
            self.handle_say()
        elif path == "/api/kick":
            self.handle_kick()
        elif path == "/api/ban":
            self.handle_ban_player()
        elif path == "/api/unban":
            self.handle_unban()
        elif path == "/api/op":
            self.handle_op()
        elif path == "/api/deop":
            self.handle_deop()
        elif path == "/api/whitelist":
            self.handle_whitelist()
        elif path == "/api/gamemode":
            self.handle_gamemode()
        elif path == "/api/tp":
            self.handle_tp()
        elif path == "/api/properties":
            self.handle_save_properties()
        elif path == "/api/file/save":
            self.handle_file_save()
        elif path == "/api/file/create":
            self.handle_file_create()
        elif path == "/api/file/mkdir":
            self.handle_file_mkdir()
        elif path == "/api/file/delete":
            self.handle_file_delete()
        elif path == "/api/file/rename":
            self.handle_file_rename()
        elif path == "/api/file/upload":
            self.handle_file_upload()
        elif path == "/api/plugins/delete":
            self.handle_plugin_delete()
        elif path == "/api/schedules":
            self.handle_schedule_create()
        elif path == "/api/schedules/delete":
            self.handle_schedule_delete()
        elif path == "/api/schedules/toggle":
            self.handle_schedule_toggle()
        else:
            self.send_json({"error": "Not found"}, 404)

    # ── Auth ──
    def handle_login(self):
        body = self.read_body()
        if not body:
            self.send_json({"error": "Missing request body"}, 400)
            return
        password = body.get("password", "")
        if password == PANEL_PASSWORD:
            token = secrets.token_hex(32)
            with token_lock:
                valid_tokens.add(token)
            self.send_json({"success": True, "token": token})
        else:
            self.send_json({"success": False, "error": "Invalid password"}, 401)

    # ── Status ──
    def handle_status(self):
        running = is_server_running()
        uptime = get_server_uptime() if running else None
        tps = get_tps() if running else 0
        ram_used = get_ram_usage()
        ram_total = get_ram_total()
        disk_used, disk_total = get_disk_usage()
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
        })

    # ── Console ──
    def handle_console(self, lines=200):
        logs = get_console_logs(lines)
        self.send_json({"logs": logs})

    # ── Command ──
    def handle_command(self):
        body = self.read_body()
        cmd = body.get("command", "") if body else ""
        if not cmd:
            self.send_json({"success": False, "message": "No command"})
            return
        if send_screen_cmd(cmd):
            self.send_json({"success": True, "message": f"Sent: {cmd}"})
        else:
            self.send_json({"success": False, "message": "Failed to send command"})

    # ── Players ──
    def handle_players(self):
        players = get_online_players()
        self.send_json({"players": players, "count": len(players)})

    # ── Stats ──
    def handle_stats(self):
        self.send_json(get_stats())

    # ── Server Controls ──
    def handle_start(self):
        if is_server_running():
            self.send_json({"success": False, "message": "Server is already running"})
            return
        try:
            os.chdir(SERVER_DIR)
            subprocess.Popen(
                ["screen", "-dmS", SCREEN_NAME, "java", "-Xms6G", "-Xmx12G",
                 "-jar", "paper.jar", "--nogui"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            self.send_json({"success": True, "message": "Server starting..."})
        except Exception as e:
            self.send_json({"success": False, "message": str(e)})

    def handle_stop(self):
        if not is_server_running():
            self.send_json({"success": False, "message": "Server is not running"})
            return
        send_screen_cmd("stop")
        self.send_json({"success": True, "message": "Stopping server..."})

    def handle_restart(self):
        if not is_server_running():
            self.send_json({"success": False, "message": "Server is not running"})
            return
        send_screen_cmd("stop")
        time.sleep(5)
        try:
            os.chdir(SERVER_DIR)
            subprocess.Popen(
                ["screen", "-dmS", SCREEN_NAME, "java", "-Xms6G", "-Xmx12G",
                 "-jar", "paper.jar", "--nogui"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            self.send_json({"success": True, "message": "Server restarting..."})
        except Exception as e:
            self.send_json({"success": False, "message": str(e)})

    def handle_backup(self):
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_name = f"backup_{timestamp}.tar.gz"
            result = subprocess.run(
                ["tar", "-czf", backup_name,
                 "--exclude=logs", "--exclude=*.jar", "--exclude=cache",
                 "world", "world_nether", "world_the_end",
                 "server.properties", "ops.json", "whitelist.json",
                 "banned-players.json", "banned-ips.json"],
                capture_output=True, text=True, timeout=300, cwd=SERVER_DIR
            )
            backup_path = os.path.join(SERVER_DIR, backup_name)
            size = os.path.getsize(backup_path) if os.path.exists(backup_path) else 0
            self.send_json({"success": True, "message": f"Backup created: {backup_name} ({format_bytes(size)})"})
        except Exception as e:
            self.send_json({"success": False, "message": f"Backup failed: {str(e)}"})

    def handle_say(self):
        body = self.read_body()
        msg = body.get("message", "") if body else ""
        if not msg:
            self.send_json({"success": False, "message": "No message"})
            return
        send_screen_cmd(f"say {msg}")
        self.send_json({"success": True, "message": "Broadcast sent"})

    # ── Player Actions ──
    def handle_kick(self):
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body"})
            return
        player = body.get("player", "")
        reason = body.get("reason", "Kicked by admin")
        cmd = f"kick {player} {reason}"
        send_screen_cmd(cmd)
        self.send_json({"success": True, "message": f"Kicked {player}"})

    def handle_ban_player(self):
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body"})
            return
        player = body.get("player", "")
        reason = body.get("reason", "Banned by admin")
        send_screen_cmd(f"ban {player} {reason}")
        self.send_json({"success": True, "message": f"Banned {player}"})

    def handle_unban(self):
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body"})
            return
        player = body.get("player", "")
        send_screen_cmd(f"pardon {player}")
        self.send_json({"success": True, "message": f"Unbanned {player}"})

    def handle_op(self):
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body"})
            return
        player = body.get("player", "")
        send_screen_cmd(f"op {player}")
        self.send_json({"success": True, "message": f"Opped {player}"})

    def handle_deop(self):
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body"})
            return
        player = body.get("player", "")
        send_screen_cmd(f"deop {player}")
        self.send_json({"success": True, "message": f"Deopped {player}"})

    def handle_whitelist(self):
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body"})
            return
        action = body.get("action", "")
        player = body.get("player", "")
        if action == "add":
            send_screen_cmd(f"whitelist add {player}")
            self.send_json({"success": True, "message": f"Added {player} to whitelist"})
        elif action == "remove":
            send_screen_cmd(f"whitelist remove {player}")
            self.send_json({"success": True, "message": f"Removed {player} from whitelist"})
        else:
            self.send_json({"success": False, "message": "Invalid action"})

    def handle_gamemode(self):
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body"})
            return
        player = body.get("player", "")
        mode = body.get("mode", "survival")
        send_screen_cmd(f"gamemode {mode} {player}")
        self.send_json({"success": True, "message": f"Set {player} to {mode}"})

    def handle_tp(self):
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body"})
            return
        player = body.get("player", "")
        target = body.get("target", "")
        send_screen_cmd(f"tp {player} {target}")
        self.send_json({"success": True, "message": f"Teleported {player}"})

    # ── Properties ──
    def handle_properties(self):
        self.send_json({"properties": read_properties()})

    def handle_save_properties(self):
        body = self.read_body()
        if not body or "properties" not in body:
            self.send_json({"success": False, "message": "Missing properties"})
            return
        if save_properties(body["properties"]):
            self.send_json({"success": True, "message": "Properties saved"})
        else:
            self.send_json({"success": False, "message": "Failed to save properties"})

    # ── Ops & Banned ──
    def handle_ops(self):
        ops = []
        ops_file = os.path.join(SERVER_DIR, "ops.json")
        if os.path.exists(ops_file):
            try:
                with open(ops_file, "r") as f:
                    data = json.load(f)
                    ops = [entry.get("name", "") for entry in data if entry.get("name")]
            except Exception:
                pass
        self.send_json({"ops": ops})

    def handle_banned(self):
        banned = []
        ban_file = os.path.join(SERVER_DIR, "banned-players.json")
        if os.path.exists(ban_file):
            try:
                with open(ban_file, "r") as f:
                    data = json.load(f)
                    banned = [{"name": e.get("name", ""), "reason": e.get("reason", "")} for e in data if e.get("name")]
            except Exception:
                pass
        self.send_json({"banned": banned})

    # ── File Manager ──
    def _resolve_path(self, rel_path):
        """Resolve a relative path to absolute path within SERVER_DIR."""
        if not rel_path or rel_path == "/":
            return SERVER_DIR
        # Strip leading slash and join
        clean = rel_path.lstrip("/")
        return os.path.join(SERVER_DIR, clean)

    def handle_files(self, rel_path):
        abs_path = self._resolve_path(rel_path)
        if not os.path.exists(abs_path) or not os.path.isdir(abs_path):
            self.send_json({"error": "Directory not found"})
            return
        # Security check: ensure we're within SERVER_DIR
        if not os.path.abspath(abs_path).startswith(SERVER_DIR):
            self.send_json({"error": "Access denied"}, 403)
            return

        files = []
        try:
            for entry in sorted(os.listdir(abs_path)):
                full = os.path.join(abs_path, entry)
                rel = os.path.relpath(full, SERVER_DIR)
                stat = os.stat(full)
                files.append({
                    "name": entry,
                    "path": "/" + rel.replace(os.sep, "/"),
                    "is_dir": os.path.isdir(full),
                    "size": stat.st_size if not os.path.isdir(full) else 0,
                    "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                })
        except PermissionError:
            self.send_json({"error": "Permission denied"}, 403)
            return
        self.send_json({"files": files})

    def handle_file_read(self, rel_path):
        abs_path = self._resolve_path(rel_path)
        if not os.path.abspath(abs_path).startswith(SERVER_DIR):
            self.send_json({"error": "Access denied"}, 403)
            return
        if not os.path.exists(abs_path) or os.path.isdir(abs_path):
            self.send_json({"error": "File not found"})
            return
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(100000)  # Limit to 100KB
            self.send_json({"content": content, "path": rel_path})
        except Exception as e:
            self.send_json({"error": str(e)})

    def handle_file_download(self, rel_path):
        abs_path = self._resolve_path(rel_path)
        if not os.path.abspath(abs_path).startswith(SERVER_DIR):
            self.send_json({"error": "Access denied"}, 403)
            return
        if not os.path.exists(abs_path):
            self.send_json({"error": "File not found"})
            return
        try:
            with open(abs_path, "rb") as f:
                data = f.read(50 * 1024 * 1024)  # Max 50MB
            filename = os.path.basename(abs_path)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_json({"error": str(e)})

    def handle_file_save(self):
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body"})
            return
        rel_path = body.get("path", "")
        content = body.get("content", "")
        abs_path = self._resolve_path(rel_path)
        if not os.path.abspath(abs_path).startswith(SERVER_DIR):
            self.send_json({"success": False, "message": "Access denied"})
            return
        try:
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)
            self.send_json({"success": True, "message": "File saved"})
        except Exception as e:
            self.send_json({"success": False, "message": str(e)})

    def handle_file_create(self):
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body"})
            return
        rel_path = body.get("path", "")
        content = body.get("content", "")
        abs_path = self._resolve_path(rel_path)
        if not os.path.abspath(abs_path).startswith(SERVER_DIR):
            self.send_json({"success": False, "message": "Access denied"})
            return
        try:
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)
            self.send_json({"success": True, "message": "File created"})
        except Exception as e:
            self.send_json({"success": False, "message": str(e)})

    def handle_file_mkdir(self):
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body"})
            return
        rel_path = body.get("path", "")
        abs_path = self._resolve_path(rel_path)
        if not os.path.abspath(abs_path).startswith(SERVER_DIR):
            self.send_json({"success": False, "message": "Access denied"})
            return
        try:
            os.makedirs(abs_path, exist_ok=True)
            self.send_json({"success": True, "message": "Directory created"})
        except Exception as e:
            self.send_json({"success": False, "message": str(e)})

    def handle_file_delete(self):
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body"})
            return
        rel_path = body.get("path", "")
        abs_path = self._resolve_path(rel_path)
        if not os.path.abspath(abs_path).startswith(SERVER_DIR):
            self.send_json({"success": False, "message": "Access denied"})
            return
        try:
            if os.path.isdir(abs_path):
                shutil.rmtree(abs_path)
            else:
                os.remove(abs_path)
            self.send_json({"success": True, "message": "Deleted"})
        except Exception as e:
            self.send_json({"success": False, "message": str(e)})

    def handle_file_rename(self):
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body"})
            return
        old_rel = body.get("old_path", "")
        new_rel = body.get("new_path", "")
        old_abs = self._resolve_path(old_rel)
        new_abs = self._resolve_path(new_rel)
        if not os.path.abspath(old_abs).startswith(SERVER_DIR):
            self.send_json({"success": False, "message": "Access denied"})
            return
        if not os.path.abspath(new_abs).startswith(SERVER_DIR):
            self.send_json({"success": False, "message": "Access denied"})
            return
        try:
            os.rename(old_abs, new_abs)
            self.send_json({"success": True, "message": "Renamed"})
        except Exception as e:
            self.send_json({"success": False, "message": str(e)})

    def handle_file_upload(self):
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self.send_json({"success": False, "message": "Expected multipart form data"})
            return

        # Parse multipart manually (basic parser)
        boundary = content_type.split("boundary=")[-1].strip()
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="replace")

        # Extract target path and file data
        upload_path = SERVER_DIR
        file_data = None
        file_name = None

        parts = body.split(f"--{boundary}")
        for part in parts:
            if 'Content-Disposition' in part and "file" in part.split('name="')[1].split('"')[0]:
                # Extract filename
                if 'filename="' in part:
                    file_name = part.split('filename="')[1].split('"')[0]
                # Extract file content (after blank line)
                if "\r\n\r\n" in part:
                    file_data = part.split("\r\n\r\n", 1)[1]
                    # Remove trailing boundary
                    file_data = file_data.rsplit("\r\n", 1)[0]
            elif 'Content-Disposition' in part and "path" in part:
                if "\r\n\r\n" in part:
                    path_val = part.split("\r\n\r\n", 1)[1].rsplit("\r\n", 1)[0].strip()
                    if path_val and path_val != "/":
                        upload_path = os.path.join(SERVER_DIR, path_val.lstrip("/"))

        if file_name and file_data:
            try:
                save_path = os.path.join(upload_path, file_name)
                if not os.path.abspath(save_path).startswith(SERVER_DIR):
                    self.send_json({"success": False, "message": "Access denied"})
                    return
                os.makedirs(upload_path, exist_ok=True)
                with open(save_path, "wb") as f:
                    f.write(file_data.encode("latin-1"))
                self.send_json({"success": True, "message": f"Uploaded {file_name}"})
            except Exception as e:
                self.send_json({"success": False, "message": str(e)})
        else:
            self.send_json({"success": False, "message": "No file data"})

    # ── Plugins ──
    def handle_plugins(self):
        plugins_dir = os.path.join(SERVER_DIR, "plugins")
        plugins = []
        if os.path.exists(plugins_dir):
            for f in sorted(os.listdir(plugins_dir)):
                if f.endswith(".jar"):
                    fp = os.path.join(plugins_dir, f)
                    stat = os.stat(fp)
                    plugins.append({
                        "name": f,
                        "size": stat.st_size,
                        "size_formatted": format_bytes(stat.st_size),
                        "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    })
        self.send_json({"plugins": plugins})

    def handle_plugin_delete(self):
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body"})
            return
        name = body.get("name", "")
        if not name:
            self.send_json({"success": False, "message": "No plugin name"})
            return
        plugin_path = os.path.join(SERVER_DIR, "plugins", name)
        if not os.path.exists(plugin_path):
            self.send_json({"success": False, "message": "Plugin not found"})
            return
        try:
            os.remove(plugin_path)
            self.send_json({"success": True, "message": f"Deleted {name}"})
        except Exception as e:
            self.send_json({"success": False, "message": str(e)})

    # ── Schedules ──
    def handle_schedules(self):
        schedules = load_schedules()
        # Add last_run info
        for s in schedules:
            name = s.get("name", "")
            if name in last_runs:
                s["last_run"] = datetime.fromtimestamp(last_runs[name]).strftime("%Y-%m-%d %H:%M:%S")
            else:
                s["last_run"] = "Never"
        self.send_json({"schedules": schedules})

    def handle_schedule_create(self):
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body"})
            return
        name = body.get("name", "").strip()
        command = body.get("command", "").strip()
        interval_minutes = int(body.get("interval_minutes", 5))
        enabled = body.get("enabled", True)

        if not name or not command:
            self.send_json({"success": False, "message": "Name and command are required"})
            return

        schedules = load_schedules()
        # Check for duplicate name
        for s in schedules:
            if s.get("name") == name:
                self.send_json({"success": False, "message": "Task name already exists"})
                return

        schedules.append({
            "name": name,
            "command": command,
            "interval_seconds": interval_minutes * 60,
            "interval_minutes": interval_minutes,
            "enabled": enabled,
        })
        save_schedules(schedules)
        self.send_json({"success": True, "message": f"Created schedule: {name}"})

    def handle_schedule_delete(self):
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body"})
            return
        name = body.get("name", "")
        schedules = load_schedules()
        schedules = [s for s in schedules if s.get("name") != name]
        save_schedules(schedules)
        if name in last_runs:
            del last_runs[name]
        self.send_json({"success": True, "message": f"Deleted schedule: {name}"})

    def handle_schedule_toggle(self):
        body = self.read_body()
        if not body:
            self.send_json({"success": False, "message": "Missing body"})
            return
        name = body.get("name", "")
        schedules = load_schedules()
        for s in schedules:
            if s.get("name") == name:
                s["enabled"] = not s.get("enabled", True)
                break
        save_schedules(schedules)
        self.send_json({"success": True, "message": f"Toggled schedule: {name}"})

    # ── Logs ──
    def handle_logs(self, search="", lines=200):
        log_file = os.path.join(SERVER_DIR, "logs", "latest.log")
        if not os.path.exists(log_file):
            self.send_json({"lines": [], "total": 0, "search": search})
            return
        try:
            result = subprocess.run(
                ["tail", f"-{lines}", log_file],
                capture_output=True, text=True, timeout=10
            )
            all_lines = result.stdout.split("\n")
            if search:
                filtered = [l for l in all_lines if search.lower() in l.lower()]
            else:
                filtered = all_lines
            self.send_json({"lines": filtered, "total": len(filtered), "search": search})
        except Exception as e:
            self.send_json({"error": str(e)})

    # ── Server Info ──
    def handle_server_info(self):
        props = read_properties()
        version = get_server_version()
        paper_ver = get_paper_version()
        world_sizes = get_world_sizes()
        plugins = get_installed_plugins()

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
        })


# ── Main ──
if __name__ == "__main__":
    print(f"🔐 Panel password: {PANEL_PASSWORD}")
    print(f"🖥️  MCPanel starting on port {PANEL_PORT}...")
    print(f"📁 Server directory: {SERVER_DIR}")

    # Start schedule runner thread
    sched_thread = threading.Thread(target=schedule_runner, daemon=True)
    sched_thread.start()
    print("⏰ Schedule runner started")

    server = HTTPServer(("0.0.0.0", PANEL_PORT), PanelHandler)
    print(f"✅ MCPanel running at http://0.0.0.0:{PANEL_PORT}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 MCPanel shutting down...")
        schedule_running = False
        server.server_close()
