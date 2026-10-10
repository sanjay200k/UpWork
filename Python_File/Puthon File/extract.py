"""
extract.py  (v3 runner)

CSV of Upwork posts -> Groq -> validate_post() -> SQLite + CSV/JSONL outputs.

Needs these files in the SAME folder:
    prompts_v3.py, schema_v3.json, scoring.py, normalize.py
    validate.py   <- your "validate (1).py" renamed (Python cannot import a name with spaces)

Usage:
    python extract.py input.csv
    python extract.py input.csv --out-dir OutPutCsv --limit 5
    python extract.py            (prompts for the CSV path)

Input CSV columns:
    required: job_title, job_description
    optional: post_id, client_key (or client_id), date_posted, job_url, source_type
    If post_id is missing it is generated (sample-001 style).
    If client_key is missing the client is "unknown" and is NEVER counted as unique.
"""
import argparse, csv, datetime, json, os, sqlite3, sys, time, traceback
from collections import Counter

try:
    from prompts_v3 import (SYSTEM_PROMPT, USER_TEMPLATE, WORKFLOW_OBJECTS, PROMPT_VERSION)
    from normalize import SEED_PATTERNS
    import validate as V
    from validate import validate_post, body_sha1, truncate_body, is_near_dup
except ImportError as e:
    sys.exit(f"Import failed: {e}\nCheck that prompts_v3.py, normalize.py, scoring.py and validate.py "
             f"(renamed from 'validate (1).py') are in the same folder as extract.py.")

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
SCHEMA_VERSION = "v3"
MAX_ATTEMPTS = 3
MAX_CONSECUTIVE_FAILS = 3
CAPABILITY_CATALOG = {}          # id -> difficulty 1-5. Empty until you define one.

# ------------------------------------------------------------------ setup
def get_api_key():
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        try:
            import config
            key = config.GROQ_API_KEY
        except Exception:
            key = None
    if not key or "your-key" in key.lower():
        sys.exit("No Groq key. Set the GROQ_API_KEY environment variable (do not put it in files you share).")
    return key

