"""Count clients, not posts. Never emit a single composite score."""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_posts (post_id TEXT PRIMARY KEY, source_type TEXT, source_url TEXT, client_key TEXT,
  title TEXT, body TEXT, body_sha1 TEXT, date_posted TEXT, date_collected TEXT, batch_query TEXT, dup_of TEXT);
CREATE TABLE IF NOT EXISTS observations (obs_id INTEGER PRIMARY KEY, post_id TEXT, run_id TEXT, model TEXT,
  prompt_version TEXT, schema_version TEXT, is_relevant INTEGER, spending_observed TEXT, evidence_grade TEXT,
  economic_signal TEXT, automation_fit TEXT, needs_review INTEGER, raw_json TEXT);
CREATE TABLE IF NOT EXISTS patterns (pattern_id TEXT PRIMARY KEY, family_id TEXT, canonical_name TEXT, definition TEXT,
  same_prototype_test TEXT, status TEXT, taxonomy_version INT, merged_into TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS pattern_assignments (obs_id INT, pattern_id TEXT, method TEXT, similarity REAL,
  taxonomy_version INT, reviewed INT);
CREATE TABLE IF NOT EXISTS validation_events (event_id INTEGER PRIMARY KEY, pattern_id TEXT, business_id TEXT,
  event_type TEXT, date TEXT, proof_ref TEXT, notes TEXT);
"""

VIEW = """
CREATE VIEW IF NOT EXISTS pattern_metrics AS
SELECT COALESCE(pt.merged_into, a.pattern_id) AS pattern_id,
  COUNT(DISTINCT p.post_id) AS posts,
  COUNT(DISTINCT p.client_key) AS unique_clients,
  COUNT(DISTINCT p.source_type) AS source_types,
  COUNT(DISTINCT CASE WHEN o.spending_observed IN ('hiring_human','paying_for_tool','stated_budget_for_fix')
        THEN p.client_key END) AS clients_with_spending,
  COUNT(DISTINCT CASE WHEN o.economic_signal LIKE 'quantified%' THEN p.client_key END) AS clients_quantified,
  SUM(CASE WHEN o.evidence_grade IN ('A','B') THEN 1 ELSE 0 END) AS strong_obs,
  MIN(p.date_posted) AS first_seen, MAX(p.date_posted) AS last_seen
FROM pattern_assignments a
JOIN observations o ON o.obs_id = a.obs_id
JOIN raw_posts p ON p.post_id = o.post_id
LEFT JOIN patterns pt ON pt.pattern_id = a.pattern_id
WHERE o.is_relevant = 1 AND p.dup_of IS NULL AND a.reviewed IN (0,1)
GROUP BY COALESCE(pt.merged_into, a.pattern_id);
"""

def init(db_path):
    con = sqlite3.connect(db_path); con.executescript(SCHEMA); con.executescript(VIEW); con.commit(); return con

def pattern_report(con):
    con.row_factory = sqlite3.Row
    out = []
    for r in con.execute("SELECT * FROM pattern_metrics ORDER BY unique_clients_with_spending DESC, unique_clients DESC"
                         .replace("unique_clients_with_spending","clients_with_spending")):
        r = dict(r); flags = []
        if r["unique_clients"] < 5: flags.append("insufficient_repetition")
        if r["clients_with_spending"] < 3: flags.append("spending_unproven")
        if r["strong_obs"] < 3: flags.append("weak_evidence_base")
        r["flags"] = flags
        r["verdict"] = "insufficient_evidence" if flags else "advance_to_A3_review"
        out.append(r)
    return out

def validation_stage(con, pattern_id):
    """Derived. Never set by hand. B-stages need proof_ref."""
    m = con.execute("SELECT * FROM pattern_metrics WHERE pattern_id=?", (pattern_id,)).fetchone()
    stage = "A0"
    if m:
        uc, sp = m[2], m[4]
        if uc >= 5: stage = "A1"
        if uc >= 5 and sp >= 3: stage = "A2"
    ev = {r[0] for r in con.execute(
        "SELECT DISTINCT event_type FROM validation_events WHERE pattern_id=? AND proof_ref IS NOT NULL AND proof_ref!=''",
        (pattern_id,))}
    ladder = [("conversation","B1"),("interest_stated","B2"),("commitment","B3"),("free_pilot","B4"),("paid_pilot","B5")]
    for etype, st in ladder:
        if etype in ev: stage = st
    paying = con.execute("SELECT COUNT(DISTINCT business_id) FROM validation_events WHERE pattern_id=? AND event_type IN ('paid_pilot','repeat_payment') AND proof_ref!=''", (pattern_id,)).fetchone()[0]
    if paying >= 3: stage = "B6"
    return stage
