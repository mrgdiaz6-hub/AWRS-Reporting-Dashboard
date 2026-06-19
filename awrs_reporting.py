#!/usr/bin/env python3
"""
AWRS Reporting Dashboard — v2026-06-19d
────────────────────────────────────────
Blended KPI dashboard: Zuper (production/techs) + Azuga (fleet/drivers) + Paylocity (labor/admin)

Run:  python3 awrs_reporting.py
Open: http://localhost:8081

Env vars:
  ZUPER_API_KEY          Zuper API key
  AZUGA_USERNAME         Azuga login
  AZUGA_PASSWORD         Azuga password
  PAYLOCITY_CLIENT_ID    (future)
  PAYLOCITY_CLIENT_SECRET
  PAYLOCITY_COMPANY_ID
  AUTH_SECRET            Token signing secret (auto-generated if not set)
  PORT                   HTTP port (default 8081)
  WHEEL_TARGET_DAILY     Daily wheel target per tech (default 9 — W/T/D target from report)
  OT_THRESHOLD           OT threshold hours (default 40)
  MOBILE_ASP             Mobile avg selling price per wheel (default 93.80)
  REMAN_ASP              Reman avg selling price per wheel (default 141.95)
"""

import json, math, threading, time, os, uuid, re, hmac, hashlib, base64, urllib.request, urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta, date
import sys
sys.stdout.reconfigure(line_buffering=True)

# ── Config ────────────────────────────────────────────────
ZUPER_KEY        = os.environ.get("ZUPER_API_KEY", "05a82304b0074c2c9b32f7ca2a1ab682")
ZUPER_BASE       = "https://us-west-1c.zuperpro.com/api"
AZUGA_USER       = os.environ.get("AZUGA_USERNAME", "")
AZUGA_PASS       = os.environ.get("AZUGA_PASSWORD", "")
AZUGA_CLIENT_ID  = "5decccae0a214939a77411a77eeff8fc"
PAY_CLIENT       = os.environ.get("PAYLOCITY_CLIENT_ID", "")
PAY_SECRET       = os.environ.get("PAYLOCITY_CLIENT_SECRET", "")
PAY_COMPANY      = os.environ.get("PAYLOCITY_COMPANY_ID", "")
PORT             = int(os.environ.get("PORT", 8081))
WTD_TARGET       = float(os.environ.get("WHEEL_TARGET_DAILY", 9))   # wheels per tech per day
OT_THRESHOLD     = int(os.environ.get("OT_THRESHOLD", 40))
MOBILE_ASP       = float(os.environ.get("MOBILE_ASP", 93.80))
REMAN_ASP        = float(os.environ.get("REMAN_ASP", 141.95))
IS_LOCAL         = not os.environ.get("RENDER")

# ── Wheel Service Product IDs (NetSuite IDs from Zuper Product Report) ────
# Each service exists across 5 categories: Local/Wholesale/Retail/Claims/REDO
MOBILE_WHEEL_IDS = {
    # Mobile Repair Custom
    "199134","199230","199278","199182","199349",
    # RQ1 Complete Finish
    "204116","204117","204118","204119","204120",
    # RQ2 Mobile Repair Combination
    "200988","201006","200994","201000","204125",
    # RQ2 Mobile Repair Hyper
    "200986","201004","200992","200998","204127",
    # RQ2 Mobile Repair Machined
    "200987","201005","200993","200999","204126",
    # RQ2 Mobile Repair Paint
    "200989","201007","200995","201001","204124",
    # RQ2 Mobile Repair Polish
    "200990","201008","200996","201002","204123",
    # RQ2 Mobile Repair Straighten
    "200985","201003","200991","200997","204128",
    # RQ3 Mobile Repair Combination
    "199132","199228","199276","199180","199347",
    # RQ3 Mobile Repair Hyper
    "199131","199227","199275","199179","199346",
    # RQ3 Mobile Repair Machined
    "199130","199226","199274","199178","199345",
    # RQ3 Mobile Repair Paint
    "199129","199225","199273","199177","199344",
    # RQ3 Mobile Repair Polish
    "199133","199229","199277","199181","199348",
    # RQ3 Mobile Repair Straighten
    "199135","199231","199279","199183","199350",
}
REMAN_WHEEL_IDS = {
    # Complete Wheel Remanufacturing
    "199112","199208","199256","199160","199327",
    # Structural Repair
    "199111","199207","199255","199159","199326",
}
NR_WHEEL_IDS = {
    # Non-Repairable Wheel (tracked separately as % of total repairs)
    "199115","199211","199259","199163","199330",
}
ALL_PRIMARY_IDS = MOBILE_WHEEL_IDS | REMAN_WHEEL_IDS | NR_WHEEL_IDS

def count_wheels_from_products(products, job_type=None):
    """Count (mobile, reman, nr) wheels from a job's products[] list.
    Uses product_id (hardcoded sets) as primary signal.
    Falls back to specific name patterns — avoids counting add-on services
    like 'Machined Face' or 'Mount and Balance' as wheels.
    job_type is accepted but not used (left for future use).
    """
    mobile = reman = nr = 0
    for p in (products or []):
        pid  = str(p.get("product_id") or "").strip()
        name = (p.get("product_name") or "").lower()
        qty  = int(p.get("quantity") or 1)
        if pid in NR_WHEEL_IDS or "non-repairable" in name or "non repairable" in name:
            nr     += qty
        elif pid in MOBILE_WHEEL_IDS or "mobile repair" in name or "mobile onsite" in name:
            mobile += qty
        elif pid in REMAN_WHEEL_IDS or "complete wheel remanufactur" in name or "structural repair" in name:
            reman  += qty
        # else: add-on service (Machined Face, Mount and Balance, etc.) — skip
    return mobile, reman, nr


def get_job_type(job):
    """Determine job type (mobile/reman/nr/other) from job_category.category_name."""
    cat = job.get("job_category") or {}
    name = (cat.get("category_name") or "").lower()
    if "mobile" in name or "onsite" in name:
        return "mobile"
    if "remanufactur" in name or "reman" in name or "structural" in name:
        return "reman"
    if "non-repairable" in name or "non repairable" in name:
        return "nr"
    return "other"

def count_wheels_from_line_items(line_items):
    """Count (mobile, reman, nr) wheels from an invoice's line_items[].
    Uses line_items[].product_ref_id.product_id.
    """
    mobile = reman = nr = 0
    for li in (line_items or []):
        pid = str((li.get("product_ref_id") or {}).get("product_id") or "").strip()
        qty = int(li.get("quantity") or 1)
        if pid in MOBILE_WHEEL_IDS:   mobile += qty
        elif pid in REMAN_WHEEL_IDS:  reman  += qty
        elif pid in NR_WHEEL_IDS:     nr     += qty
    return mobile, reman, nr

# ── Auth ──────────────────────────────────────────────────
AUTH_SECRET   = os.environ.get("AUTH_SECRET") or hashlib.sha256(f"awrs-{ZUPER_KEY}".encode()).hexdigest()[:32]
ALLOWED_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@alloywheel\.com$", re.I)
AUTH_TTL      = 30 * 86400

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

_cache = {}
_cache_lock = threading.Lock()
_warming = set()
_warming_lock = threading.Lock()

def cache_get(key):
    with _cache_lock:
        e = _cache.get(key)
        if e and time.time() - e["ts"] < e["ttl"]:
            return e["data"]
    return None

def cache_set(key, data, ttl=120):
    with _cache_lock:
        _cache[key] = {"data": data, "ts": time.time(), "ttl": ttl}

def fetch_teams():
    c = cache_get("teams")
    if c: return c
    try:
        resp = zuper_get("/team?limit=100&page=1")
        teams = resp.get("data") or []
        cache_set("teams", teams, 300)
        return teams
    except Exception as e:
        print(f"[teams] {e}", flush=True)
        return []

def fetch_users():
    c = cache_get("users")
    if c: return c
    users = []
    for p in range(1, 20):
        try:
            path = f"/user/all?limit=100&page={p}"
            resp = zuper_get(path)
            batch = resp.get("data") or []
            if not batch: break
            users.extend(batch)
            if len(batch) < 100: break
        except Exception as e:
            print(f"[users] p{p} {e}", flush=True)
            try:
                resp = zuper_get(f"/user?limit=100&page={p}")
                batch = resp.get("data") or []
                users.extend(batch)
                if len(batch) < 100: break
            except:
                break
    cache_set("users", users, 300)
    return users

def build_employee_map():
    """Build cross-system employee lookup.
    Keys: normalized full name (lower), user_uid, emp_code (if numeric).
    Value: {user_uid, emp_code, name, designation, email, team_uid, hourly_rate}
    emp_code in Zuper = Employee ID in Azuga = Employee # in Paylocity.
    Azuga→Zuper join: fullName (lower) → emp_code.
    """
    c = cache_get("emp_map")
    if c: return c
    users = fetch_users()
    # Build uid → team_uid mapping from teams
    uid_to_team = {}
    try:
        teams = fetch_teams()
        for t in teams:
            tuid = t.get("team_uid") or t.get("uid") or ""
            for m in (t.get("users") or []):
                muid = m.get("user_uid") or m.get("uid") or ""
                if muid and tuid:
                    uid_to_team[muid] = tuid
    except Exception:
        pass
    by_name = {}  # normalized full name → record
    by_uid  = {}  # user_uid → record
    by_emp  = {}  # emp_code (numeric str) → record
    for u in users:
        if u.get("is_deleted"): continue
        uid   = u.get("user_uid") or ""
        fname = (u.get("first_name") or "").strip()
        lname = (u.get("last_name")  or "").strip()
        name  = f"{fname} {lname}".strip()
        emp   = str(u.get("emp_code") or "").strip()
        rec = {
            "user_uid":    uid,
            "emp_code":    emp,
            "name":        name,
            "designation": u.get("designation") or "",
            "email":       u.get("email") or "",
            "hourly_rate": u.get("hourly_labor_charge"),
            "team_uid":    uid_to_team.get(uid, ""),
        }
        if name: by_name[name.lower()] = rec
        if uid:  by_uid[uid]           = rec
        if emp and emp.isdigit(): by_emp[emp] = rec
    result = {"by_name": by_name, "by_uid": by_uid, "by_emp": by_emp}
    cache_set("emp_map", result, 300)
    return result

def emp_lookup_by_name(full_name, emp_map=None):
    """Lookup employee record by Azuga fullName. Returns rec or {}."""
    if not full_name: return {}
    if emp_map is None: emp_map = build_employee_map()
    return emp_map["by_name"].get(full_name.strip().lower(), {})

def get_job_status(job):
    # current_job_status is a dict with the active status
    cjs = job.get("current_job_status")
    if isinstance(cjs, dict):
        name = (cjs.get("status_name") or cjs.get("name") or "").strip()
        if name: return name
    # job_status is a list of history entries — take the last one
    js = job.get("job_status")
    if isinstance(js, list) and js:
        last = js[-1]
        name = (last.get("status_name") or last.get("name") or "").strip()
        if name: return name
    # Legacy fallback
    cs = job.get("current_status")
    if isinstance(cs, dict):
        return (cs.get("status_name") or cs.get("name") or "").strip()
    return ""

