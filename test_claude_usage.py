"""AI-Usage-Gauge self-check. FAKES ONLY: dummy credentials, loopback usage/token
server, made-up session logs, a private single-instance socket. Never reads or
writes the real ~/.claude files and never calls Anthropic."""
import json, os, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent"  # no real tray icon from tests
TMP = Path(tempfile.mkdtemp(prefix="cutest-"))
PORT = 18290
mode = {"usage": 200, "refreshes": 0}
USAGE = {"limits": [{"kind": "session", "percent": 42, "resets_at": "2099-01-01T00:00:00Z"},
                    {"kind": "weekly_all", "percent": 91}]}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _json(self, code, body):
        b = json.dumps(body).encode()
        self.send_response(code); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        assert self.headers["Authorization"].startswith("Bearer dummy-")
        self._json(mode["usage"], USAGE if mode["usage"] == 200 else {"error": "x"})
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert body["refresh_token"] == "dummy-refresh"
        mode["refreshes"] += 1
        self._json(200, {"access_token": "dummy-new", "expires_in": 3600})

def setup(core):
    core.CREDS_PATH = TMP / "creds.json"
    core.PROJECTS_DIR = TMP / "projects"
    core.CACHE_DIR = TMP / "cache"
    core.USAGE_CACHE_FILE = core.CACHE_DIR / "usage.json"
    core.USAGE_URL = f"http://127.0.0.1:{PORT}/usage"
    core.TOKEN_URL = f"http://127.0.0.1:{PORT}/token"

def write_creds(expires_in_s):
    (TMP / "creds.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "dummy-old", "refreshToken": "dummy-refresh",
        "expiresAt": int((time.time() + expires_in_s) * 1000)}}))

if len(sys.argv) > 1 and sys.argv[1] == "--child":
    # A full app instance with every path/URL/socket pointed at the fakes.
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path.insert(0, str(HERE))
    import claude_usage as cu
    setup(cu.core)
    cu.os.environ["QT_QPA_PLATFORM"] = "offscreen"
    cu.SOCKET_NAME = f"claude-usage-test-{os.getpid() if sys.argv[2] == 'own' else sys.argv[2]}"
    cu.present = lambda win: print("PRESENTED", flush=True)
    sys.argv = [sys.argv[0], "--tray"]
    sys.exit(cu.main())

srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
sys.path.insert(0, str(HERE))
import claude_usage_core as core
setup(core)
assert str(core.CREDS_PATH).startswith(str(TMP)) and "127.0.0.1" in core.USAGE_URL

results = []
def check(name, cond):
    results.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name)

write_creds(3600)
r = core.fetch_usage()
check("fetch returns live usage", r["usage"] == USAGE and not r["stale"] and r["error"] is None)
check("cache written", core.USAGE_CACHE_FILE.exists())
mode["usage"] = 500
check("within interval: served from cache, no call", core.fetch_usage()["usage"] == USAGE)
mode["usage"] = 429
r = core.fetch_usage(force=True)
check("429 -> stale cache + rate-limit message", r["stale"] and r["usage"] == USAGE and "Rate limited" in r["error"])
mode["usage"] = 500
r = core.fetch_usage(force=True)
check("500 -> stale cache + network error", r["stale"] and r["error"].startswith("Network error"))
core.USAGE_CACHE_FILE.unlink()
core.USAGE_URL = "http://127.0.0.1:1/usage"
r = core.fetch_usage(force=True)
check("unreachable, no cache -> usage None", r["usage"] is None and r["stale"] and "Network error" in r["error"])
core.USAGE_URL = f"http://127.0.0.1:{PORT}/usage"
(TMP / "creds.json").unlink()
check("no credentials -> login message", "Not logged in" in core.fetch_usage(force=True)["error"])
mode["usage"] = 200
write_creds(-60)  # already expired: this app must never refresh it
before = (TMP / "creds.json").read_bytes()
r = core.fetch_usage(force=True)
check("expired login: says so, never refreshes", "expired" in (r["error"] or "") and mode["refreshes"] == 0)
check("credentials file never rewritten", (TMP / "creds.json").read_bytes() == before)
write_creds(3600)
mode["usage"] = 401
check("a 401 also reads as an expired login", "expired" in (core.fetch_usage(force=True)["error"] or ""))
mode["usage"] = 200

