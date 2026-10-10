"""Deterministic validation. LLM output is untrusted. Code owns grades, caps, enums, spans."""
import re, hashlib

ENUMS = {
    "hiring_type": {"replacing_human_labor","exploring_tech","one_off_project","unclear"},
    "spending_observed": {"none","hiring_human","paying_for_tool","stated_budget_for_fix"},
    "buyer_intent_observed": {"none","vague_interest","active_request","explicit_scope_and_budget"},
    "automation_fit": {"high","medium","low","none"},
    "pain_type": {"delay","error","volume","cost","visibility","inconsistency","missed_opportunity","other"},
}
NUM = re.compile(r"\d")
CADENCE = re.compile(r"\b(daily|weekly|monthly|hourly|every\s+(day|week|month|order|call|lead|shift)|per\s+(day|week|month))\b", re.I)
MONEY = re.compile(r"[$€£₹]|\d+\s*%|revenue|lost|missed", re.I)
FIT_ORDER = ["none","low","medium","high"]
MAX_BODY_CHARS = 24000

def norm(s): return re.sub(r"\s+", " ", s or "").strip().lower()

def min_fit(a, b):
    if a not in FIT_ORDER: return b
    return FIT_ORDER[min(FIT_ORDER.index(a), FIT_ORDER.index(b))]

def body_sha1(text): return hashlib.sha1(norm(text).encode("utf-8","ignore")).hexdigest()

def truncate_body(body):
    return (body[:MAX_BODY_CHARS], True) if len(body) > MAX_BODY_CHARS else (body, False)

def shingles(text, k=5):
    w = norm(text).split()
    return {" ".join(w[i:i+k]) for i in range(max(1, len(w)-k+1))}

def jaccard(a, b):
    sa, sb = shingles(a), shingles(b)
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0

def is_near_dup(a, b, thresh=0.85): return jaccard(a, b) >= thresh

def validate(obs: dict, source_text: str, vocab_ids: set, cap_ids: set) -> dict:
    errs = []
    src = norm(source_text)

    for k, allowed in ENUMS.items():
        if obs.get(k) is not None and obs[k] not in allowed:
            errs.append(f"bad_enum:{k}"); obs[k] = None

    verified = 0
    for ev in obs.get("evidence_items") or []:
        ev["span_verified"] = int(bool(ev.get("span")) and norm(ev["span"]) in src)
        verified += ev["span_verified"]
        if not ev["span_verified"]: errs.append(f"span_not_found:{ev.get('field')}")
    if obs.get("relevance_span") and norm(obs["relevance_span"]) not in src:
        errs.append("relevance_span_not_found"); obs["relevance_span"] = None

    for f in ("freq","time","impact"):
        blk = obs.get(f)
        if not blk: continue
        val = blk.get("value") or ""
        if not blk.get("span") or norm(blk["span"]) not in src:
            errs.append(f"unsupported:{f}"); obs[f] = None; continue
        if not (NUM.search(val) or CADENCE.search(val)):
            obs[f] = None; continue
        for n in re.findall(r"\d+(?:[.,]\d+)?", val):
            if n not in source_text:
                errs.append(f"number_not_in_source:{f}:{n}"); obs[f] = None; break

    p = obs.get("pattern") or {}
    if p.get("existing_id") and p["existing_id"] not in vocab_ids:
        errs.append("unknown_pattern_id"); p["existing_id"] = None
    obs["pattern"] = p
    obs["required_capabilities"] = [c for c in obs.get("required_capabilities") or [] if c in cap_ids]

    if obs.get("is_relevant") and verified == 0:
        errs.append("relevant_without_verified_evidence"); obs["is_relevant"] = None

    q = sum(1 for f in ("freq","time","impact") if obs.get(f))
    obs_spans = [e for e in obs.get("evidence_items") or [] if e.get("kind")=="observation" and e.get("span_verified")]
    if q >= 2 and len(obs_spans) >= 2: grade = "A"
    elif q >= 1 and obs_spans: grade = "B"
    elif obs_spans: grade = "C"
    else: grade = "D"
    obs["evidence_grade"] = grade

    imp, tm = obs.get("impact"), obs.get("time")
    if imp and MONEY.search(imp["value"]): econ = "quantified_money_or_loss"
    elif tm: econ = "quantified_time"
    elif obs.get("spending_observed") not in (None,"none"): econ = "spending_only"
    else: econ = "none_stated"
    obs["economic_signal"] = econ

    if grade in ("C","D") and obs.get("automation_fit") == "high": obs["automation_fit"] = "medium"
    if obs.get("automation_inappropriate"): obs["automation_fit"] = min_fit(obs.get("automation_fit"), "low")

    obs["validation_errors"] = errs
    obs["needs_review"] = bool(errs) or grade == "D" or obs.get("is_relevant") is None
    return obs