def get_job_date(job):
    return (job.get("scheduled_start_time") or job.get("created_on") or "")[:10]

def get_job_duration_hours(job):
    dur = job.get("duration") or 0
    return dur / 60.0 if dur else 1.0

def get_job_assigned_uids(job):
    uids = set()
    assigned = job.get("assigned_to") or []
    if isinstance(assigned, list):
        for u in assigned:
            # Zuper structure: assigned_to[].user.user_uid
            user_obj = u.get("user") or {}
            uid = user_obj.get("user_uid") or u.get("user_uid") or u.get("uid") or ""
            if uid: uids.add(uid)
    elif isinstance(assigned, dict):
        user_obj = assigned.get("user") or {}
        uid = user_obj.get("user_uid") or assigned.get("user_uid") or assigned.get("uid") or ""
        if uid: uids.add(uid)
    return uids

def get_job_team_uid(job):
    # Team is nested inside assigned_to[].team in new Zuper API responses
    assigned = job.get("assigned_to") or []
    if isinstance(assigned, list) and assigned:
        t = assigned[0].get("team") or {}
        if isinstance(t, dict):
            uid = t.get("team_uid") or t.get("uid") or ""
            if uid: return uid
    # Legacy: top-level team field
    t = job.get("team") or {}
    if isinstance(t, dict):
        return t.get("team_uid") or t.get("uid") or ""
    return ""

def get_job_category(job):
    cats = job.get("job_category") or job.get("category") or {}
    if isinstance(cats, dict):
        return cats.get("name") or cats.get("category_name") or ""
    if isinstance(cats, list) and cats:
        return cats[0].get("name") or ""
    return ""

def is_mobile_job(job):
    cat = get_job_category(job).lower()
    return "mobile" in cat or "mrf" in cat

PAGE_SIZE      = 100   # smaller payload per page → faster per-request response
MAX_SCAN_SECS  = 300   # hard time budget for the entire scan (5 min; month needs ~150 pages)

def fetch_jobs_for_period(start_date: str, end_date: str):
    """Fetch Zuper jobs for date range.
    Zuper /jobs/filter returns jobs newest-first (confirmed by debug).
    filter_rules don't filter server-side, so we do early termination:
    stop as soon as max(date) in a page < start_date — all subsequent pages are older.
    """
    start_dt  = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt    = datetime.strptime(end_date,   "%Y-%m-%d")
    jobs      = []
    t0        = time.time()
    for page in range(1, 301):
        elapsed = time.time() - t0
        if elapsed > MAX_SCAN_SECS:
            print(f"[jobs] time limit reached at p{page} ({elapsed:.0f}s), returning {len(jobs)} jobs", flush=True)
            break
        print(f"[jobs] fetching p{page} ({elapsed:.0f}s)…", flush=True)
        try:
            resp  = zuper_post("/jobs/filter", {"limit": PAGE_SIZE, "page": page, "filter_rules": []}, timeout=20)
            batch = resp.get("data") or []
            if not batch:
                print(f"[jobs] p{page} empty — done", flush=True)
                break
            in_range = []
            max_date  = None
            for job in batch:
                jd_str = get_job_date(job)
                if not jd_str: continue
                try:
                    jd = datetime.strptime(jd_str, "%Y-%m-%d")
                    if start_dt <= jd <= end_dt:
                        in_range.append(job)
                    if max_date is None or jd > max_date:
                        max_date = jd
                except ValueError:
                    pass
            jobs.extend(in_range)
            print(f"[jobs] p{page}: {len(batch)} returned, {len(in_range)} in range, max={max_date}", flush=True)
            if len(batch) < PAGE_SIZE: break
            # Zuper newest-first: once max date falls below start, we're past our window
            if max_date is not None and max_date < start_dt:
                print(f"[jobs] early stop p{page}: max={max_date.date()} < {start_date}", flush=True)
                break
        except Exception as e:
            print(f"[jobs] p{page} error: {e}", flush=True)
            break
    total = time.time() - t0
    print(f"[jobs] {start_date}→{end_date}: {len(jobs)} jobs in {total:.0f}s", flush=True)
    return jobs

# ── Azuga helpers ─────────────────────────────────────────
_azuga = {"token": None, "ts": 0.0, "lock": threading.Lock()}

