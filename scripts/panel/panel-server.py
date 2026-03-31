#!/usr/bin/env python3

from http.server import HTTPServer, SimpleHTTPRequestHandler
import json
import subprocess
import os
import shutil
import urllib.parse
import time

SERVER_DIR = os.path.abspath("minecraft-server")
PANEL_DIR = os.path.dirname(os.path.abspath(__file__))

# ✅ NEW: Configure your Cloudflare domains here
FRONTEND_DOMAIN = "https://panel.projectxglory.qzz.io"
API_DOMAIN = "https://wings.projectxglory.qzz.io"

class PanelHandler(SimpleHTTPRequestHandler):

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self.serve_file(os.path.join(PANEL_DIR, "index.html"), "text/html")
            return

        routes = {
            "/api/status": lambda: self.handle_status(),
            "/api/console": lambda: self.handle_console(),
            "/api/players": lambda: self.handle_players(),
            "/api/stats": lambda: self.handle_stats(),
            "/api/properties": lambda: self.handle_get_properties(),
            "/api/ops": lambda: self.handle_ops_list(),
            "/api/banned": lambda: self.handle_banned_list(),
        }

        if path in routes:
            routes[path]()
        elif path == "/api/files":
            self.handle_files(params.get("path", ["/"])[0])
        elif path == "/api/file/read":
            self.handle_read_file(params.get("path", [""])[0])
        elif path == "/api/file/download":
            self.handle_download(params.get("path", [""])[0])
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/file/upload":
            self.handle_upload()
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"

        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            data = {}

        routes = {
            "/api/command": lambda: self.handle_command(data),
            "/api/start": lambda: self.handle_start(),
            "/api/stop": lambda: self.handle_stop(),
            "/api/restart": lambda: self.handle_restart(),
            "/api/backup": lambda: self.handle_backup(),
            "/api/file/save": lambda: self.handle_save_file(data),
            "/api/file/delete": lambda: self.handle_delete_file(data),
            "/api/file/create": lambda: self.handle_create_file(data),
            "/api/file/mkdir": lambda: self.handle_mkdir(data),
            "/api/file/rename": lambda: self.handle_rename_file(data),
            "/api/properties": lambda: self.handle_save_properties(data),
            "/api/kick": lambda: self.handle_kick(data),
            "/api/ban": lambda: self.handle_ban(data),
            "/api/unban": lambda: self.handle_unban(data),
            "/api/op": lambda: self.handle_op(data),
            "/api/deop": lambda: self.handle_deop(data),
            "/api/whitelist": lambda: self.handle_whitelist(data),
            "/api/gamemode": lambda: self.handle_gamemode(data),
            "/api/tp": lambda: self.handle_tp(data),
            "/api/say": lambda: self.handle_say(data),
        }

        if path in routes:
            routes[path]()
        else:
            self.send_error(404)

    # ✅ UPDATED: Enhanced send_json with CORS headers
    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        # ✅ NEW: Specific Cloudflare domain CORS
        self.send_header("Access-Control-Allow-Origin", FRONTEND_DOMAIN)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "3600")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def serve_file(self, filepath, content_type):
        try:
            with open(filepath, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_error(404)

    def mc_cmd(self, cmd):
        subprocess.run(
            ["screen", "-S", "minecraft", "-X", "stuff", f"{cmd}\r"],
            capture_output=True,
        )

    # ── server controls ──

    def handle_status(self):
        result = subprocess.run(["screen", "-list"], capture_output=True, text=True)
        running = "minecraft" in result.stdout

        uptime = "N/A"
        if running:
            try:
                pr = subprocess.run(["pgrep", "-f", "paper.jar"], capture_output=True, text=True)
                if pr.stdout.strip():
                    pid = pr.stdout.strip().split("\n")[0]
                    et = subprocess.run(["ps", "-p", pid, "-o", "etime="], capture_output=True, text=True)
                    uptime = et.stdout.strip()
            except Exception:
                pass

        disk = shutil.disk_usage(SERVER_DIR)

        ram_total = ram_used = 0
        try:
            mr = subprocess.run(["free", "-m"], capture_output=True, text=True)
            parts = mr.stdout.strip().split("\n")[1].split()
            ram_total = int(parts[1])
            ram_used = int(parts[2])
        except Exception:
            pass

        cpu = 0
        try:
            cr = subprocess.run(["grep", "cpu ", "/proc/stat"], capture_output=True, text=True)
            p = cr.stdout.split()
            cpu = round((int(p[1]) + int(p[3])) / sum(int(x) for x in p[1:]) * 100, 1)
        except Exception:
            pass

        self.send_json({
            "running": running,
            "uptime": uptime,
            "cpu": cpu,
            "ram_used": ram_used,
            "ram_total": ram_total,
            "disk_used": round(disk.used / (1024 ** 3), 2),
            "disk_total": round(disk.total / (1024 ** 3), 2),
            "java_version": subprocess.run(
                ["java", "-version"], capture_output=True, text=True
            ).stderr.split("\n")[0],
        })

    def handle_start(self):
        r = subprocess.run(["screen", "-list"], capture_output=True, text=True)
        if "minecraft" in r.stdout:
            self.send_json({"success": False, "message": "Server already running"})
            return
        os.chdir(SERVER_DIR)
        subprocess.Popen(["screen", "-dmS", "minecraft", "java", "-Xms6G", "-Xmx12G", "-jar", "paper.jar", "--nogui"])
        os.chdir(PANEL_DIR)
        self.send_json({"success": True, "message": "Server starting..."})

    def handle_stop(self):
        self.mc_cmd("stop")
        self.send_json({"success": True, "message": "Server stopping..."})

    def handle_restart(self):
        self.mc_cmd("stop")
        time.sleep(10)
        os.chdir(SERVER_DIR)
        subprocess.Popen(["screen", "-dmS", "minecraft", "java", "-Xms6G", "-Xmx12G", "-jar", "paper.jar", "--nogui"])
        os.chdir(PANEL_DIR)
        self.send_json({"success": True, "message": "Server restarting..."})

    def handle_command(self, data):
        cmd = data.get("command", "")
        if not cmd:
            self.send_json({"success": False, "message": "No command"})
            return
        self.mc_cmd(cmd)
        self.send_json({"success": True, "message": f"Sent: {cmd}"})

    def handle_say(self, data):
        msg = data.get("message", "")
        if not msg:
            self.send_json({"success": False, "message": "No message"})
            return
        self.mc_cmd(f"say {msg}")
        self.send_json({"success": True, "message": f"Broadcast: {msg}"})

    # ── console ──

    def handle_console(self):
        log = os.path.join(SERVER_DIR, "logs", "latest.log")
        try:
            with open(log, "r") as f:
                lines = f.readlines()
            self.send_json({"logs": "".join(lines[-150:])})
        except FileNotFoundError:
            self.send_json({"logs": "No log file found."})

    # ── players ──

    def handle_players(self):
        log = os.path.join(SERVER_DIR, "logs", "latest.log")
        players = []
        try:
            with open(log, "r") as f:
                for line in f:
                    if "joined the game" in line:
                        n = line.split("]: ")[1].split(" joined")[0].strip()
                        if n not in players:
                            players.append(n)
                    if "left the game" in line:
                        n = line.split("]: ")[1].split(" left")[0].strip()
                        if n in players:
                            players.remove(n)
        except Exception:
            pass
        self.send_json({"players": players, "count": len(players)})

    def handle_stats(self):
        log = os.path.join(SERVER_DIR, "logs", "latest.log")
        s = {"total_joins": 0, "total_leaves": 0, "deaths": 0, "advancements": 0, "chat_messages": 0}
        try:
            with open(log, "r") as f:
                for line in f:
                    if "joined the game" in line: s["total_joins"] += 1
                    elif "left the game" in line: s["total_leaves"] += 1
                    elif any(w in line for w in ["died", "was slain", "was killed", "was shot", "fell", "drowned", "burned", "blew up", "withered", "was squished"]): s["deaths"] += 1
                    elif "has made the advancement" in line: s["advancements"] += 1
                    elif "<" in line and ">" in line: s["chat_messages"] += 1
        except Exception:
            pass
        self.send_json(s)

    # ── player management ──

    def handle_kick(self, d):
        p = d.get("player", "")
        r = d.get("reason", "Kicked by admin")
        if not p: self.send_json({"success": False, "message": "No player"}); return
        self.mc_cmd(f"kick {p} {r}")
        self.send_json({"success": True, "message": f"Kicked {p}"})

    def handle_ban(self, d):
        p = d.get("player", "")
        r = d.get("reason", "Banned by admin")
        if not p: self.send_json({"success": False, "message": "No player"}); return
        self.mc_cmd(f"ban {p} {r}")
        self.send_json({"success": True, "message": f"Banned {p}"})

    def handle_unban(self, d):
        p = d.get("player", "")
        if not p: self.send_json({"success": False, "message": "No player"}); return
        self.mc_cmd(f"pardon {p}")
        self.send_json({"success": True, "message": f"Unbanned {p}"})

    def handle_op(self, d):
        p = d.get("player", "")
        if not p: self.send_json({"success": False, "message": "No player"}); return
        self.mc_cmd(f"op {p}")
        self.send_json({"success": True, "message": f"Opped {p}"})

    def handle_deop(self, d):
        p = d.get("player", "")
        if not p: self.send_json({"success": False, "message": "No player"}); return
        self.mc_cmd(f"deop {p}")
        self.send_json({"success": True, "message": f"De-opped {p}"})

    def handle_whitelist(self, d):
        action = d.get("action", "")
        p = d.get("player", "")
        if action == "list":
            wf = os.path.join(SERVER_DIR, "whitelist.json")
            try:
                with open(wf, "r") as f: wl = json.load(f)
                self.send_json({"success": True, "whitelist": wl})
            except Exception:
                self.send_json({"success": True, "whitelist": []})
            return
        if not p: self.send_json({"success": False, "message": "No player"}); return
        self.mc_cmd(f"whitelist {action} {p}")
        self.send_json({"success": True, "message": f"Whitelist {action}: {p}"})

    def handle_gamemode(self, d):
        p = d.get("player", "")
        m = d.get("mode", "survival")
        if not p: self.send_json({"success": False, "message": "No player"}); return
        self.mc_cmd(f"gamemode {m} {p}")
        self.send_json({"success": True, "message": f"{p} → {m}"})

    def handle_tp(self, d):
        p = d.get("player", "")
        t = d.get("target", "")
        if not p or not t: self.send_json({"success": False, "message": "Missing fields"}); return
        self.mc_cmd(f"tp {p} {t}")
        self.send_json({"success": True, "message": f"Teleported {p} → {t}"})

    def handle_ops_list(self):
        f = os.path.join(SERVER_DIR, "ops.json")
        try:
            with open(f, "r") as fh: data = json.load(fh)
            self.send_json({"success": True, "ops": data})
        except Exception:
            self.send_json({"success": True, "ops": []})

    def handle_banned_list(self):
        f = os.path.join(SERVER_DIR, "banned-players.json")
        try:
            with open(f, "r") as fh: data = json.load(fh)
            self.send_json({"success": True, "banned": data})
        except Exception:
            self.send_json({"success": True, "banned": []})

    # ── files ──

    def handle_files(self, dir_path):
        full = os.path.abspath(os.path.join(SERVER_DIR, dir_path.lstrip("/")))
        if not full.startswith(SERVER_DIR):
            self.send_json({"error": "Access denied"}, 403); return
        if not os.path.exists(full):
            self.send_json({"error": "Not found"}, 404); return
        files = []
        try:
            for item in sorted(os.listdir(full)):
                ip = os.path.join(full, item)
                isd = os.path.isdir(ip)
                files.append({
                    "name": item,
                    "is_dir": isd,
                    "size": 0 if isd else os.path.getsize(ip),
                    "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(ip))),
                    "path": os.path.relpath(ip, SERVER_DIR),
                })
        except PermissionError:
            self.send_json({"error": "Permission denied"}, 403); return
        self.send_json({"current_path": os.path.relpath(full, SERVER_DIR), "files": files})

    def handle_read_file(self, fp):
        full = os.path.abspath(os.path.join(SERVER_DIR, fp.lstrip("/")))
        if not full.startswith(SERVER_DIR):
            self.send_json({"error": "Access denied"}, 403); return
        try:
            if os.path.getsize(full) > 5 * 1024 * 1024:
                self.send_json({"error": "File too large (>5MB)"}, 400); return
            with open(full, "r", errors="replace") as f: content = f.read()
            self.send_json({"content": content, "path": fp})
        except FileNotFoundError:
            self.send_json({"error": "Not found"}, 404)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def handle_save_file(self, d):
        fp = d.get("path", "")
        content = d.get("content", "")
        full = os.path.abspath(os.path.join(SERVER_DIR, fp.lstrip("/")))
        if not full.startswith(SERVER_DIR):
            self.send_json({"error": "Access denied"}, 403); return
        try:
            with open(full, "w") as f: f.write(content)
            self.send_json({"success": True, "message": "Saved"})
        except Exception as e:
            self.send_json({"success": False, "message": str(e)})

    def handle_delete_file(self, d):
        fp = d.get("path", "")
        full = os.path.abspath(os.path.join(SERVER_DIR, fp.lstrip("/")))
        if not full.startswith(SERVER_DIR):
            self.send_json({"error": "Access denied"}, 403); return
        try:
            if os.path.isdir(full): shutil.rmtree(full)
            else: os.remove(full)
            self.send_json({"success": True, "message": "Deleted"})
        except Exception as e:
            self.send_json({"success": False, "message": str(e)})

    def handle_create_file(self, d):
        fp = d.get("path", "")
        content = d.get("content", "")
        full = os.path.abspath(os.path.join(SERVER_DIR, fp.lstrip("/")))
        if not full.startswith(SERVER_DIR):
            self.send_json({"error": "Access denied"}, 403); return
        try:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f: f.write(content)
            self.send_json({"success": True, "message": "Created"})
        except Exception as e:
            self.send_json({"success": False, "message": str(e)})

    def handle_mkdir(self, d):
        dp = d.get("path", "")
        full = os.path.abspath(os.path.join(SERVER_DIR, dp.lstrip("/")))
        if not full.startswith(SERVER_DIR):
            self.send_json({"error": "Access denied"}, 403); return
        try:
            os.makedirs(full, exist_ok=True)
            self.send_json({"success": True, "message": "Created"})
        except Exception as e:
            self.send_json({"success": False, "message": str(e)})

    def handle_rename_file(self, d):
        old = os.path.abspath(os.path.join(SERVER_DIR, d.get("old_path", "").lstrip("/")))
        new = os.path.abspath(os.path.join(SERVER_DIR, d.get("new_path", "").lstrip("/")))
        if not old.startswith(SERVER_DIR) or not new.startswith(SERVER_DIR):
            self.send_json({"error": "Access denied"}, 403); return
        try:
            os.rename(old, new)
            self.send_json({"success": True, "message": "Renamed"})
        except Exception as e:
            self.send_json({"success": False, "message": str(e)})

    def handle_download(self, fp):
        full = os.path.abspath(os.path.join(SERVER_DIR, fp.lstrip("/")))
        if not full.startswith(SERVER_DIR) or not os.path.isfile(full):
            self.send_error(404); return
        try:
            sz = os.path.getsize(full)
            fn = os.path.basename(full)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{fn}"')
            self.send_header("Content-Length", str(sz))
            self.end_headers()
            with open(full, "rb") as f:
                while True:
                    chunk = f.read(8192)
                    if not chunk: break
                    self.wfile.write(chunk)
        except Exception:
            self.send_error(500)

    def handle_upload(self):
        ct = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ct:
            self.send_json({"success": False, "message": "Invalid request"}); return
        boundary = ct.split("boundary=")[1].encode()
        cl = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(cl)
        parts = body.split(b"--" + boundary)
        upload_path = ""
        file_data = b""
        filename = ""
        for part in parts:
            if b"Content-Disposition" not in part: continue
            he = part.find(b"\r\n\r\n")
            if he == -1: continue
            hdr = part[:he].decode("utf-8", errors="replace")
            dat = part[he + 4:]
            if dat.endswith(b"\r\n"): dat = dat[:-2]
            if 'name="path"' in hdr:
                upload_path = dat.decode("utf-8").strip()
            elif 'name="file"' in hdr:
                for h in hdr.split("\r\n"):
                    if "filename=" in h: filename = h.split('filename="')[1].split('"')[0]
                file_data = dat
        if not filename:
            self.send_json({"success": False, "message": "No file"}); return
        sd = os.path.abspath(os.path.join(SERVER_DIR, upload_path.lstrip("/")))
        if not sd.startswith(SERVER_DIR):
            self.send_json({"success": False, "message": "Access denied"}); return
        os.makedirs(sd, exist_ok=True)
        sp = os.path.join(sd, filename)
        try:
            with open(sp, "wb") as f: f.write(file_data)
            mb = len(file_data) / (1024 * 1024)
            self.send_json({"success": True, "message": f"Uploaded {filename} ({mb:.1f} MB)"})
        except Exception as e:
            self.send_json({"success": False, "message": str(e)})

    # ── properties ──

    def handle_get_properties(self):
        pf = os.path.join(SERVER_DIR, "server.properties")
        props = {}
        try:
            with open(pf, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        props[k.strip()] = v.strip()
            self.send_json({"properties": props})
        except Exception:
            self.send_json({"properties": {}})

    def handle_save_properties(self, d):
        props = d.get("properties", {})
        pf = os.path.join(SERVER_DIR, "server.properties")
        try:
            with open(pf, "w") as f:
                for k, v in props.items():
                    f.write(f"{k}={v}\n")
            self.send_json({"success": True, "message": "Saved. Restart to apply."})
        except Exception as e:
            self.send_json({"success": False, "message": str(e)})

    # ── backup ──

    def handle_backup(self):
        self.mc_cmd("save-all")
        time.sleep(5)
        r = subprocess.run(
            ["tar", "-czf", "backup-manual.tar.gz", "world/", "world_nether/", "world_the_end/", "server.properties", "plugins/"],
            cwd=SERVER_DIR, capture_output=True, text=True,
        )
        if r.returncode == 0:
            sz = os.path.getsize(os.path.join(SERVER_DIR, "backup-manual.tar.gz"))
            self.send_json({"success": True, "message": f"Backup done ({sz / 1048576:.1f} MB)"})
        else:
            self.send_json({"success": False, "message": "Backup failed"})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    port = 8080
    srv = HTTPServer(("0.0.0.0", port), PanelHandler)
    print(f"Panel → http://0.0.0.0:{port}")
    srv.serve_forever()