def load_schema_text():
    p = os.path.join(HERE, "schema_v3.json")
    if not os.path.exists(p):
        sys.exit("schema_v3.json not found next to extract.py")
    with open(p, encoding="utf-8") as f:
        raw = f.read()
    return raw, json.loads(raw)

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_posts (
  post_id TEXT PRIMARY KEY, source_type TEXT, source_url TEXT, client_key TEXT, title TEXT, body TEXT,
  body_sha1 TEXT, date_posted TEXT, date_collected TEXT, dup_of TEXT, copied_template INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS observations (
  obs_id INTEGER PRIMARY KEY AUTOINCREMENT, post_id TEXT, run_id TEXT, model TEXT, prompt_version TEXT,
  schema_version TEXT, post_type TEXT, is_relevant INTEGER, relevance_reason TEXT, client_ask TEXT,
  industry_stated TEXT, post_errors TEXT, attempts INTEGER, json_first_try INTEGER, error TEXT, raw_json TEXT);
CREATE TABLE IF NOT EXISTS problems (
  problem_id INTEGER PRIMARY KEY AUTOINCREMENT, obs_id INTEGER, post_id TEXT, problem_idx INTEGER, role TEXT,
  problem_statement TEXT, workflow_object TEXT, pain_type TEXT, evidence_grade TEXT, automation_fit TEXT,
  spending_observed TEXT, hiring_type TEXT, needs_review INTEGER, scores_json TEXT, problem_json TEXT);
CREATE TABLE IF NOT EXISTS patterns (
  pattern_id TEXT PRIMARY KEY, family_id TEXT, definition TEXT, same_prototype_test TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS pattern_assignments (
  problem_id INTEGER, pattern_id TEXT, proposed_name TEXT, method TEXT, similarity REAL, reviewed INTEGER);
CREATE VIEW IF NOT EXISTS problem_metrics AS
SELECT a.pattern_id AS pattern_id,
  COUNT(DISTINCT p.post_id) AS posts,
  COUNT(DISTINCT p.client_key) AS unique_clients,
  COUNT(DISTINCT CASE WHEN pr.spending_observed IN ('hiring_human','paying_for_tool','stated_budget_for_fix')
        THEN p.client_key END) AS clients_with_spending,
  SUM(CASE WHEN pr.evidence_grade IN ('A','B') THEN 1 ELSE 0 END) AS strong_obs,
  MIN(p.date_posted) AS first_seen, MAX(p.date_posted) AS last_seen
FROM pattern_assignments a
JOIN problems pr ON pr.problem_id = a.problem_id
JOIN observations o ON o.obs_id = pr.obs_id
JOIN raw_posts p ON p.post_id = o.post_id
WHERE o.is_relevant = 1 AND p.dup_of IS NULL AND a.pattern_id IS NOT NULL AND a.reviewed IN (0,1)
GROUP BY a.pattern_id;
"""

def open_db(path):
    con = sqlite3.connect(path)
    con.executescript(DB_SCHEMA)
    for pid, fam, definition, test in SEED_PATTERNS:
        con.execute("INSERT OR IGNORE INTO patterns VALUES (?,?,?,?,?)", (pid, fam, definition, test, "active"))
    con.commit()
    return con

# ------------------------------------------------------------------ input
def read_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit("Input CSV is empty.")
    missing = [c for c in ("job_title", "job_description") if c not in rows[0]]
    if missing:
        sys.exit(f"Input CSV is missing required columns: {missing}. Found: {list(rows[0].keys())}")
    out = []
    for i, r in enumerate(rows, 1):
        out.append({
            "post_id": (r.get("post_id") or "").strip() or f"sample-{i:03d}",
            "client_key": (r.get("client_key") or r.get("client_id") or "").strip() or None,
            "title": (r.get("job_title") or "").strip(),
            "body": (r.get("job_description") or "").strip(),
            "date_posted": (r.get("date_posted") or "").strip(),
            "source_url": (r.get("job_url") or "").strip(),
            "source_type": (r.get("source_type") or "upwork").strip(),
            "_orig": r,
        })
    return out

# ------------------------------------------------------------------ model call
def build_messages(post, body, truncated, system_text, vocab_text):
    user = USER_TEMPLATE.format(
        workflow_objects=", ".join(WORKFLOW_OBJECTS),
        pattern_vocabulary=vocab_text,
        capability_catalog=", ".join(CAPABILITY_CATALOG) or "none defined",
        post_id=post["post_id"],
        date_posted=post["date_posted"] or "unknown",
        body_truncated=str(truncated).lower(),
        job_title=post["title"],
        job_description=body,
    )
    return [{"role": "system", "content": system_text}, {"role": "user", "content": user}]

def shape_error(res, schema):
    """Return None if res is acceptable, else a short reason string."""
    try:
        import jsonschema
        try:
            jsonschema.validate(res, schema)
            return None
        except jsonschema.ValidationError as e:
            return f"schema: {e.message[:150]}"
    except ImportError:
        pass
    if not isinstance(res, dict): return "not an object"
    for k in ("post_type", "is_relevant", "relevance_reason", "problems"):
        if k not in res: return f"missing key {k}"
    if not isinstance(res["problems"], list): return "problems not a list"
    return None

def call_model(client, messages, schema):
    """Returns (result_dict|None, attempts, first_try_ok, error|None)."""
    last_err = None
    msgs = list(messages)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = client.chat.completions.create(
                model=MODEL, messages=msgs, temperature=0, max_completion_tokens=6000,
                response_format={"type": "json_object"},
                extra_body={"reasoning_effort": "low"})
            text = resp.choices[0].message.content or ""
            res = json.loads(text)
            bad = shape_error(res, schema)
            if bad is None:
                return res, attempt, attempt == 1, None
            last_err = bad
            msgs = messages + [{"role": "user", "content":
                f"Your previous output was invalid ({bad}). Return ONLY a corrected JSON object matching the schema."}]
        except json.JSONDecodeError as e:
            last_err = f"bad_json: {e}"
            msgs = messages + [{"role": "user", "content":
                "Your previous output was not valid JSON. Return ONLY one valid JSON object."}]
        except Exception as e:            # network / rate limit / API error
            last_err = f"{type(e).__name__}: {str(e)[:200]}"
            wait = 20 if "429" in str(e) or "rate" in str(e).lower() else 2 * attempt
            time.sleep(wait)
    return None, MAX_ATTEMPTS, False, last_err

# ------------------------------------------------------------------ storage
def save_post(con, post, body_hash, dup_of, copied):
    con.execute("INSERT OR REPLACE INTO raw_posts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (post["post_id"], post["source_type"], post["source_url"], post["client_key"], post["title"],
         post["body"], body_hash, post["date_posted"], datetime.date.today().isoformat(), dup_of, int(copied)))

def save_observation(con, post, res, run_id, attempts, first_ok, error, vocab_ids):
    cur = con.execute(
        "INSERT INTO observations (post_id,run_id,model,prompt_version,schema_version,post_type,is_relevant,"
        "relevance_reason,client_ask,industry_stated,post_errors,attempts,json_first_try,error,raw_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (post["post_id"], run_id, MODEL, PROMPT_VERSION, SCHEMA_VERSION,
         (res or {}).get("post_type"),
         None if not res or res.get("is_relevant") is None else int(bool(res["is_relevant"])),
         (res or {}).get("relevance_reason"), (res or {}).get("client_ask"), (res or {}).get("industry_stated"),
         json.dumps((res or {}).get("post_errors", [])), attempts, int(first_ok), error,
         json.dumps(res, ensure_ascii=False) if res else None))
    obs_id = cur.lastrowid
    review_rows = []
    for idx, p in enumerate((res or {}).get("problems") or []):
        pc = con.execute(
            "INSERT INTO problems (obs_id,post_id,problem_idx,role,problem_statement,workflow_object,pain_type,"
            "evidence_grade,automation_fit,spending_observed,hiring_type,needs_review,scores_json,problem_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (obs_id, post["post_id"], idx, p.get("role"), p.get("problem_statement"), p.get("workflow_object"),
             p.get("pain_type"), p.get("evidence_grade"), p.get("automation_fit"), p.get("spending_observed"),
             p.get("hiring_type"), int(bool(p.get("needs_review"))), json.dumps(p.get("scores", {})),
             json.dumps(p, ensure_ascii=False)))
        pid = pc.lastrowid
        pat = p.get("pattern") or {}
        existing = pat.get("existing_id")
        if existing and existing in vocab_ids:
            # LLM chose a vocabulary pattern. No embedder yet, so mark unreviewed (still counted).
            con.execute("INSERT INTO pattern_assignments VALUES (?,?,?,?,?,?)", (pid, existing, None, "llm", None, 0))
        else:
            proposed = V_snake(pat.get("proposed_name"))
            con.execute("INSERT INTO pattern_assignments VALUES (?,?,?,?,?,?)", (pid, None, proposed, "llm_proposed", None, 2))
            review_rows.append({"post_id": post["post_id"], "problem_idx": idx, "proposed_pattern": proposed,
                                "reason": pat.get("reason", ""), "problem_statement": p.get("problem_statement", "")})
    con.commit()
    return review_rows

def V_snake(s):
    import re
    s = re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")
    return s or "unclassified_problem"

# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input_csv", nargs="?")
    ap.add_argument("--out-dir", default="OutPutCsv")
    ap.add_argument("--db", default=None, help="SQLite path (default: <out-dir>/problem_library.db)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-resume", action="store_true", help="re-analyze posts already in the DB")
    args = ap.parse_args()

    in_path = args.input_csv or input("Enter the path to your input CSV: ").strip().strip('"')
    if not os.path.exists(in_path):
        sys.exit(f"File not found: {in_path}")
    os.makedirs(args.out_dir, exist_ok=True)
    db_path = args.db or os.path.join(args.out_dir, "problem_library.db")
    run_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    try:
        from groq import Groq
    except ImportError:
        sys.exit("pip install groq")
    client = Groq(api_key=get_api_key())

    schema_raw, schema = load_schema_text()
    schema_min = json.dumps(json.loads(schema_raw), separators=(",", ":"))
    system_text = SYSTEM_PROMPT + "\n\nOUTPUT JSON SCHEMA (follow exactly):\n" + schema_min
    vocab_text = "\n".join(f"- {pid} [{fam}]: {d} | same_prototype_test: {t}" for pid, fam, d, t in SEED_PATTERNS)
    vocab_ids = {p[0] for p in SEED_PATTERNS}

    posts = read_rows(in_path)
    if args.limit: posts = posts[:args.limit]
    con = open_db(db_path)

    done = set()
    if not args.no_resume:
        done = {r[0] for r in con.execute(
            "SELECT post_id FROM observations WHERE prompt_version=? AND error IS NULL", (PROMPT_VERSION,))}

    print(f"Run {run_id} | model {MODEL} | prompt {PROMPT_VERSION} | {len(posts)} posts | db {db_path}")
    jsonl_path = os.path.join(args.out_dir, f"raw_results_{run_id}.jsonl")
    csv_rows, review_all, seen = [], [], []
    stats = Counter(); consecutive_fails = 0

    with open(jsonl_path, "w", encoding="utf-8") as jf:
        for n, post in enumerate(posts, 1):
            tag = f"[{n}/{len(posts)}] {post['title'][:60]}"
            if post["post_id"] in done:
                print(f"{tag}\n  skipped (already analyzed)"); stats["skipped"] += 1; continue

            body, truncated = truncate_body(post["body"])
            h = body_sha1(post["body"])

            # ---- dedupe against earlier posts in this run / DB
            dup_of, copied = None, False
            for pid, ck, b in seen:
                if h == body_sha1(b) or is_near_dup(post["body"], b):
                    if post["client_key"] and ck == post["client_key"]:
                        dup_of = pid
                    else:
                        copied = True
                    break
            seen.append((post["post_id"], post["client_key"], post["body"]))
            save_post(con, post, h, dup_of, copied)
            if dup_of:
                print(f"{tag}\n  duplicate of {dup_of} (same client): not sent to model")
                stats["dup"] += 1; continue

            # ---- model call
            msgs = build_messages(post, body, truncated, system_text, vocab_text)
            print(tag)
            res, attempts, first_ok, err = call_model(client, msgs, schema)
            stats["calls"] += 1; stats["first_try_ok"] += int(first_ok)

            if res is None:
                consecutive_fails += 1; stats["failed"] += 1
                print(f"  FAILED after {attempts} attempts: {err}")
                save_observation(con, post, None, run_id, attempts, False, err, vocab_ids)
                if consecutive_fails >= MAX_CONSECUTIVE_FAILS:
                    print(f"\nStopping: {MAX_CONSECUTIVE_FAILS} failures in a row. Fix the cause, then re-run (resume skips finished posts).")
                    break
                continue
            consecutive_fails = 0

            # ---- validate (code owns grades, caps, spans)
            try:
                res = validate_post(res, post["title"] + "\n" + post["body"], vocab_ids,
                                    set(CAPABILITY_CATALOG), CAPABILITY_CATALOG or None)
            except Exception:
                traceback.print_exc()
                save_observation(con, post, res, run_id, attempts, first_ok, "validate_post crashed", vocab_ids)
                stats["failed"] += 1; continue

            review_all += save_observation(con, post, res, run_id, attempts, first_ok, None, vocab_ids)
            jf.write(json.dumps({"post_id": post["post_id"], "result": res}, ensure_ascii=False) + "\n")

            stats[f"type:{res.get('post_type')}"] += 1
            probs = res.get("problems") or []
            if res.get("is_relevant"): stats["relevant"] += 1
            print(f"  {res.get('post_type')} | relevant={res.get('is_relevant')} | problems={len(probs)}"
                  + (f" | {probs[0].get('pattern',{}).get('existing_id') or probs[0].get('pattern',{}).get('proposed_name')}" if probs else ""))

            base = {"post_id": post["post_id"], "client_key": post["client_key"], "title": post["title"],
                    "date_posted": post["date_posted"], "post_type": res.get("post_type"),
                    "is_relevant": res.get("is_relevant"), "copied_template": int(copied),
                    "relevance_reason": res.get("relevance_reason"), "client_ask": res.get("client_ask")}
            if not probs:
                csv_rows.append(base)
            for idx, p in enumerate(probs):
                sc = p.get("scores", {}); pt = p.get("pattern") or {}
                def val(k): return (p.get(k) or {}).get("value") if isinstance(p.get(k), dict) else None
                csv_rows.append({**base, "problem_idx": idx, "role": p.get("role"),
                    "problem_statement": p.get("problem_statement"), "workflow_object": p.get("workflow_object"),
                    "pain_type": p.get("pain_type"), "pattern_id": pt.get("existing_id"),
                    "proposed_pattern": pt.get("proposed_name"), "evidence_grade": p.get("evidence_grade"),
                    "freq": val("freq"), "time": val("time"), "impact": val("impact"),
                    "hiring_type": p.get("hiring_type"), "spending_observed": p.get("spending_observed"),
                    "automation_fit": p.get("automation_fit"), "automation_inappropriate": p.get("automation_inappropriate"),
                    "recurrence_strength": sc.get("recurrence_strength"), "time_cost_signal": sc.get("time_cost_signal"),
                    "business_impact_signal": sc.get("business_impact_signal"),
                    "problem_confidence": sc.get("problem_confidence"), "needs_review": p.get("needs_review"),
                    "validation_errors": "; ".join(p.get("validation_errors", []))})

    # ---- write CSV outputs (timestamped: never overwrites earlier runs)
    if csv_rows:
        cols = list(dict.fromkeys(k for r in csv_rows for k in r))
        out_csv = os.path.join(args.out_dir, f"problems_{run_id}.csv")
        with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(csv_rows)
        print(f"\nProblems CSV:     {out_csv}")
    if review_all:
        rv = os.path.join(args.out_dir, f"pattern_review_{run_id}.csv")
        with open(rv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(review_all[0].keys())); w.writeheader(); w.writerows(review_all)
        print(f"Pattern review:   {rv}  (human decides merge / new)")
    print(f"Raw JSONL:        {jsonl_path}\nDatabase:         {db_path}")

    # ---- summary
    print("\n" + "=" * 60 + "\nSUMMARY (v3)\n" + "=" * 60)
    calls = stats["calls"]
    print(f"Posts in file: {len(posts)} | model calls: {calls} | skipped: {stats['skipped']} | "
          f"dup (same client): {stats['dup']} | failed: {stats['failed']}")
    if calls:
        print(f"JSON valid on first try: {stats['first_try_ok']}/{calls} ({100*stats['first_try_ok']//calls}%)")
    print(f"Relevant posts: {stats['relevant']}")
    print("Post types:", {k[5:]: v for k, v in stats.items() if k.startswith("type:")})
    grades = Counter(r[0] for r in con.execute(
        "SELECT pr.evidence_grade FROM problems pr JOIN observations o ON o.obs_id=pr.obs_id WHERE o.run_id=?", (run_id,)))
    print("Evidence grades:", dict(grades))
    print("\nPattern metrics (CLIENTS, not posts; unknown clients are not counted):")
    for r in con.execute("SELECT pattern_id, posts, unique_clients, clients_with_spending, strong_obs "
                         "FROM problem_metrics ORDER BY unique_clients DESC, posts DESC"):
        flag = "" if r[2] >= 5 else "  <- insufficient_repetition"
        print(f"  {r[0]:36s} posts={r[1]:<3d} clients={r[2]:<3d} spending={r[3]:<3d} strong={r[4]}{flag}")
    con.close()

if __name__ == "__main__":
    main()