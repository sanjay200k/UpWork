"""
extract_v4.py - job posts -> ranked, evidence-backed leads.

Pipeline per post
  1. DISCOVERY     (LLM, recall-biased)  Is this paid work worth a look? Which service? What hiring signal?
  2. QUALIFICATION (LLM, candidates only) Evidence-backed facts: intent, role, budget, problems, pattern, advice.
  3. VALIDATION    (code)  Every claim must be quoted from the post, else it becomes 'unknown' / is flagged for review.
  4. SCORING       (code)  Lead quality (6 components) and automation fit, kept separate.
  Every post stays its own lead. Duplicates are grouped (dup_group) but never merged or dropped.

Usage:   python extract_v4.py input.csv --out-dir OutPutCsv [--limit 5] [--as-of 2026-10-10]
Key:     set GROQ_API_KEY (or a local config.py). Model: set GROQ_MODEL / GROQ_DISCOVERY_MODEL.
"""
import argparse, csv, datetime, json, os, sys, time, traceback
from collections import Counter

import common, my_profile
from taxonomy import CATEGORIES, SEED_PATTERNS, seed_vocab
from prompts_v4 import PROMPT_VERSION, DISCOVERY_SYSTEM, QUALIFY_SYSTEM, discovery_user, qualify_user
import validate_v4 as V
import scoring_v4 as S

MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
DISCOVERY_MODEL = os.environ.get("GROQ_DISCOVERY_MODEL", MODEL)
MAX_BODY = 24000
MAX_CONSECUTIVE_FAILS = 3

class Fatal(Exception): pass

# ---------------------------------------------------------------- LLM
def get_api_key():
    k = os.environ.get("GROQ_API_KEY")
    if not k:
        try:
            import config; k = config.GROQ_API_KEY
        except Exception: k = None
    if not k or "your-key" in k.lower():
        sys.exit("No Groq key. Set the GROQ_API_KEY environment variable (never put a real key in files you share).")
    return k

def llm_json(client, model, system, user, check, attempts=3, max_out=6000):
    base = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    cur, last = list(base), None
    for n in range(1, attempts + 1):
        fix = None
        try:
            kw = dict(model=model, messages=cur, temperature=0, response_format={"type": "json_object"})
            if "gpt-oss" in model: kw.update(max_completion_tokens=max_out, extra_body={"reasoning_effort": "low"})
            else: kw["max_tokens"] = min(max_out, 4000)
            text = client.chat.completions.create(**kw).choices[0].message.content or ""
            obj = json.loads(text)
            bad = check(obj)
            if not bad: return obj, n, None
            last, fix = bad, f"Your previous output was invalid ({bad}). Return ONLY a corrected JSON object."
        except json.JSONDecodeError as e:
            last, fix = f"bad_json: {e}", "That was not valid JSON. Return ONLY one valid JSON object."
        except Exception as e:
            s = str(e); last = f"{type(e).__name__}: {s[:200]}"
            if "model_not_found" in s or "404" in s or "401" in s or "invalid_api_key" in s:
                raise Fatal(last)
            time.sleep(20 if "429" in s or "rate" in s.lower() else 2 * n)
        cur = base + ([{"role": "user", "content": fix}] if fix else [])
    return None, attempts, last

def check_discovery(o):
    if not isinstance(o, dict): return "not an object"
    for k in ("is_candidate", "service_category", "hiring_signal"):
        if k not in o: return f"missing {k}"
    if not isinstance(o["is_candidate"], bool): return "is_candidate must be true/false"

def check_qual(o):
    if not isinstance(o, dict): return "not an object"
    for k in ("service_category", "buyer_intent", "problems", "advice"):
        if k not in o: return f"missing {k}"
    if not isinstance(o["problems"], list): return "problems must be a list"

