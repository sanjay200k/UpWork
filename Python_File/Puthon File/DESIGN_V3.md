# Problem Discovery Engine: Audit + Redesign v3

Files: `prompts_v3.py` (S8, S9), `schema_v3.json` (S7), `validate.py` (S10, `validate_post`), `scoring.py` (S12, S14, S15), `adversarial_tests.json` (S21). Older: `../pde_v2/` (aggregate.py SQL, normalize.py thresholds, EVAL.md).

## S1 Executive diagnosis
- Engine = cheap filter for "what do businesses pay humans to do repeatedly". Useful. Not demand proof.
- Prototype plumbing works: 30 in, 23 relevant, 7 filtered, 0 ERROR, filtered ones all correct.
- Prototype results prove nothing about market. Data synthetic, clean, quantified.
- Biggest defects: label fragmentation, post-count not client-count, LLM owns confidence and urgency, bare cadence word passes gate, no evidence span check, one problem per post, binary hiring_type.
- Biggest strategic risk: build engine, skip talking to humans. Upwork = biased sample (buyers who already hire freelancers). Engine reaches "problem exists on Upwork". Never reaches "your reachable customers pay you".

## S2 Already good (keep)
- Upwork as signal source, not lead source.
- Relevance gate before extraction. All 7 filtered posts correct.
- "Don't trust requested solution" rule.
- Hard gate in code that only downgrades.
- Filtered-out sidecar CSV (audit trail).
- Seen-job dedup store, per-row flush, resume, 429 backoff.
- `problem_pattern` as cross-industry label idea.
- Prompt ban on "AI/n8n/budget raises confidence".

## S3 Currently broken (evidence from your run)
1. Fragmentation: `lead_follow_up_delay` 2 + `manual_lead_follow_up_delay` 2 (true 4). `manual_order_data_entry` 2 + `manual_order_entry` 1 (true 3). 3 invoice labels at 1 each (true 3 in one family).
2. Rosy numbers: 20/23 High confidence, 21/23 High fit, 22/23 "replacing human labor". Real data will not look like this.
3. sample-006: High with only "daily", no impact. Cadence word counted as quantified.
4. `problem_occurrence` counts rows. One client posting 5 times = 5.
5. `hiring_type` binary. "unclear" forced into a bucket.
6. `solution_type` has no `none` / `process_fix` / `hire_human`. Tech bias baked in.
7. `id` = row index. Collides across batches.
8. ERROR rows all keyed `"ERROR"` on resume. Collapse and duplicate.
9. Filtered CSV opened `"w"` each run. Old filtered rows lost.
10. `--fresh` double-counts occurrences.
11. Hardcoded `F:\` path. Breaks in Docker. Stores vanish.
12. API key in `config_example.py`. Rotate.
13. README stale (Claude, P1, LEAD_TYPE).

## S4 Root causes
- LLM does judgment fields code should compute (confidence, urgency, fit ceilings).
- Label is free text. No controlled vocabulary. "manual" leaks into identity.
- Unit of count = row, not independent client.
- No verbatim-span contract. Evidence is paraphrase, uncheckable.
- One flat record per post. No place for cause, consequence, second problem.
- Test data = LLM-written. Model reads its own style.
- No benchmark. No way to say if a change helped.

## S5 Target architecture
```
SOURCE (manual CSV / approved API; save raw text unchanged)
 -> INGEST (client_key, body_sha1, truncate, spam filter, log rejects)
 -> DEDUP (exact sha1; near shingle>=0.85; same client_key+30d)      [CODE]
 -> LLM EXTRACT (posts -> 0..3 problems, spans, enums)                [LLM]
 -> VALIDATE (schema, enums, spans verbatim, numbers in source)       [CODE]
 -> SCORE (ordinal dimensions, 4 confidences)                         [CODE]
 -> EVIDENCE STORE (observation vs inference, span_verified)          [DB]
 -> NORMALIZE (vocab pick -> embed rank -> LLM compare -> human)      [LLM+EMBED+HUMAN]
 -> CANONICAL PROBLEM LIBRARY (versioned, relations)                  [DB]
 -> AGGREGATE (clients not posts; profile; trend only if enough)      [SQL/CODE]
 -> VALIDATION LEVEL (derived; B-levels need proof)                   [CODE]
 -> LEARNING + BUILD RECS (rules over capability catalog)             [CODE]
 -> HUMAN REVIEW QUEUE
 -> BUSINESS MATCHING (per pattern, direct evidence only)             [LLM assist + HUMAN]
 -> RESEARCH BRIEF -> HUMAN decides -> CUSTOMER CONVERSATION
 -> PILOT -> PAID PILOT -> REPEAT PAID -> PRODUCTIZE
