"""Shared data layer for the Claude usage dashboard + tray icon.

Two independent data sources:
  - Live plan limits: api.anthropic.com/api/oauth/usage, an undocumented
    endpoint the Claude Code CLI itself calls (using the same OAuth token
    from ~/.claude/.credentials.json) to show its own usage warnings.
  - Historical token/cost breakdown: parsed from local Claude Code session
    logs (~/.claude/projects/**/*.jsonl), which record per-request token
    usage but not plan-limit percentages.
"""
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CREDS_PATH = Path.home() / ".claude" / ".credentials.json"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
CACHE_DIR = Path.home() / ".cache" / "claude-usage"
USAGE_CACHE_FILE = CACHE_DIR / "usage.json"

LOGIN_EXPIRED = "Claude Code's login has expired. Use Claude Code once to renew it."
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

# The usage endpoint 429s if polled faster than this — it's undocumented and
# clearly not built for high-frequency polling.
MIN_REFRESH_INTERVAL_S = 300

# $/MTok. Source: platform.claude.com/docs/en/about-claude/pricing (2026-08-27).
# Cache write/read are derived multipliers (1.25x / 2x / 0.1x of base input)
# but hardcoded here since the JSONL logs don't carry a pricing version.
PRICING = {
    "claude-fable-5-1":  {"input": 10.00, "output": 50.00, "cache_write_5m": 12.50, "cache_write_1h": 20.00, "cache_read": 0.25},  # read is 0.025x, per the model docs
    "claude-mythos-5-1": {"input": 10.00, "output": 50.00, "cache_write_5m": 12.50, "cache_write_1h": 20.00, "cache_read": 1.00},  # ponytail: read rate unannounced at launch, 0.1x assumed
    "claude-fable-5":    {"input": 10.00, "output": 50.00, "cache_write_5m": 12.50, "cache_write_1h": 20.00, "cache_read": 1.00},
    "claude-mythos-5":   {"input": 10.00, "output": 50.00, "cache_write_5m": 12.50, "cache_write_1h": 20.00, "cache_read": 1.00},
    "claude-opus-5-5":   {"input": 4.00,  "output": 20.00, "cache_write_5m": 5.00,  "cache_write_1h": 8.00,  "cache_read": 0.20},  # read is 0.05x, per the model docs
    "claude-opus-5":     {"input": 5.00,  "output": 25.00, "cache_write_5m": 6.25,  "cache_write_1h": 10.00, "cache_read": 0.50},
    "claude-opus-4-8":   {"input": 5.00,  "output": 25.00, "cache_write_5m": 6.25,  "cache_write_1h": 10.00, "cache_read": 0.50},
    "claude-opus-4-7":   {"input": 5.00,  "output": 25.00, "cache_write_5m": 6.25,  "cache_write_1h": 10.00, "cache_read": 0.50},
    "claude-opus-4-6":   {"input": 5.00,  "output": 25.00, "cache_write_5m": 6.25,  "cache_write_1h": 10.00, "cache_read": 0.50},
    "claude-opus-4-5":   {"input": 5.00,  "output": 25.00, "cache_write_5m": 6.25,  "cache_write_1h": 10.00, "cache_read": 0.50},
    "claude-opus-4-1":   {"input": 15.00, "output": 75.00, "cache_write_5m": 18.75, "cache_write_1h": 30.00, "cache_read": 1.50},
    "claude-opus-4":     {"input": 15.00, "output": 75.00, "cache_write_5m": 18.75, "cache_write_1h": 30.00, "cache_read": 1.50},
    "claude-sonnet-5":   {"input": 2.00,  "output": 10.00, "cache_write_5m": 2.50,  "cache_write_1h": 4.00,  "cache_read": 0.20},
    "claude-sonnet-4-6": {"input": 3.00,  "output": 15.00, "cache_write_5m": 3.75,  "cache_write_1h": 6.00,  "cache_read": 0.30},
    "claude-sonnet-4-5": {"input": 3.00,  "output": 15.00, "cache_write_5m": 3.75,  "cache_write_1h": 6.00,  "cache_read": 0.30},
    "claude-sonnet-4":   {"input": 3.00,  "output": 15.00, "cache_write_5m": 3.75,  "cache_write_1h": 6.00,  "cache_read": 0.30},
    "claude-haiku-4-5":  {"input": 1.00,  "output": 5.00,  "cache_write_5m": 1.25,  "cache_write_1h": 2.00,  "cache_read": 0.10},
    "claude-haiku-3-5":  {"input": 0.80,  "output": 4.00,  "cache_write_5m": 1.00,  "cache_write_1h": 1.60,  "cache_read": 0.08},
}
DEFAULT_PRICING_MODEL = "claude-sonnet-5"  # fallback for models not in the table above


