"""Deterministic validation. The model's output is untrusted: code decides what counts as supported.

Policy
- A claim (problem, impact, buyer role, budget, intent...) survives only if its span is found in the post.
- Unsupported SOFT claims are replaced by 'unknown'/None and logged in `downgrades` (the lead is kept).
- Unsupported HARD claims (a problem statement with no quote, a client ask with no quote) are removed,
  logged in `errors`, and the lead is flagged `needs_review`.
- Span matching ignores punctuation, quote style, case and markdown, and repairs near-misses
  (>= 0.88 token similarity) to the exact source text. Repaired spans are counted, never silently trusted."""
import re, unicodedata
from difflib import SequenceMatcher
from taxonomy import CATEGORIES, INTENT_LEVELS, WORKFLOW_OBJECTS, BAD_NAME_TOKENS, snake

TOKEN = re.compile(r"\w+", re.UNICODE)
NUM = re.compile(r"\d+(?:\.\d+)?")
CADENCE = re.compile(r"\b(daily|weekly|monthly|hourly|every\s+\w+|each\s+\w+|per\s+(day|week|month)|a\s+(day|week|month))\b", re.I)
EXPLORATORY = re.compile(r"\b(open to ideas|not sure|exploring|explore|maybe|thinking (about|of)|curious|brainstorm|wondering|"
                         r"considering|might|possibly|just looking|any suggestions|what do you think)\b", re.I)
SOLUTION_NAMED = re.compile(r"\b(chatbot|ai agent|zapier|n8n|need(s|ed)? (a|an) (crm|website|app|bot))\b", re.I)

PAIN = {"delay","error","volume","cost","visibility","inconsistency","missed_opportunity","other"}
ROLES = {"owner","manager","ops","technical","unknown"}
HANDLERS = {"owner","employee","assistant","contractor","tool","nobody","unknown"}
HIRING = {"replacing_labor","upgrading_process","new_capability","unclear"}
SPEND = {"none","hiring_human","paying_for_tool","stated_budget_for_fix"}
AUTO = {"high","medium","low","none"}

def _fold(s): return unicodedata.normalize("NFKC", s).lower()
def _tokens(s): return [_fold(m.group()) for m in TOKEN.finditer(s or "")]

class Source:
    def __init__(self, text):
        self.text = text or ""
        self.toks, self.pos = [], []
        for m in TOKEN.finditer(self.text):
            self.toks.append(_fold(m.group())); self.pos.append((m.start(), m.end()))

    def _find(self, t, start):
        n, N = len(t), len(self.toks)
        if n == 0 or n > N: return None
        for i in range(start, N - n + 1):
            if self.toks[i:i + n] == t: return i, i + n, "exact"
        if n < 4: return None
        tset, best, best_r = set(t), None, 0.0
        for size in (n, n - 1, n + 1):
            for i in range(start, N - size + 1):
                w = self.toks[i:i + size]
                if len(tset & set(w)) / len(tset) < 0.7: continue
                r = SequenceMatcher(None, w, t).ratio()
                if r > best_r: best, best_r = (i, i + size), r
        if best and best_r >= 0.88: return best[0], best[1], "fuzzy"
        return None

    def verify(self, span, min_tokens=2):
        """-> {ok, text, method}. text is the canonical substring of the source."""
        parts = [p for p in re.split(r"\s*(?:\.\.\.|\u2026)\s*", span or "") if _tokens(p)]
        if not parts or sum(len(_tokens(p)) for p in parts) < min_tokens: return {"ok": False}
        outs, methods, cur = [], [], 0
        for p in parts:
            r = self._find(_tokens(p), cur)
            if not r: return {"ok": False}
            i, j, m = r
            outs.append(self.text[self.pos[i][0]:self.pos[j - 1][1]]); methods.append(m); cur = j
        return {"ok": True, "text": " \u2026 ".join(outs)[:400], "method": "exact" if set(methods) == {"exact"} else "fuzzy"}

class Ctx:
    def __init__(self, src, title):
        self.src, self.title = src, title
        self.errors, self.downs, self.repairs, self.evidence = [], [], 0, []

    def claim(self, blk, name, min_tokens=2, quote_key=None):
        """Return a copy of blk with a verified canonical span, or None."""
        if not isinstance(blk, dict): return None
        if not blk.get("span"):
            self.downs.append(f"{name}:no_span"); return None
        r = self.src.verify(blk["span"], min_tokens)
        if not r["ok"]:
            self.downs.append(f"{name}:span_not_found"); return None
        if r["method"] == "fuzzy": self.repairs += 1
        out = dict(blk); out["span"] = r["text"]; out["span_method"] = r["method"]
        self.evidence.append({"field": name, "span": r["text"], "method": r["method"]})
        return out

    def quant(self, blk, name):
        c = self.claim(blk, name, min_tokens=2)
        if not c: return None
        val = str(c.get("value") or "")
        span_nums = {n.replace(",", "") for n in NUM.findall(c["span"].replace(",", ""))}
        nums = [n for n in NUM.findall(val.replace(",", ""))]
        if nums:
            if not all(n in span_nums for n in nums):
                self.downs.append(f"{name}:number_not_in_span"); return None
        elif not CADENCE.search(val + " " + c["span"]):
            self.downs.append(f"{name}:no_number_or_cadence"); return None
        return c

