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