```
Change vs your diagram: DEDUP before LLM (save cost). SCORE is code. Learning/Build sit after validation level, not before business matching. Customer conversation is a parallel track starting at month 1, not a late stage.

## S6 Data model (SQLite, Stage 2)
```sql
raw_posts(post_id PK, source_type, source_url, client_key, title, body, body_sha1,
          date_posted, date_collected, batch_query, dup_of, copied_template_flag)
extractions(extraction_id PK, post_id, run_id, model, prompt_version, schema_version,
            post_type, is_relevant, relevance_reason, relevance_span, client_ask,
            industry_stated, post_errors, raw_json)
problem_observations(obs_id PK, extraction_id, role, problem_statement, workflow_object, pain_type,
            recurrence_level, recurrence_span, freq_value, freq_span, time_value, time_span,
            impact_value, impact_span, derived_hours_per_week,         -- derived, flagged
            current_handler, current_solution, tools, hiring_type, spending_observed,
            buyer_intent_observed, buyer_role, automation_fit, automation_inappropriate,
            solution_modes, required_capabilities, missing_info,
            evidence_grade, scores_json, validation_errors, needs_review)
evidence_items(ev_id PK, obs_id, field, span, kind, span_verified, supports_claim)
claims(claim_id PK, obs_id, type CHECK(type IN('cause','consequence')), text, span, kind)
canonical_problems(problem_id PK, canonical_name, human_name, definition, same_prototype_test,
            family, subfamily, status, taxonomy_version, merged_into, created_at)
problem_aliases(alias TEXT, problem_id)                                 -- raw phrasings seen
problem_relations(a_id, b_id, relation CHECK(relation IN('related','subproblem','cause','consequence','conflicting')),
            reviewed_by, date)