def _toks_set(name): return set(name.split("_"))

def _pattern(pt, vocab, ctx):
    pt = pt if isinstance(pt, dict) else {}
    eid, prop = pt.get("existing_id"), pt.get("proposed")
    if eid and eid in vocab: return {"existing_id": eid, "proposed": None}
    if eid: ctx.downs.append("pattern:unknown_existing_id")
    if not isinstance(prop, dict) and eid: prop = {"name": eid}
    if isinstance(prop, dict) and prop.get("name"):
        name = snake(prop["name"])
        if name in vocab: return {"existing_id": name, "proposed": None}   # model proposed a name that already exists
        toks = [t for t in name.split("_") if t not in BAD_NAME_TOKENS]
        if len(toks) != len(name.split("_")): ctx.downs.append("pattern:banned_word_removed_from_name")
        name = "_".join(toks[:5]) or "unclassified_problem"
        if name in vocab: return {"existing_id": name, "proposed": None}
        syn = next((v for v in vocab if len(_toks_set(v) & _toks_set(name)) / len(_toks_set(v) | _toks_set(name)) >= 0.6), None)
        cat = prop.get("category") if prop.get("category") in CATEGORIES else None
        return {"existing_id": None, "proposed": {
            "name": name, "category": cat, "definition": str(prop.get("definition") or "")[:240],
            "same_prototype_test": str(prop.get("same_prototype_test") or "")[:160], "likely_synonym_of": syn}}
    ctx.downs.append("pattern:none_given")
    return {"existing_id": None, "proposed": None}

