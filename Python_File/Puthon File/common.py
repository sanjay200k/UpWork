"""CSV reading, value parsers and the SQLite schema shared by every v4 script."""
import csv, datetime, hashlib, json, re, sqlite3, sys

ALIASES = {
    "title": ["job_title", "title"],
    "body": ["job_description", "description", "body"],
    "url": ["job_url", "url", "link"],
    "date_posted": ["date_posted", "posted_at", "posted", "date"],
    "client_key": ["client_key", "client_id"],
    "budget": ["budget", "job_budget", "price", "hourly_range"],
    "proposals": ["proposals", "proposals_count", "applicants"],
    "client_payment_verified": ["client_payment_verified", "payment_verified"],
    "client_total_spend": ["client_total_spend", "total_spent", "client_spend"],
    "client_hire_rate": ["client_hire_rate", "hire_rate"],
    "client_rating": ["client_rating", "rating"],
    "connects_required": ["connects_required", "connects"],
}

def pick(row, names):
    for n in names:
        v = row.get(n)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return None

def sha1_text(s):
    return hashlib.sha1(re.sub(r"\s+", " ", s or "").strip().lower().encode("utf-8", "ignore")).hexdigest()

def derive_lead_id(row):
    pid = pick(row, ["post_id", "lead_id", "id"])
    if pid: return pid
    url = pick(row, ALIASES["url"])
    if url: return url
    t = (pick(row, ALIASES["title"]) or "") + "|" + (pick(row, ALIASES["body"]) or "")
    return "post-" + sha1_text(t)[:10]

def read_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows: sys.exit("Input CSV is empty.")
    cols = rows[0].keys()
    if not any(c in cols for c in ALIASES["title"]) or not any(c in cols for c in ALIASES["body"]):
        sys.exit(f"CSV needs a title column (job_title) and a body column (job_description). Found: {list(cols)}")
    out, seen = [], set()
    for r in rows:
        lid = derive_lead_id(r)
        if lid in seen:                      # same id twice in one file: keep both leads, make ids unique
            lid = f"{lid}#{len(seen)}"
        seen.add(lid)
        meta = {k: pick(r, ALIASES[k]) for k in ("budget", "proposals", "client_payment_verified", "client_total_spend",
                                                  "client_hire_rate", "client_rating", "connects_required")}
        out.append({"lead_id": lid, "client_key": pick(r, ALIASES["client_key"]),
                    "title": pick(r, ALIASES["title"]) or "", "body": pick(r, ALIASES["body"]) or "",
                    "url": pick(r, ALIASES["url"]) or "", "date_posted": pick(r, ALIASES["date_posted"]) or "",
                    "meta": meta, "orig": r})
    return out

# ---------------- parsers (all return None when unknown) ----------------
def parse_money(s):
    if s is None: return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*([kKmM])?", str(s).replace(",", ""))
    if not m: return None
    v = float(m.group(1)); u = (m.group(2) or "").lower()
    return v * (1000 if u == "k" else 1_000_000 if u == "m" else 1)

def parse_budget(s):
    t = str(s or "").strip()
    if not t: return None
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", t.replace(",", ""))]
    low = t.lower()
    hourly = bool(re.search(r"/\s*h|hour|\bhr\b", low))
    return {"kind": "hourly" if hourly else ("fixed" if nums else "unclear"),
            "amount": max(nums) if nums else None, "raw": t}

def parse_lower_bound_int(s):
    if s is None: return None
    t = str(s).lower()
    if "less than" in t: return 0
    m = re.search(r"\d+", t)
    return int(m.group()) if m else None

def parse_bool(s):
    if s is None: return None
    t = str(s).strip().lower()
    if re.search(r"unverified|not verified|\bno\b|false|^0$", t): return False
    if re.search(r"verified|\byes\b|true|^1$", t): return True
    return None

def parse_pct(s):
    m = re.search(r"\d+(?:\.\d+)?", str(s)) if s is not None else None
    return float(m.group()) if m else None

def parse_date(s):
    if not s: return None
    t = str(s).strip()
    for fmt, n in (("%Y-%m-%d", 10), ("%m/%d/%Y", 10), ("%d/%m/%Y", 10)):
        try: return datetime.datetime.strptime(t[:n], fmt).date()
        except ValueError: pass
    return None

# ---------------- database ----------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (lead_id TEXT PRIMARY KEY, source_url TEXT, client_key TEXT, title TEXT, body TEXT,
  body_sha1 TEXT, date_posted TEXT, collected_at TEXT, meta_json TEXT, dup_group TEXT, dup_kind TEXT, reuse_of TEXT, run_id TEXT);