def price_for_model(model_id):
    known = model_id in PRICING
    return PRICING.get(model_id, PRICING[DEFAULT_PRICING_MODEL]), known


def cost_for_usage(model_id, usage):
    """usage: dict with input_tokens, output_tokens, cache_creation_input_tokens,
    cache_read_input_tokens, and optionally cache_creation.{ephemeral_5m,1h}_input_tokens."""
    prices, known = price_for_model(model_id)
    cache_creation = usage.get("cache_creation") or {}
    write_5m = cache_creation.get("ephemeral_5m_input_tokens")
    write_1h = cache_creation.get("ephemeral_1h_input_tokens")
    if write_5m is None and write_1h is None:
        # No breakdown available — assume the (usually short-lived) 5m tier.
        write_5m = usage.get("cache_creation_input_tokens", 0) or 0
        write_1h = 0
    cost = (
        usage.get("input_tokens", 0) * prices["input"]
        + usage.get("output_tokens", 0) * prices["output"]
        + (write_5m or 0) * prices["cache_write_5m"]
        + (write_1h or 0) * prices["cache_write_1h"]
        + usage.get("cache_read_input_tokens", 0) * prices["cache_read"]
    ) / 1_000_000
    return cost, known


# ---------------------------------------------------------------------------
# Live plan usage (OAuth endpoint)
# ---------------------------------------------------------------------------

class UsageError(Exception):
    pass


def _http(url, body=None, headers=None):
    """JSON in, JSON out. POSTs when `body` is given."""
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        # Cloudflare has 403'd (error 1010) the default "Python-urllib" User-Agent.
        headers={"Content-Type": "application/json", "User-Agent": "claude-usage-app", **(headers or {})},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def _load_credentials():
    if not CREDS_PATH.exists():
        raise UsageError("Not logged in — run `claude` once to authenticate.")
    with open(CREDS_PATH) as f:
        data = json.load(f)
    oauth = data.get("claudeAiOauth")
    if not oauth or not oauth.get("accessToken"):
        raise UsageError("No Claude Code OAuth token found — run `claude` to log in.")
    return data, oauth


def _get_access_token():
    """Read-only: the token Claude Code itself keeps fresh. This app never
    refreshes or rewrites Claude Code's login, so it can't disturb it; an
    expired login just means using Claude Code once to renew it."""
    _data, oauth = _load_credentials()
    expires_at_ms = oauth.get("expiresAt", 0)
    if expires_at_ms and expires_at_ms / 1000 <= time.time():
        raise UsageError(LOGIN_EXPIRED)
    return oauth["accessToken"]


def _read_cache():
    try:
        with open(USAGE_CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def _write_cache(payload):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(CACHE_DIR))
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    os.replace(tmp_path, USAGE_CACHE_FILE)


