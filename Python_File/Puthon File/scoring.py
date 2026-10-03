import re
"""Deterministic scoring. LLM extracts facts. Code computes independent ordinal dimensions.
No composite score. Thresholds marked HEURISTIC: calibrate on benchmark."""
import re, datetime
from collections import defaultdict

ORD = ["none","weak","moderate","strong"]
HOURS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|to)?\s*(\d+(?:\.\d+)?)?\s*(?:hours?|hrs?)\s*(?:/|per|a|each|every)?\s*(day|week|month)", re.I)
MONEY = re.compile(r"[$€£₹]|\d+\s*%|revenue|lost|losing|missed|\d+\s+(?:sales|deals|orders|leads|clients|patients|bookings)", re.I)
CAD_WORD = re.compile(r"\b(daily|weekly|monthly|hourly|every\s+\w+|per\s+(day|week|month))\b", re.I)

def derived_hours_per_week(time_value):
    """DERIVED, not explicit. Returns None if not parseable."""
    m = HOURS.search(time_value or "")
    if not m: return None
    lo = float(m.group(1)); hi = float(m.group(2)) if m.group(2) else lo
    mid = (lo + hi) / 2; unit = m.group(3).lower()
    return round(mid * {"day": 5, "week": 1, "month": 1/4.33}[unit], 1)

def recurrence_strength(p):
    f = p.get("freq"); r = p.get("recurrence") or {}
    if f and re.search(r"\d", f.get("value") or ""): return "strong"
    if f and CAD_WORD.search(f.get("value") or ""): return "moderate"
    if r.get("level") == "explicit": return "moderate"
    if r.get("level") == "implied": return "weak"
    return "none"

def time_cost_signal(p):   # HEURISTIC thresholds
    t = p.get("time")
    if not t: return "none", None
    h = derived_hours_per_week(t.get("value"))
    if h is None: return "weak", None
    return ("strong" if h >= 5 else "moderate" if h >= 1 else "weak"), h

def business_impact_signal(p):
    i = p.get("impact")
    if not i: return "none"
    v = i.get("value") or ""
    return "strong" if (re.search(r"\d", v) and MONEY.search(v)) else "moderate" if re.search(r"\d", v) else "weak"

def existing_labor_signal(p):
    if p.get("hiring_type") == "replacing_labor" and p.get("spending_observed") == "hiring_human": return "strong"
    if p.get("current_handler") in ("employee","assistant","contractor"): return "moderate"
    if p.get("current_handler") == "owner": return "weak"
    return "none"

def existing_spending_signal(p):
    s = p.get("spending_observed")
    return {"stated_budget_for_fix":"strong","paying_for_tool":"moderate","hiring_human":"moderate"}.get(s,"none")

def urgency_signal(p, rec, imp, tc):
    # never from posting date
    if imp == "strong" and rec in ("moderate","strong"): return "strong"
    if imp in ("moderate","weak") or tc in ("moderate","strong"): return "moderate" if rec != "none" else "weak"
    return "none"

def integration_complexity(p, cap_difficulty):
    caps = p.get("required_capabilities") or []
    if not caps or not cap_difficulty: return "unknown"
    s = sum(cap_difficulty.get(c, 3) for c in caps)
    return "low" if s <= 6 else "medium" if s <= 12 else "high"

def buyer_clarity(p):
    br = p.get("buyer_role") or {}
    has_role = br.get("role") in ("owner","manager","ops") and br.get("span")
    if has_role and p.get("spending_observed") == "stated_budget_for_fix": return "strong"
    if has_role: return "moderate"
    return "none"

def confidences(p, grade, validation_errors, assignment=None):
    ext = "high" if not validation_errors else "medium" if len(validation_errors) <= 1 else "low"
    evid = {"A":"high","B":"medium","C":"low","D":"low"}[grade]
    rec = recurrence_strength(p)
    full_quant = all(p.get(k) for k in ("freq","time","impact"))
    if grade == "D" or rec in ("none","weak"): prob = "low"
    elif grade in ("A","B") and rec in ("moderate","strong") and (time_cost_signal(p)[0] in ("moderate","strong") or business_impact_signal(p) in ("moderate","strong")): prob = "high"
    else: prob = "medium"
    if p.get("hiring_type") in ("upgrading_process","new_capability","unclear") and not full_quant and prob == "high": prob = "medium"
    norm = "low"
    if assignment:
        if assignment.get("method") == "human": norm = "high"
        elif assignment.get("method") == "auto": norm = "high" if assignment.get("similarity", 0) >= 0.85 else "medium"
    return {"extraction_confidence": ext, "evidence_confidence": evid, "problem_confidence": prob, "normalization_confidence": norm}