CREATE TABLE IF NOT EXISTS discovery (lead_id TEXT, run_id TEXT, model TEXT, is_candidate INTEGER, service_category TEXT,
  hiring_signal TEXT, reason TEXT, span TEXT, span_verified INTEGER, attempts INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS qualification (qual_id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id TEXT, run_id TEXT, model TEXT,
  prompt_version TEXT, service_category TEXT, buyer_intent TEXT, buyer_role TEXT, evidence_grade TEXT, needs_review INTEGER,
  review_reasons TEXT, downgrades TEXT, attempts INTEGER, json_first_try INTEGER, error TEXT, raw_json TEXT, validated_json TEXT);
CREATE TABLE IF NOT EXISTS problems (problem_id INTEGER PRIMARY KEY AUTOINCREMENT, qual_id INTEGER, lead_id TEXT, idx INTEGER,
  role TEXT, problem_statement TEXT, support_span TEXT, workflow_object TEXT, pain_type TEXT, pattern_id TEXT,
  proposed_pattern TEXT, problem_json TEXT);
CREATE TABLE IF NOT EXISTS patterns (pattern_id TEXT PRIMARY KEY, category TEXT, definition TEXT, same_prototype_test TEXT,
  status TEXT, first_lead TEXT, likely_synonym_of TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS scores (lead_id TEXT, run_id TEXT, qual_id INTEGER, lead_score REAL, tier TEXT, coverage REAL,
  buyer_intent TEXT, service_category TEXT, service_fit TEXT, automation_fit TEXT, automation_score REAL, needs_review INTEGER,
  next_action TEXT, components_json TEXT, caps_json TEXT, action_json TEXT, PRIMARY KEY (lead_id, run_id));
CREATE TABLE IF NOT EXISTS review_queue (id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id TEXT, run_id TEXT, kind TEXT, detail TEXT,
  resolved INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS outcomes (outcome_id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id TEXT UNIQUE, applied_at TEXT,
  connects_spent INTEGER, bid_amount REAL, replied INTEGER DEFAULT 0, replied_at TEXT, interview INTEGER DEFAULT 0,
  hired INTEGER DEFAULT 0, revenue REAL DEFAULT 0, lead_score REAL, tier TEXT, components_json TEXT, notes TEXT, updated_at TEXT);

CREATE VIEW IF NOT EXISTS current_quals AS
  SELECT * FROM qualification q WHERE q.qual_id = (SELECT MAX(qual_id) FROM qualification WHERE lead_id = q.lead_id);

CREATE VIEW IF NOT EXISTS current_leads AS
SELECT l.lead_id, l.title, l.source_url, l.client_key, l.date_posted, l.dup_group, l.dup_kind,
       s.run_id, s.lead_score, s.tier, s.buyer_intent, s.service_category, s.service_fit, s.automation_fit,
       s.automation_score, s.needs_review, s.next_action, s.components_json,
       (SELECT d.is_candidate FROM discovery d WHERE d.lead_id = l.lead_id ORDER BY d.run_id DESC LIMIT 1) AS is_candidate
FROM leads l JOIN scores s ON s.lead_id = l.lead_id
  AND s.run_id = (SELECT MAX(run_id) FROM scores WHERE lead_id = l.lead_id);

CREATE VIEW IF NOT EXISTS pattern_metrics AS
SELECT pa.pattern_id AS pattern_id, pt.category AS category, pt.status AS status,
  COUNT(DISTINCT pr.lead_id) AS leads,
  COUNT(DISTINCT CASE WHEN l.dup_kind IS NULL OR l.dup_kind != 'repost_same_client' THEN l.client_key END) AS unique_clients,
  SUM(CASE WHEN l.dup_kind = 'copied_template' THEN 1 ELSE 0 END) AS copied_template_leads,
  MIN(l.date_posted) AS first_seen, MAX(l.date_posted) AS last_seen
FROM (SELECT problem_id, COALESCE(pattern_id, proposed_pattern) AS pattern_id FROM problems) pa
JOIN problems pr ON pr.problem_id = pa.problem_id
JOIN current_quals q ON q.qual_id = pr.qual_id
JOIN leads l ON l.lead_id = pr.lead_id
LEFT JOIN patterns pt ON pt.pattern_id = pa.pattern_id
WHERE pa.pattern_id IS NOT NULL
GROUP BY pa.pattern_id;
"""

def connect(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.commit()
    return con
