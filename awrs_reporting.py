#!/usr/bin/env python3
"""
AWRS Reporting Dashboard — v2026-06-19
────────────────────────────────────────
Blended KPI dashboard pulling from Zuper, Azuga, and Paylocity.

Run:  python3 awrs_reporting.py
Open: http://localhost:8081

Env vars:
  ZUPER_API_KEY          Zuper API key (default: hardcoded key)
  AZUGA_USERNAME         Azuga login email
  AZUGA_PASSWORD         Azuga login password
  PAYLOCITY_CLIENT_ID    Paylocity OAuth client ID (future)
  PAYLOCITY_CLIENT_SECRET
  PAYLOCITY_COMPANY_ID
  AUTH_SECRET            Token signing secret (auto-generated if not set)
  PORT                   HTTP port (default 8081)
  WHEEL_TARGET           Weekly wheel target (default 500)
  OT_THRESHOLD           OT threshold in hours (default 40)
"""

import json, math, threading, time, os, uuid, re, hmac, hashlib, base64, urllib.request, urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta, date
import sys
sys.stdout.reconfigure(line_buffering=True)

# ── Config ────────────────────────────────────────────────
ZUPER_KEY   = os.environ.get("ZUPER_API_KEY", "05a82304b0074c2c9b32f7ca2a1ab682")
ZUPER_BASE  = "https://us-west-1c.zuperpro.com/api"
AZUGA_USER  = os.environ.get("AZUGA_USERNAME", "")
AZUGA_PASS  = os.environ.get("AZUGA_PASSWORD", "")
AZUGA_CLIENT_ID = "5decccae0a214939a77411a77eeff8fc"
PAY_CLIENT  = os.environ.get("PAYLOCITY_CLIENT_ID", "")
PAY_SECRET  = os.environ.get("PAYLOCITY_CLIENT_SECRET", "")
PAY_COMPANY = os.environ.get("PAYLOCITY_COMPANY_ID", "")
PORT        = int(os.environ.get("PORT", 8081))
WHEEL_TARGET = int(os.environ.get("WHEEL_TARGET", 500))
OT_THRESHOLD = int(os.environ.get("OT_THRESHOLD", 40))
IS_LOCAL    = not os.environ.get("RENDER")

# ── Auth ──────────────────────────────────────────────────
AUTH_SECRET   = os.environ.get("AUTH_SECRET") or uuid.uuid4().hex
ALLOWED_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@alloywheel\.com$", re.I)
AUTH_TTL      = 30 * 86400  # 30 days