# ---------------------------------------------------------------- storage
def save_lead(con, lead, run_id, sha, dup_group, dup_kind, reuse_of):
    con.execute("INSERT OR REPLACE INTO leads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (lead["lead_id"], lead["url"], lead["client_key"], lead["title"], lead["body"], sha, lead["date_posted"],
         datetime.date.today().isoformat(), json.dumps({**lead["meta"], "orig": lead["orig"]}, ensure_ascii=False),
         dup_group, dup_kind, reuse_of, run_id))

def save_discovery(con, lead_id, run_id, model, d, span_ok, attempts, err):
    d = d or {}
    con.execute("INSERT INTO discovery VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (lead_id, run_id, model, None if "is_candidate" not in d else int(d["is_candidate"]), d.get("service_category"),
         d.get("hiring_signal"), d.get("reason"), d.get("span"), None if span_ok is None else int(span_ok), attempts, err))

def save_qual(con, lead_id, run_id, v, raw, attempts, first_ok, err):
    cur = con.execute(
        "INSERT INTO qualification (lead_id,run_id,model,prompt_version,service_category,buyer_intent,buyer_role,evidence_grade,"
        "needs_review,review_reasons,downgrades,attempts,json_first_try,error,raw_json,validated_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (lead_id, run_id, MODEL, PROMPT_VERSION, (v or {}).get("service_category"),
         ((v or {}).get("buyer_intent") or {}).get("level"), ((v or {}).get("buyer_role") or {}).get("role"),
         (v or {}).get("evidence_grade"), int(bool(v and v["needs_review"]) or bool(err)),
         json.dumps((v or {}).get("review_reasons", [])), json.dumps(((v or {}).get("validation") or {}).get("downgrades", [])),
         attempts, int(first_ok), err, json.dumps(raw, ensure_ascii=False) if raw else None,
         json.dumps(v, ensure_ascii=False) if v else None))
    qid = cur.lastrowid
    for i, p in enumerate((v or {}).get("problems", [])):
        pt = p["pattern"]; prop = pt.get("proposed")
        con.execute("INSERT INTO problems (qual_id,lead_id,idx,role,problem_statement,support_span,workflow_object,pain_type,"
                    "pattern_id,proposed_pattern,problem_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (qid, lead_id, i, p["role"], p["problem_statement"], p["support_span"], p["workflow_object"], p["pain_type"],
             pt.get("existing_id"), prop["name"] if prop else None, json.dumps(p, ensure_ascii=False)))
        if prop:
            con.execute("INSERT OR IGNORE INTO patterns VALUES (?,?,?,?,?,?,?,?)",
                (prop["name"], prop.get("category"), prop.get("definition"), prop.get("same_prototype_test"), "proposed",
                 lead_id, prop.get("likely_synonym_of"), datetime.date.today().isoformat()))
    return qid

def save_scores(con, lead_id, run_id, qid, q, auto, act, v, cat):
    con.execute("INSERT OR REPLACE INTO scores VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (lead_id, run_id, qid, q["lead_score"], q["tier"], q["coverage"], ((v or {}).get("buyer_intent") or {}).get("level"),
         cat, q.get("fit_level"), auto["automation_fit"], auto["automation_score"], int(bool(v and v.get("needs_review"))),
         act["next_action"], json.dumps(q["components"]), json.dumps(q["caps_applied"]), json.dumps(act, ensure_ascii=False)))

def queue(con, lead_id, run_id, kind, detail):
    con.execute("INSERT INTO review_queue (lead_id,run_id,kind,detail) VALUES (?,?,?,?)", (lead_id, run_id, kind, detail))