assignments(obs_id, problem_id, method, similarity, taxonomy_version, reviewed)
validation_events(event_id PK, problem_id, business_id, event_type, date, proof_ref NOT NULL, notes)
businesses(business_id PK, name, url, location)
business_evidence(id PK, business_id, problem_id, source_type, url, span, kind CHECK(kind IN('direct','capability_signal')), date)
capabilities(cap_id PK, name, difficulty, nocode_possible, prereq)
my_skills(cap_id PK, level)
eval_runs(run_id, model, prompt_version, metrics_json, date)
```
**Canonical-problem fields: keep** id, name, definition, same_prototype_test, family, status, version, merged_into, aliases (separate table), relations (separate table).
**Derive from observations, never store by hand:** industries_observed, evidence_count, unique_*_count, frequency/time distributions, economic signals, existing_spending signals, first/last_seen, validation_stage, trend.
**Defer:** typical_workflow, common_causes/consequences (aggregate from `claims` once N is big), existing_solution_patterns, buyer_role/budget_owner (aggregate from observations), technical_complexity (from capability catalog join).
**Drop:** subfamily (until 100+ patterns), human_readable_name duplicate of canonical_name (keep one + `definition`), integration_requirements (= required_capabilities join).

## S7 Final extraction schema
See `schema_v3.json`. Field kinds:

| field | req | null | type | made by |
|---|---|---|---|---|
| post_type | Y | N | enum | LLM |
| is_relevant | Y | N | bool | LLM, code may set null |
| relevance_reason / span | Y | span Y | str | LLM, span code-checked |
| client_ask | N | Y | str | LLM |
| industry_stated | N | Y | str | LLM (only if stated) |
| problems[0..3] | Y | [] ok | array | LLM |
| role | Y | N | primary/secondary | LLM, code forces exactly 1 primary |
| problem_statement | Y | N | str | LLM, code rejects solution names |
| workflow_object, pain_type | Y | N | enum | LLM |
| recurrence{level,span} | Y | span Y | enum | LLM, span code-checked |
| freq/time/impact{value,span} | N | Y | obj | LLM, code checks span + numbers in source |
| derived_hours_per_week | N | Y | num | **code** (marked derived) |
| causes[], consequences[] | N | [] ok | claim[] | LLM, observation spans code-checked |
| current_handler, hiring_type, spending_observed, buyer_intent_observed | Y | N | enum | LLM |
| buyer_role{role,span} | N | Y | obj | LLM |
| automation_fit | Y | N | enum | LLM, code caps |
| automation_inappropriate | Y | N | bool | LLM |
| solution_modes, required_capabilities | N | [] ok | enum[] | LLM, catalog-checked |
| missing_info | N | [] ok | str[] | LLM |
| pattern{existing_id/proposed_name} | Y | N | obj | LLM, code checks vocab |
| evidence_items | Y | N | array | LLM, spans code-checked |
| evidence_grade, scores{...}, 4 confidences, needs_review | - | - | - | **code only** |

LLM never emits: confidence, urgency, any number not in source, any score.

## S8 / S9 Prompt + template
See `prompts_v3.py`. Prompt design: silent ordered reasoning, verbatim-span contract, solution-vs-problem rule, split-only-independent rule, 4-way hiring_type, all derivation delegated to code. ~900 words. `COMPARE_PROMPT` for normalization mid-band.

## S10 Deterministic validation
Implemented in `validate.py: validate_post`. Checks:
- schema valid (jsonschema in extract.py, retry once on failure)
- post_type/enum validity; unknown value -> null + error
- span present AND verbatim in normalized source, for: relevance, recurrence, freq/time/impact, buyer_role, evidence_items, observation-causes/consequences
- vague words ("often", "a lot") dropped as unquantified
- every number inside freq/time/impact value must appear in source
- problem_statement naming solution (chatbot, AI agent, "need a CRM") -> error, review
- exactly 1 primary, max 3 problems
- pattern id in vocabulary, capability ids in catalog
- is_relevant true with 0 supported problems -> null, review
- evidence_grade A-D from verified spans + quantified fields (code)
- caps: grade C/D cannot hold fit=high; inappropriate -> fit<=low
- to add in extract.py (not written): jsonschema validation, body truncation flag, impossible combos (spending=hiring_human with hiring_type=new_capability -> flag), snake_case enforcement on proposed names, duplicate-pattern near-synonym check (embedding).

## S11 Canonical normalization
**Identity** = `(workflow_object, pain_type)` + definition + `same_prototype_test`. Facets are extracted; label chosen from vocabulary. "manual" = mode, never in a name. Your 3 invoice labels all become `invoice_chasing_manual` family only if text is about chasing; invoice *creation* is a different pattern (different prototype).

**Relations** (store as edges, never merge on these):
- SAME: one first prototype fixes both. Merge.
- RELATED: overlap, different fix. Edge `related`.
- SUBPROBLEM: B part of A. Edge, B keeps own id.
- CAUSE / CONSEQUENCE: edge. Never a pattern of its own unless it recurs as independent workflow.
- SOLUTION: not a problem. Reject; extract the problem it implies, or null.
Example: "slow lead response" vs "manual lead follow-up": slow response = *pain* (delay), manual follow-up = *mode*. Under facets both = (lead, delay): SAME. "manual lead qualification" = (lead, volume/inconsistency): RELATED, not merged.

**Pipeline**
1. Extract picks id from vocabulary, or proposes new (LLM).
2. Embed `problem_statement` (workflow-level text only). Rank against canonical definitions + aliases. Embedding ranks; never decides.
3. Bands (GUESS; calibrate on 100 labeled pairs):
   - LLM id == top-1 and sim >= 0.80 -> auto assign.
   - Proposed-new and nearest sim >= 0.75 -> likely synonym -> `COMPARE_PROMPT` -> human if not `different`.
   - 0.65-0.80 or LLM/embed disagree -> `COMPARE_PROMPT` -> human queue.
   - Proposed-new and sim < 0.65 -> new-candidate -> human approves.
4. Only `relation=same` + `same_prototype=true` + human OK merges.
5. Merge: set `merged_into`, bump `taxonomy_version`, re-run assignments. Never mutate history.
6. Split trigger: members of one pattern cluster by differing `required_capabilities`/`solution_modes`, or two buyer roles. Human decides.
7. Monthly: list patterns with <3 members after 3 months -> archive candidates; list pairs sim>0.8 -> merge candidates.

Seed vocabulary: 20-25 hand-made patterns from your first 100 real posts. No embeddings until about 500 observations. Before then, human review of all new/mid cases is cheaper than embedding infra.

## S12 Occurrence + market-pattern engine
`scoring.occurrence_profile(rows)` (rows = observations joined to posts):
- raw_occurrences: rows after removing `dup_of`.
- unique_posts: distinct post_id.
- unique_clients: distinct non-null `client_key`. Unknown-client posts counted in `unknown_client_posts`, never as unique.
- unique_industries: distinct `industry_stated` (stated only).
- unique_sources: distinct source_type.
- months_span: distinct YYYY-MM.
- clients_grade_AB / clients_labor / clients_spending: distinct clients meeting each.
- market_repetition: strong >=10 clients, >=2 industries, >=2 months; moderate >=5 clients; weak >=2 (HEURISTIC).
- trend: `insufficient_data` unless >=3 months AND >=30 unique clients AND >=5 clients in each of >=3 months. Then growing/stable/declining by half-split mean (ratio 1.5). Otherwise say nothing.
- existing_labor_signal / spending: from clients counts above.
**Dedup rules:** exact sha1 -> dup. Shingle Jaccard >=0.85 same client -> dup. Same text different client -> `copied_template_flag` (agency templates, common): keep both rows, count once toward unique_clients via template group. Same client within 30 days, title sim >=0.9 -> dup.
`client_key`: Upwork gives client history; if you can't get a stable id, hash (location + member-since + total-spent) and mark `client_key_quality=weak`. If impossible, don't claim unique clients; claim unique posts only.

## S13 Evidence engine
Every claim = row in `evidence_items`/`claims` with `field`, `span`, `kind`, `span_verified`. Query "why this problem?" = join obs -> evidence_items where span_verified=1. Rules:
- EXPLICIT = verbatim span supports value. INFERENCE = reading. Both stored, labeled.
- Unverified span -> field nulled in code, error logged. LLM never gets the last word.
- DERIVED = code arithmetic on explicit values (`derived_hours_per_week`). Stored in separate column, flagged. "Probably costs thousands" has no home in schema, so it cannot exist.
- Evidence strength: strong = verbatim number tied to the workflow; moderate = verbatim qualitative; weak = inference-only. Grade A-D is code.

## S14 Scoring engine (independent ordinal dimensions, no composite)
Implemented in `scoring.py`. All values none/weak/moderate/strong unless noted. Thresholds marked HEURISTIC; recalibrate against benchmark.
- recurrence_strength: strong = freq with digit; moderate = cadence word or explicit recurrence span; weak = implied wording; none.
- time_cost_signal: from derived hours/week: strong >=5, moderate 1-5, weak <1 or unparseable.
- business_impact_signal: strong = number + money/loss wording; moderate = number only; weak = qualitative stated; none.
- existing_labor_signal: strong = replacing_labor + hiring_human; moderate = employee/assistant/contractor does it; weak = owner does it.
- existing_spending_signal: strong = stated budget for fix; moderate = paying tool / hiring human.
- urgency_signal: strong = quantified impact + recurrence >=moderate; never from posting date.
- automation_fit: LLM, then code caps (grade C/D cannot be high).
- integration_complexity: sum of capability difficulty (low <=6, medium <=12, high).
- buyer_clarity: strong = role+span+stated budget; moderate = role+span.
- problem_strength: not a stored dimension. = read recurrence + impact + time together. Collapsing them hides which part is missing.
- market_repetition, validation_level: pattern-level, S12/S15.
**Four confidences** (ordinal low/medium/high, code):
- extraction_confidence: 0 validation errors = high; 1 = medium; >=2 = low.
- evidence_confidence: grade A=high, B=medium, C/D=low.
- problem_confidence: high only if grade A/B, recurrence >=moderate, and (time >=moderate or impact >=moderate). Low if grade D or recurrence none/weak. Non-labor post without full quantification capped at medium.
- normalization_confidence: human-approved = high; auto sim>=0.85 = high; auto <0.85 = medium; pending = low.
No numeric 0.87. Ordinal avoids false precision. No composite score, because weights would be unfounded guesses. If you later need a ranking, sort lexicographically: validation_level, clients_spending, unique_clients, evidence grades. Sort key, not score.

## S15 Validation ladder (L0-L13, improved)
Your 14 levels kept, with proof rules. L1-L5 are per-observation levels; a PATTERN reaches Lk only if >=3 independent clients reach Lk (HEURISTIC).
| lvl | name | qualifies | does NOT qualify | next action |
|---|---|---|---|---|
| L0 | weak | generic/AI-request | any "I want AI" | ignore/log |
| L1 | mentioned | verbatim problem span | industry assumption | watch |
| L2 | recurring | cadence/volume/standing role | one-off task | quantify |
| L3 | time/freq quantified | span with number | "often/a lot" | find impact |
| L4 | consequence | stated loss/delay/error | inferred cost | find spending |
| L5 | labor/spending | hiring human, tool paid, budget stated | big client budget alone | find repetition |
| L6 | independent repetition | >=5 unique clients, >=2 months (HEURISTIC) | one client reposting, copied templates | find reachable businesses |
| L7 | target business evidence | direct span from a real reachable business | industry membership, website form (capability only) | manual conversation |
| L8 | conversation confirms | dated notes; owner describes pain in own words | "interesting", "sounds good" | show specific solution |
| L9 | solution interest | reacts to a specific solution, asks price/next step | compliments | propose pilot |
| L10 | free pilot | ran on real workflow, measured before/after | demo on fake data | ask for payment |
| L11 | paid pilot | money received (invoice id) | verbal "we'd pay" | repeat with others |
| L12 | repeated paid | >=3 distinct paying customers, similar config | 3 custom builds | measure setup time |
| L13 | productize | setup time per customer dropping, config stable | high customization | decide MicroSaaS vs service |
Rules: L8+ require `proof_ref`. Level is derived, never set by hand. Direct-referral shortcuts allowed, tagged `sourced_outside_engine`. Frequency on Upwork alone caps at L6. "Validated" is allowed only at L9+.

## S16 Business matching (Stage 4, per ONE pattern)
- Business types of evidence: `direct` (their own post/review text/statement about the problem, with URL+span+date) vs `capability_signal` (has enquiry form, uses WhatsApp). Capability = can use a fix, not has the pain.
- Industry never evidence.
- Match record: business -> evidence rows -> pattern -> reason.
- match_confidence (code): low = 1 direct; medium = >=2 independent direct (different sources/dates); high NOT allowed before a conversation (L8).
- Review-based evidence: need >=2 independent mentions about the specific workflow, because a customer complaint is not owner willingness to pay.
- Output = research brief (business, pattern, direct evidence list, capability signals labeled "not pain evidence", unknowns, one validation question, possible solution mode, human decision field).
- Legit public sources only. Respect site ToS. No scraping Upwork.

## S17 Human review (exact points)
1. New pattern approval, every merge/split.
2. `needs_review` rows (errors, grade D relevant, null relevance).
3. Normalization mid-band + disagreement.
4. Benchmark labeling (you, twice).
5. Any pattern crossing L6 (confirm reachable market).
6. Every business match brief.
7. Every outreach, every message.
8. Any L8+ event entry (attach proof).
9. Build/no-build per pattern.
Budget: 30 min/week queue. If queue > budget, cut volume, not review quality.

## S18 Learning engine
Inputs: pattern -> `required_capabilities`/optional (human-written once per pattern from top observations; LLM suggestion accepted per pattern) + `capabilities` (difficulty, nocode, prereq) + `my_skills`.
```python
def learning_plan(pattern):
    req = pattern_caps[pattern].required
    gaps = [c for c in req if my_skills.get(c, "none") != "can_build"]
    order = topo_sort(gaps, prereq)
    d = sum(cap[c].difficulty for c in gaps)
    scope = "small" if d <= 6 else "medium" if d <= 12 else "large"
    return {"learn_now": order, "nocode_first": [c for c in order if cap[c].nocode_possible],
            "defer": pattern_caps[pattern].optional, "not_needed_yet": ALL_CAPS - set(req) - set(optional),
            "done_when": acceptance_test(pattern)}