def fetch_usage(force=False):
    """Returns {"usage": {...}, "fetched_at": epoch, "stale": bool, "error": str|None}."""
    cached = _read_cache()
    now = time.time()
    if cached and not force and now - cached.get("fetched_at", 0) < MIN_REFRESH_INTERVAL_S:
        return {**cached, "stale": False}

    try:
        token = _get_access_token()
        usage = _http(USAGE_URL, headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
        })
        payload = {"usage": usage, "fetched_at": now, "error": None}
        _write_cache(payload)
        return {**payload, "stale": False}
    except (UsageError, OSError, ValueError) as e:
        if getattr(e, "code", None) == 429:
            msg = "Rate limited by the usage endpoint — try again in a few minutes."
        elif getattr(e, "code", None) == 401:
            msg = LOGIN_EXPIRED
        elif isinstance(e, UsageError):
            msg = str(e)
        else:
            msg = f"Network error: {e}"
        return {**(cached or {"usage": None, "fetched_at": now}), "stale": True, "error": msg}


# ---------------------------------------------------------------------------
# Historical token/cost breakdown (local session logs)
# ---------------------------------------------------------------------------

def scan_history(days=30):
    """Walks every ~/.claude/projects/**/*.jsonl and aggregates token usage
    from assistant messages that carry a `usage` block."""
    cutoff = time.time() - days * 86400
    by_day = {}      # "YYYY-MM-DD" -> {"cost": float, "tokens": int}
    by_model = {}     # model_id -> {"input":.., "output":.., "cache_read":.., "cache_write":.., "cost":.., "known": bool}
    by_project = {}   # cwd -> {"cost": float, "sessions": set()}
    total_cost = 0.0
    total_cost_all_time = 0.0
    total_tokens = 0
    session_ids = set()

    unknown_models = set()

    for jsonl_path in PROJECTS_DIR.rglob("*.jsonl"):
        try:
            with open(jsonl_path, "r", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    message = entry.get("message") or {}
                    usage = message.get("usage") or entry.get("usage")
                    if not usage:
                        continue
                    model_id = message.get("model") or entry.get("model")
                    if not model_id or model_id == "<synthetic>":
                        continue

                    ts_raw = entry.get("timestamp")
                    try:
                        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        continue

                    cost, known = cost_for_usage(model_id, usage)
                    if not known:
                        unknown_models.add(model_id)

                    tokens = (
                        usage.get("input_tokens", 0)
                        + usage.get("output_tokens", 0)
                        + usage.get("cache_creation_input_tokens", 0)
                        + usage.get("cache_read_input_tokens", 0)
                    )

                    total_cost_all_time += cost

                    session_id = entry.get("sessionId") or entry.get("session_id")
                    if session_id:
                        session_ids.add(session_id)

                    cwd = entry.get("cwd") or "unknown"
                    proj = by_project.setdefault(cwd, {"cost": 0.0, "sessions": set()})
                    proj["cost"] += cost
                    if session_id:
                        proj["sessions"].add(session_id)

                    if ts < cutoff:
                        continue

                    total_cost += cost
                    total_tokens += tokens

                    day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                    d = by_day.setdefault(day, {"cost": 0.0, "tokens": 0})
                    d["cost"] += cost
                    d["tokens"] += tokens

                    m = by_model.setdefault(model_id, {
                        "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
                        "cost": 0.0, "known": known,
                    })
                    m["input"] += usage.get("input_tokens", 0)
                    m["output"] += usage.get("output_tokens", 0)
                    m["cache_read"] += usage.get("cache_read_input_tokens", 0)
                    m["cache_write"] += usage.get("cache_creation_input_tokens", 0)
                    m["cost"] += cost
        except OSError:
            continue

    by_project_out = {
        cwd: {"cost": v["cost"], "sessions": len(v["sessions"])}
        for cwd, v in by_project.items()
    }

    return {
        "by_day": by_day,
        "by_model": by_model,
        "by_project": by_project_out,
        "total_cost_30d": total_cost,
        "total_cost_all_time": total_cost_all_time,
        "total_tokens_30d": total_tokens,
        "session_count": len(session_ids),
        "unknown_models": sorted(unknown_models),
    }