h = core.scan_history()
check("missing logs folder -> empty totals", h["session_count"] == 0 and h["total_cost_all_time"] == 0 and h["by_day"] == {})
proj = TMP / "projects" / "p"; proj.mkdir(parents=True)
now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
lines = [{"timestamp": now, "sessionId": "s1", "cwd": "/x", "message": {"model": "claude-opus-5",
          "usage": {"input_tokens": 1_000_000, "output_tokens": 0}}},
         {"timestamp": now, "sessionId": "s2", "cwd": "/y", "message": {"model": "mystery-model",
          "usage": {"input_tokens": 0, "output_tokens": 1_000_000}}},
         "not json"]
(proj / "a.jsonl").write_text("\n".join(l if isinstance(l, str) else json.dumps(l) for l in lines))
h = core.scan_history()
check("history cost: opus 5 $5 + fallback $10", abs(h["total_cost_30d"] - 15.0) < 1e-9)
check("history counts sessions and unknown models", h["session_count"] == 2 and h["unknown_models"] == ["mystery-model"])

# -- dashboard (fake data, offscreen) -----------------------------------------
import claude_usage as cu
from datetime import timedelta
from PyQt6.QtWidgets import QApplication
app = QApplication(["t", "-platform", "offscreen"])
check("severity has a word + glyph for every band",
      [cu.severity(p)[1:] for p in (10, 75, 95)] == [("●", "OK"), ("▲", "High"), ("■", "Near limit")])
check("token formatting", [cu.fmt_tokens(n) for n in (999, 1234, 10_000_000, 9_600_000, 2_194_700_000)] == ["999", "1.2K", "10M", "9.6M", "2.2B"])
check("tick steps are round", [cu.nice_step(x) for x in (3, 70, 1400, 3_200_000)] == [5, 100, 2000, 5_000_000])
ctl = cu.UsageController()
ctl.latest = {"usage": {"limits": [{"kind": "session", "percent": 95}, {"kind": "weekly_all", "percent": 20},
                                   {"kind": "weekly_opus", "percent": 50}],
                        "spend": {"enabled": True, "percent": 10, "used": {"amount_minor": 500, "exponent": 2},
                                  "limit": {"amount_minor": 5000, "exponent": 2}}},
              "fetched_at": time.time(), "stale": False, "error": None}
core.scan_history = lambda days=30: h   # the fake-log result from above
w = cu.Dashboard(ctl)
w.show(); app.processEvents(); w._history_thread.wait(); app.processEvents()
check("session gauge shows 95%, status in words", w.session.gauge.pct == 95 and "Near limit" in w.session.status.text())
check("dial maps 0/50/100% to 210/90/-30 degrees and clamps", [cu.gauge_angle(v) for v in (0, 50, 100, 150, -5)] == [210, 90, -30, -30, 210])
check("unknown limit + spend become meter rows", w.extra.count() == 2)
check("chart always spans 14 days, gaps as zero", len(w.chart.days) == 14 and sum(d[1] for d in w.chart.days) == h["total_tokens_30d"])
check("table view has the same 14 days", w.daily_table.rowCount() == 14)
w.table_btn.click()
check("Table button swaps the chart for the table", w.daily_stack.currentIndex() == 1)
check("model rows listed, unknown price flagged",
      w.models.lay.count() == 2 and any("estimated price" in l.text() for l in w.models.findChildren(cu.QLabel)))
w._on_limits_result({"usage": None, "fetched_at": time.time(), "stale": True, "error": "Network error: x"})
check("no data: gauge empty and the error is spelled out",
      w.session.gauge.pct is None and "Network error" in w.limits_error.text() and w.extra.count() == 0)
many = {f"/p{i}": {"cost": float(20 - i), "sessions": 1} for i in range(12)}
w._on_history_result({**h, "by_project": many})
app.processEvents()
names = [l.text() for l in w.projects.findChildren(cu.QLabel)]
check("projects: top 8 + an Other row", w.projects.lay.count() == 9 and any(n.startswith("Other (4 projects)") for n in names))
tray = cu.UsageTray(ctl)
tray._on_usage(ctl.latest)
check("tray tooltip names the severity", "Near limit" in tray.toolTip() and "OK" in tray.toolTip())

# Two app instances on a private socket: the second hands off and exits 0.
env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent"}
name = f"x{os.getpid()}"
first = subprocess.Popen([sys.executable, __file__, "--child", name], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
time.sleep(3)
second = subprocess.run([sys.executable, __file__, "--child", name], env=env,
                        capture_output=True, text=True, timeout=20)
time.sleep(1)
first.terminate()
out = first.communicate(timeout=10)[0]
check("second launch exits 0 without showing itself", second.returncode == 0 and "PRESENTED" not in second.stdout)
check("first instance was asked to show once", out.count("PRESENTED") == 1)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