# ================= v3: multi-problem post validation =================
from scoring import score_problem
POST_TYPES = {"operational_problem","tech_request_no_problem","one_off_task","consulting_or_strategy","vague","spam","other"}
HIRING_V3 = {"replacing_labor","upgrading_process","new_capability","unclear"}

def _check_span(blk, src, errs, name):
    if blk and (not blk.get("span") or norm(blk["span"]) not in src):
        errs.append(f"unsupported:{name}"); return None
    return blk

def validate_post(res: dict, source_text: str, vocab_ids: set, cap_ids: set, cap_difficulty=None):
    errs = []; src = norm(source_text)
    if res.get("post_type") not in POST_TYPES: errs.append("bad_post_type"); res["post_type"] = "other"
    if res.get("relevance_span") and norm(res["relevance_span"]) not in src:
        errs.append("relevance_span_not_found"); res["relevance_span"] = None
    probs = res.get("problems") or []
    if res.get("post_type") != "operational_problem" or not res.get("is_relevant"):
        res["is_relevant"] = False if res.get("post_type") != "operational_problem" else res.get("is_relevant")
        if not res.get("is_relevant"): res["problems"] = []; res["post_errors"] = errs; return res
    probs = probs[:3]
    if sum(1 for p in probs if p.get("role") == "primary") != 1:
        errs.append("primary_count_not_1")
        for i, p in enumerate(probs): p["role"] = "primary" if i == 0 else "secondary"
    good = []
    for p in probs:
        perrs = []
        if p.get("hiring_type") not in HIRING_V3: perrs.append("bad_enum:hiring_type"); p["hiring_type"] = "unclear"
        for k in ("spending_observed","buyer_intent_observed","automation_fit","pain_type"):
            if p.get(k) is not None and p[k] not in ENUMS.get(k, {p[k]}) | ({"none"} if k in ("spending_observed","buyer_intent_observed") else set()):
                perrs.append(f"bad_enum:{k}"); p[k] = None
        # solution-named statements are rejected
        if re.search(r"\b(chatbot|ai agent|need(s|ed)? (a|an) (crm|website|app|bot))\b", p.get("problem_statement",""), re.I):
            perrs.append("problem_statement_names_solution")
        rec = p.get("recurrence") or {"level":"none"}
        if rec.get("level") in ("explicit","implied") and (not rec.get("span") or norm(rec["span"]) not in src):
            perrs.append("recurrence_span_not_found"); rec["level"] = "none"
        p["recurrence"] = rec
        for f in ("freq","time","impact"):
            blk = _check_span(p.get(f), src, perrs, f); p[f] = blk
            if blk:
                v = blk.get("value") or ""
                if not (NUM.search(v) or CADENCE.search(v)): p[f] = None; continue
                for n in re.findall(r"\d+(?:[.,]\d+)?", v):
                    if n not in source_text: perrs.append(f"number_not_in_source:{f}:{n}"); p[f] = None; break
        p["buyer_role"] = _check_span(p.get("buyer_role"), src, perrs, "buyer_role")
        for lst in ("causes","consequences"):
            keep = []
            for c in p.get(lst) or []:
                if c.get("kind") == "observation" and (not c.get("span") or norm(c["span"]) not in src):
                    perrs.append(f"unsupported:{lst}"); continue
                keep.append(c)
            p[lst] = keep
        verified = 0
        for ev in p.get("evidence_items") or []:
            ev["span_verified"] = int(bool(ev.get("span")) and norm(ev["span"]) in src); verified += ev["span_verified"]
            if not ev["span_verified"]: perrs.append(f"span_not_found:{ev.get('field')}")
        pt = p.get("pattern") or {}
        if pt.get("existing_id") and pt["existing_id"] not in vocab_ids: perrs.append("unknown_pattern_id"); pt["existing_id"] = None
        p["pattern"] = pt
        p["required_capabilities"] = [c for c in p.get("required_capabilities") or [] if c in cap_ids]
        q = sum(1 for f in ("freq","time","impact") if p.get(f))
        obs_spans = [e for e in p.get("evidence_items") or [] if e.get("kind") == "observation" and e.get("span_verified")]
        grade = "A" if q >= 2 and len(obs_spans) >= 2 else "B" if q >= 1 and obs_spans else "C" if obs_spans else "D"
        if grade in ("C","D") and p.get("automation_fit") == "high": p["automation_fit"] = "medium"
        if p.get("automation_inappropriate"): p["automation_fit"] = min_fit(p.get("automation_fit"), "low")
        p["evidence_grade"] = grade; p["validation_errors"] = perrs
        p["scores"] = score_problem(p, grade, perrs, cap_difficulty)
        p["needs_review"] = bool(perrs) or grade == "D"
        if verified == 0: perrs.append("problem_without_verified_evidence"); p["needs_review"] = True; continue
        good.append(p)
    res["problems"] = good
    if not good: res["is_relevant"] = None; errs.append("relevant_but_no_supported_problem")
    res["post_errors"] = errs
    return res
