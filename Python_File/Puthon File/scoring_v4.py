"""Deterministic scoring. The model extracts facts; code computes scores.

Two SEPARATE assessments per job:
  1. lead_quality()          -> is this job worth applying to?  (0-100, six visible components)
  2. automation_assessment() -> could the underlying problem be automated?  (never mixed into lead quality)

All weights, thresholds and caps live in my_profile.py and are HEURISTIC until calibrated with outcomes.py.
A component with no data is None and is excluded from the weighted average (unknown is not the same as bad);
`coverage` reports how much of the weight actually had data."""
import datetime
import my_profile as P
from common import parse_money, parse_budget, parse_lower_bound_int, parse_bool, parse_pct, parse_date

INTENT_POINTS = {"explicit_hire": 100, "defined_project": 75, "exploratory": 35, "research_inquiry": 15, "vague": 10, "unknown": 0}
AUTO_POINTS = {"high": 90, "medium": 60, "low": 30, "none": 0}
TIER_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "N": 5}

def intent_score(v):
    return INTENT_POINTS.get((v.get("buyer_intent") or {}).get("level", "unknown"), 0)

def fit_score(v):
    lv = P.SERVICE_FIT.get(v.get("service_category"), "out")
    pts = P.FIT_POINTS[lv]
    for c in v.get("secondary_categories") or []:
        pts = max(pts, 0.8 * P.FIT_POINTS[P.SERVICE_FIT.get(c, "out")])
    return round(pts), lv

def clarity_score(v):
    pts = 0
    if v.get("client_ask"): pts += 25
    pts += min(20, 7 * len(v.get("deliverables") or []))
    probs = v.get("problems") or []
    if probs:
        p = max(probs, key=lambda x: sum(1 for k in ("freq", "time", "impact") if x.get(k)))
        pts += 20
        pts += {"explicit": 10, "implied": 5}.get((p.get("recurrence") or {}).get("level"), 0)
        pts += 8 * sum(1 for k in ("freq", "time", "impact") if p.get(k))
    return min(100, pts)

def budget_score(v, meta):
    b = parse_budget(meta.get("budget"))
    if not b and v.get("budget"):
        b = parse_budget(v["budget"].get("value") or v["budget"].get("span"))
        if b and v["budget"].get("kind") in ("fixed", "hourly"): b["kind"] = v["budget"]["kind"]
    if not b: return 0, None
    if b["amount"] is None: return 30, b
    thr = P.MIN_HOURLY_RATE if b["kind"] == "hourly" else P.MIN_FIXED_BUDGET
    return (100 if b["amount"] >= thr else 40), b

def client_score(meta):
    pts, present = 0, 0
    pv = parse_bool(meta.get("client_payment_verified"))
    if pv is not None: present += 1; pts += 35 if pv else 0
    sp = parse_money(meta.get("client_total_spend"))
    if sp is not None:
        present += 1; pts += 40 if sp >= 10000 else 30 if sp >= 2000 else 20 if sp >= 500 else 8 if sp > 0 else 0
    hr = parse_pct(meta.get("client_hire_rate"))
    if hr is not None: present += 1; pts += 15 if hr >= 50 else 8 if hr >= 25 else 0
    try: rt = float(meta.get("client_rating")) if meta.get("client_rating") else None
    except ValueError: rt = None
    if rt is not None: present += 1; pts += 10 if rt >= 4.5 else 5 if rt >= 4 else 0
    return (min(100, pts) if present else None), pv, sp

def recency_competition(meta, date_posted, as_of):
    parts, days = [], None
    d = parse_date(date_posted)
    if d:
        days = (as_of - d).days
        parts.append(100 if days <= 1 else 80 if days <= 3 else 55 if days <= 7 else 30 if days <= 14 else 10)
    n = parse_lower_bound_int(meta.get("proposals"))
    if n is not None:
        parts.append(100 if n < 5 else 80 if n < 10 else 60 if n < 15 else 40 if n < 20 else 20 if n < 50 else 5)
    return (round(sum(parts) / len(parts)) if parts else None), days, n

def tier_for(score):
    for t, cut in P.TIERS:
        if score >= cut: return t
    return "D"

def lead_quality(v, meta, date_posted, as_of, discovery_candidate=True):
    fit, fit_level = fit_score(v)
    bud, bobj = budget_score(v, meta)
    cli, pv, spend = client_score(meta)
    rc, days, props = recency_competition(meta, date_posted, as_of)
    comps = {"buyer_intent": intent_score(v), "service_fit": fit, "problem_clarity": clarity_score(v),
             "budget_evidence": bud, "client_signals": cli, "recency_competition": rc}
    used = {k: P.WEIGHTS[k] for k, s in comps.items() if s is not None}
    total_w = sum(P.WEIGHTS.values())
    score = sum(P.WEIGHTS[k] * comps[k] for k in used) / sum(used.values())
    caps = []
    lvl = (v.get("buyer_intent") or {}).get("level", "unknown")
    cap = 100
    if lvl in P.CAP_BY_INTENT and P.CAP_BY_INTENT[lvl] < cap: cap = P.CAP_BY_INTENT[lvl]; caps.append(f"intent={lvl}")
    if fit_level == "out" and P.CAP_OUT_OF_FIT < cap: cap = P.CAP_OUT_OF_FIT; caps.append("service_out_of_fit")
    if days is not None and days > P.STALE_DAYS and 54 < cap: cap = 54; caps.append(f"stale>{P.STALE_DAYS}d")
    score = round(min(score, cap), 1)
    return {"lead_score": score, "tier": tier_for(score), "components": comps,
            "coverage": round(sum(used.values()) / total_w, 2), "caps_applied": caps,
            "fit_level": fit_level, "budget": bobj, "payment_verified": pv, "client_spend": spend,
            "days_old": days, "proposals": props}

