"""
eval_leads.py - measure how many good leads the system misses and how many bad leads it ranks highly.

Step 1 (BLIND, before looking at any model output):
    python eval_leads.py sample real_posts.csv --n 100 --out labels_todo.csv
  Open labels_todo.csv and fill `human_label` with exactly one of: relevant | irrelevant | uncertain
    relevant   = a job you would genuinely apply to
    irrelevant = a job you would never apply to
    uncertain  = you could not decide from the text
  Sample real posts across industries and dates, and include plenty of obviously bad ones.

Step 2 (after running extract_v4.py on the SAME file):
    python eval_leads.py score labels_done.csv --db OutPutCsv/lead_library_v4.db [--min-tier B] [--k 10,20]
"""
import argparse, csv, json, math, os, random, sqlite3, sys, datetime
import common

LABELS = {"relevant", "irrelevant", "uncertain"}
TIERS = ["A", "B", "C", "D"]

def wilson(k, n, z=1.96):
    if n == 0: return (None, None)
    p = k / n; d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d; h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(max(0, c - h), 3), round(min(1, c + h), 3))

def fmt(k, n):
    if n == 0: return "n/a (0 cases)"
    lo, hi = wilson(k, n)
    return f"{k}/{n} = {k / n:.0%}  (95% CI {lo:.0%}-{hi:.0%})"

def cmd_sample(a):
    rows = common.read_rows(a.input_csv)
    random.Random(a.seed).shuffle(rows)
    pick = rows[:a.n]
    with open(a.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["lead_id", "title", "excerpt", "url", "human_label", "human_service_category", "notes"])
        for r in pick: w.writerow([r["lead_id"], r["title"], r["body"][:600].replace("\n", " "), r["url"], "", "", ""])
    print(f"Wrote {len(pick)} posts to {a.out}. Fill human_label (relevant|irrelevant|uncertain) BEFORE viewing model output.")
    if len(pick) < 100: print("Warning: under 100 labels gives very wide confidence intervals.")

def cmd_score(a):
    con = sqlite3.connect(a.db); con.row_factory = sqlite3.Row
    cur = {r["lead_id"]: dict(r) for r in con.execute("SELECT * FROM current_leads")}
    lab, bad_rows = [], 0
    with open(a.labels, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            l = (r.get("human_label") or "").strip().lower()
            if l not in LABELS: bad_rows += 1; continue
            if r["lead_id"] not in cur: print(f"  not scored by model: {r['lead_id']}"); continue
            lab.append({**r, **cur[r["lead_id"]], "label": l})
    if not lab: sys.exit("No labelled rows matched model output.")
    cut = TIERS.index(a.min_tier)
    def pos(r): return r["tier"] in TIERS and TIERS.index(r["tier"]) <= cut
    rel = [r for r in lab if r["label"] == "relevant"]; irr = [r for r in lab if r["label"] == "irrelevant"]
    unc = [r for r in lab if r["label"] == "uncertain"]
    print(f"\nLabelled: {len(lab)} (relevant {len(rel)}, irrelevant {len(irr)}, uncertain {len(unc)}; {bad_rows} rows skipped: label missing/invalid)")
    if len(rel) < 30: print("WARNING: fewer than 30 relevant labels. Treat every number below as a rough indication only.")
    print(f"Positive prediction = tier {a.min_tier} or better.\n")

    missed = [r for r in rel if not pos(r)]
    lost_disc = [r for r in rel if r["is_candidate"] == 0]
    false_hi = [r for r in irr if pos(r)]
    pred_pos = [r for r in lab if pos(r) and r["label"] != "uncertain"]
    print("MISSED GOOD LEADS (recall)")
    print(f"  good leads found at/above tier {a.min_tier}: {fmt(len(rel) - len(missed), len(rel))}")
    print(f"  good leads lost already at discovery:        {fmt(len(lost_disc), len(rel))}   <- stage 1 should be ~0")
    print("BAD LEADS RANKED HIGH (precision)")
    print(f"  irrelevant among predicted-positive:        {fmt(len(false_hi), len(pred_pos))}")
    for k in [int(x) for x in a.k.split(",")]:
        top = sorted([r for r in lab if r["label"] != "uncertain" and r["tier"] in TIERS], key=lambda r: -r["lead_score"])[:k]
        g = sum(1 for r in top if r["label"] == "relevant")
        print(f"  precision@{k}: {fmt(g, len(top))}")
    print("BY TIER (labelled, uncertain excluded)")
    for t in TIERS + ["N"]:
        sub = [r for r in lab if r["tier"] == t and r["label"] != "uncertain"]
        if sub: print(f"  tier {t}: {len(sub):3d} jobs, {sum(1 for r in sub if r['label']=='relevant'):3d} relevant")
    hi_unc = [r for r in unc if pos(r)]
    if unc: print(f"UNCERTAIN: {len(unc)} total, {len(hi_unc)} ranked at/above tier {a.min_tier} (these need a human decision)")

    out = a.out_dir; os.makedirs(out, exist_ok=True)
    p = os.path.join(out, f"eval_errors_{datetime.datetime.now():%Y%m%d_%H%M%S}.csv")
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f); w.writerow(["error_type", "lead_id", "title", "tier", "lead_score", "is_candidate", "buyer_intent", "service_category", "notes"])
        for kind, rows in (("missed_good_lead", missed), ("bad_lead_ranked_high", false_hi)):
            for r in rows: w.writerow([kind, r["lead_id"], r["title"], r["tier"], r["lead_score"], r["is_candidate"], r["buyer_intent"], r["service_category"], r.get("notes", "")])
    print(f"\nError list for review: {p}")
    print("Rule (EVAL.md): change a prompt or weight only for an error pattern seen at least 3 times.")

def main():
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample"); s.add_argument("input_csv"); s.add_argument("--n", type=int, default=100)
    s.add_argument("--seed", type=int, default=1); s.add_argument("--out", default="labels_todo.csv")
    c = sub.add_parser("score"); c.add_argument("labels"); c.add_argument("--db", default="OutPutCsv/lead_library_v4.db")
    c.add_argument("--min-tier", default="B", choices=["A", "B", "C"]); c.add_argument("--k", default="10,20")
    c.add_argument("--out-dir", default="OutPutCsv")
    a = ap.parse_args()
    {"sample": cmd_sample, "score": cmd_score}[a.cmd](a)

if __name__ == "__main__":
    main()