# ---------------------------------------------------------------- main
def run(args, client):
    as_of = datetime.date.fromisoformat(args.as_of) if args.as_of else datetime.date.today()
    run_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(args.out_dir, exist_ok=True)
    con = common.connect(args.db or os.path.join(args.out_dir, "lead_library_v4.db"))
    for pid, cat, d, t in SEED_PATTERNS:
        con.execute("INSERT OR IGNORE INTO patterns VALUES (?,?,?,?,?,?,?,?)", (pid, cat, d, t, "seed", None, None, None))
    con.commit()
    vocab = seed_vocab()
    leads = common.read_rows(args.input_csv)
    if args.limit: leads = leads[:args.limit]
    print(f"Run {run_id} | models: discovery={DISCOVERY_MODEL} qualify={MODEL} | prompt {PROMPT_VERSION} | {len(leads)} posts | as-of {as_of}")

    stats, rows, first_by_sha, seen = Counter(), [], {}, []
    cache = {}                                  # sha1 -> (discovery, validated, qid) for exact duplicates in this run
    consecutive = 0
    for n, lead in enumerate(leads, 1):
        lid, title, body = lead["lead_id"], lead["title"], lead["body"][:MAX_BODY]
        print(f"[{n}/{len(leads)}] {title[:70]}")
        sha = common.sha1_text(lead["body"])

        # ---- duplicate grouping: every post stays a lead; groups only label similarity
        dup_group, dup_kind, reuse_of = lid, None, None
        for oid, ock, obody, ogroup in seen:
            if sha == common.sha1_text(obody) or V.jaccard(lead["body"], obody) >= 0.85:
                dup_group = ogroup
                if lead["client_key"] and ock == lead["client_key"]: dup_kind = "repost_same_client"
                elif lead["client_key"] and ock: dup_kind = "copied_template"
                else: dup_kind = "possible_repost_unknown_client"
                if sha == common.sha1_text(obody): reuse_of = oid
                break
        seen.append((lid, lead["client_key"], lead["body"], dup_group))
        save_lead(con, lead, run_id, sha, dup_group, dup_kind, reuse_of)
        if dup_kind: stats[dup_kind] += 1

        disc = v = None; qid = None; err = None
        try:
            if reuse_of and reuse_of in cache:                       # identical text: reuse extraction, rescore with THIS post's metadata
                disc, v, _ = cache[reuse_of]
                save_discovery(con, lid, run_id, "reused", disc, None, 0, None)
                qid = save_qual(con, lid, run_id, v, None, 0, True, None) if v else None   # own rows, so pattern counts see this lead
                stats["reused"] += 1
            else:
                prev = None if args.no_resume else con.execute(
                    "SELECT * FROM current_quals WHERE lead_id=? AND prompt_version=? AND error IS NULL", (lid, PROMPT_VERSION)).fetchone()
                if prev and prev["validated_json"]:
                    v, qid = json.loads(prev["validated_json"]), prev["qual_id"]
                    d0 = con.execute("SELECT * FROM discovery WHERE lead_id=? ORDER BY run_id DESC LIMIT 1", (lid,)).fetchone()
                    disc = dict(d0) if d0 else {"is_candidate": True, "service_category": v["service_category"]}
                    disc["is_candidate"] = bool(disc.get("is_candidate", True))
                    stats["resumed"] += 1
                else:
                    disc, a1, e1 = llm_json(client, DISCOVERY_MODEL, DISCOVERY_SYSTEM, discovery_user(title, body), check_discovery, max_out=1500)
                    stats["discovery_calls"] += 1
                    if disc is None:
                        err = f"discovery failed: {e1}"
                    else:
                        sp = V.Source(f"{title}\n{body}").verify(disc.get("span") or "", 2)["ok"] if disc.get("span") else None
                        save_discovery(con, lid, run_id, DISCOVERY_MODEL, disc, sp, a1, None)
                        if disc["is_candidate"]:
                            raw, a2, e2 = llm_json(client, MODEL, QUALIFY_SYSTEM, qualify_user(title, body, len(lead["body"]) > MAX_BODY), check_qual)
                            stats["qualify_calls"] += 1; stats["first_try_ok"] += int(a2 == 1 and raw is not None)
                            if raw is None: err = f"qualification failed: {e2}"; qid = save_qual(con, lid, run_id, None, None, a2, False, err)
                            else:
                                v = V.validate_lead(raw, title, body, vocab)
                                qid = save_qual(con, lid, run_id, v, raw, a2, a2 == 1, None)
                    if disc is None and err: save_discovery(con, lid, run_id, DISCOVERY_MODEL, None, None, 3, err)
        except Fatal as e:
            print(f"\nFATAL: {e}\nFix this (model name / API key), then re-run. Finished posts are skipped on re-run.")
            break
        except Exception as e:
            err = f"{type(e).__name__}: {e}"; traceback.print_exc()

        # ---- decide row outcome
        if err:
            consecutive += 1; stats["failed"] += 1
            print(f"  FAILED: {err}")
            queue(con, lid, run_id, "error", err)
            rows.append({"lead_id": lid, "title": title, "tier": "E", "lead_score": 0, "next_action": "Extraction failed: re-run.",
                         "url": lead["url"], "needs_review": True, "review_reasons": err})
            con.commit()
            if consecutive >= MAX_CONSECUTIVE_FAILS: print(f"\nStopping: {MAX_CONSECUTIVE_FAILS} failures in a row."); break
            continue
        consecutive = 0
        if reuse_of is None and v is not None: cache[lid] = (disc, v, qid)

        if not disc.get("is_candidate") or v is None:
            stats["not_candidate"] += 1
            q = {"lead_score": 0, "tier": "N", "coverage": 0, "components": {}, "caps_applied": ["not_a_candidate"], "fit_level": None}
            auto = {"automation_fit": "not_assessed", "automation_score": None}
            act = {"next_action": f"Skip: {disc.get('reason') or 'not a paid-work request'}", "evidence": [], "risks": [], "score_drivers": []}
            save_scores(con, lid, run_id, qid, q, auto, act, None, disc.get("service_category"))
            print("  not a candidate")
            rows.append({"lead_id": lid, "title": title, "url": lead["url"], "tier": "N", "lead_score": 0,
                         "service_category": disc.get("service_category"), "next_action": act["next_action"]})
            con.commit(); continue

        q = S.lead_quality(v, lead["meta"], lead["date_posted"], as_of)
        auto = S.automation_assessment(v)
        act = S.build_action(v, q, auto)
        save_scores(con, lid, run_id, qid, q, auto, act, v, v["service_category"])
        if v["needs_review"]: queue(con, lid, run_id, "validation", ", ".join(v["review_reasons"]))
        for k in v["validation"]["errors"]: stats["err:" + k] += 1
        for k in v["validation"]["downgrades"]: stats["down:" + k.split("->")[0]] += 1
        stats["span_repairs"] += v["validation"]["span_repairs"]
        stats["tier:" + q["tier"]] += 1
        pr = (v["problems"] or [{}])[0].get("pattern", {})
        print(f"  {v['service_category']} | intent={v['buyer_intent']['level']} | tier {q['tier']} ({q['lead_score']}) | "
              f"automation={auto['automation_fit']}" + (" | REVIEW" if v["needs_review"] else "")
              + (f" | pattern={pr.get('existing_id') or (pr.get('proposed') or {}).get('name')}" if pr else ""))
        sc = q["components"]
        rows.append({"lead_id": lid, "title": title, "url": lead["url"], "client_key": lead["client_key"],
            "tier": q["tier"], "lead_score": q["lead_score"], "score_coverage": q["coverage"], "caps_applied": "; ".join(q["caps_applied"]),
            "service_category": v["service_category"], "service_needed": act["service_needed"], "buyer_intent": v["buyer_intent"]["level"],
            "intent_score": sc["buyer_intent"], "fit_score": sc["service_fit"], "clarity_score": sc["problem_clarity"],
            "budget_score": sc["budget_evidence"], "client_score": sc["client_signals"], "recency_competition_score": sc["recency_competition"],
            "automation_fit": auto["automation_fit"], "automation_score": auto["automation_score"],
            "evidence": " | ".join(act["evidence"]), "reason_to_apply": act["reason_to_apply"], "potential_solution": act["potential_solution"],
            "risks": "; ".join(act["risks"]), "score_drivers": "; ".join(act["score_drivers"]), "next_action": act["next_action"],
            "pattern_id": pr.get("existing_id"), "proposed_pattern": (pr.get("proposed") or {}).get("name"),
            "dup_group": dup_group, "dup_kind": dup_kind, "evidence_grade": v["evidence_grade"],
            "needs_review": v["needs_review"], "review_reasons": "; ".join(v["review_reasons"]),
            "validation_downgrades": "; ".join(v["validation"]["downgrades"])})
        con.commit()

    # ---------------- outputs (timestamped; nothing is overwritten)
    rows.sort(key=lambda r: (S.TIER_ORDER.get(r["tier"], 9), -float(r.get("lead_score") or 0)))
    if rows:
        cols = list(dict.fromkeys(k for r in rows for k in r))
        p = os.path.join(args.out_dir, f"leads_{run_id}.csv")
        with open(p, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)
        print(f"\nRanked leads:      {p}")
    rq = con.execute("SELECT r.lead_id, l.title, r.kind, r.detail FROM review_queue r JOIN leads l ON l.lead_id=r.lead_id WHERE r.run_id=?", (run_id,)).fetchall()
    if rq:
        p = os.path.join(args.out_dir, f"review_queue_{run_id}.csv")
        with open(p, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f); w.writerow(["lead_id", "title", "kind", "detail"]); w.writerows([tuple(x) for x in rq])
        print(f"Review queue:      {p}  ({len(rq)} records flagged)")
    pp = con.execute("SELECT pattern_id, category, definition, same_prototype_test, likely_synonym_of, first_lead FROM patterns WHERE status='proposed' AND created_at=?",
                     (datetime.date.today().isoformat(),)).fetchall()
    if pp:
        p = os.path.join(args.out_dir, f"pattern_proposals_{run_id}.csv")
        with open(p, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f); w.writerow(["pattern_id", "category", "definition", "same_prototype_test", "likely_synonym_of", "first_lead"])
            w.writerows([tuple(x) for x in pp])
        print(f"Pattern proposals: {p}")
    print(f"Database:          {args.db or os.path.join(args.out_dir, 'lead_library_v4.db')}")

    print("\n" + "=" * 64 + "\nSUMMARY (v4)\n" + "=" * 64)
    print(f"posts: {len(leads)} | discovery calls: {stats['discovery_calls']} | qualify calls: {stats['qualify_calls']} | "
          f"resumed: {stats['resumed']} | reused (identical text): {stats['reused']} | failed: {stats['failed']}")
    print(f"not candidates: {stats['not_candidate']} | qualify JSON valid first try: {stats['first_try_ok']}/{stats['qualify_calls']}")
    print("tiers:", {k[5:]: v for k, v in sorted(stats.items()) if k.startswith("tier:")})
    print(f"duplicates grouped (all kept as separate leads): same-client {stats['repost_same_client']}, "
          f"copied template {stats['copied_template']}, unknown client {stats['possible_repost_unknown_client']}")
    print(f"span repairs: {stats['span_repairs']} | hard errors: {sum(v for k, v in stats.items() if k.startswith('err:'))}")
    for k, v in stats.most_common():
        if k.startswith(("err:", "down:")): print(f"   {k}: {v}")
    print("\nTop leads:")
    for r in [r for r in rows if r["tier"] in "AB"][:10]:
        print(f"  [{r['tier']} {r['lead_score']:>5}] {r['title'][:55]:55s} | {r.get('service_category')} | {r['next_action'][:60]}")
    con.close()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input_csv", nargs="?")
    ap.add_argument("--out-dir", default="OutPutCsv")
    ap.add_argument("--db", default=None)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--as-of", help="YYYY-MM-DD used for recency (default: today). Use the collection date for old samples.")
    ap.add_argument("--no-resume", action="store_true", help="call the model again even if a result exists")
    args = ap.parse_args()
    args.input_csv = args.input_csv or input("Enter the path to your input CSV: ").strip().strip('"')
    if not os.path.exists(args.input_csv): sys.exit(f"File not found: {args.input_csv}")
    try: from groq import Groq
    except ImportError: sys.exit("pip install groq")
    run(args, Groq(api_key=get_api_key()))

if __name__ == "__main__":
    main()
