"""
outcomes.py - record what actually happened after you applied, then check whether the scores predicted it.

  python outcomes.py add    --lead-id ID --connects 12 [--bid 400] [--applied-at 2026-10-12] [--notes "..."]
  python outcomes.py update --lead-id ID [--replied yes] [--interview yes] [--hired yes] [--revenue 450] [--replied-at DATE]
  python outcomes.py report    [--min-age-days 7]
  python outcomes.py calibrate [--min-age-days 7]

`add` stores a SNAPSHOT of the lead's score and components at application time, so re-scoring later never rewrites history.
Only applications at least --min-age-days old count in reports (a reply may still be coming).
"""
import argparse, datetime, json, sys
import common
import my_profile

def yn(s): return None if s is None else int(str(s).lower() in ("y", "yes", "1", "true"))

def cmd_add(a, con):
    row = con.execute("SELECT lead_score, tier, components_json FROM current_leads WHERE lead_id=?", (a.lead_id,)).fetchone()
    if not row: sys.exit(f"Lead {a.lead_id} not found in {a.db}. Run extract_v4.py first.")
    con.execute("INSERT OR REPLACE INTO outcomes (lead_id,applied_at,connects_spent,bid_amount,lead_score,tier,components_json,notes,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (a.lead_id, a.applied_at or datetime.date.today().isoformat(), a.connects, a.bid, row["lead_score"], row["tier"],
                 row["components_json"], a.notes, datetime.datetime.now().isoformat()))
    con.commit(); print(f"Recorded application to {a.lead_id} (tier {row['tier']}, score {row['lead_score']}).")

def cmd_update(a, con):
    sets, vals = [], []
    for col, v in (("replied", yn(a.replied)), ("interview", yn(a.interview)), ("hired", yn(a.hired))):
        if v is not None: sets.append(f"{col}=?"); vals.append(v)
    if a.revenue is not None: sets.append("revenue=?"); vals.append(a.revenue)
    if a.replied_at: sets.append("replied_at=?"); vals.append(a.replied_at)
    if a.notes: sets.append("notes=COALESCE(notes,'')||' '||?"); vals.append(a.notes)
    if not sets: sys.exit("Nothing to update.")
    if a.hired == "yes" or a.interview == "yes": sets.append("replied=1")          # an interview or hire implies a reply
    sets.append("updated_at=?"); vals.append(datetime.datetime.now().isoformat())
    cur = con.execute(f"UPDATE outcomes SET {', '.join(sets)} WHERE lead_id=?", vals + [a.lead_id])
    if cur.rowcount == 0: sys.exit(f"No application recorded for {a.lead_id}. Use `add` first.")
    con.commit(); print("Updated.")

def mature(con, days):
    cut = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    return [dict(r) for r in con.execute("SELECT * FROM outcomes WHERE applied_at <= ?", (cut,))]

def rate(k, n): return f"{k}/{n} ({k / n:.0%})" if n else "-"

def cmd_report(a, con):
    rows = mature(con, a.min_age_days)
    allrows = con.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
    print(f"Applications: {allrows} recorded, {len(rows)} at least {a.min_age_days} days old (used below)")
    if not rows: return
    def block(name, groups):
        print(f"\n{name}")
        print(f"  {'group':10s} {'applied':>7s} {'connects':>8s} {'replies':>12s} {'interviews':>12s} {'hires':>10s} {'revenue':>9s}  rev/connect")
        for g, rs in groups:
            if not rs: continue
            n = len(rs); cn = sum(r["connects_spent"] or 0 for r in rs); rv = sum(r["revenue"] or 0 for r in rs)
            print(f"  {g:10s} {n:7d} {cn:8d} {rate(sum(r['replied'] for r in rs), n):>12s} {rate(sum(r['interview'] for r in rs), n):>12s} "
                  f"{rate(sum(r['hired'] for r in rs), n):>10s} {rv:9.0f}  {(rv / cn if cn else 0):.2f}" + ("   <- n<20, noisy" if n < 20 else ""))
    block("BY TIER AT APPLICATION TIME", [(t, [r for r in rows if r["tier"] == t]) for t in "ABCD"])
    bands = [("75-100", 75, 101), ("55-74", 55, 75), ("35-54", 35, 55), ("0-34", 0, 35)]
    block("BY SCORE BAND", [(n, [r for r in rows if lo <= (r["lead_score"] or 0) < hi]) for n, lo, hi in bands])
    print("\nIf tier A does not reply/hire more often than tier B/C once each has ~20+ applications, the weights are wrong.")

def cmd_calibrate(a, con):
    rows = mature(con, a.min_age_days)
    pos = sum(r["replied"] for r in rows); neg = len(rows) - pos
    print(f"Mature applications: {len(rows)} (replied {pos}, no reply {neg})")
    if len(rows) < 50 or pos < 10 or neg < 10:
        print("Not enough data to fit weights (need >= 50 applications with >= 10 replies and >= 10 non-replies).\n"
              "Keep applying, keep recording outcomes, and use `report` meanwhile."); return
    try:
        from sklearn.linear_model import LogisticRegression
    except ImportError: sys.exit("pip install scikit-learn")
    keys = list(my_profile.WEIGHTS)
    X, y = [], []
    for r in rows:
        c = json.loads(r["components_json"] or "{}")
        X.append([(c.get(k) if c.get(k) is not None else 50) / 100 for k in keys]); y.append(r["replied"])
    m = LogisticRegression(C=1.0, max_iter=1000).fit(X, y)
    coef = [max(0.0, float(x)) for x in m.coef_[0]]; tot = sum(coef) or 1
    print("\ncomponent             current weight   suggested weight   (logistic coefficient)")
    for k, c, raw in zip(keys, coef, m.coef_[0]):
        print(f"  {k:20s} {my_profile.WEIGHTS[k]:10.2f} {c / tot:16.2f} {raw:18.2f}")
    print("\nSuggestions only; nothing is applied. Cautions:\n"
          " - You mostly apply to high-scoring jobs, so the data barely covers low scores (range restriction).\n"
          "   Apply to a few B/C jobs on purpose to learn whether the low scores really are worse.\n"
          " - Replies measure whether the client noticed you; hires and revenue are the real goal. Re-run this on hires once you have enough.\n"
          " - Change my_profile.WEIGHTS only if the shift is large and holds on a second batch of applications.")

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--db", default="OutPutCsv/lead_library_v4.db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("add"); s.add_argument("--lead-id", required=True); s.add_argument("--connects", type=int, default=0)
    s.add_argument("--bid", type=float); s.add_argument("--applied-at"); s.add_argument("--notes")
    s = sub.add_parser("update"); s.add_argument("--lead-id", required=True)
    for k in ("replied", "interview", "hired"): s.add_argument(f"--{k}", choices=["yes", "no"])
    s.add_argument("--revenue", type=float); s.add_argument("--replied-at"); s.add_argument("--notes")
    for n in ("report", "calibrate"):
        s = sub.add_parser(n); s.add_argument("--min-age-days", type=int, default=7)
    a = ap.parse_args(); con = common.connect(a.db)
    {"add": cmd_add, "update": cmd_update, "report": cmd_report, "calibrate": cmd_calibrate}[a.cmd](a, con)

if __name__ == "__main__":
    main()
