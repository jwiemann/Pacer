"""Local stand-ins for the Paperclip API and the Anthropic usage endpoint, served on 127.0.0.1 from a thread.

The fake records every request and keeps agent status like the real server does (pause/resume POSTs change it), so a
governor tick sees the effect of the previous one.
"""
import json, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import fsutil
import pacer_config

COMPANY = "co-1"
CONNECTION = {"id": "conn-1", "name": "Personal", "provider": "anthropic", "method": "subscription", "ownership": "personal"}


def point_pacer_at(url):
    """Write the pre-resolved config (fresh, so no probe runs) with Paperclip answering at `url`."""
    cfg = {
        "instance": "default", "serverTask": "Paperclip Server (WSL)", "repairTask": "Paperclip WSL Repair",
        "systemdUnit": "paperclip.service", "dir": pacer_config.DIR, "dirWsl": "/mnt/x", "wslDistro": "Ubuntu",
        "wslHome": "/home/tester", "wslPkg": "/home/tester/pkg", "wslNodeBin": "/home/tester/bin",
        "wslInstance": "/home/tester/.paperclip/instances/default", "runLogs": None, "serverControl": "none",
        "url": url, "db": dict(pacer_config.DEFAULT_DB, port=pacer_config.DEFAULT_PG_PORT),
        "claudeExe": "no-such-claude.exe", "detectedAt": time.time(), "overridesMtime": 0,
    }
    fsutil.write_json_atomic(pacer_config.DETECTED, cfg)


class FakePaperclip:
    def __init__(self):
        self.lock = threading.Lock()
        self.agents = []                 # [{"id", "name", "adapterType", "status"}]
        self.live_agent_ids = set()      # agents with a run in progress
        self.companies = [{"id": COMPANY, "status": "active"}]
        self.health = (200, {"status": "ok"})
        self.usage = (200, {})           # (status, body) of GET /api/oauth/usage; bytes are sent as is
        self.requests = []               # (method, path) in arrival order
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self._answer("GET")

            def do_POST(self):
                self._answer("POST")

            def _answer(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                status, body = fake.handle(method, self.path)
                data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # ---- test setup
    def add_agent(self, name, adapter, status="active", model=None):
        agent_id = f"agent-{len(self.agents) + 1}"
        self.agents.append({"id": agent_id, "name": name, "adapterType": adapter, "status": status,
                            "adapterConfig": {"model": model}})
        return agent_id

    def status(self, agent_id):
        return next(a["status"] for a in self.agents if a["id"] == agent_id)

    def set_usage(self, five, seven, resets5=None, resets7=None):
        self.usage = (200, {"five_hour": {"utilization": five, "resets_at": resets5},
                            "seven_day": {"utilization": seven, "resets_at": resets7}})

    # ---- test assertions
    def posted(self, action):
        """Agent ids that received POST /api/agents/<id>/<action>, in arrival order."""
        return [p.split("/")[3] for m, p in self.requests
                if m == "POST" and p.startswith("/api/agents/") and p.endswith("/" + action)]

    def handle(self, method, path):
        with self.lock:
            self.requests.append((method, path))
            base = f"/api/companies/{COMPANY}"
            if path == "/api/health":
                return self.health
            if path == "/api/oauth/usage":
                return self.usage
            if path == "/api/companies":
                return 200, self.companies
            if path == f"{base}/agents":
                return 200, [dict(a) for a in self.agents]
            if path == f"{base}/live-runs":
                return 200, [{"agentId": i} for i in sorted(self.live_agent_ids)]
            if path == f"{base}/ai-connections":
                return 200, {"connections": [CONNECTION]}
            if path == f"{base}/ai-connections/local/attempts":
                return 200, {"sessionId": "3f1c2a4e-5b6d-4e7f-8a9b-0c1d2e3f4a5b"}
            if path == f"{base}/ai-connections/local":
                return 200, {}
            parts = path.split("/")  # /api/agents/<id>/<pause|resume>
            if method == "POST" and len(parts) == 5 and parts[2] == "agents" and parts[4] in ("pause", "resume"):
                agent = next((a for a in self.agents if a["id"] == parts[3]), None)
                if agent is None:
                    return 404, {"error": "no such agent"}
                agent["status"] = "paused" if parts[4] == "pause" else "active"
                return 200, {}
            return 404, {"error": f"not served by the fake: {method} {path}"}