def azuga_token():
    with _azuga["lock"]:
        if _azuga["token"] and time.time() - _azuga["ts"] < 6 * 3600:
            return _azuga["token"]
    if not (AZUGA_USER and AZUGA_PASS): return None
    body = json.dumps({"userName": AZUGA_USER, "password": AZUGA_PASS,
                       "clientId": AZUGA_CLIENT_ID, "loginType": 1}).encode()
    try:
        req = urllib.request.Request(
            "https://auth.azuga.com/azuga-as/oauth2/login/oauthtoken.json?loginType=1",
            data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
        tok = (d.get("data") or {}).get("access_token")
        with _azuga["lock"]:
            _azuga["token"], _azuga["ts"] = tok, time.time()
        return tok
    except Exception as e:
        print(f"[azuga] auth: {e}", flush=True)
        return None

def azuga_trips(day_str):
    """Fetch all Azuga trips for day_str (YYYY-MM-DD). Matches dispatcher format exactly."""
    tok = azuga_token()
    if not tok: return None
    # Use ISO timestamps ~midnight→midnight EDT (same as dispatcher)
    d0    = datetime.strptime(day_str, "%Y-%m-%d")
    start = d0.strftime("%Y-%m-%dT04:00:00.000Z")
    end   = (d0 + timedelta(days=1)).strftime("%Y-%m-%dT06:00:00.000Z")
    rows, seen, index = [], set(), 0
    while index < 60:
        body = json.dumps({
            "index": index, "size": 200, "desc": False,
            "browserTimezone": "America/New_York",
            "sortField": "tsTimeVehicleTimezone",
            "filter": {"orFilter": {"vehicleId": []}, "matchFilter": {}},
            "startDate": start, "endDate": end
        }).encode()
        req = urllib.request.Request(
            "https://services.azuga.com/reports/v3/reports/trip", data=body,
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read())
        except Exception as e:
            print(f"[azuga] trips idx {index}: {e}", flush=True)
            break
        chunk = (data.get("data") or {}).get("result") or []
        new   = [t for t in chunk if t.get("id") not in seen]
        for t in new: seen.add(t.get("id"))
        rows.extend(new)
        if len(chunk) < 200 or not new: break
        index += 1
    print(f"[azuga] {day_str}: {len(rows)} trips", flush=True)
    return rows

def _trip_miles(t):
    # Azuga tripDistance is in km — convert to miles
    km = float(t.get("tripDistance") or t.get("distance") or t.get("totalDistance") or 0)
    return km / 1.60934

def _trip_hours(t):
    # tripTime is always seconds in Azuga v3 API (confirmed: matches tsTime→teTime elapsed)
    raw = t.get("tripTime") or t.get("duration") or t.get("tripDuration") or 0
    return float(raw) / 3600.0

def _trip_driver(t):
    return str(t.get("driver") or t.get("fullName") or t.get("driverName") or "Unknown").strip()

def _trip_vehicle(t):
    return str(t.get("vehicleId") or t.get("vehicle_id") or t.get("vehicleName") or "").strip()

# ── Paylocity stub ────────────────────────────────────────
def paylocity_available():
    return bool(PAY_CLIENT and PAY_SECRET and PAY_COMPANY)

# ── Date helpers ──────────────────────────────────────────
def workdays_in_range(start_str, end_str):
    start = datetime.strptime(start_str, "%Y-%m-%d")
    end   = datetime.strptime(end_str,   "%Y-%m-%d")
    days  = []
    d = start
    while d <= end:
        if d.weekday() < 5:  # Mon–Fri
            days.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return days

def days_remaining_in_range(end_str):
    today = date.today()
    end   = datetime.strptime(end_str, "%Y-%m-%d").date()
    if today > end: return 0
    remaining = 0
    d = today
    while d <= end:
        if d.weekday() < 5:
            remaining += 1
        d += timedelta(days=1)
    return remaining

COMPLETED_STATUSES = {"completed", "closed", "done", "work completed", "complete",
                      "job completed", "service completed"}

# ── KPI Computation ───────────────────────────────────────
def compute_production_kpis(jobs, start_date, end_date, team_uid=None):
    workdays = workdays_in_range(start_date, end_date)
    day_set  = set(workdays)

    if team_uid and team_uid != "all":
        jobs = [j for j in jobs if get_job_team_uid(j) == team_uid]

    completed = [j for j in jobs if get_job_status(j).lower() in COMPLETED_STATUSES]
    in_period = [j for j in jobs if get_job_date(j) in day_set]

    # Daily production
    daily_wheels   = {d: 0 for d in workdays}
    daily_mobile   = {d: 0 for d in workdays}
    daily_reman    = {d: 0 for d in workdays}
    daily_hours    = {d: 0.0 for d in workdays}
    tech_days      = set()  # (uid, day) pairs for unique tech-days
    employee_hours = {}

    daily_nr = {d: 0 for d in workdays}

    for job in in_period:
        d = get_job_date(job)
        if d not in day_set: continue
        status   = get_job_status(job).lower()
        jtype    = get_job_type(job)         # mobile / reman / nr / other
        products = job.get("products") or []
        mob, rem, nr_cnt = count_wheels_from_products(products, job_type=jtype)
        job_wheels = mob + rem
        # Count wheels if job has products (products present = work was performed)
        # OR if status explicitly shows completion. This handles cases where status
        # field name varies across Zuper API versions.
        count_wheels = job_wheels > 0 or (status and status in COMPLETED_STATUSES)
        if count_wheels:
            daily_wheels[d] += job_wheels
            daily_mobile[d] += mob
            daily_reman[d]  += rem
            daily_nr[d]     += nr_cnt
        assigned = get_job_assigned_uids(job)
        dur = get_job_duration_hours(job)
        for uid in assigned:
            tech_days.add((uid, d))
            if uid not in employee_hours:
                employee_hours[uid] = {"name": uid[:8], "hours": {dd: 0.0 for dd in workdays},
                                        "wheels": {dd: 0 for dd in workdays},
                                        "mobile": {dd: 0 for dd in workdays},
                                        "reman":  {dd: 0 for dd in workdays},
                                        "total_hrs": 0.0, "total_wheels": 0,
                                        "total_mobile": 0, "total_reman": 0,
                                        "type": "Hourly"}
            employee_hours[uid]["hours"][d] = employee_hours[uid]["hours"].get(d, 0.0) + dur
            employee_hours[uid]["total_hrs"] += dur
            if count_wheels:
                employee_hours[uid]["wheels"][d] = employee_hours[uid]["wheels"].get(d, 0) + job_wheels
                employee_hours[uid]["mobile"][d] = employee_hours[uid]["mobile"].get(d, 0) + mob
                employee_hours[uid]["reman"][d]  = employee_hours[uid]["reman"].get(d, 0)  + rem
                employee_hours[uid]["total_wheels"] += job_wheels
                employee_hours[uid]["total_mobile"] += mob
                employee_hours[uid]["total_reman"]  += rem
        if assigned:
            daily_hours[d] = daily_hours.get(d, 0.0) + dur

    # W/T/D — wheels per tech per day
    total_wheels    = sum(daily_wheels.values())
    total_mobile    = sum(daily_mobile.values())
    total_reman     = sum(daily_reman.values())
    total_tech_days = len(tech_days) or 1

    wtd_overall = total_wheels / total_tech_days
    wtd_mobile  = total_mobile / max(len({(uid,d) for uid,d in tech_days if daily_mobile.get(d,0)>0}), 1)
    wtd_reman   = total_reman  / max(len({(uid,d) for uid,d in tech_days if daily_reman.get(d,0)>0}), 1)

    # Target: WTD_TARGET × techs × workdays
    num_techs   = len(employee_hours) or 1
    total_target = WTD_TARGET * num_techs * len(workdays)
    days_left    = days_remaining_in_range(end_date)
    pace_per_day = (total_target - total_wheels) / max(days_left, 1) if total_wheels < total_target else 0

    # OT
    over_ot  = sum(1 for e in employee_hours.values() if e["total_hrs"] > OT_THRESHOLD and e["type"] != "Salaried")
    on_watch = sum(1 for e in employee_hours.values() if OT_THRESHOLD*0.8 <= e["total_hrs"] <= OT_THRESHOLD and e["type"] != "Salaried")

    # Daily ratio
    total_hrs = sum(daily_hours.values()) or 1
    daily_ratio = {d: round(daily_wheels[d] / max(daily_hours[d], 0.01), 3) for d in workdays}

    # Estimated revenue
    est_revenue_mobile = total_mobile * MOBILE_ASP
    est_revenue_reman  = total_reman  * REMAN_ASP
    est_revenue_total  = est_revenue_mobile + est_revenue_reman

    total_nr = sum(daily_nr.values())

    emp_list = []
    for uid, v in employee_hours.items():
        active_days = len([d for d in workdays if v["hours"].get(d,0)>0]) or 1
        wtd_emp = v["total_wheels"] / active_days
        status  = ("Over OT" if v["total_hrs"] > OT_THRESHOLD and v["type"]!="Salaried"
                   else "Watch" if v["total_hrs"] >= OT_THRESHOLD*0.8 and v["type"]!="Salaried"
                   else "Salaried" if v["type"]=="Salaried" else "On track")
        emp_list.append({
            "uid": uid, "name": v["name"], "type": v["type"],
            "total_hrs": round(v["total_hrs"], 1),
            "total_wheels": v["total_wheels"],
            "total_mobile": v.get("total_mobile", 0),
            "total_reman":  v.get("total_reman", 0),
            "wtd": round(wtd_emp, 1), "hrs_left": max(OT_THRESHOLD - v["total_hrs"], 0),
            "status": status,
            "daily_hrs":    {d: round(v["hours"].get(d,0.0),1) for d in workdays},
            "daily_wheels": {d: v["wheels"].get(d,0) for d in workdays},
            "daily_mobile": {d: v["mobile"].get(d,0) for d in workdays},
            "daily_reman":  {d: v["reman"].get(d,0)  for d in workdays},
        })
    emp_list.sort(key=lambda e: -e["total_wheels"])

    return {
        "start_date": start_date, "end_date": end_date,
        "workdays": workdays,
        "total_wheels": total_wheels, "total_mobile": total_mobile, "total_reman": total_reman,
        "total_target": round(total_target),
        "variance": total_wheels - round(total_target),
        "pct_of_target": round(total_wheels / max(total_target, 1) * 100, 1),
        "wtd": round(wtd_overall, 2), "wtd_mobile": round(wtd_mobile, 2), "wtd_reman": round(wtd_reman, 2),
        "wtd_target": WTD_TARGET,
        "pace_per_day": round(pace_per_day),
        "days_left": days_left,
        "total_hours": round(total_hrs, 1),
        "weekly_ratio": round(total_wheels / max(total_hrs, 1), 3),
        "over_ot": over_ot, "on_watch": on_watch,
        "num_techs": num_techs,
        "daily_wheels": daily_wheels,
        "daily_mobile": daily_mobile,
        "daily_reman":  daily_reman,
        "daily_hours":  {d: round(daily_hours.get(d,0.0),1) for d in workdays},
        "daily_ratio":  daily_ratio,
        "est_revenue": round(est_revenue_total),
        "est_revenue_mobile": round(est_revenue_mobile),
        "est_revenue_reman":  round(est_revenue_reman),
        "total_nr": total_nr,
        "nr_pct": round(total_nr / max(total_wheels + total_nr, 1) * 100, 1),
        "daily_nr": daily_nr,
        "employees": emp_list,
        "total_jobs": len(jobs),
        "completed_jobs": len(completed),
    }

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

    def parse_path(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        return parsed.path, {k: v[0] for k, v in qs.items()}

    def do_GET(self):
        path, params = self.parse_path()
        if path == "/":
            email = self.auth_email()
            self.send_html(LOGIN_HTML if not email else DASHBOARD_HTML.replace("{{EMAIL}}", email))
            return
        if not path.startswith("/api/"):
            self.send_json({"error": "not found"}, 404); return
        if not self.auth_email():
            self.send_json({"error": "unauthorized"}, 401); return

        if path == "/api/locations":       self.api_locations()
        elif path == "/api/production":    self.api_production(params)
        elif path == "/api/fleet":         self.api_fleet(params)
        elif path == "/api/paylocity":     self.api_paylocity(params)
        elif path == "/api/config":        self.send_json({"wtd_target": WTD_TARGET, "ot_threshold": OT_THRESHOLD,
                                                           "mobile_asp": MOBILE_ASP, "reman_asp": REMAN_ASP,
                                                           "azuga": bool(AZUGA_USER), "paylocity": paylocity_available()})
        elif path == "/api/debug_user":
            # Returns first Zuper user object raw — used to identify numeric employee ID field
            try:
                resp = zuper_get("/user/all?limit=1&page=1")
                users = resp.get("data") or []
                sample = users[0] if users else {}
                self.send_json({"keys": list(sample.keys()), "sample": sample})
            except Exception as e:
                self.send_json({"error": str(e)})
        elif path == "/api/debug_azuga_drivers":
            # Fetch Azuga driver list to find numeric employee ID field
            result = {}
            try:
                tok = azuga_token()
                result["token"] = bool(tok)
                if tok:
                    for endpoint in [
                        "https://services.azuga.com/reports/v3/driver",
                        "https://services.azuga.com/api/v1/driver",
                        "https://services.azuga.com/api/driver",
                        "https://services.azuga.com/reports/v3/reports/driver",
                    ]:
                        try:
                            req = urllib.request.Request(endpoint,
                                headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
                            with urllib.request.urlopen(req, timeout=15) as r:
                                data = json.loads(r.read())
                            result["endpoint"] = endpoint
                            result["status"] = "ok"
                            result["keys"] = list(data.keys()) if isinstance(data, dict) else str(type(data))
                            # Look for driver records
                            records = (data.get("data") or {}).get("result") or data.get("data") or []
                            if isinstance(records, list) and records:
                                result["sample_driver"] = records[0]
                                result["sample_keys"] = list(records[0].keys()) if isinstance(records[0], dict) else []
                            else:
                                result["raw_sample"] = str(data)[:500]
                            break
                        except Exception as e:
                            result[endpoint] = str(e)
            except Exception as e:
                result["error"] = str(e)
            self.send_json(result)
        elif path == "/api/debug_azuga":
            # Tests Azuga auth + one day of trips; returns raw data for debugging
            day = params.get("day") or (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
            result = {"azuga_user_set": bool(AZUGA_USER), "day": day}
            try:
                tok = azuga_token()
                result["token_obtained"] = bool(tok)
                if tok:
                    d0    = datetime.strptime(day, "%Y-%m-%d")
                    start = d0.strftime("%Y-%m-%dT04:00:00.000Z")
                    end   = (d0 + timedelta(days=1)).strftime("%Y-%m-%dT06:00:00.000Z")
                    body  = json.dumps({"index": 0, "size": 5, "desc": False,
                                        "browserTimezone": "America/New_York",
                                        "sortField": "tsTimeVehicleTimezone",
                                        "filter": {"orFilter": {"vehicleId": []}, "matchFilter": {}},
                                        "startDate": start, "endDate": end}).encode()
                    req = urllib.request.Request(
                        "https://services.azuga.com/reports/v3/reports/trip", data=body,
                        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}, method="POST")
                    with urllib.request.urlopen(req, timeout=30) as r:
                        raw = json.loads(r.read())
                    result["raw_response_keys"] = list(raw.keys()) if isinstance(raw, dict) else str(type(raw))
                    result["data_keys"] = list((raw.get("data") or {}).keys()) if isinstance(raw.get("data"), dict) else str(raw.get("data"))
                    chunk = (raw.get("data") or {}).get("result") or []
                    result["trip_count_sample"] = len(chunk)
                    result["first_trip_keys"] = list(chunk[0].keys()) if chunk else []
                    result["first_trip"] = chunk[0] if chunk else None
            except Exception as e:
                result["error"] = str(e)
            self.send_json(result)
        elif path == "/api/debug_job_sample":
            # Show raw structure of 3 recent jobs to diagnose wheel counting
            try:
                resp  = zuper_post("/jobs/filter", {"limit": 3, "page": 1, "filter_rules": []}, timeout=15)
                batch = resp.get("data") or []
                samples = []
                for job in batch:
                    samples.append({
                        "top_level_keys":      sorted(job.keys()),
                        "status_resolved":     get_job_status(job),
                        "date":                get_job_date(job),
                        "assigned_uids":       list(get_job_assigned_uids(job)),
                        "assigned_to_raw":     job.get("assigned_to"),
                        "current_job_status":  job.get("current_job_status"),
                        "job_status":          job.get("job_status"),
                        "current_status":      job.get("current_status"),
                        "products":            job.get("products"),
                        "job_category":        job.get("job_category"),
                        "wheels_counted":      sum(count_wheels_from_products(job.get("products") or [])),
                    })
                self.send_json({"count": len(batch), "jobs": samples})
            except Exception as e:
                self.send_json({"error": str(e)})
        elif path == "/api/debug_jobs_filter":
            # Test Zuper filter_rules date filter formats
            start = params.get("start") or date.today().strftime("%Y-%m-%d")
            end   = params.get("end")   or start
            result = {"start": start, "end": end, "formats_tried": []}
            # Format 1: scheduled_start_time with between operator
            for fmt_name, filter_rules in [
                ("scheduled_start_time_between", [{"attribute":"scheduled_start_time","operator":"between","value":[f"{start}T00:00:00Z", f"{end}T23:59:59Z"]}]),
                ("scheduled_start_time_gte_lte",  [{"attribute":"scheduled_start_time","operator":">=","value":f"{start}T00:00:00Z"},{"attribute":"scheduled_start_time","operator":"<=","value":f"{end}T23:59:59Z"}]),
                ("job_date_between",              [{"attribute":"job_date","operator":"between","value":[start, end]}]),
                ("empty_filter",                  []),
            ]:
                try:
                    resp = zuper_post("/jobs/filter", {"limit":5,"page":1,"filter_rules":filter_rules}, timeout=10)
                    batch = resp.get("data") or []
                    dates = [get_job_date(j) for j in batch if get_job_date(j)]
                    result["formats_tried"].append({"format":fmt_name,"count":len(batch),"sample_dates":dates[:5],"error":None})
                except Exception as e:
                    result["formats_tried"].append({"format":fmt_name,"count":0,"sample_dates":[],"error":str(e)})
            self.send_json(result)
        else: self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        path, _ = self.parse_path()
        if path == "/api/login":   self.api_login()
        elif path == "/api/logout":
            self.send_response(200)
            self.send_header("Set-Cookie", "awrs_token=; Path=/; Max-Age=0")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        else: self.send_json({"error": "not found"}, 404)

    def api_login(self):
        try: data = json.loads(self.read_body())
        except: self.send_json({"error": "bad request"}, 400); return
        email = (data.get("email") or "").strip().lower()
        if not ALLOWED_EMAIL.match(email):
            self.send_json({"error": "Only @alloywheel.com accounts are allowed."}, 403); return
        token = make_auth_token(email)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", f"awrs_token={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={AUTH_TTL}")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "email": email}).encode())

    def api_locations(self):
        c = cache_get("locations")
        if c: self.send_json(c); return
        try:
            teams = fetch_teams()
            result = [{"uid": t.get("team_uid") or t.get("uid",""),
                       "name": t.get("team_name") or t.get("name") or "?"} for t in teams]
            result = [r for r in result if r["uid"]]
            cache_set("locations", result, 300)
            self.send_json(result)
        except Exception as e:
            self.send_json([])

    def api_production(self, params):
        import traceback as _tb
        start  = params.get("start") or date.today().strftime("%Y-%m-%d")
        end    = params.get("end")   or start
        loc    = params.get("location") or "all"
        key    = f"prod_{start}_{end}_{loc}"

        # 1. Specific-location KPI cache hit → instant response
        c = cache_get(key)
        if c: self.send_json(c); return

        # 2. Raw jobs already cached for this date range → compute KPIs locally (fast, no Zuper scan)
        jkey = f"jobs_{start}_{end}"
        jobs = cache_get(jkey)
        if jobs is not None:
            try:
                kpis = compute_production_kpis(jobs, start, end, team_uid=loc)
                emp_map = build_employee_map()
                for e in kpis["employees"]:
                    rec = emp_map["by_uid"].get(e["uid"], {})
                    if rec.get("name"):        e["name"]        = rec["name"]
                    if rec.get("emp_code"):    e["emp_code"]    = rec["emp_code"]
                    if rec.get("designation"): e["designation"] = rec["designation"]
                cache_set(key, kpis, 90)
                self.send_json(kpis)
            except Exception as e:
                print(f"[production] KPI compute error: {e}", flush=True)
                _tb.print_exc()
                self.send_json({"error": str(e)}, 500)
            return

        # 3. Full cache miss — always kick off "all" scan to populate raw jobs cache.
        #    Location-specific requests will compute instantly once "all" scan completes.
        all_key = f"prod_{start}_{end}_all"
        with _warming_lock:
            already = all_key in _warming
            if not already:
                _warming.add(all_key)
        if not already:
            threading.Thread(target=self._warm_production,
                             args=(all_key, start, end, "all"), daemon=True).start()
        self.send_json({"status": "loading"}, 202)

    def _warm_production(self, key, start, end, loc):
        import traceback as _tb
        try:
            # Always scan "all" so we can populate the raw jobs cache.
            # Location-specific keys recompute from the cached jobs list.
            jkey = f"jobs_{start}_{end}"
            jobs = cache_get(jkey)
            if jobs is None:
                jobs = fetch_jobs_for_period(start, end)
                cache_set(jkey, jobs, 600)   # 10-min raw jobs cache
                print(f"[production warm] jobs cached: {len(jobs)} for {start}→{end}", flush=True)
            kpis = compute_production_kpis(jobs, start, end, team_uid=loc)
            emp_map = build_employee_map()
            for e in kpis["employees"]:
                rec = emp_map["by_uid"].get(e["uid"], {})
                if rec.get("name"):        e["name"]        = rec["name"]
                if rec.get("emp_code"):    e["emp_code"]    = rec["emp_code"]
                if rec.get("designation"): e["designation"] = rec["designation"]
            cache_set(key, kpis, 90)
            print(f"[production warm] {key} ready", flush=True)
        except Exception as e:
            print(f"[production warm] {key} error: {e}", flush=True)
            _tb.print_exc()
        finally:
            with _warming_lock:
                _warming.discard(key)

    def api_fleet(self, params):
        start = params.get("start") or date.today().strftime("%Y-%m-%d")
        end   = params.get("end")   or start
        loc   = params.get("location") or "all"
        key   = f"fleet_{start}_{end}_{loc}"
        c     = cache_get(key)
        if c: self.send_json(c); return
        if not (AZUGA_USER and AZUGA_PASS):
            self.send_json({"stub": True, "message": "Set AZUGA_USERNAME and AZUGA_PASSWORD in Render environment."}); return
        try:
            emp_map = build_employee_map()
            # Build set of driver names allowed for this location filter
            loc_driver_names = None  # None = all
            if loc and loc != "all":
                loc_driver_names = {
                    rec["name"].lower()
                    for rec in emp_map["by_uid"].values()
                    if rec.get("team_uid") == loc and rec.get("name")
                }
            days = workdays_in_range(start, end)
            daily = {}
            driver_totals = {}  # name → {trips, miles, hours, emp_code, designation, days}
            for d in days:
                trips = azuga_trips(d) or []
                drivers = {}
                for t in trips:
                    drv = _trip_driver(t)
                    # Skip unnamed / unknown drivers
                    if not drv or drv.lower() in ("unknown", ""):
                        continue
                    emp = emp_lookup_by_name(drv, emp_map)
                    # Apply location filter via team membership
                    if loc_driver_names is not None and drv.lower() not in loc_driver_names:
                        continue
                    m = _trip_miles(t)
                    h = _trip_hours(t)
                    if drv not in drivers:
                        drivers[drv] = {"trips":0,"miles":0.0,"hours":0.0,
                                        "emp_code": emp.get("emp_code",""),
                                        "designation": emp.get("designation","")}
                    drivers[drv]["trips"] += 1
                    drivers[drv]["miles"] += m
                    drivers[drv]["hours"] += h
                    if drv not in driver_totals:
                        driver_totals[drv] = {"trips":0,"miles":0.0,"hours":0.0,"days":0,
                                              "emp_code": emp.get("emp_code",""),
                                              "designation": emp.get("designation","")}
                    driver_totals[drv]["trips"] += 1
                    driver_totals[drv]["miles"] += m
                    driver_totals[drv]["hours"] += h
                for drv in drivers:
                    driver_totals[drv]["days"] = driver_totals[drv].get("days", 0) + 1
                for drv in drivers:
                    drivers[drv]["miles"] = round(drivers[drv]["miles"], 1)
                    drivers[drv]["hours"] = round(drivers[drv]["hours"], 2)
                # Day-level totals only count included drivers
                inc_trips = list(drivers.values())
                daily[d] = {
                    "trips":    sum(v["trips"]  for v in inc_trips),
                    "miles":    round(sum(v["miles"]  for v in inc_trips), 1),
                    "hours":    round(sum(v["hours"]  for v in inc_trips), 1),
                    "vehicles": len(drivers),
                    "drivers":  drivers,
                }
            for drv in driver_totals:
                driver_totals[drv]["miles"] = round(driver_totals[drv]["miles"], 1)
                driver_totals[drv]["hours"] = round(driver_totals[drv]["hours"], 2)
            totals = {
                "trips":    sum(v["trips"]    for v in daily.values()),
                "miles":    round(sum(v["miles"]    for v in daily.values()), 1),
                "hours":    round(sum(v["hours"]    for v in daily.values()), 1),
                "vehicles": max((v["vehicles"] for v in daily.values()), default=0),
            }
            result = {"start": start, "end": end, "daily": daily, "totals": totals, "drivers": driver_totals}
            cache_set(key, result, 180)
            self.send_json(result)
        except Exception as e:
            print(f"[fleet] {e}", flush=True)
            self.send_json({"error": str(e)}, 500)

    def api_paylocity(self, params):
        if not paylocity_available():
            self.send_json({"stub": True, "message": "Set PAYLOCITY_CLIENT_ID, PAYLOCITY_CLIENT_SECRET, PAYLOCITY_COMPANY_ID."})
        else:
            self.send_json({"stub": True, "message": "Paylocity integration coming soon."})


# ── HTML ──────────────────────────────────────────────────
LOGIN_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AWRS Reporting</title>
<style>*{box-sizing:border-box;margin:0;padding:0}body{font-family:'Segoe UI',system-ui,sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;display:flex;align-items:center;justify-content:center}.card{background:#1e293b;border:1px solid #334155;border-radius:16px;padding:48px 40px;width:100%;max-width:420px;box-shadow:0 25px 50px rgba(0,0,0,.5)}.brand{font-size:1.8rem;font-weight:800;color:#f8fafc;text-align:center;letter-spacing:-.5px}.sub{font-size:.85rem;color:#64748b;text-align:center;margin-top:4px;margin-bottom:36px}label{display:block;font-size:.78rem;font-weight:600;color:#94a3b8;margin-bottom:6px;text-transform:uppercase;letter-spacing:.06em}input{width:100%;background:#0f172a;border:1px solid #334155;border-radius:8px;padding:12px 14px;color:#e2e8f0;font-size:.95rem;outline:none;transition:border .15s}input:focus{border-color:#3b82f6}.btn{width:100%;margin-top:24px;background:#3b82f6;color:#fff;border:none;border-radius:8px;padding:13px;font-size:.95rem;font-weight:600;cursor:pointer;transition:background .15s}.btn:hover{background:#2563eb}.err{color:#f87171;font-size:.83rem;margin-top:12px;text-align:center;display:none}</style></head>
<body><div class="card"><div class="brand">AWRS</div><div class="sub">Reporting Dashboard</div>
<label>Email</label><input id="em" type="email" placeholder="you@alloywheel.com">
<button class="btn" onclick="go()">Sign in</button><div class="err" id="err"></div></div>
<script>document.getElementById('em').addEventListener('keydown',e=>e.key==='Enter'&&go());
async function go(){const e=document.getElementById('em').value.trim(),r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:e})});const d=await r.json();if(d.ok)location.reload();else{document.getElementById('err').textContent=d.error||'Login failed';document.getElementById('err').style.display='block';}}</script></body></html>"""

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AWRS Reporting Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0f172a;--surf:#1e293b;--surf2:#243044;--bdr:#334155;--txt:#e2e8f0;--muted:#64748b;
  --accent:#3b82f6;--green:#22c55e;--yellow:#eab308;--red:#ef4444;--orange:#f97316;--purple:#a855f7}
body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--txt);min-height:100vh;font-size:14px}

/* Topbar */
.topbar{background:var(--surf);border-bottom:1px solid var(--bdr);padding:0 20px;display:flex;align-items:center;height:52px;gap:12px}
.brand{font-size:1.05rem;font-weight:800;color:#f8fafc;letter-spacing:-.3px}
.brand-sub{font-size:.7rem;color:var(--muted)}
.topbar-right{margin-left:auto;display:flex;align-items:center;gap:10px}
.user-lbl{font-size:.75rem;color:var(--muted)}
.sign-out{background:none;border:1px solid var(--bdr);border-radius:5px;color:var(--muted);font-size:.72rem;padding:4px 9px;cursor:pointer;transition:all .15s}
.sign-out:hover{border-color:var(--red);color:var(--red)}

/* Date range bar */
.datebar{background:var(--surf);border-bottom:1px solid var(--bdr);padding:10px 20px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.range-btn{background:none;border:1px solid var(--bdr);border-radius:6px;color:var(--muted);font-size:.78rem;padding:6px 12px;cursor:pointer;transition:all .15s;white-space:nowrap}
.range-btn:hover{border-color:var(--accent);color:var(--accent)}
.range-btn.active{background:rgba(59,130,246,.15);border-color:var(--accent);color:var(--accent);font-weight:600}
.divider{width:1px;height:20px;background:var(--bdr);margin:0 4px}
.custom-dates{display:none;align-items:center;gap:6px}
.custom-dates.show{display:flex}
.custom-dates input{background:#0f172a;border:1px solid var(--bdr);border-radius:6px;color:var(--txt);padding:5px 8px;font-size:.78rem;outline:none}
.custom-dates input:focus{border-color:var(--accent)}
.go-btn{background:var(--accent);border:none;border-radius:6px;color:#fff;font-size:.78rem;font-weight:600;padding:6px 12px;cursor:pointer}
.loc-wrap{margin-left:auto;display:flex;align-items:center;gap:8px}
.loc-label{font-size:.75rem;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.05em}
.loc-select{background:#0f172a;border:1px solid var(--bdr);border-radius:6px;color:var(--txt);padding:6px 10px;font-size:.8rem;outline:none}
.last-upd{font-size:.72rem;color:var(--muted)}

/* Tabs */
.tabs{display:flex;background:var(--surf);border-bottom:1px solid var(--bdr);padding:0 20px}
.tab{padding:13px 18px;font-size:.83rem;font-weight:600;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent;transition:all .15s;white-space:nowrap}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.tab-panel{display:none;padding:20px}
.tab-panel.active{display:block}

/* KPI row */
.kpi-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px}
.kpi{background:var(--surf);border:1px solid var(--bdr);border-radius:10px;padding:16px;position:relative;overflow:hidden}
.kpi::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:var(--accent)}
.kpi.c-green::before{background:var(--green)}.kpi.c-red::before{background:var(--red)}.kpi.c-yellow::before{background:var(--yellow)}.kpi.c-purple::before{background:var(--purple)}.kpi.c-orange::before{background:var(--orange)}
.kpi-lbl{font-size:.68rem;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;font-weight:600;margin-bottom:6px}
.kpi-val{font-size:1.7rem;font-weight:800;line-height:1;color:#f8fafc}
.kpi-sub{font-size:.72rem;color:var(--muted);margin-top:5px}
.pos{color:var(--green)}.neg{color:var(--red)}.warn{color:var(--yellow)}

/* Section cards */
.card{background:var(--surf);border:1px solid var(--bdr);border-radius:10px;overflow:hidden;margin-bottom:20px}
.card-hdr{padding:14px 18px;border-bottom:1px solid var(--bdr);display:flex;align-items:center;justify-content:space-between}
.card-title{font-size:.85rem;font-weight:700}
.card-badge{font-size:.7rem;color:var(--muted);background:#0f172a;border-radius:16px;padding:2px 10px;border:1px solid var(--bdr)}
.card-body{padding:18px}

/* Charts */
.chart-2col{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:700px){.chart-2col{grid-template-columns:1fr}}
.chart-wrap{position:relative;height:200px}

/* Tables */
table{width:100%;border-collapse:collapse;font-size:.79rem}
th{text-align:left;color:var(--muted);font-weight:600;font-size:.68rem;text-transform:uppercase;letter-spacing:.06em;padding:7px 10px;border-bottom:1px solid var(--bdr)}
td{padding:8px 10px;border-bottom:1px solid rgba(51,65,85,.4)}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(255,255,255,.02)}
.tr{text-align:right}

/* Badges */
.badge{display:inline-block;font-size:.67rem;font-weight:700;padding:2px 7px;border-radius:12px}
.badge.on-track{background:rgba(34,197,94,.15);color:var(--green)}
.badge.watch{background:rgba(234,179,8,.15);color:var(--yellow)}
.badge.over-ot{background:rgba(239,68,68,.15);color:var(--red)}
.badge.salaried{background:rgba(100,116,139,.15);color:var(--muted)}
.chip{display:inline-block;font-size:.7rem;font-weight:700;padding:2px 6px;border-radius:3px}
.chip-red{background:rgba(239,68,68,.2);color:var(--red)}
.chip-yellow{background:rgba(234,179,8,.2);color:var(--yellow)}
.chip-green{background:rgba(34,197,94,.2);color:var(--green)}
.chip-blue{background:rgba(59,130,246,.2);color:var(--accent)}

/* Stub banner */
.stub{background:rgba(234,179,8,.07);border:1px dashed var(--yellow);border-radius:8px;padding:14px 18px;display:flex;align-items:flex-start;gap:12px;margin-bottom:16px}
.stub .ico{font-size:1.1rem;flex-shrink:0;margin-top:1px}
.stub .msg{font-size:.8rem;color:var(--yellow);line-height:1.5}
.stub .msg strong{color:#fde047}
.stub .msg code{background:rgba(0,0,0,.3);padding:1px 5px;border-radius:3px;font-size:.75rem}

/* Fleet cards */
.fleet-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:16px}
.fleet-card{background:#0f172a;border:1px solid var(--bdr);border-radius:8px;padding:12px;text-align:center}
.fleet-card .fv{font-size:1.3rem;font-weight:800;color:var(--accent)}
.fleet-card .fl{font-size:.66rem;color:var(--muted);text-transform:uppercase;margin-top:3px}

/* Paylocity planned items list */
.plan-list{list-style:none;margin-top:12px}
.plan-list li{font-size:.8rem;color:var(--muted);line-height:2;padding-left:16px;position:relative}
.plan-list li::before{content:'→';position:absolute;left:0;color:var(--accent)}

/* loading */
.loading{text-align:center;padding:36px;color:var(--muted);font-size:.85rem}
.sp{display:inline-block;width:18px;height:18px;border:2px solid var(--bdr);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite;margin-right:6px;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head><body>

<!-- Topbar -->
<div class="topbar">
  <div><div class="brand">AWRS</div><div class="brand-sub">Reporting Dashboard</div></div>
  <div class="topbar-right">
    <span class="user-lbl" id="userEmail">{{EMAIL}}</span>
    <button class="sign-out" onclick="logout()">Sign out</button>
  </div>
</div>

<!-- Date range bar -->
<div class="datebar">
  <button class="range-btn" onclick="setRange('today',this)">Today</button>
  <button class="range-btn active" onclick="setRange('week',this)">This Week</button>
  <button class="range-btn" onclick="setRange('month',this)">This Month</button>
  <button class="range-btn" onclick="setRange('last_month',this)">Last Month</button>
  <button class="range-btn" onclick="setRange('custom',this)">Custom</button>
  <div class="custom-dates" id="customDates">
    <input type="date" id="customStart">
    <span style="color:var(--muted)">→</span>
    <input type="date" id="customEnd">
    <button class="go-btn" onclick="applyCustom()">Go</button>
  </div>
  <div class="divider"></div>
  <div class="loc-wrap">
    <span class="loc-label">Location</span>
    <select class="loc-select" id="locationPicker" onchange="loadAll()">
      <option value="all">All Locations</option>
    </select>
    <span class="last-upd" id="lastUpd"></span>
  </div>
</div>

<!-- Tabs -->
<div class="tabs">
  <div class="tab active" onclick="switchTab('production',this)">📦 Production</div>
  <div class="tab" onclick="switchTab('drivers',this)">🚗 Drivers</div>
  <div class="tab" onclick="switchTab('techs',this)">🔧 Mobile Techs</div>
  <div class="tab" onclick="switchTab('admin',this)">📊 Admin</div>
</div>

<!-- Production tab -->
<div class="tab-panel active" id="tab-production">
  <div class="kpi-row" id="prod-kpis"><div class="loading"><span class="sp"></span>Loading…</div></div>
  <div class="chart-2col">
    <div class="card">
      <div class="card-hdr"><span class="card-title">Wheels by Day</span><span class="card-badge" id="prod-type-badge">MRF + Reman</span></div>
      <div class="card-body"><div class="chart-wrap"><canvas id="wheelsChart"></canvas></div></div>
    </div>
    <div class="card">
      <div class="card-hdr"><span class="card-title">W/T/D (Wheels per Tech per Day)</span><span class="card-badge">Target: <span id="wtdTargetBadge">9.0</span></span></div>
      <div class="card-body"><div class="chart-wrap"><canvas id="wtdChart"></canvas></div></div>
    </div>
  </div>
  <div class="card">
    <div class="card-hdr"><span class="card-title">Daily Breakdown</span></div>
    <div class="card-body" id="prod-table"><div class="loading"><span class="sp"></span>Loading…</div></div>
  </div>
</div>

<!-- Drivers tab -->
<div class="tab-panel" id="tab-drivers">
  <div id="drivers-content"><div class="loading"><span class="sp"></span>Loading fleet data…</div></div>
</div>

<!-- Mobile Techs tab -->
<div class="tab-panel" id="tab-techs">
  <div class="kpi-row" id="techs-kpis"></div>
  <div class="card">
    <div class="card-hdr"><span class="card-title">Tech Performance</span><span class="card-badge" id="techs-badge">–</span></div>
    <div class="card-body" id="techs-table"><div class="loading"><span class="sp"></span>Loading…</div></div>
  </div>
</div>

<!-- Admin tab -->
<div class="tab-panel" id="tab-admin">
  <div class="kpi-row" id="admin-kpis"></div>
  <div class="card">
    <div class="card-hdr"><span class="card-title">Paylocity — Labor & Payroll</span></div>
    <div class="card-body" id="admin-paylocity"><div class="loading"><span class="sp"></span>Checking Paylocity…</div></div>
  </div>
  <div class="card">
    <div class="card-hdr"><span class="card-title">Location Scorecard</span><span class="card-badge">Est. from Zuper job data</span></div>
    <div class="card-body" id="admin-scorecard"><div class="loading"><span class="sp"></span>Loading…</div></div>
  </div>
</div>

<script>
// ── State ──────────────────────────────────────────────────
let CFG={wtd_target:9,ot_threshold:40,mobile_asp:93.80,reman_asp:141.95,azuga:false,paylocity:false};
let RANGE={start:'',end:''};
let PROD=null, FLEET=null;
let wheelsChart=null, wtdChart=null;
let driversLoaded=false, adminLoaded=false;

// ── Init ───────────────────────────────────────────────────
(async function init(){
  try{ CFG=await apiFetch('/api/config'); }catch(e){}
  document.getElementById('wtdTargetBadge').textContent=CFG.wtd_target.toFixed(1);

  // Load locations
  try{
    const locs=await apiFetch('/api/locations');
    const sel=document.getElementById('locationPicker');
    (locs||[]).forEach(l=>{
      const o=document.createElement('option');
      o.value=l.uid; o.textContent=l.name; sel.appendChild(o);
    });
  }catch(e){}

  setRange('week', document.querySelector('.range-btn.active'));
})();

// ── Date range ─────────────────────────────────────────────
function setRange(type, el){
  document.querySelectorAll('.range-btn').forEach(b=>b.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('customDates').classList.remove('show');
  const today=new Date();
  const fmt=d=>d.toISOString().slice(0,10);
  if(type==='today'){
    RANGE={start:fmt(today),end:fmt(today)};
  } else if(type==='week'){
    const mon=new Date(today); mon.setDate(today.getDate()-today.getDay()+(today.getDay()===0?-6:1));
    const fri=new Date(mon); fri.setDate(mon.getDate()+4);
    RANGE={start:fmt(mon),end:fmt(fri)};
  } else if(type==='month'){
    const s=new Date(today.getFullYear(),today.getMonth(),1);
    const e=new Date(today.getFullYear(),today.getMonth()+1,0);
    RANGE={start:fmt(s),end:fmt(e)};
  } else if(type==='last_month'){
    const s=new Date(today.getFullYear(),today.getMonth()-1,1);
    const e=new Date(today.getFullYear(),today.getMonth(),0);
    RANGE={start:fmt(s),end:fmt(e)};
  } else if(type==='custom'){
    document.getElementById('customDates').classList.add('show');
    document.getElementById('customStart').value=RANGE.start||fmt(today);
    document.getElementById('customEnd').value=RANGE.end||fmt(today);
    return;
  }
  loadAll();
}

function applyCustom(){
  RANGE.start=document.getElementById('customStart').value;
  RANGE.end  =document.getElementById('customEnd').value;
  if(!RANGE.start||!RANGE.end) return;
  loadAll();
}

// ── Load all data ──────────────────────────────────────────
async function loadAll(){
  document.getElementById('lastUpd').textContent='Loading…';
  driversLoaded=false; adminLoaded=false;
  await loadProduction();
  // Always pre-load fleet in background so Drivers tab is ready when clicked
  loadDrivers();
  if(document.getElementById('tab-admin').classList.contains('active')) await loadAdmin();
  document.getElementById('lastUpd').textContent='Updated '+new Date().toLocaleTimeString();
}

// ── Production ─────────────────────────────────────────────
let _prodPollTimer=null;
let _prodPollCount=0;
async function loadProduction(){
  if(_prodPollTimer){clearTimeout(_prodPollTimer);_prodPollTimer=null;}
  setEl('prod-kpis','<div class="loading"><span class="sp"></span>Loading…</div>');
  setEl('prod-table','<div class="loading"><span class="sp"></span>Loading…</div>');
  const loc=document.getElementById('locationPicker').value;
  const url=`/api/production?start=${RANGE.start}&end=${RANGE.end}&location=${loc}`;
  let data;
  try{
    data=await apiFetch(url);
  }catch(e){
    // 502/503 = server still starting or restarting — retry silently
    const retryable=e.message.includes('502')||e.message.includes('503')||e.message.includes('fetch');
    if(retryable && _prodPollCount<20){
      _prodPollCount++;
      setEl('prod-kpis','<div class="loading"><span class="sp"></span>Connecting to server…</div>');
      _prodPollTimer=setTimeout(loadProduction,8000);
    }else{
      setEl('prod-kpis',`<div class="loading" style="color:var(--red)">Error: ${e.message}</div>`);
      _prodPollCount=0;
    }
    return;
  }
  // Server returns 202 + {status:"loading"} while Zuper job scan runs in background
  if(data && data.status==='loading'){
    _prodPollCount++;
    const elapsed=_prodPollCount*6;
    setEl('prod-kpis',`<div class="loading"><span class="sp"></span>Scanning Zuper jobs… (~${elapsed}s elapsed, checking every 6s)</div>`);
    _prodPollTimer=setTimeout(loadProduction,6000);
    return;
  }
  _prodPollCount=0;
  PROD=data;
  renderProdKPIs(PROD);
  renderWheelsChart(PROD);
  renderWTDChart(PROD);
  renderProdTable(PROD);
  renderTechsTab(PROD);
  // If fleet already loaded (pre-load beat production), re-render drivers to pick up wheel data
  if(FLEET) renderDrivers(FLEET);
}

function renderProdKPIs(d){
  const varC=d.variance>=0?'pos':'neg';
  const varS=d.variance>=0?'+':'';
  const wtdC=d.wtd>=CFG.wtd_target?'c-green':d.wtd>=CFG.wtd_target*.9?'c-yellow':'c-red';
  const pctC=d.pct_of_target>=95?'c-green':d.pct_of_target>=80?'c-yellow':'c-red';
  const nr=d.total_nr||0, nrPct=d.nr_pct||0;
  const nrC=nrPct<=5?'c-green':nrPct<=10?'c-yellow':'c-red';
  document.getElementById('prod-kpis').innerHTML=`
    <div class="kpi ${pctC}"><div class="kpi-lbl">Total Wheels</div><div class="kpi-val">${n(d.total_wheels)}</div>
      <div class="kpi-sub">Target ~${n(d.total_target)} &nbsp;<span class="${varC}">${varS}${n(d.variance)}</span></div></div>
    <div class="kpi ${wtdC}"><div class="kpi-lbl">W/T/D (Blended)</div><div class="kpi-val">${d.wtd.toFixed(1)}</div>
      <div class="kpi-sub">Target ${CFG.wtd_target.toFixed(1)} &nbsp;|&nbsp; Mobile ${d.wtd_mobile.toFixed(1)} / Reman ${d.wtd_reman.toFixed(1)}</div></div>
    <div class="kpi"><div class="kpi-lbl">Mobile Wheels</div><div class="kpi-val">${n(d.total_mobile)}</div>
      <div class="kpi-sub">Est. $${n(d.est_revenue_mobile)} rev @ $${CFG.mobile_asp} ASP</div></div>
    <div class="kpi"><div class="kpi-lbl">Reman Wheels</div><div class="kpi-val">${n(d.total_reman)}</div>
      <div class="kpi-sub">Est. $${n(d.est_revenue_reman)} rev @ $${CFG.reman_asp} ASP</div></div>
    <div class="kpi ${nrC}"><div class="kpi-lbl">Non-Repairable (NR)</div><div class="kpi-val">${nr}</div>
      <div class="kpi-sub">${nrPct}% of total repairs &nbsp;|&nbsp; <span class="${nrC}">${nrPct<=5?'▼ Low':nrPct<=10?'▲ Elevated':'⚠ High'}</span></div></div>
    <div class="kpi ${pctC}"><div class="kpi-lbl">% of Target</div><div class="kpi-val">${d.pct_of_target}%</div>
      <div class="kpi-sub">${d.days_left} workday${d.days_left!==1?'s':''} remaining &nbsp;|&nbsp; ${d.pace_per_day>0?n(d.pace_per_day)+' needed/day':'On pace ✓'}</div></div>
    <div class="kpi c-purple"><div class="kpi-lbl">Est. Total Revenue</div><div class="kpi-val">$${n(d.est_revenue)}</div>
      <div class="kpi-sub">${n(d.completed_jobs)} jobs / ${n(d.total_jobs)} total</div></div>
    <div class="kpi ${d.over_ot>0?'c-red':d.on_watch>0?'c-yellow':'c-green'}">
      <div class="kpi-lbl">OT Status</div><div class="kpi-val">${d.over_ot}</div>
      <div class="kpi-sub">${d.over_ot} over &nbsp;|&nbsp; ${d.on_watch} on watch</div></div>
  `;
}

function renderWheelsChart(d){
  if(wheelsChart) wheelsChart.destroy();
  const days=d.workdays, labels=days.map(dd=>fmtDay(dd));
  const mobile=days.map(dd=>d.daily_mobile[dd]||0);
  const reman =days.map(dd=>d.daily_reman[dd]||0);
  const nr    =days.map(dd=>d.daily_nr[dd]||0);
  const dtarget=Array(days.length).fill(CFG.wtd_target*(d.num_techs||1));
  wheelsChart=new Chart(document.getElementById('wheelsChart'),{
    type:'bar',
    data:{labels,datasets:[
      {label:'Mobile',data:mobile,backgroundColor:'rgba(59,130,246,.75)',borderRadius:4,stack:'a'},
      {label:'Reman', data:reman, backgroundColor:'rgba(168,85,247,.75)',borderRadius:4,stack:'a'},
      {label:'NR',    data:nr,    backgroundColor:'rgba(239,68,68,.5)', borderRadius:4,stack:'a'},
      {label:'Target',data:dtarget,type:'line',borderColor:'rgba(239,68,68,.7)',borderWidth:2,borderDash:[5,3],pointRadius:0,fill:false}
    ]},
    options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:'#94a3b8',font:{size:10}}}},
      scales:{x:{stacked:true,ticks:{color:'#64748b'},grid:{color:'#1e293b'}},y:{stacked:true,ticks:{color:'#64748b'},grid:{color:'#1e293b'}}}}
  });
}

function renderWTDChart(d){
  if(wtdChart) wtdChart.destroy();
  const days=d.workdays, labels=days.map(dd=>fmtDay(dd));
  // Compute daily W/T/D = wheels / unique techs active that day
  const dailyWTD=days.map(dd=>{
    const w=d.daily_wheels[dd]||0;
    const activeTechs=d.employees.filter(e=>(e.daily_wheels[dd]||0)>0).length||1;
    return +(w/activeTechs).toFixed(2);
  });
  const wtdC=dailyWTD.map(v=>v>=CFG.wtd_target?'rgba(34,197,94,.8)':v>=CFG.wtd_target*.9?'rgba(234,179,8,.8)':'rgba(239,68,68,.8)');
  wtdChart=new Chart(document.getElementById('wtdChart'),{
    type:'bar',
    data:{labels,datasets:[
      {label:'W/T/D',data:dailyWTD,backgroundColor:wtdC,borderRadius:4},
      {label:'Target',data:Array(days.length).fill(CFG.wtd_target),type:'line',
       borderColor:'rgba(239,68,68,.7)',borderWidth:2,borderDash:[5,3],pointRadius:0,fill:false}
    ]},
    options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:'#94a3b8',font:{size:10}}}},
      scales:{x:{ticks:{color:'#64748b'},grid:{color:'#1e293b'}},
              y:{ticks:{color:'#64748b'},grid:{color:'#1e293b'},min:0,suggestedMax:Math.max(CFG.wtd_target+2,Math.max(...dailyWTD)+1)}}}
  });
}

function renderProdTable(d){
  const days=d.workdays;
  let html=`<table><thead><tr><th>Day</th><th class="tr">Mobile</th><th class="tr">Reman</th><th class="tr">Total</th><th class="tr" style="color:var(--red)">NR</th><th class="tr" style="color:var(--red)">NR%</th><th class="tr">Hours</th><th class="tr">W/T/D</th></tr></thead><tbody>`;
  days.forEach((day,i)=>{
    const mob=d.daily_mobile[day]||0, rem=d.daily_reman[day]||0, tot=d.daily_wheels[day]||0;
    const nr=d.daily_nr[day]||0, nrPct=tot+nr>0?((nr/(tot+nr))*100).toFixed(1):'0.0';
    const nrC=parseFloat(nrPct)<=5?'':'color:var('+(parseFloat(nrPct)<=10?'--yellow':'--red')+')';
    const hrs=d.daily_hours[day]||0, rat=d.daily_ratio[day]||0;
    const activeTechs=d.employees.filter(e=>(e.daily_wheels[day]||0)>0).length||1;
    const wtd=+(tot/activeTechs).toFixed(1);
    const wc=wtd>=CFG.wtd_target?'chip-green':wtd>=CFG.wtd_target*.9?'chip-yellow':'chip-red';
    html+=`<tr><td>${fmtDay(day)}<span style="color:var(--muted);font-size:.7rem;margin-left:6px">${day}</span></td>
      <td class="tr">${mob}</td><td class="tr">${rem}</td><td class="tr"><strong>${tot}</strong></td>
      <td class="tr" style="${nrC}">${nr||'—'}</td>
      <td class="tr" style="${nrC}">${nr?nrPct+'%':'—'}</td>
      <td class="tr">${hrs}</td>
      <td class="tr"><span class="chip ${wc}">${wtd}</span></td></tr>`;
  });
  const tw=d.total_wheels, tnr=d.total_nr||0, tnrPct=d.nr_pct||0;
  const nrTotC=tnrPct<=5?'':'color:var('+(tnrPct<=10?'--yellow':'--red')+')';
  const wc=d.wtd>=CFG.wtd_target?'chip-green':d.wtd>=CFG.wtd_target*.9?'chip-yellow':'chip-red';
  html+=`</tbody><tfoot><tr style="font-weight:700"><td>Total / Avg</td>
    <td class="tr">${d.total_mobile}</td><td class="tr">${d.total_reman}</td><td class="tr">${tw}</td>
    <td class="tr" style="${nrTotC}">${tnr||'—'}</td>
    <td class="tr" style="${nrTotC}">${tnr?tnrPct+'%':'—'}</td>
    <td class="tr">${d.total_hours}</td>
    <td class="tr"><span class="chip ${wc}">${d.wtd.toFixed(1)}</span></td>
  </tr></tfoot></table>
  <div style="margin-top:10px;font-size:.7rem;color:var(--muted)">
    NR% guide: <span style="color:var(--green)">≤5% Good</span> &nbsp;
    <span style="color:var(--yellow)">5–10% Elevated</span> &nbsp;
    <span style="color:var(--red)">&gt;10% High</span> &nbsp;&nbsp;|&nbsp;&nbsp;
    W/T/D target: <strong>${CFG.wtd_target}</strong> wheels per tech per day
  </div>`;
  setEl('prod-table', html);
}

// ── Techs tab ──────────────────────────────────────────────
function renderTechsTab(d){
  const emps=d.employees||[];
  const days=d.workdays;
  document.getElementById('techs-badge').textContent=`${emps.length} techs · ${d.completed_jobs} jobs`;
  document.getElementById('techs-kpis').innerHTML=`
    <div class="kpi c-accent"><div class="kpi-lbl">Active Techs</div><div class="kpi-val">${d.num_techs}</div><div class="kpi-sub">from Zuper assignments</div></div>
    <div class="kpi ${d.wtd>=CFG.wtd_target?'c-green':d.wtd>=CFG.wtd_target*.9?'c-yellow':'c-red'}">
      <div class="kpi-lbl">Avg W/T/D</div><div class="kpi-val">${d.wtd.toFixed(1)}</div><div class="kpi-sub">Target ${CFG.wtd_target}</div></div>
    <div class="kpi ${d.over_ot>0?'c-red':d.on_watch>0?'c-yellow':'c-green'}">
      <div class="kpi-lbl">OT — Over</div><div class="kpi-val">${d.over_ot}</div><div class="kpi-sub">${d.on_watch} on watch</div></div>
    <div class="kpi"><div class="kpi-lbl">Total Hours</div><div class="kpi-val">${d.total_hours}</div><div class="kpi-sub">OT threshold ${CFG.ot_threshold} hrs</div></div>
  `;
  if(!emps.length){
    setEl('techs-table','<div style="color:var(--muted);font-size:.82rem;text-align:center;padding:28px">No tech assignments found for this period in Zuper.</div>');
    return;
  }
  let html=`<table><thead><tr><th>Technician</th>${days.map(d=>`<th class="tr">${fmtDay(d)}</th>`).join('')}<th class="tr">Mobile</th><th class="tr">Reman</th><th class="tr">Total Whl</th><th class="tr">Total Hrs</th><th class="tr">W/T/D</th><th class="tr">Hrs Left</th><th>Status</th></tr></thead><tbody>`;
  emps.forEach(e=>{
    const bc={'On track':'badge on-track','Watch':'badge watch','Over OT':'badge over-ot','Salaried':'badge salaried'}[e.status]||'badge';
    const wc=e.wtd>=CFG.wtd_target?'chip-green':e.wtd>=CFG.wtd_target*.9?'chip-yellow':'chip-red';
    const hc=e.total_hrs>CFG.ot_threshold?'neg':e.total_hrs>=CFG.ot_threshold*.8?'warn':'';
    const mob=e.total_mobile||0, rem=e.total_reman||0;
    html+=`<tr><td>${esc(e.name)}<span style="color:var(--muted);font-size:.68rem;margin-left:5px">${e.type}</span></td>
      ${days.map(d=>{const w=e.daily_wheels[d]||0;const h=(e.daily_hrs||{})[d]||0;return`<td class="tr" style="font-size:.75rem">${w?w+' whl':''}<br>${h?h+'h':''}</td>`;}).join('')}
      <td class="tr">${mob||'—'}</td>
      <td class="tr">${rem||'—'}</td>
      <td class="tr"><strong>${e.total_wheels}</strong></td>
      <td class="tr ${hc}">${e.total_hrs}</td>
      <td class="tr"><span class="chip ${wc}">${e.wtd.toFixed(1)}</span></td>
      <td class="tr">${e.type==='Salaried'?'—':e.hrs_left.toFixed(1)}</td>
      <td><span class="${bc}">${e.status}</span></td></tr>`;
  });
  html+=`</tbody></table>`;
  setEl('techs-table', html);
}

// ── Drivers tab ────────────────────────────────────────────
async function loadDrivers(){
  if(driversLoaded) return;
  driversLoaded=true;
  setEl('drivers-content','<div class="loading"><span class="sp"></span>Loading Azuga fleet data…</div>');
  try{
    const loc=document.getElementById('locationPicker').value;
    FLEET=await apiFetch(`/api/fleet?start=${RANGE.start}&end=${RANGE.end}&location=${loc}`);
    renderDrivers(FLEET);
  }catch(e){
    setEl('drivers-content',`<div class="stub"><div class="ico">⚠️</div><div class="msg">Error: ${e.message}</div></div>`);
  }
}

function renderDrivers(f){
  if(f.stub||f.error){
    setEl('drivers-content',`
      <div class="stub"><div class="ico">🚗</div><div class="msg">
        <strong>Azuga not connected.</strong><br>
        Add <code>AZUGA_USERNAME</code> and <code>AZUGA_PASSWORD</code> to Render environment variables to enable live fleet & driver data.<br><br>
        When connected, this section will show: miles per driver, drive hours, trips, idle time, accidents, and vehicle utilization.
      </div></div>`);
    return;
  }
  const t=f.totals||{};
  let html=`
    <div class="fleet-row">
      <div class="fleet-card"><div class="fv">${t.vehicles||0}</div><div class="fl">Vehicles Active</div></div>
      <div class="fleet-card"><div class="fv">${t.trips||0}</div><div class="fl">Total Trips</div></div>
      <div class="fleet-card"><div class="fv">${(t.miles||0).toLocaleString()}</div><div class="fl">Total Miles</div></div>
      <div class="fleet-card"><div class="fv">${(t.hours||0).toFixed(1)}</div><div class="fl">Drive Hours</div></div>
    </div>
    <div class="card"><div class="card-hdr"><span class="card-title">Daily Fleet Activity</span></div><div class="card-body">
    <table><thead><tr><th>Day</th><th class="tr">Trips</th><th class="tr">Miles</th><th class="tr">Drive Hrs</th><th class="tr">Vehicles</th></tr></thead><tbody>`;
  Object.entries(f.daily||{}).forEach(([d,v])=>{
    html+=`<tr><td>${fmtDay(d)} <span style="color:var(--muted);font-size:.7rem">${d}</span></td>
      <td class="tr">${v.trips}</td><td class="tr">${v.miles.toLocaleString()}</td>
      <td class="tr">${v.hours.toFixed(1)}</td><td class="tr">${v.vehicles}</td></tr>`;
  });
  html+=`</tbody></table></div></div>`;

  // Build wheel lookup from production data (Zuper employees joined by name)
  const whlByName={};
  if(PROD && PROD.employees){
    PROD.employees.forEach(e=>{
      whlByName[(e.name||'').toLowerCase()]={total:e.total_wheels||0, mobile:e.total_mobile||0, reman:e.total_reman||0};
    });
  }
  const prodLoaded=!!PROD;

  // Use backend-aggregated driver totals (f.drivers) — already summed across all days
  const dlist=Object.entries(f.drivers||{}).sort((a,b)=>b[1].miles-a[1].miles);
  if(dlist.length){
    const prodNote=prodLoaded
      ?'<span style="font-size:.72rem;color:var(--muted);margin-left:8px">Wheels joined from Zuper production data</span>'
      :'<span style="font-size:.72rem;color:var(--yellow);margin-left:8px">⏳ Production still loading — wheel data pending</span>';
    html+=`<div class="card"><div class="card-hdr"><span class="card-title">Driver Summary</span>${prodNote}</div><div class="card-body">
    <table><thead><tr>
      <th>Driver</th>
      <th class="tr">Trips</th>
      <th class="tr">Miles</th>
      <th class="tr">Drive Hrs</th>
      <th class="tr">Mph Avg</th>
      <th class="tr">Wheels</th>
      <th class="tr" title="Wheels repaired per mile driven">Whl/Mile</th>
    </tr></thead><tbody>`;
    dlist.forEach(([name,v])=>{
      const mph=v.hours>0?(v.miles/v.hours).toFixed(1):'—';
      const wdata=whlByName[name.toLowerCase()]||{};
      const wheels=wdata.total||0;
      const wpm=v.miles>0&&wheels>0?(wheels/v.miles).toFixed(3):'—';
      const wpmStyle=wpm!=='—'?(parseFloat(wpm)>=0.08?'color:var(--green)':parseFloat(wpm)>=0.04?'color:var(--yellow)':'color:var(--muted)'):'';
      html+=`<tr>
        <td>${esc(name)}<span style="font-size:.68rem;color:var(--muted);margin-left:5px">${v.designation||''}</span></td>
        <td class="tr">${v.trips}</td>
        <td class="tr">${v.miles.toFixed(1)}</td>
        <td class="tr">${v.hours.toFixed(1)}</td>
        <td class="tr">${mph}</td>
        <td class="tr">${prodLoaded?(wheels||'—'):'…'}</td>
        <td class="tr" style="${wpmStyle}">${prodLoaded?wpm:'…'}</td>
      </tr>`;
    });
    // Totals row
    const totWheels=Object.values(whlByName).reduce((s,w)=>s+w.total,0);
    const totMiles=dlist.reduce((s,[,v])=>s+v.miles,0);
    const totWpm=totMiles>0&&totWheels>0?(totWheels/totMiles).toFixed(3):'—';
    html+=`</tbody><tfoot><tr style="font-weight:700"><td>Total / Fleet</td>
      <td class="tr">${t.trips}</td>
      <td class="tr">${t.miles.toLocaleString()}</td>
      <td class="tr">${t.hours.toFixed(1)}</td>
      <td class="tr">${t.hours>0?(t.miles/t.hours).toFixed(1):'—'}</td>
      <td class="tr">${prodLoaded?(totWheels||'—'):'…'}</td>
      <td class="tr">${prodLoaded?totWpm:'…'}</td>
    </tr></tfoot></table>
    <div style="margin-top:8px;font-size:.7rem;color:var(--muted)">Whl/Mile guide: <span style="color:var(--green)">≥0.080 Good</span> &nbsp;<span style="color:var(--yellow)">0.040–0.079 Avg</span> &nbsp;<span style="color:var(--muted)">&lt;0.040 Low</span></div>
    </div></div>`;
  }
  setEl('drivers-content', html);
}

// ── Admin tab ──────────────────────────────────────────────
async function loadAdmin(){
  if(adminLoaded||!PROD) return;
  adminLoaded=true;
  const d=PROD;
  document.getElementById('admin-kpis').innerHTML=`
    <div class="kpi c-purple"><div class="kpi-lbl">Est. Revenue</div><div class="kpi-val">$${n(d.est_revenue)}</div><div class="kpi-sub">Mobile + Reman (wheels × ASP)</div></div>
    <div class="kpi"><div class="kpi-lbl">Active Employees</div><div class="kpi-val">${d.num_techs}</div><div class="kpi-sub">from Zuper job assignments</div></div>
    <div class="kpi ${d.over_ot>0?'c-red':d.on_watch>0?'c-yellow':'c-green'}">
      <div class="kpi-lbl">OT Exposure</div><div class="kpi-val">${d.over_ot}</div><div class="kpi-sub">${d.on_watch} approaching threshold</div></div>
    <div class="kpi"><div class="kpi-lbl">Total Labor Hrs</div><div class="kpi-val">${n(d.total_hours)}</div><div class="kpi-sub">Paylocity will add $ cost</div></div>
  `;

  // Paylocity
  try{
    const p=await apiFetch(`/api/paylocity?start=${RANGE.start}&end=${RANGE.end}`);
    if(p.stub){
      setEl('admin-paylocity',`
        <div class="stub"><div class="ico">💰</div><div class="msg">
          <strong>Paylocity not connected.</strong><br>
          Set <code>PAYLOCITY_CLIENT_ID</code>, <code>PAYLOCITY_CLIENT_SECRET</code>, <code>PAYLOCITY_COMPANY_ID</code> in Render to unlock:<br>
        </div></div>
        <ul class="plan-list">
          <li>Clock-in / clock-out per employee (actual vs scheduled)</li>
          <li>OT cost (regular + 1.5× overtime wages)</li>
          <li>Labor cost as % of estimated revenue</li>
          <li>Cost per wheel (labor ÷ wheels produced)</li>
          <li>Payroll period summary by location</li>
        </ul>`);
    }
  }catch(e){}

  // Location scorecard from Zuper data
  const byTeam={};
  (PROD.employees||[]).forEach(emp=>{
    // Without team data per-employee we show overall summary
  });
  setEl('admin-scorecard',`
    <table><thead><tr>
      <th>Metric</th><th class="tr">This Period</th><th class="tr">Target</th><th class="tr">vs Target</th>
    </tr></thead><tbody>
      <tr><td>W/T/D (Blended)</td><td class="tr">${d.wtd.toFixed(1)}</td><td class="tr">${CFG.wtd_target.toFixed(1)}</td>
        <td class="tr"><span class="chip ${d.wtd>=CFG.wtd_target?'chip-green':'chip-red'}">${d.wtd>=CFG.wtd_target?'+':''}${(d.wtd-CFG.wtd_target).toFixed(1)}</span></td></tr>
      <tr><td>W/T/D (Mobile)</td><td class="tr">${d.wtd_mobile.toFixed(1)}</td><td class="tr">${CFG.wtd_target.toFixed(1)}</td>
        <td class="tr"><span class="chip ${d.wtd_mobile>=CFG.wtd_target?'chip-green':'chip-red'}">${d.wtd_mobile>=CFG.wtd_target?'+':''}${(d.wtd_mobile-CFG.wtd_target).toFixed(1)}</span></td></tr>
      <tr><td>W/T/D (Reman)</td><td class="tr">${d.wtd_reman.toFixed(1)}</td><td class="tr">${CFG.wtd_target.toFixed(1)}</td>
        <td class="tr"><span class="chip ${d.wtd_reman>=CFG.wtd_target?'chip-green':'chip-red'}">${d.wtd_reman>=CFG.wtd_target?'+':''}${(d.wtd_reman-CFG.wtd_target).toFixed(1)}</span></td></tr>
      <tr><td>Wheels Produced</td><td class="tr">${n(d.total_wheels)}</td><td class="tr">~${n(d.total_target)}</td>
        <td class="tr"><span class="chip ${d.variance>=0?'chip-green':'chip-red'}">${d.variance>=0?'+':''}${n(d.variance)}</span></td></tr>
      <tr><td>Est. Revenue</td><td class="tr">$${n(d.est_revenue)}</td><td class="tr">—</td><td class="tr">—</td></tr>
      <tr><td>OT Threshold</td><td class="tr">${CFG.ot_threshold} hrs</td><td class="tr">&lt;${CFG.ot_threshold}</td>
        <td class="tr"><span class="chip ${d.over_ot===0?'chip-green':'chip-red'}">${d.over_ot} over</span></td></tr>
    </tbody></table>
    <div style="margin-top:12px;font-size:.74rem;color:var(--muted)">Revenue estimated from Zuper job completions × ASP. Actual P&amp;L requires accounting system integration.</div>
  `);
}

// ── Tab switching ──────────────────────────────────────────
function switchTab(name, el){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p=>p.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('tab-'+name).classList.add('active');
  if(name==='drivers') loadDrivers();
  if(name==='admin')   loadAdmin();
}

// ── Utilities ──────────────────────────────────────────────
async function apiFetch(url){
  const r=await fetch(url);
  if(r.status===401){location.reload();throw new Error('Session expired');}
  if(r.status===202){return r.json();} // loading/warming — caller handles
  if(!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}
function setEl(id,html){const e=document.getElementById(id);if(e)e.innerHTML=html;}
function esc(s){const d=document.createElement('div');d.textContent=s||'';return d.innerHTML;}
function n(v){return (Math.round(v)||0).toLocaleString();}
function fmtDay(d){const days=['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];const dt=new Date(d+'T12:00:00');return days[dt.getDay()];}
async function logout(){await fetch('/api/logout',{method:'POST'});location.reload();}
</script>
</body></html>"""

# ── Startup cache pre-warm ────────────────────────────────
def _do_prewarm(label, start, end):
    """Internal: fetch + cache jobs and KPIs for one date range."""
    import traceback as _tb
    key = f"prod_{start}_{end}_all"
    jkey = f"jobs_{start}_{end}"
    with _warming_lock:
        if key in _warming:
            return
        _warming.add(key)
    try:
        print(f"[prewarm] {label} scan {start}→{end}", flush=True)
        jobs = fetch_jobs_for_period(start, end)
        cache_set(jkey, jobs, 600)   # 10-min raw jobs cache (serves location filters)
        kpis = compute_production_kpis(jobs, start, end, team_uid="all")
        emp_map = build_employee_map()
        for e in kpis["employees"]:
            rec = emp_map["by_uid"].get(e["uid"], {})
            if rec.get("name"):        e["name"]        = rec["name"]
            if rec.get("emp_code"):    e["emp_code"]    = rec["emp_code"]
            if rec.get("designation"): e["designation"] = rec["designation"]
        cache_set(key, kpis, 90)
        print(f"[prewarm] {label} ready — {len(jobs)} jobs", flush=True)
    except Exception as e:
        print(f"[prewarm] {label} error: {e}", flush=True)
        _tb.print_exc()
    finally:
        with _warming_lock:
            _warming.discard(key)

def _prewarm_production():
    """Pre-warm this-week AND this-month production caches on startup (sequentially).
    Runs in a background thread so the server starts immediately.
    """
    today = date.today()
    mon   = today - timedelta(days=today.weekday())

    # ── This Week ──────────────────────────────────────────
    w_start = mon.strftime("%Y-%m-%d")
    w_end   = min(mon + timedelta(days=4), today).strftime("%Y-%m-%d")
    _do_prewarm("week", w_start, w_end)

    # ── This Month ─────────────────────────────────────────
    m_start = date(today.year, today.month, 1).strftime("%Y-%m-%d")
    m_end   = today.strftime("%Y-%m-%d")
    if m_start != w_start or m_end != w_end:   # skip if same as week (e.g. first Mon of month)
        _do_prewarm("month", m_start, m_end)

# ── Server ────────────────────────────────────────────────
def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"AWRS Reporting Dashboard → http://localhost:{PORT}", flush=True)
    # Pre-warm production cache in the background so first page load is instant
    threading.Thread(target=_prewarm_production, daemon=True, name="prewarm").start()
    if IS_LOCAL:
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)

if __name__ == "__main__":
    main()