```
Run only for patterns at L6+. Acceptance test = concrete, e.g. "form entry -> WhatsApp message + sheet row in <60s, 10 runs, 0 failures". Learning stops there. Example (lead follow-up): webhooks, one messaging API, Sheets/CRM API, one workflow tool, structured LLM output only if drafting messages. Not needed: Docker, agents, vector DB, SaaS stack.

## S19 Build engine (rule table, code)
- DO NOT BUILD YET: level < L6, or mostly grade C/D, or automation_inappropriate.
- MANUAL SERVICE: L7-L8; you do the workflow by hand for 1 business.
- CONCIERGE MVP: L8-L9; spreadsheet + messaging, you operate it.
- NO-CODE/LOW-CODE AUTOMATION: L9 + complexity low/medium + capabilities all nocode_possible.
- SMALL CUSTOM SCRIPT: L9-L10, a step nocode can't do.
- PRODUCTIZED SERVICE: L11, >=2 paying, same config.
- INTERNAL TOOL: L11+, you hit repeated manual pain operating it.
- MICRO-SaaS: L12 only if config standard, setup time falling, integrations few, support burden low.
- FULL SOFTWARE PRODUCT: not before >=10 paying customers (HEURISTIC), still no.
Frequent pain alone never = SaaS.

## S20 Evaluation / benchmark
See `../pde_v2/EVAL.md` (base). Extensions:
- Size: 200 minimum now (not 150), 500 target. Interval at n=200 ~ +-7 points: treat smaller diffs as noise.
- Composition (real posts only): 30% clear problems, 15% subtle, 25% irrelevant, 10% vague/AI-hype, 5% spam/template, 5% duplicate/repost, 5% long, 5% adversarial.
- Labeling: label BEFORE model run. Two passes a week apart (kappa on 40). Second labeler (friend) on 40 for inter-rater.
- Per-task labels: relevance, post_type, spans support (y/n each), problem_statement acceptable, pattern, hiring_type, fit, number of problems.
- Metrics: relevance P/R/FP/FN; span-verbatim rate (code); unsupported-inference rate (human audit 50); problem accuracy; hiring_type accuracy; multi-problem F1 (match problems by workflow_object+pain_type); JSON validity first-try/after retry; schema compliance; 3-run field agreement; pairwise same/different on 150 pairs -> merge precision/recall, false merge, false split; duplicate detector P/R on 50 seeded dup pairs.
- Gate: no prompt/model change ships unless frozen benchmark rerun shows no regression on the regression subset.
- Grow: every production error -> benchmark.

## S21 Adversarial tests
`adversarial_tests.json`: 20 cases with expected behavior (all your 20 categories). Run as unit tests alongside benchmark. Cases 12-13 test dedupe (code, no LLM). Case 10 tests multi-problem split. Cases 17-18 test cause/consequence not becoming problems. Case 11 tests solution-as-problem rejection in code.
Note: expected values are targets, not model output. Run them and record actual.

## S22 Failure modes
| mode | why | danger | detect | prevent | fallback |
|---|---|---|---|---|---|
| hallucinated problem | model completes plausible story | high | span not found in code | verbatim-span contract, null on miss | drop field, review |
| over-extraction | splits detail into problems | med | >3 problems, per-step splits in benchmark | split-only-independent rule, cap 3 | keep primary |
| under-extraction | drops passing mention | med | benchmark recall on subtle set | recall-first relevance prompt | review grade-D relevant |
| solution bias | "chatbot" -> problem=chatbot | high | regex on problem_statement + adv11 | client_ask separate | reject, review |
| industry stereotyping | "clinic -> scheduling" | high | adv tests, business matching rule | banned in prompt, direct-evidence rule | no match |
| label fragmentation | free-text labels | high | pattern count vs benchmark clusters | controlled vocab, facets | merge queue |
| false merges | embedding conflates | high | pairwise precision | same_prototype test, human | split queue |
| false splits | near-synonyms | med | pairs sim>0.8 monthly | alias table, review | merge queue |
| duplicate inflation | repost/copy | high | dup_of rate | sha1, shingle, client+30d | count once |
| client inflation | 1 client many posts | high | clients vs posts ratio | count clients | report both |
| source bias | Upwork sample | high | state source in every report | add 2nd source, interviews | cap level L6 |
| synthetic-data bias | clean fake data | high | gap synthetic vs real metrics | real benchmark | ignore synthetic counts |
| confidence inflation | LLM says High | high | High-rate vs evidence grade | code-derived ordinals | cap |
| unsupported economics | invented $ | high | numbers-not-in-source check | derived only in code | null |
| prompt drift | edits accumulate | med | benchmark diff | version, frozen eval | rollback |
| model drift | provider update | med | scheduled benchmark rerun, pin model id | pin version | rerun, rollback |
| schema drift | added fields ad hoc | med | schema_version in rows | versioned schema, migrations | reject unknown |
| stale taxonomy | vocab ages | med | proposed-new rate rising | monthly review | review burst |
| false urgency | "ASAP", recent date | med | urgency only from impact+recurrence | code rule | none |
| multi-problem confusion | 2 workflows merged | med | adv10 | primary/secondary rules | review |

## S23 Roadmap
- **Stage 1 (now, 1-2 wk) CSV+Python+LLM:** fix P0 bugs; wire `validate_post` + `scoring`; 200-post real benchmark; 20 seed patterns. Trigger out: benchmark metrics measured.
- **Stage 2 (wk 3-6) SQLite:** tables above; clients-not-posts counts; weekly review. Trigger: >300 observations or CSV merges painful. NOT yet: embeddings, UI.
- **Stage 3 (only if >500 obs) embeddings:** local small embedding model; candidate ranking only. Trigger: review queue > 30 min/week because of label volume.
- **Stage 4 (only if one pattern at L6) business evidence:** one pattern, 20-30 businesses, manual evidence logging. NOT yet: scraping at scale.
- **Stage 5 (only if >50 business records) review UI:** SQLite browser or spreadsheet first.
- **Stage 6 (production):** probably never needed for a solo builder. Skip.
Kill rule: after 300 real posts, if nothing reaches L6 in a segment you can reach, stop growing the engine; go to interviews.

## S24 What NOT to build (NOT NOW / WHY / BUILD WHEN)
- Dashboard: NOT NOW; spreadsheet + SQL enough; when queue/records exceed spreadsheet.
- SaaS UI: NOT NOW; no L11 evidence; when L12.
- Automated outreach: NOT NOW; inferred pain, reputation risk; never auto; at most draft-assist after L7.
- Agents/multi-agent: NOT NOW; one extraction call + code does the job; when a task needs tool loops proven by benchmark.
- CRM: NOT NOW; spreadsheet; when >50 active conversations.
- Postgres/Docker/queues: NOT NOW; SQLite single file; when concurrent writers.
- Forecasting: NOT NOW; data too small; trend rule in S12.
- Second source scraper: NOT NOW; first source unevaluated; after benchmark pass.
- Composite score: NEVER (false precision).
- Per-post learning plans: NOT NOW; only for L6+ patterns.
- Auto taxonomy generation: NOT NOW; human approves patterns; when >1000 obs and mid-band queue unmanageable.
- Fancy prompt chains: NOT NOW; benchmark first.

## S25 Model strategy
- Extraction: one structured LLM call per post. Current `gpt-oss-120b` via Groq is plausible; I cannot verify its quality for you. Test it against `gpt-oss-20b` and one other on YOUR benchmark; use the cheapest that meets your recall/hallucination bar. Don't pick by size.
- Reasoning/comparison (normalization mid-band): small model fine, volume low.
- Embeddings: small local model; free; only ranks candidates; only at Stage 3.
- Deterministic: validation, scoring, dedup, aggregation, levels, build rules.
- DB: counts, profiles, trend.
- Human: everything in S17.
- Pin model id + prompt_version + schema_version in every row. `reasoning_format` may not be accepted by gpt-oss on Groq; test and remove if rejected. Use `max_completion_tokens` instead of `max_tokens`.

## S26 Six-month plan (every learn step tied to a validation task)
- **Month 1 DISCOVER+VALIDATE:** fix P0 bugs, rotate key. Collect 200 real posts, label them. Run v3. Read results by hand. Parallel: 5 conversations with local owners (gyms/D2C/service) about lead follow-up and order handling, no pitch. Learn only: SQLite basics, jsonschema. Gate: benchmark metrics known; >=1 pattern has recurring evidence.
- **Month 2 VALIDATE:** grow to 300 posts. Pick top 1-2 patterns. 15 more conversations. Log in `validation_events` with proof. Learn: just enough to hand-run the workflow (spreadsheet + WhatsApp). Gate: >=3 owners confirm pain in own words (L8). If none: switch pattern or segment.
- **Month 3 LEARN+BUILD:** concierge: run the workflow for 1-2 free pilot businesses manually. Learn only gaps from `learning_plan` (webhooks, one messaging API, one workflow tool). Gate: measured before/after on one real business (L10).
- **Month 4 BUILD+TEST+SELL:** automate the manual steps that cost you the most time. Ask for money for continued service: paid pilot (L11). Gate: first invoice or stop and revisit the problem.
- **Month 5 SELL+REPEAT:** repeat with 2-3 more similar businesses. Track setup time per customer. Gate: >=2 paying on similar configuration.
- **Month 6 REPEAT or DECIDE:** if >=3 paying (L12): package as productized service; productization question only after setup time falls. If <2 paying: written post-mortem; change problem/segment using the engine's next pattern.
Never a "learn for a month" block. Engine time target: <=15% of weekly time.

## S27 Decision gates (stop/go)
- level < L5 -> do not build; collect.
- L5, repetition < 5 clients -> wait/collect, don't theorize.
- L6, no reachable business evidence -> find 20 businesses, log direct evidence.
- L7 -> contact manually (human drafts).
- L8 fail (nobody confirms) after 10 conversations -> problem is not painful where you can reach; drop segment.
- L9 no interest in the specific solution after 5 showings -> change solution mode, not problem, once.
- L10 no measurable improvement -> stop.
- L11 nobody pays after 3 asks -> not a business; revisit problem.
- L12 repeated paid -> productize question.
Rule: two consecutive failed gates on the same pattern = archive it.

## S28 Priorities
**P0 (now):** rotate Groq key; remove `F:\` hardcode (env `OUTPUT_DIR`); relevance coerce string "false"; ERROR rows keyed uniquely and not carried; filtered CSV append/carry; delete synthetic state files before real run; wire `validate_post`/`scoring`; client_key; 200-post real benchmark.
**P1:** SQLite store; seed vocabulary + vocabulary-in-prompt; pairwise normalization labels; weekly review; 10+ owner conversations in parallel; README rewrite.
**P2:** embeddings; business evidence tables; research brief generator; trend (needs data).
**P3 (do not build):** dashboard, outreach automation, agents, SaaS, forecasting, composite score, second source.

## S29 Final assessment
Q: does this materially improve a solo builder's ability to find real, recurring, economically meaningful, automatable problems and turn them into validated opportunities?

Answer from evidence on hand: **partly, and unproven.**
- Proven: plumbing runs; relevance gate rejected 7/7 junk on 30 clean synthetic posts; label fragmentation observed (3 families split); count inflation risk confirmed by design.
- NOT proven: any market claim. Zero real posts processed. Synthetic ceiling effects (20/23 High) show the tool agrees with itself on easy data.
- What it improves: speed of reading hundreds of posts, traceability (span-checked evidence), de-duplicated counting, a clear "insufficient_evidence" outcome. These cut wasted *reading*.
- What it cannot improve: whether your reachable customers pay. Only conversations and paid pilots do that (L8-L12). The engine reaches L6 at most.
- Net: worth ~10-15% of your time for 6 weeks, then judge by one measurable outcome: did it surface a pattern that survived 10 owner conversations? If yes, keep. If no, drop the engine and keep interviewing. The honest risk is spending month 1-3 perfecting the engine while no one has said "I'd pay."