def score_problem(p, grade, errors, cap_difficulty=None, assignment=None):
    rec = recurrence_strength(p); tc, hpw = time_cost_signal(p); imp = business_impact_signal(p)
    return {
      "recurrence_strength": rec, "time_cost_signal": tc, "derived_hours_per_week": hpw,
      "business_impact_signal": imp, "existing_labor_signal": existing_labor_signal(p),
      "existing_spending_signal": existing_spending_signal(p), "urgency_signal": urgency_signal(p, rec, imp, tc),
      "automation_fit": p.get("automation_fit"), "integration_complexity": integration_complexity(p, cap_difficulty),
      "buyer_clarity": buyer_clarity(p), **confidences(p, grade, errors, assignment)}

# ---------- pattern level ----------
def occurrence_profile(rows):
    """rows: dicts with client_key, post_id, industry, source_type, date_posted (YYYY-MM-DD), dup_of,
    evidence_grade, labor, spending (strong/moderate/weak/none strings). Counts CLIENTS, not posts."""
    rows = [r for r in rows if not r.get("dup_of")]
    clients = {r["client_key"] for r in rows if r.get("client_key")}
    unk = sum(1 for r in rows if not r.get("client_key"))   # unknown client: counted separately, never as unique
    ind = {r["industry"] for r in rows if r.get("industry")}
    months = sorted({r["date_posted"][:7] for r in rows if r.get("date_posted")})
    def n(cond): return len({r["client_key"] for r in rows if r.get("client_key") and cond(r)})
    return {
      "raw_occurrences": len(rows), "unique_posts": len({r["post_id"] for r in rows}),
      "unique_clients": len(clients), "unknown_client_posts": unk,
      "unique_industries": len(ind), "unique_sources": len({r["source_type"] for r in rows}),
      "months_span": len(months), "first_seen": months[0] if months else None, "last_seen": months[-1] if months else None,
      "clients_grade_AB": n(lambda r: r.get("evidence_grade") in ("A","B")),
      "clients_labor": n(lambda r: r.get("labor") in ("moderate","strong")),
      "clients_spending": n(lambda r: r.get("spending") in ("moderate","strong")),
      "market_repetition": market_repetition(len(clients), len(ind), len(months)),
      "trend": trend_signal(rows)}

def market_repetition(clients, industries, months):  # HEURISTIC
    if clients >= 10 and industries >= 2 and months >= 2: return "strong"
    if clients >= 5: return "moderate"
    if clients >= 2: return "weak"
    return "single_or_none"

def trend_signal(rows, min_clients=30, min_months=3, min_per_month=5):
    by = defaultdict(set)
    for r in rows:
        if r.get("client_key") and r.get("date_posted"): by[r["date_posted"][:7]].add(r["client_key"])
    months = sorted(by)
    total = len(set().union(*by.values())) if by else 0
    if len(months) < min_months or total < min_clients or sum(1 for m in months if len(by[m]) >= min_per_month) < min_months:
        return "insufficient_data"
    h = len(months) // 2
    a = sum(len(by[m]) for m in months[:h]) / h; b = sum(len(by[m]) for m in months[h:]) / (len(months) - h)
    return "growing" if b > 1.5 * a else "declining" if b < a / 1.5 else "stable"

LADDER = ["L0 weak","L1 problem_mentioned","L2 recurring","L3 time_or_freq_quantified","L4 business_consequence",
 "L5 labor_or_spending","L6 independent_repetition","L7 target_business_evidence","L8 conversation_confirms",
 "L9 solution_interest","L10 free_pilot","L11 paid_pilot","L12 repeated_paid","L13 productize"]

def validation_level(profile, event_types, proofs_ok=True, n_paying=0, target_business_with_direct_evidence=False):
    """DERIVED. Levels 1-5 need >=3 independent clients at that level (HEURISTIC). 8+ need events with proof_ref."""
    lvl = 0
    if profile["unique_clients"] >= 1: lvl = 1
    if profile["clients_grade_AB"] >= 3: lvl = 2
    # L3-L5 approximated from client-level counts supplied in profile (extend as data grows)
    if profile.get("clients_quantified", 0) >= 3: lvl = max(lvl, 3)
    if profile.get("clients_impact", 0) >= 3: lvl = max(lvl, 4)
    if profile["clients_labor"] >= 3 or profile["clients_spending"] >= 3: lvl = max(lvl, 5)
    if lvl >= 5 and profile["unique_clients"] >= 5 and profile["months_span"] >= 2: lvl = 6
    if lvl >= 6 and target_business_with_direct_evidence: lvl = 7
    if proofs_ok:
        for et, L in [("conversation",8),("interest_stated",9),("free_pilot",10),("paid_pilot",11)]:
            if et in event_types and lvl >= L - 1 + (0 if L == 8 else 0): lvl = max(lvl, L)
        if n_paying >= 3: lvl = max(lvl, 12)
    return LADDER[lvl]