def automation_assessment(v):
    """Separate from lead quality. A job can be an excellent opportunity with automation = not_assessed/none."""
    probs = v.get("problems") or []
    if not probs:
        return {"automation_fit": "not_assessed", "automation_score": None, "automation_reason": "No operational problem described in the post.",
                "automation_inappropriate": False, "automation_evidence_grade": v.get("evidence_grade")}
    p = probs[0]; au = p.get("automation") or {}
    fit = au.get("fit") or "low"
    grade = v.get("evidence_grade")
    if grade in ("C", "D") and fit == "high": fit = "medium"      # high needs quantified evidence
    if au.get("inappropriate"): fit = "low" if fit in ("high", "medium") else fit
    return {"automation_fit": fit, "automation_score": AUTO_POINTS[fit], "automation_reason": au.get("reason", ""),
            "automation_inappropriate": bool(au.get("inappropriate")), "automation_evidence_grade": grade}

def risk_flags(v, q):
    r = []
    if q["payment_verified"] is False: r.append("client payment not verified")
    if q["client_spend"] is not None and q["client_spend"] == 0: r.append("client has no spend history")
    if q["proposals"] is not None and q["proposals"] >= 20: r.append(f"high competition ({q['proposals']}+ proposals)")
    if q["days_old"] is not None and q["days_old"] > 14: r.append(f"posted {q['days_old']} days ago")
    b = q["budget"]
    if b and b["amount"] is not None and q["components"]["budget_evidence"] == 40: r.append("budget below your minimum")
    if q["components"]["budget_evidence"] == 0: r.append("no budget stated")
    lvl = (v.get("buyer_intent") or {}).get("level")
    if lvl in ("exploratory", "research_inquiry", "vague", "unknown"): r.append(f"buyer intent is {lvl}")
    if q["fit_level"] == "out": r.append("outside your core services")
    if v.get("needs_review"): r.append("extraction flagged for review")
    return r

def drivers(q):
    c, out = q["components"], []
    if c["buyer_intent"] >= 75: out.append("clear hiring intent")
    if c["service_fit"] >= 100: out.append("core service")
    if c["problem_clarity"] >= 60: out.append("well-defined scope/problem")
    if c["budget_evidence"] == 100: out.append("budget stated and above minimum")
    if (c["client_signals"] or 0) >= 70: out.append("strong client history")
    if (c["recency_competition"] or 0) >= 70: out.append("fresh, low competition")
    return out

def next_action(v, q, auto, flags):
    tier, lvl = q["tier"], (v.get("buyer_intent") or {}).get("level")
    gaps = []
    if q["components"]["budget_evidence"] == 0: gaps.append("budget")
    if q["components"]["problem_clarity"] < 50: gaps.append("scope")
    ask = f" Ask about {' and '.join(gaps)}." if gaps else ""
    hook = ((v.get("deliverables") or [{}])[0].get("text") or (v.get("client_ask") or {}).get("text") or "their stated need")
    if v.get("needs_review"): return "Check the flagged extraction before deciding."
    if tier == "A": return f"Apply now. Open with {hook}.{ask}"
    if tier == "B": return f"Apply if you have capacity; keep it short and specific to {hook}.{ask}"
    if tier == "C":
        if lvl in ("exploratory", "research_inquiry", "vague", "unknown"): return "Skip unless cheap: ask one clarifying question before spending Connects."
        if q["fit_level"] == "out": return "Skip unless you want to expand into this service."
        return "Low priority: watch for client replies or a budget update."
    return "Skip."

def build_action(v, q, auto):
    flags = risk_flags(v, q)
    adv = v.get("advice") or {}
    ev = [e["span"] for e in v.get("evidence") or []
          if e["field"] in ("client_ask", "buyer_intent", "problem", "budget", "impact", "deliverable")][:4]
    risks = flags + [r for r in adv.get("risks", []) if r][:3]
    return {"service_needed": adv.get("service_needed") or v.get("service_category"),
            "evidence": ev, "reason_to_apply": adv.get("reason_to_apply"),
            "potential_solution": adv.get("potential_solution"), "risks": risks,
            "score_drivers": drivers(q), "next_action": next_action(v, q, auto, flags)}