def make_auth_token(email):
    exp = int(time.time()) + AUTH_TTL
    payload = f"{email}|{exp}"
    sig = hmac.new(AUTH_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(payload.encode()).decode() + "." + sig

def check_auth_token(token):
    try:
        b64, sig = token.split(".", 1)
        payload = base64.urlsafe_b64decode(b64.encode()).decode()
        good = hmac.new(AUTH_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good): return None
        email, exp = payload.rsplit("|", 1)
        if int(exp) < time.time(): return None
        return email
    except Exception:
        return None

# ── Zuper helpers ─────────────────────────────────────────
def zuper_get(path):
    req = urllib.request.Request(f"{ZUPER_BASE}{path}",
        headers={"x-api-key": ZUPER_KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

def zuper_post(path, body, timeout=20):
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{ZUPER_BASE}{path}", data=data,
        headers={"x-api-key": ZUPER_KEY, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

_teams_cache = {"data": None, "ts": 0}

def fetch_teams():
    if _teams_cache["data"] and time.time() - _teams_cache["ts"] < 300:
        return _teams_cache["data"]
    try:
        resp = zuper_get("/team?limit=100&page=1")
        teams = resp.get("data") or []
        _teams_cache["data"] = teams
        _teams_cache["ts"] = time.time()
        return teams
    except Exception as e:
        print(f"[teams] error: {e}", flush=True)
        return []

def fetch_users(page=1, use_all=True):
    """Fetch all users/technicians."""
    users = []
    for p in range(1, 20):
        try:
            path = f"/user/all?limit=100&page={p}" if use_all else f"/user?limit=100&page={p}"
            resp = zuper_get(path)
            batch = resp.get("data") or []
            if not batch: break
            users.extend(batch)
            if len(batch) < 100: break
        except Exception as e:
            print(f"[users] page {p} error: {e}", flush=True)
            break
    return users

def get_job_status(job):
    cs = job.get("current_status") or {}
    if isinstance(cs, dict):
        return cs.get("status_name") or cs.get("name") or ""
    return str(cs)

def get_job_date(job):
    return (job.get("scheduled_start_time") or job.get("created_on") or "")[:10]

def get_job_duration_hours(job):
    """Estimate hours from scheduled duration or actual start/end."""
    dur = job.get("duration") or 0  # Zuper stores duration in minutes
    if dur:
        return dur / 60.0
    # fallback: fixed 1 hr per job
    return 1.0

def get_job_assigned_uids(job):
    """Return set of user UIDs assigned to a job."""
    uids = set()
    assigned = job.get("assigned_to") or []
    if isinstance(assigned, list):
        for u in assigned:
            uid = u.get("user_uid") or u.get("uid") or ""
            if uid: uids.add(uid)
    elif isinstance(assigned, dict):
        uid = assigned.get("user_uid") or assigned.get("uid") or ""
        if uid: uids.add(uid)
    return uids

def get_job_team(job):
    """Return team UID from job."""
    t = job.get("team") or {}
    if isinstance(t, dict):
        return t.get("team_uid") or t.get("uid") or ""
    return ""

def fetch_jobs_for_period(start_date: str, end_date: str):
    """
    Fetch all jobs with scheduled_start_time in [start_date, end_date].
    Uses paginated /jobs/filter — max 3000 jobs to keep it snappy.
    """
    jobs = []
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt   = datetime.strptime(end_date,   "%Y-%m-%d")

    for page in range(1, 61):  # max 60 pages × 100 = 6000 jobs
        try:
            resp = zuper_post("/jobs/filter", {"limit": 100, "page": page, "filter_rules": []}, timeout=15)
            batch = resp.get("data") or []
            if not batch: break
            for job in batch:
                jd_str = get_job_date(job)
                if not jd_str: continue
                try:
                    jd = datetime.strptime(jd_str, "%Y-%m-%d")
                    if start_dt <= jd <= end_dt:
                        jobs.append(job)
                except ValueError:
                    pass
            if len(batch) < 100: break
        except Exception as e:
            print(f"[jobs] page {page} error: {e}", flush=True)
            break
    return jobs

# ── Azuga helpers ─────────────────────────────────────────
_azuga = {"token": None, "ts": 0.0, "lock": threading.Lock()}

def azuga_token():
    with _azuga["lock"]:
        if _azuga["token"] and time.time() - _azuga["ts"] < 6 * 3600:
            return _azuga["token"]
    if not (AZUGA_USER and AZUGA_PASS):
        return None
    body = json.dumps({"userName": AZUGA_USER, "password": AZUGA_PASS,
                       "clientId": AZUGA_CLIENT_ID, "loginType": 1}).encode()
    try:
        req = urllib.request.Request(
            "https://auth.azuga.com/azuga-as/oauth2/login/oauthtoken.json?loginType=1",
            data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            tok = json.loads(r.read()).get("access_token")
        with _azuga["lock"]:
            _azuga["token"], _azuga["ts"] = tok, time.time()
        return tok
    except Exception as e:
        print(f"[azuga] auth failed: {e}", flush=True)
        return None

def azuga_trips(day_str):
    """Fetch all Azuga trips for a given date (YYYY-MM-DD)."""
    tok = azuga_token()
    if not tok:
        return None
    rows, index = [], 1
    while True:
        try:
            body = json.dumps({
                "reportType": "TRIP", "startDate": day_str, "endDate": day_str,
                "pageIndex": index, "pageSize": 500
            }).encode()
            req = urllib.request.Request(
                "https://services.azuga.com/reports/v3/reports/trip",
                data=body,
                headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                method="POST")
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read())
            batch = data.get("data") or data.get("tripReport") or []
            if not batch: break
            rows.extend(batch)
            if len(batch) < 500: break
            index += 1
        except Exception as e:
            print(f"[azuga] trips page {index} error: {e}", flush=True)
            break
    return rows

def azuga_summary_for_week(week_dates):
    """Aggregate Azuga trip data for each day of the week."""
    summary = {}
    for d in week_dates:
        trips = azuga_trips(d) or []
        total_miles  = sum(float(t.get("distance") or t.get("totalDistance") or 0) for t in trips)
        total_hours  = sum(float(t.get("duration")  or t.get("tripDuration")  or 0) for t in trips) / 60.0
        vehicles     = len({t.get("vehicleId") or t.get("vehicle_id") or "" for t in trips if t.get("vehicleId") or t.get("vehicle_id")})
        summary[d]   = {"trips": len(trips), "miles": round(total_miles, 1), "hours": round(total_hours, 1), "vehicles": vehicles}
    return summary

# ── Paylocity stub ────────────────────────────────────────
def paylocity_labor(week_start: str, location=None):
    """
    Stub — returns None until Paylocity credentials are configured.
    To wire up: set PAYLOCITY_CLIENT_ID, PAYLOCITY_CLIENT_SECRET, PAYLOCITY_COMPANY_ID.
    """
    if not (PAY_CLIENT and PAY_SECRET and PAY_COMPANY):
        return None
    # Future: OAuth2 token → GET /v2/companies/{companyId}/employees → timecard data
    return None

# ── Date helpers ──────────────────────────────────────────
def week_dates(week_start: str):
    """Return list of YYYY-MM-DD strings for Mon–Fri of the given week."""
    monday = datetime.strptime(week_start, "%Y-%m-%d")
    return [(monday + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(5)]

def current_week_start():
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    return monday.strftime("%Y-%m-%d")

# ── KPI computation ───────────────────────────────────────
COMPLETED_STATUSES = {"completed", "closed", "done", "work completed", "complete"}

def compute_kpis(jobs, team_uid=None, week_start=None):
    """
    Given a list of Zuper jobs, compute the weekly KPI dict.
    Each completed job = 1 wheel produced.
    """
    days = week_dates(week_start) if week_start else []
    day_set = set(days)

    # Filter by team if requested
    if team_uid and team_uid != "all":
        jobs = [j for j in jobs if get_job_team(j) == team_uid]

    # Filter to completed jobs only
    completed = [j for j in jobs if get_job_status(j).lower() in COMPLETED_STATUSES]
    all_week  = [j for j in jobs if get_job_date(j) in day_set] if day_set else jobs

    # Daily production
    daily_production = {d: 0 for d in days}
    daily_hours      = {d: 0.0 for d in days}
    employee_hours   = {}  # uid → {name, hours_by_day, total, type}

    for job in all_week:
        d = get_job_date(job)
        status = get_job_status(job).lower()
        if status in COMPLETED_STATUSES:
            daily_production[d] = daily_production.get(d, 0) + 1
        assigned = get_job_assigned_uids(job)
        dur = get_job_duration_hours(job)
        for uid in assigned:
            if uid not in employee_hours:
                employee_hours[uid] = {"name": uid[:8], "hours": {dd: 0.0 for dd in days}, "total": 0.0, "type": "Hourly"}
            employee_hours[uid]["hours"][d] = employee_hours[uid]["hours"].get(d, 0.0) + dur
            employee_hours[uid]["total"] += dur
        if assigned:
            daily_hours[d] = daily_hours.get(d, 0.0) + dur

    # Hydrate names from users (best effort — cached)
    # (Skipped here for speed; names are set in /api/labor using cached user list)

    total_wheels = sum(daily_production.values())
    total_hours  = sum(daily_hours.values()) or 1  # avoid /0
    weekly_ratio = total_wheels / total_hours if total_hours else 0

    days_elapsed = sum(1 for d in days if daily_production.get(d, 0) > 0 or (
        d <= date.today().strftime("%Y-%m-%d")))
    days_elapsed = max(days_elapsed, 1)
    days_remaining = max(5 - days_elapsed, 1)
    pace_per_day = (WHEEL_TARGET - total_wheels) / days_remaining if total_wheels < WHEEL_TARGET else 0

    over_ot  = sum(1 for e in employee_hours.values() if e["total"] > OT_THRESHOLD and e["type"] != "Salaried")
    on_watch = sum(1 for e in employee_hours.values() if OT_THRESHOLD * 0.8 <= e["total"] <= OT_THRESHOLD and e["type"] != "Salaried")

    return {
        "week_start":        week_start,
        "wheel_target":      WHEEL_TARGET,
        "ot_threshold":      OT_THRESHOLD,
        "total_wheels":      total_wheels,
        "variance":          total_wheels - WHEEL_TARGET,
        "pct_of_target":     round(total_wheels / WHEEL_TARGET * 100, 1) if WHEEL_TARGET else 0,
        "total_hours":       round(total_hours, 1),
        "weekly_ratio":      round(weekly_ratio, 3),
        "pace_per_day":      round(pace_per_day, 0),
        "over_ot":           over_ot,
        "on_watch":          on_watch,
        "daily_production":  {d: daily_production.get(d, 0) for d in days},
        "daily_hours":       {d: round(daily_hours.get(d, 0), 1) for d in days},
        "daily_ratio":       {d: round(daily_production.get(d, 0) / max(daily_hours.get(d, 1), 0.01), 3) for d in days},
        "employee_hours":    [
            {"uid": uid, **v,
             "hrs_left": max(OT_THRESHOLD - v["total"], 0),
             "status": "Over OT" if v["total"] > OT_THRESHOLD and v["type"] != "Salaried"
                       else "Watch" if v["total"] >= OT_THRESHOLD * 0.8 and v["type"] != "Salaried"
                       else "Salaried" if v["type"] == "Salaried"
                       else "On track"}
            for uid, v in employee_hours.items()
        ],
        "total_jobs":        len(jobs),
        "completed_jobs":    len(completed),
    }

# ── In-memory cache ───────────────────────────────────────
_cache = {}
_cache_lock = threading.Lock()

def cache_get(key):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.time() - entry["ts"] < entry["ttl"]:
            return entry["data"]
    return None

def cache_set(key, data, ttl=120):
    with _cache_lock:
        _cache[key] = {"data": data, "ts": time.time(), "ttl": ttl}

# ── HTTP Handler ──────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[http] {fmt % args}", flush=True)

    def auth_email(self):
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k.strip() == "awrs_token":
                return check_auth_token(v.strip())
        return None

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html, status=200):
        body = html.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length).decode("utf-8", errors="replace") if length else ""

    def parse_qs(self, path):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(path)
        qs = parse_qs(parsed.query)
        return parsed.path, {k: v[0] for k, v in qs.items()}

    # ── GET ───────────────────────────────────────────────
    def do_GET(self):
        path, params = self.parse_qs(self.path)

        if path == "/":
            email = self.auth_email()
            if not email:
                self.send_html(LOGIN_HTML)
            else:
                self.send_html(DASHBOARD_HTML.replace("{{EMAIL}}", email))
            return

        if not path.startswith("/api/"):
            self.send_json({"error": "not found"}, 404)
            return

        email = self.auth_email()
        if not email:
            self.send_json({"error": "unauthorized"}, 401)
            return

        if path == "/api/locations":
            self.handle_locations()
        elif path == "/api/kpis":
            self.handle_kpis(params)
        elif path == "/api/fleet":
            self.handle_fleet(params)
        elif path == "/api/paylocity":
            self.handle_paylocity(params)
        elif path == "/api/users":
            self.handle_users()
        else:
            self.send_json({"error": "not found"}, 404)

    # ── POST ──────────────────────────────────────────────
    def do_POST(self):
        path, _ = self.parse_qs(self.path)
        if path == "/api/login":
            self.handle_login()
        elif path == "/api/logout":
            self.send_response(200)
            self.send_header("Set-Cookie", "awrs_token=; Path=/; Max-Age=0")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        else:
            self.send_json({"error": "not found"}, 404)

    # ── Handlers ──────────────────────────────────────────
    def handle_login(self):
        body = self.read_body()
        try:
            data = json.loads(body)
        except Exception:
            self.send_json({"error": "bad request"}, 400); return
        email = (data.get("email") or "").strip().lower()
        if not ALLOWED_EMAIL.match(email):
            self.send_json({"error": "Only @alloywheel.com accounts are allowed."}, 403); return
        token = make_auth_token(email)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", f"awrs_token={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={AUTH_TTL}")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "email": email}).encode())

    def handle_locations(self):
        cached = cache_get("locations")
        if cached:
            self.send_json(cached); return
        try:
            teams = fetch_teams()
            result = [{"uid": t.get("team_uid") or t.get("uid", ""), "name": t.get("name", "Unknown")} for t in teams]
            result = [r for r in result if r["uid"]]
            cache_set("locations", result, ttl=300)
            self.send_json(result)
        except Exception as e:
            print(f"[locations] error: {e}", flush=True)
            self.send_json([])

    def handle_users(self):
        cached = cache_get("users")
        if cached:
            self.send_json(cached); return
        try:
            users = fetch_users()
            result = [{"uid": u.get("user_uid") or u.get("uid", ""),
                       "name": f"{u.get('first_name','')} {u.get('last_name','')}".strip(),
                       "team": u.get("team_uid") or ""} for u in users]
            cache_set("users", result, ttl=300)
            self.send_json(result)
        except Exception as e:
            print(f"[users] error: {e}", flush=True)
            self.send_json([])

    def handle_kpis(self, params):
        ws = params.get("week") or current_week_start()
        location = params.get("location") or "all"
        cache_key = f"kpis_{ws}_{location}"
        cached = cache_get(cache_key)
        if cached:
            self.send_json(cached); return

        try:
            dates = week_dates(ws)
            jobs = fetch_jobs_for_period(dates[0], dates[-1])
            kpis = compute_kpis(jobs, team_uid=location, week_start=ws)

            # Hydrate employee names
            users = cache_get("users") or []
            uid_to_name = {u["uid"]: u["name"] for u in users if u.get("uid") and u.get("name")}
            for emp in kpis["employee_hours"]:
                emp["name"] = uid_to_name.get(emp["uid"], emp.get("name", emp["uid"][:8]))

            cache_set(cache_key, kpis, ttl=90)
            self.send_json(kpis)
        except Exception as e:
            print(f"[kpis] error: {e}", flush=True)
            self.send_json({"error": str(e)}, 500)

    def handle_fleet(self, params):
        d = params.get("date") or date.today().strftime("%Y-%m-%d")
        ws = params.get("week") or current_week_start()
        cache_key = f"fleet_{ws}"
        cached = cache_get(cache_key)
        if cached:
            self.send_json(cached); return

        if not (AZUGA_USER and AZUGA_PASS):
            self.send_json({"error": "Azuga credentials not configured. Set AZUGA_USERNAME and AZUGA_PASSWORD.", "stub": True})
            return
        try:
            dates = week_dates(ws)
            summary = azuga_summary_for_week(dates)
            result = {
                "week": ws,
                "days": summary,
                "totals": {
                    "trips":    sum(v["trips"]    for v in summary.values()),
                    "miles":    round(sum(v["miles"]    for v in summary.values()), 1),
                    "hours":    round(sum(v["hours"]    for v in summary.values()), 1),
                    "vehicles": max((v["vehicles"] for v in summary.values()), default=0),
                }
            }
            cache_set(cache_key, result, ttl=180)
            self.send_json(result)
        except Exception as e:
            print(f"[fleet] error: {e}", flush=True)
            self.send_json({"error": str(e)}, 500)

    def handle_paylocity(self, params):
        ws = params.get("week") or current_week_start()
        location = params.get("location") or "all"
        data = paylocity_labor(ws, location)
        if data is None:
            self.send_json({
                "stub": True,
                "message": "Paylocity integration pending. Set PAYLOCITY_CLIENT_ID, PAYLOCITY_CLIENT_SECRET, and PAYLOCITY_COMPANY_ID."
            })
        else:
            self.send_json(data)


# ── HTML templates ────────────────────────────────────────
LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AWRS Reporting Dashboard</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Segoe UI',system-ui,sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;display:flex;align-items:center;justify-content:center}
  .card{background:#1e293b;border:1px solid #334155;border-radius:16px;padding:48px 40px;width:100%;max-width:420px;box-shadow:0 25px 50px rgba(0,0,0,.5)}
  .logo{text-align:center;margin-bottom:32px}
  .logo .brand{font-size:1.6rem;font-weight:800;color:#f8fafc;letter-spacing:-.5px}
  .logo .sub{font-size:.85rem;color:#64748b;margin-top:4px}
  label{display:block;font-size:.8rem;font-weight:600;color:#94a3b8;margin-bottom:6px;text-transform:uppercase;letter-spacing:.05em}
  input{width:100%;background:#0f172a;border:1px solid #334155;border-radius:8px;padding:12px 14px;color:#e2e8f0;font-size:.95rem;outline:none;transition:border .15s}
  input:focus{border-color:#3b82f6}
  .btn{width:100%;margin-top:24px;background:#3b82f6;color:#fff;border:none;border-radius:8px;padding:13px;font-size:.95rem;font-weight:600;cursor:pointer;transition:background .15s}
  .btn:hover{background:#2563eb}
  .err{color:#f87171;font-size:.85rem;margin-top:12px;text-align:center;display:none}
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <div class="brand">AWRS</div>
    <div class="sub">Reporting Dashboard</div>
  </div>
  <label for="email">Email address</label>
  <input id="email" type="email" placeholder="you@alloywheel.com" autocomplete="email">
  <button class="btn" onclick="login()">Sign in</button>
  <div class="err" id="err"></div>
</div>
<script>
document.getElementById('email').addEventListener('keydown', e => e.key === 'Enter' && login());
async function login(){
  const email = document.getElementById('email').value.trim();
  const err   = document.getElementById('err');
  err.style.display = 'none';
  const r = await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email})});
  const d = await r.json();
  if(d.ok) location.reload();
  else { err.textContent = d.error || 'Login failed'; err.style.display = 'block'; }
}
</script>
</body>
</html>"""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AWRS Reporting Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  :root{
    --bg:#0f172a; --surface:#1e293b; --border:#334155;
    --text:#e2e8f0; --muted:#64748b; --accent:#3b82f6;
    --green:#22c55e; --yellow:#eab308; --red:#ef4444; --orange:#f97316;
  }
  body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}

  /* ── Top bar ── */
  .topbar{background:var(--surface);border-bottom:1px solid var(--border);padding:0 24px;display:flex;align-items:center;gap:16px;height:56px}
  .topbar .brand{font-size:1.1rem;font-weight:800;letter-spacing:-.3px;color:#f8fafc}
  .topbar .sub{font-size:.75rem;color:var(--muted);font-weight:500}
  .topbar-right{margin-left:auto;display:flex;align-items:center;gap:12px}
  .user-pill{font-size:.78rem;color:var(--muted)}
  .logout-btn{background:none;border:1px solid var(--border);border-radius:6px;color:var(--muted);font-size:.75rem;padding:5px 10px;cursor:pointer;transition:all .15s}
  .logout-btn:hover{border-color:var(--red);color:var(--red)}

  /* ── Controls bar ── */
  .controls{padding:16px 24px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;border-bottom:1px solid var(--border);background:var(--surface)}
  .controls label{font-size:.78rem;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.05em}
  .controls select, .controls input[type=date]{background:#0f172a;border:1px solid var(--border);border-radius:7px;color:var(--text);padding:7px 10px;font-size:.85rem;outline:none}
  .controls select:focus, .controls input:focus{border-color:var(--accent)}
  .refresh-btn{background:var(--accent);border:none;border-radius:7px;color:#fff;font-size:.82rem;font-weight:600;padding:8px 14px;cursor:pointer;transition:background .15s}
  .refresh-btn:hover{background:#2563eb}
  .last-update{font-size:.75rem;color:var(--muted);margin-left:auto}

  /* ── Main layout ── */
  .main{padding:24px;display:flex;flex-direction:column;gap:24px}

  /* ── KPI cards ── */
  .kpi-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px}
  .kpi{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:18px 16px;position:relative;overflow:hidden}
  .kpi::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:var(--accent)}
  .kpi.green::before{background:var(--green)}
  .kpi.yellow::before{background:var(--yellow)}
  .kpi.red::before{background:var(--red)}
  .kpi.orange::before{background:var(--orange)}
  .kpi-label{font-size:.72rem;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;font-weight:600;margin-bottom:8px}
  .kpi-val{font-size:1.8rem;font-weight:800;line-height:1;color:#f8fafc}
  .kpi-sub{font-size:.75rem;color:var(--muted);margin-top:6px}
  .kpi-sub .pos{color:var(--green)} .kpi-sub .neg{color:var(--red)}

  /* ── Section cards ── */
  .section{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden}
  .section-header{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
  .section-title{font-size:.9rem;font-weight:700;color:var(--text)}
  .section-badge{font-size:.72rem;color:var(--muted);background:#0f172a;border-radius:20px;padding:3px 10px;border:1px solid var(--border)}
  .section-body{padding:20px}

  /* ── Charts ── */
  .chart-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}
  @media(max-width:720px){.chart-grid{grid-template-columns:1fr}}
  .chart-wrap{position:relative;height:220px}

  /* ── Tables ── */
  table{width:100%;border-collapse:collapse;font-size:.82rem}
  th{text-align:left;color:var(--muted);font-weight:600;font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;padding:8px 10px;border-bottom:1px solid var(--border)}
  td{padding:9px 10px;border-bottom:1px solid #1e293b}
  tr:last-child td{border-bottom:none}
  tr:hover td{background:rgba(255,255,255,.03)}
  .badge{display:inline-block;font-size:.7rem;font-weight:700;padding:2px 8px;border-radius:20px}
  .badge.on-track{background:rgba(34,197,94,.15);color:var(--green)}
  .badge.watch{background:rgba(234,179,8,.15);color:var(--yellow)}
  .badge.over-ot{background:rgba(239,68,68,.15);color:var(--red)}
  .badge.salaried{background:rgba(100,116,139,.15);color:var(--muted)}
  .ratio-chip{display:inline-block;font-size:.75rem;font-weight:700;padding:2px 7px;border-radius:4px}
  .ratio-red{background:rgba(239,68,68,.2);color:var(--red)}
  .ratio-yellow{background:rgba(234,179,8,.2);color:var(--yellow)}
  .ratio-green{background:rgba(34,197,94,.2);color:var(--green)}
  .ratio-blue{background:rgba(59,130,246,.2);color:var(--accent)}

  /* ── Fleet & Paylocity sections ── */
  .stub-banner{background:rgba(251,191,36,.08);border:1px dashed var(--yellow);border-radius:8px;padding:14px 18px;display:flex;align-items:center;gap:12px}
  .stub-banner .icon{font-size:1.2rem}
  .stub-banner .msg{font-size:.82rem;color:var(--yellow)}
  .fleet-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin-top:16px}
  .fleet-card{background:#0f172a;border:1px solid var(--border);border-radius:8px;padding:14px;text-align:center}
  .fleet-card .val{font-size:1.4rem;font-weight:800;color:var(--accent)}
  .fleet-card .lbl{font-size:.7rem;color:var(--muted);margin-top:4px;text-transform:uppercase}

  /* ── Tabs ── */
  .tabs{display:flex;gap:4px;padding:0 20px;border-bottom:1px solid var(--border)}
  .tab{padding:12px 16px;font-size:.83rem;font-weight:600;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent;transition:all .15s}
  .tab.active{color:var(--accent);border-bottom-color:var(--accent)}
  .tab-panel{display:none}
  .tab-panel.active{display:block}

  /* ── Loading ── */
  .loading{text-align:center;padding:40px;color:var(--muted);font-size:.88rem}
  .spinner{display:inline-block;width:20px;height:20px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite;margin-right:8px;vertical-align:middle}
  @keyframes spin{to{transform:rotate(360deg)}}

  /* ── Misc ── */
  .row-2{display:grid;grid-template-columns:1fr 1fr;gap:20px}
  @media(max-width:720px){.row-2{grid-template-columns:1fr}}
  .text-right{text-align:right}
  .text-green{color:var(--green)} .text-red{color:var(--red)} .text-yellow{color:var(--yellow)}
</style>
</head>
<body>

<!-- Top bar -->
<div class="topbar">
  <div>
    <div class="brand">AWRS</div>
    <div class="sub">Reporting Dashboard</div>
  </div>
  <div class="topbar-right">
    <span class="user-pill" id="userPill">{{EMAIL}}</span>
    <button class="logout-btn" onclick="logout()">Sign out</button>
  </div>
</div>

<!-- Controls -->
<div class="controls">
  <label>Week</label>
  <input type="date" id="weekPicker" onchange="load()">
  <label style="margin-left:8px">Location</label>
  <select id="locationPicker" onchange="load()">
    <option value="all">All Locations</option>
  </select>
  <button class="refresh-btn" onclick="load(true)">&#8635; Refresh</button>
  <span class="last-update" id="lastUpdate"></span>
</div>

<!-- Main -->
<div class="main">

  <!-- KPI cards -->
  <div class="kpi-row" id="kpiRow">
    <div class="loading"><span class="spinner"></span>Loading KPIs…</div>
  </div>

  <!-- Tabs: Production | Labor | Fleet | Paylocity -->
  <div class="section">
    <div class="tabs">
      <div class="tab active" onclick="switchTab('production',this)">Production</div>
      <div class="tab" onclick="switchTab('labor',this)">Labor & OT</div>
      <div class="tab" onclick="switchTab('fleet',this)">Fleet (Azuga)</div>
      <div class="tab" onclick="switchTab('paylocity',this)">Payroll (Paylocity)</div>
    </div>

    <!-- Production tab -->
    <div class="tab-panel active" id="tab-production">
      <div class="section-body">
        <div class="chart-grid">
          <div>
            <div style="font-size:.78rem;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.06em;margin-bottom:12px">Daily Wheels vs Target</div>
            <div class="chart-wrap"><canvas id="productionChart"></canvas></div>
          </div>
          <div>
            <div style="font-size:.78rem;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.06em;margin-bottom:12px">Efficiency Ratio (wheels/hr)</div>
            <div class="chart-wrap"><canvas id="ratioChart"></canvas></div>
          </div>
        </div>
        <div style="margin-top:20px">
          <div style="font-size:.78rem;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.06em;margin-bottom:12px">Daily Breakdown</div>
          <div id="dailyTable"><div class="loading"><span class="spinner"></span>Loading…</div></div>
        </div>
      </div>
    </div>

    <!-- Labor tab -->
    <div class="tab-panel" id="tab-labor">
      <div class="section-body">
        <div id="laborContent"><div class="loading"><span class="spinner"></span>Loading employee data…</div></div>
      </div>
    </div>

    <!-- Fleet tab -->
    <div class="tab-panel" id="tab-fleet">
      <div class="section-body" id="fleetContent">
        <div class="loading"><span class="spinner"></span>Loading fleet data…</div>
      </div>
    </div>

    <!-- Paylocity tab -->
    <div class="tab-panel" id="tab-paylocity">
      <div class="section-body" id="paylocityContent">
        <div class="loading"><span class="spinner"></span>Checking Paylocity…</div>
      </div>
    </div>
  </div>

</div><!-- /main -->

<script>
const DAY_LABELS = ['Mon','Tue','Wed','Thu','Fri'];
let kpisData = null;
let prodChart = null, ratioChart = null;
let fleetLoaded = false, payLoaded = false;

// ── Init ──────────────────────────────────────────────────
(async function init(){
  const today = new Date();
  const monday = new Date(today);
  monday.setDate(today.getDate() - today.getDay() + (today.getDay()===0 ? -6 : 1));
  document.getElementById('weekPicker').value = monday.toISOString().slice(0,10);

  // Load locations
  try {
    const locs = await apiFetch('/api/locations');
    const sel = document.getElementById('locationPicker');
    (locs||[]).forEach(l => {
      const o = document.createElement('option');
      o.value = l.uid; o.textContent = l.name;
      sel.appendChild(o);
    });
  } catch(e){}

  await load();
})();

// ── Load all data ─────────────────────────────────────────
async function load(bust=false){
  document.getElementById('lastUpdate').textContent = 'Loading…';
  const week = document.getElementById('weekPicker').value;
  const loc  = document.getElementById('locationPicker').value;
  fleetLoaded = false; payLoaded = false;

  // KPIs + production
  try {
    const url = `/api/kpis?week=${week}&location=${loc}${bust?'&_='+Date.now():''}`;
    kpisData = await apiFetch(url);
    renderKPIs(kpisData);
    renderProduction(kpisData);
    renderLabor(kpisData);
  } catch(e){
    document.getElementById('kpiRow').innerHTML = `<div class="loading" style="color:var(--red)">Error loading data: ${e.message}</div>`;
  }

  document.getElementById('lastUpdate').textContent = 'Updated ' + new Date().toLocaleTimeString();
}

// ── KPI cards ─────────────────────────────────────────────
function renderKPIs(d){
  const varColor = d.variance >= 0 ? 'pos' : 'neg';
  const varSign  = d.variance >= 0 ? '+' : '';
  const ratioColor = d.weekly_ratio < 0.85 ? 'red' : d.weekly_ratio < 0.95 ? 'yellow' : d.weekly_ratio <= 1.10 ? 'green' : 'yellow';
  const pctColor   = d.pct_of_target >= 95 ? 'green' : d.pct_of_target >= 80 ? 'yellow' : 'red';

  document.getElementById('kpiRow').innerHTML = `
    <div class="kpi ${pctColor}">
      <div class="kpi-label">Wheels Produced</div>
      <div class="kpi-val">${d.total_wheels.toLocaleString()}</div>
      <div class="kpi-sub">Target: ${d.wheel_target} &nbsp;|&nbsp; <span class="${varColor}">${varSign}${d.variance}</span></div>
    </div>
    <div class="kpi ${pctColor}">
      <div class="kpi-label">% of Target</div>
      <div class="kpi-val">${d.pct_of_target}%</div>
      <div class="kpi-sub">Week pace</div>
    </div>
    <div class="kpi ${ratioColor}">
      <div class="kpi-label">Wheels / Man-Hour</div>
      <div class="kpi-val">${d.weekly_ratio.toFixed(2)}</div>
      <div class="kpi-sub">1.0 = perfect &nbsp;|&nbsp; ${d.weekly_ratio >= 1.0 ? '<span class="pos">On pace</span>' : '<span class="neg">Below pace</span>'}</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Total Man-Hours</div>
      <div class="kpi-val">${d.total_hours.toLocaleString()}</div>
      <div class="kpi-sub">OT threshold: ${d.ot_threshold} hrs</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Pace Needed/Day</div>
      <div class="kpi-val">${d.pace_per_day > 0 ? d.pace_per_day : '✓'}</div>
      <div class="kpi-sub">${d.pace_per_day > 0 ? 'wheels remaining' : 'Target met!'}</div>
    </div>
    <div class="kpi ${d.over_ot > 0 ? 'red' : d.on_watch > 0 ? 'yellow' : 'green'}">
      <div class="kpi-label">OT Status</div>
      <div class="kpi-val">${d.over_ot}</div>
      <div class="kpi-sub">${d.over_ot} over &nbsp;|&nbsp; ${d.on_watch} on watch</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Jobs (Zuper)</div>
      <div class="kpi-val">${d.completed_jobs}</div>
      <div class="kpi-sub">${d.total_jobs} total this week</div>
    </div>
  `;
}

// ── Production charts + table ─────────────────────────────
function renderProduction(d){
  const days  = Object.keys(d.daily_production);
  const prod  = days.map(k => d.daily_production[k]);
  const hours = days.map(k => d.daily_hours[k]);
  const ratio = days.map(k => d.daily_ratio[k]);
  const daily_target = Math.round(d.wheel_target / 5);

  // Bar chart
  if(prodChart) prodChart.destroy();
  prodChart = new Chart(document.getElementById('productionChart'),{
    type:'bar',
    data:{
      labels: DAY_LABELS,
      datasets:[
        {label:'Wheels',data:prod,backgroundColor:'rgba(59,130,246,.7)',borderRadius:5,order:1},
        {label:'Target',data:Array(5).fill(daily_target),type:'line',borderColor:'rgba(239,68,68,.8)',borderWidth:2,borderDash:[5,3],pointRadius:0,fill:false,order:0}
      ]
    },
    options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:'#94a3b8',font:{size:11}}}},
      scales:{x:{ticks:{color:'#64748b'},grid:{color:'#1e293b'}},y:{ticks:{color:'#64748b'},grid:{color:'#1e293b'}}}}
  });

  // Ratio line chart
  if(ratioChart) ratioChart.destroy();
  const ratioColors = ratio.map(r => r < 0.85 ? 'rgba(239,68,68,.8)' : r < 0.95 ? 'rgba(234,179,8,.8)' : 'rgba(34,197,94,.8)');
  ratioChart = new Chart(document.getElementById('ratioChart'),{
    type:'line',
    data:{
      labels: DAY_LABELS,
      datasets:[
        {label:'Ratio',data:ratio,borderColor:'rgba(59,130,246,.9)',backgroundColor:'rgba(59,130,246,.1)',pointBackgroundColor:ratioColors,pointRadius:5,fill:true,tension:.3},
        {label:'Perfect (1.0)',data:Array(5).fill(1.0),borderColor:'rgba(34,197,94,.5)',borderWidth:1,borderDash:[4,4],pointRadius:0,fill:false}
      ]
    },
    options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:'#94a3b8',font:{size:11}}}},
      scales:{x:{ticks:{color:'#64748b'},grid:{color:'#1e293b'}},y:{ticks:{color:'#64748b'},grid:{color:'#1e293b'},min:0,max:Math.max(1.5,Math.max(...ratio)+0.2)}}}
  });

  // Daily table
  let html = `<table><thead><tr>
    <th>Day</th><th class="text-right">Wheels</th><th class="text-right">Hours</th>
    <th class="text-right">Ratio</th><th class="text-right">vs Daily Target</th>
  </tr></thead><tbody>`;
  days.forEach((day, i) => {
    const r = ratio[i];
    const rc = r < 0.85 ? 'ratio-red' : r < 0.95 ? 'ratio-yellow' : r <= 1.10 ? 'ratio-green' : 'ratio-blue';
    const vs = prod[i] - daily_target;
    const vsClass = vs >= 0 ? 'text-green' : 'text-red';
    const vsSign = vs >= 0 ? '+' : '';
    html += `<tr>
      <td>${DAY_LABELS[i]}<span style="color:var(--muted);font-size:.75rem;margin-left:6px">${day}</span></td>
      <td class="text-right">${prod[i]}</td>
      <td class="text-right">${hours[i].toFixed(1)}</td>
      <td class="text-right"><span class="ratio-chip ${rc}">${r.toFixed(3)}</span></td>
      <td class="text-right ${vsClass}">${vsSign}${vs}</td>
    </tr>`;
  });
  const totalVs = d.variance;
  const tvClass = totalVs >= 0 ? 'text-green' : 'text-red';
  html += `</tbody><tfoot><tr style="font-weight:700">
    <td>Total</td>
    <td class="text-right">${d.total_wheels}</td>
    <td class="text-right">${d.total_hours.toFixed(1)}</td>
    <td class="text-right"><span class="ratio-chip ${d.weekly_ratio<0.85?'ratio-red':d.weekly_ratio<0.95?'ratio-yellow':'ratio-green'}">${d.weekly_ratio.toFixed(3)}</span></td>
    <td class="text-right ${tvClass}">${totalVs >= 0 ? '+' : ''}${totalVs}</td>
  </tr></tfoot></table>`;
  document.getElementById('dailyTable').innerHTML = html;
}

// ── Labor table ───────────────────────────────────────────
function renderLabor(d){
  const emps = (d.employee_hours || []).sort((a,b) => b.total - a.total);
  if(!emps.length){
    document.getElementById('laborContent').innerHTML = `<div style="color:var(--muted);font-size:.85rem;text-align:center;padding:30px">No employee data found for this week. Job assignments in Zuper will populate this table.</div>`;
    return;
  }
  const days = Object.keys(d.daily_production);
  let html = `<table><thead><tr>
    <th>Employee</th>
    ${DAY_LABELS.slice(0,days.length).map(l=>`<th class="text-right">${l}</th>`).join('')}
    <th class="text-right">Total Hrs</th>
    <th class="text-right">Hrs Left</th>
    <th>Status</th>
  </tr></thead><tbody>`;
  emps.forEach(e => {
    const statusBadge = {
      'On track': 'badge on-track',
      'Watch':    'badge watch',
      'Over OT':  'badge over-ot',
      'Salaried': 'badge salaried'
    }[e.status] || 'badge';
    const dayHours = days.map(d => (e.hours[d] || 0).toFixed(1));
    const totalColor = e.total > d.ot_threshold ? 'text-red' : e.total >= d.ot_threshold * 0.8 ? 'text-yellow' : '';
    html += `<tr>
      <td>${esc(e.name)}<span style="color:var(--muted);font-size:.7rem;margin-left:6px">${e.type}</span></td>
      ${dayHours.map(h=>`<td class="text-right">${h === '0.0' ? '—' : h}</td>`).join('')}
      <td class="text-right ${totalColor}"><strong>${e.total.toFixed(1)}</strong></td>
      <td class="text-right">${e.type==='Salaried' ? '—' : e.hrs_left.toFixed(1)}</td>
      <td><span class="${statusBadge}">${e.status}</span></td>
    </tr>`;
  });
  html += `</tbody><tfoot><tr style="font-weight:700">
    <td>Totals</td>
    ${days.map(day => `<td class="text-right">${d.daily_hours[day].toFixed(1)}</td>`).join('')}
    <td class="text-right">${d.total_hours.toFixed(1)}</td>
    <td></td>
    <td><span style="font-size:.75rem;color:var(--muted)">${d.over_ot} over OT · ${d.on_watch} watch</span></td>
  </tr></tfoot></table>
  <div style="margin-top:12px;font-size:.74rem;color:var(--muted)">
    Ratio guide: &nbsp;
    <span class="ratio-chip ratio-red">Red &lt;0.85</span> &nbsp;
    <span class="ratio-chip ratio-yellow">Amber 0.85–0.95</span> &nbsp;
    <span class="ratio-chip ratio-green">Green 0.95–1.10</span> &nbsp;
    <span class="ratio-chip ratio-blue">Amber &gt;1.10</span>
  </div>`;
  document.getElementById('laborContent').innerHTML = html;
}

// ── Fleet tab (lazy load) ─────────────────────────────────
async function loadFleet(){
  if(fleetLoaded) return;
  fleetLoaded = true;
  const week = document.getElementById('weekPicker').value;
  try {
    const f = await apiFetch(`/api/fleet?week=${week}`);
    if(f.stub || f.error){
      document.getElementById('fleetContent').innerHTML = `
        <div class="stub-banner">
          <div class="icon">🚗</div>
          <div class="msg"><strong>Azuga not connected.</strong><br>
          Set <code>AZUGA_USERNAME</code> and <code>AZUGA_PASSWORD</code> in your Render environment to enable fleet data.</div>
        </div>`;
      return;
    }
    const t = f.totals || {};
    let html = `<div class="fleet-grid">
      <div class="fleet-card"><div class="val">${t.vehicles||0}</div><div class="lbl">Active Vehicles</div></div>
      <div class="fleet-card"><div class="val">${t.trips||0}</div><div class="lbl">Total Trips</div></div>
      <div class="fleet-card"><div class="val">${(t.miles||0).toLocaleString()}</div><div class="lbl">Total Miles</div></div>
      <div class="fleet-card"><div class="val">${(t.hours||0).toFixed(1)}</div><div class="lbl">Drive Hours</div></div>
    </div>
    <div style="margin-top:20px">
      <table><thead><tr><th>Day</th><th class="text-right">Trips</th><th class="text-right">Miles</th><th class="text-right">Drive Hours</th><th class="text-right">Vehicles</th></tr></thead><tbody>`;
    const dayMap = f.days || {};
    Object.entries(dayMap).forEach(([d,v],i) => {
      html += `<tr><td>${DAY_LABELS[i]||d} <span style="color:var(--muted);font-size:.75rem">${d}</span></td>
        <td class="text-right">${v.trips}</td>
        <td class="text-right">${v.miles.toLocaleString()}</td>
        <td class="text-right">${v.hours.toFixed(1)}</td>
        <td class="text-right">${v.vehicles}</td></tr>`;
    });
    html += `</tbody></table></div>`;
    document.getElementById('fleetContent').innerHTML = html;
  } catch(e){
    document.getElementById('fleetContent').innerHTML = `<div class="stub-banner"><div class="icon">⚠️</div><div class="msg">Error loading fleet data: ${e.message}</div></div>`;
  }
}

// ── Paylocity tab (lazy load) ─────────────────────────────
async function loadPaylocity(){
  if(payLoaded) return;
  payLoaded = true;
  const week = document.getElementById('weekPicker').value;
  try {
    const p = await apiFetch(`/api/paylocity?week=${week}`);
    if(p.stub){
      document.getElementById('paylocityContent').innerHTML = `
        <div class="stub-banner">
          <div class="icon">💰</div>
          <div class="msg"><strong>Paylocity not connected.</strong><br>
          Set <code>PAYLOCITY_CLIENT_ID</code>, <code>PAYLOCITY_CLIENT_SECRET</code>, and <code>PAYLOCITY_COMPANY_ID</code> in your Render environment to pull timecard and payroll data.</div>
        </div>
        <div style="margin-top:20px;color:var(--muted);font-size:.82rem">
          When connected, this tab will show:
          <ul style="margin-top:8px;margin-left:20px;line-height:1.8">
            <li>Clock-in / clock-out by employee</li>
            <li>Actual vs scheduled hours</li>
            <li>OT cost (regular + 1.5× OT wages)</li>
            <li>Labor cost per wheel produced</li>
            <li>Payroll period summary</li>
          </ul>
        </div>`;
    } else {
      document.getElementById('paylocityContent').innerHTML = JSON.stringify(p, null, 2);
    }
  } catch(e){
    document.getElementById('paylocityContent').innerHTML = `<div class="stub-banner"><div class="icon">⚠️</div><div class="msg">${e.message}</div></div>`;
  }
}

// ── Tab switching ─────────────────────────────────────────
function switchTab(name, el){
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('tab-'+name).classList.add('active');
  if(name==='fleet') loadFleet();
  if(name==='paylocity') loadPaylocity();
}

// ── Utility ───────────────────────────────────────────────
async function apiFetch(url){
  const r = await fetch(url);
  if(r.status === 401){ location.reload(); throw new Error('Session expired'); }
  if(!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

function esc(s){ const d=document.createElement('div'); d.textContent=s||''; return d.innerHTML; }

async function logout(){
  await fetch('/api/logout',{method:'POST'});
  location.reload();
}
</script>
</body>
</html>"""


# ── Server start ──────────────────────────────────────────
def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"AWRS Reporting Dashboard running on http://localhost:{PORT}", flush=True)
    if IS_LOCAL:
        import webbrowser, threading
        threading.Timer(0.8, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.", flush=True)

if __name__ == "__main__":
    main()