def validate_lead(res, title, body, vocab):
    """vocab: dict pattern_id -> category.  Returns the validated lead record."""
    ctx = Ctx(Source(f"{title}\n{body}"), title)
    reasons = []
    cat = res.get("service_category")
    if cat not in CATEGORIES: ctx.downs.append("service_category:invalid"); cat = "other"
    sec = [c for c in (res.get("secondary_categories") or []) if c in CATEGORIES and c != cat][:2]

    ask = ctx.claim(res.get("client_ask"), "client_ask", 3)
    if res.get("client_ask") and not ask:
        ctx.errors.append("client_ask_unsupported"); reasons.append("client_ask_unsupported")

    deliverables = []
    for d in (res.get("deliverables") or [])[:6]:
        c = ctx.claim(d, "deliverable", 3)
        if c: deliverables.append(c)

    # ---- buyer intent
    bi = res.get("buyer_intent") if isinstance(res.get("buyer_intent"), dict) else {}
    lvl = bi.get("level") if bi.get("level") in INTENT_LEVELS else "unknown"
    ic = ctx.claim(bi, "buyer_intent", 2) if lvl != "unknown" else None
    if lvl != "unknown" and not ic:
        ctx.downs.append("buyer_intent:unsupported->unknown"); lvl = "unknown"
    ispan = ic["span"] if ic else ""
    if lvl == "explicit_hire" and EXPLORATORY.search(f"{ispan} {title}"):
        lvl = "defined_project"; ctx.downs.append("buyer_intent:explicit_hire->defined_project(exploratory wording)")
        reasons.append("intent_conflict")
    if lvl in ("explicit_hire", "defined_project") and not ask and not deliverables:
        lvl = "vague"; ctx.downs.append("buyer_intent:no_verified_scope->vague")
    buyer_intent = {"level": lvl, "span": ic["span"] if ic else None}

    # ---- buyer role / industry / budget
    br = res.get("buyer_role") if isinstance(res.get("buyer_role"), dict) else {}
    role = br.get("role") if br.get("role") in ROLES else "unknown"
    rc = ctx.claim(br, "buyer_role", 2) if role != "unknown" else None
    if role != "unknown" and not rc: ctx.downs.append("buyer_role:unsupported->unknown"); role = "unknown"
    buyer_role = {"role": role, "span": rc["span"] if rc else None}

    ind = ctx.claim(res.get("industry"), "industry", 1) if res.get("industry") else None
    budget = None
    if isinstance(res.get("budget"), dict):
        b = ctx.claim(res["budget"], "budget", 1)
        if b and NUM.search(b["span"]):
            b["kind"] = b.get("kind") if b.get("kind") in ("fixed", "hourly", "unclear") else "unclear"; budget = b
        elif b: ctx.downs.append("budget:no_number_in_span")

    # ---- problems
    kept, rejected = [], []
    for p in (res.get("problems") or [])[:3] if isinstance(res.get("problems"), list) else []:
        if not isinstance(p, dict): continue
        stmt = p.get("problem_statement")
        sup = ctx.claim({"span": p.get("support_span")}, "problem", 3)
        if not stmt or not sup:
            rejected.append({"problem_statement": stmt, "reason": "support_span_not_found"})
            ctx.errors.append("problem_unsupported"); reasons.append("problem_unsupported"); continue
        if SOLUTION_NAMED.search(stmt): ctx.downs.append("problem_statement:names_solution"); reasons.append("problem_names_solution")
        rec = p.get("recurrence") if isinstance(p.get("recurrence"), dict) else {}
        rl = rec.get("level") if rec.get("level") in ("explicit", "implied", "none") else "none"
        rcl = ctx.claim(rec, "recurrence", 2) if rl != "none" else None
        if rl != "none" and not rcl: ctx.downs.append("recurrence:span_not_found->none"); rl = "none"
        keep = {"role": p.get("role") if p.get("role") in ("primary", "secondary") else "secondary",
                "problem_statement": str(stmt)[:300], "support_span": sup["span"],
                "workflow_object": p.get("workflow_object") if p.get("workflow_object") in WORKFLOW_OBJECTS else "other",
                "pain_type": p.get("pain_type") if p.get("pain_type") in PAIN else "other",
                "recurrence": {"level": rl, "span": rcl["span"] if rcl else None},
                "freq": ctx.quant(p.get("freq"), "freq"), "time": ctx.quant(p.get("time"), "time"),
                "impact": ctx.quant(p.get("impact"), "impact"),
                "current_handler": p.get("current_handler") if p.get("current_handler") in HANDLERS else "unknown",
                "hiring_type": p.get("hiring_type") if p.get("hiring_type") in HIRING else "unclear",
                "spending_observed": p.get("spending_observed") if p.get("spending_observed") in SPEND else "none"}
        for lst in ("causes", "consequences"):
            keepl = []
            for c in p.get(lst) or []:
                if not isinstance(c, dict): continue
                if c.get("kind") == "observation":
                    vc = ctx.claim(c, lst[:-1], 2)
                    if vc: keepl.append(vc)
                else:
                    keepl.append({"text": c.get("text"), "span": None, "kind": "inference"})
            keep[lst] = keepl
        au = p.get("automation") if isinstance(p.get("automation"), dict) else {}
        keep["automation"] = {"fit": au.get("fit") if au.get("fit") in AUTO else None,
                              "reason": str(au.get("reason") or "")[:300], "inappropriate": bool(au.get("inappropriate"))}
        keep["pattern"] = _pattern(p.get("pattern"), vocab, ctx)
        kept.append(keep)
    if rejected and not kept: reasons.append("all_problems_unsupported")
    for i, p in enumerate(kept): p["role"] = "primary" if i == 0 else "secondary"

    # ---- advice (inference: no spans) and evidence grade
    adv = res.get("advice") if isinstance(res.get("advice"), dict) else {}
    advice = {"service_needed": str(adv.get("service_needed") or "")[:160],
              "reason_to_apply": str(adv.get("reason_to_apply") or "")[:300],
              "potential_solution": str(adv.get("potential_solution") or "")[:400],
              "risks": [str(r)[:160] for r in (adv.get("risks") or []) if r][:4]}
    q = max((sum(1 for k in ("freq", "time", "impact") if p.get(k)) for p in kept), default=0)
    grade = "A" if kept and q >= 2 else "B" if kept and q == 1 else "C" if kept else "D"
    if len(ctx.downs) >= 3: reasons.append("many_downgrades")

    return {"service_category": cat, "secondary_categories": sec, "client_ask": ask, "buyer_intent": buyer_intent,
            "buyer_role": buyer_role, "industry": ind, "budget": budget, "deliverables": deliverables,
            "problems": kept, "rejected_problems": rejected, "advice": advice, "evidence": ctx.evidence,
            "evidence_grade": grade,
            "validation": {"errors": ctx.errors, "downgrades": ctx.downs, "span_repairs": ctx.repairs},
            "review_reasons": sorted(set(reasons)), "needs_review": bool(reasons)}

# ---------- duplicate helpers ----------
def shingles(text, k=5):
    w = _tokens(text)
    return {" ".join(w[i:i + k]) for i in range(max(1, len(w) - k + 1))}

def jaccard(a, b):
    sa, sb = shingles(a), shingles(b)
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0
