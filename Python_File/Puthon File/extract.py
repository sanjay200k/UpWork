#!/usr/bin/env python3
"""
Upwork Problem-Discovery Extractor
-----------------------------------
Implements Build Order Steps 1-2 of the Problem Discovery & AI/Automation Lead
Generation plan:
  Step 1: problem_library schema (provenance, hiring_type, task_frequency,
          problem_occurrence, etc.)
  Step 2: AI extraction prompt with hard gate + hiring_type weighting

Reads a CSV of raw Upwork job posts (job_title, job_description, job_url,
client_payment_verified, client_total_spend, date_posted), sends each row to
an LLM for problem extraction, applies a hard-gate confidence cap IN CODE
(never trust the model alone for this), adds code-generated provenance
fields, tracks problem_occurrence across runs over time, and writes a
problem_library-shaped CSV.

This script does NOT search or scrape Upwork - per the plan (Section 4),
Upwork's job-search API requires approval and general scraping violates
Upwork's ToS. Job posts must be collected manually (or via an approved API)
and placed in the input CSV before running this script.

Usage:
    python3 extract.py
    (it will prompt you for the input CSV path)

    or

    python3 extract.py /path/to/input.csv
    python3 extract.py /path/to/input.csv /path/to/output.csv

Requires:
    GROQ_API_KEY environment variable set (or config_example.py / config.py
    next to this script with GROQ_API_KEY set).
"""

import os
import re
import sys
import json
import time
import hashlib
import datetime
import csv as csv_module

import pandas as pd
from groq import Groq, APIError, APIStatusError

from prompts import SYSTEM_PROMPT, USER_TEMPLATE

# Groq deprecated llama-3.3-70b-versatile on the free/developer tier.
# openai/gpt-oss-120b is their recommended replacement - strong quality,
# fast inference. Swap for another model name from console.groq.com/docs/models
# if you want (e.g. openai/gpt-oss-20b for a faster/cheaper run).
MODEL = "openai/gpt-oss-120b"
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5

# gpt-oss is a reasoning model: it burns tokens on internal "thinking" before
# writing the final JSON answer, and those thinking tokens count against
# max_tokens. Give it a big budget and hide/limit the reasoning so it doesn't
# eat the space meant for the actual answer.
MAX_TOKENS = 4000
REASONING_EFFORT = "low"       # low | medium | high
REASONING_FORMAT = "hidden"    # hidden | parsed | raw

# Where enriched CSVs land by default (used when no output path is given
# on the command line). Change this if you move the project folder.
DEFAULT_OUTPUT_DIR = r"F:\AI automation\Up_work_Leads\main Project\UpWork\Python_File\OutPutCsv"

# Where the running problem_occurrence counts are stored, so this pattern-
# frequency signal accumulates across every batch you run over time, not
# just within a single 15-row file (a single small batch can't tell you
# "17 posts this month" on its own).
OCCURRENCE_STORE_PATH = os.path.join(DEFAULT_OUTPUT_DIR, "problem_occurrence_counts.json")

# Rate limits (429s) are different from a bad/truncated response: they mean
# "you've used up your tokens-per-minute quota, slow down" rather than
# "something is wrong with this request." For those specifically, wait much
# longer and escalate each time, since retrying quickly just gets another 429.
RATE_LIMIT_BACKOFF_SECONDS = [200, 300, 450, 600]  # 1st, 2nd, 3rd, 4th+ rate-limit hit
RATE_LIMIT_MAX_RETRIES = len(RATE_LIMIT_BACKOFF_SECONDS)

REQUIRED_INPUT_COLUMNS = [
    "job_title",
    "job_description",
    "job_url",
    "client_payment_verified",
    "client_total_spend",
    "date_posted",
]

# Fields the LLM is responsible for producing (see prompts.py SYSTEM_PROMPT).
LLM_FIELDS = [
    "evidence",
    "extracted_problem",
    "industry",
    "problem_pattern",
    "hiring_type",
    "task_frequency",
    "time_cost",
    "business_impact",
    "current_solution",
    "tools_involved",
    "urgency",
    "problem_confidence",
    "confidence_reason",
    "automation_fit",
    "solution_type",
]

# Fields generated in code (provenance + cross-batch signals), never by the
# LLM - these should never be hallucinated.
CODE_FIELDS = [
    "id",
    "source",
    "source_url",
    "source_job_id",
    "date_collected",
    "client_spend_signal",
    "problem_occurrence",
]

# Full problem_library schema, in the order it should appear in the output.
ANALYSIS_COLUMNS = [
    "id",
    "source",
    "source_url",
    "source_job_id",
    "date_collected",
    "evidence",
    "extracted_problem",
    "industry",
    "problem_pattern",
    "hiring_type",
    "task_frequency",
    "problem_occurrence",
    "time_cost",
    "business_impact",
    "current_solution",
    "tools_involved",
    "urgency",
    "problem_confidence",
    "confidence_reason",
    "automation_fit",
    "solution_type",
    "client_spend_signal",
]

# Fields whose presence (a real, quantified value) counts as "quantified
# evidence" for the hard gate. Plain non-empty text isn't enough - we also
# require at least one digit, since "a lot" or "frequently" is not a
# quantified figure.
QUANTIFICATION_FIELDS = ["task_frequency", "time_cost", "business_impact"]

HIRING_TYPE_LABOR = "replacing human labor"
HIRING_TYPE_CURIOSITY = "exploring AI/tech upgrade"
CONFIDENCE_ORDER = {"Low": 0, "Medium": 1, "High": 2}


def is_empty(value) -> bool:
    if value is None:
        return True
    s = str(value).strip().lower()
    return s in ("", "null", "none", "n/a", "na")


def is_quantified(value) -> bool:
    """A field counts as quantified evidence only if it has real text AND
    contains at least one digit (a number, frequency, or amount)."""
    if is_empty(value):
        return False
    return bool(re.search(r"\d", str(value)))


def get_client() -> Groq:
    api_key = os.environ.get("GROQ_API_KEY")

    if not api_key:
        # Fall back to config_example.py / config.py sitting next to this
        # script, so you don't have to set an env var every session.
        for module_name in ("config", "config_example"):
            try:
                config = __import__(module_name)
                api_key = getattr(config, "GROQ_API_KEY", None)
                if api_key:
                    break
            except ImportError:
                continue

    if not api_key or api_key == "gsk_paste_your_real_key_here":
        print("ERROR: no Groq API key found.")
        print("Either:")
        print("  1) set it for this session:  $env:GROQ_API_KEY=\"gsk_your_real_key\"")
        print("  2) or open config_example.py and paste your real key in there")
        print("Get a key at: https://console.groq.com/keys")
        sys.exit(1)

    return Groq(api_key=api_key)


def _print_raw_error_body(e):
    """Best-effort dump of the raw HTTP response so a failure is actually
    debuggable instead of showing an empty failed_generation."""
    try:
        body = e.response.text
        if body:
            print(f"  Raw response body (first 1000 chars): {body[:1000]}")
    except Exception:
        pass


def call_model(client: Groq, row: dict) -> dict:
    user_message = USER_TEMPLATE.format(
        job_title=row.get("job_title", ""),
        job_description=row.get("job_description", ""),
        client_payment_verified=row.get("client_payment_verified", ""),
        client_total_spend=row.get("client_total_spend", ""),
        date_posted=row.get("date_posted", ""),
    )

    last_error = None
    rate_limit_hits = 0  # separate counter, does not consume normal MAX_RETRIES

    attempt = 1
    while attempt <= MAX_RETRIES:
        try:
            kwargs = dict(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                temperature=0.2,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
            )

            # reasoning_effort / reasoning_format are gpt-oss-specific params.
            # Older groq SDK versions may not accept them - if you hit a
            # TypeError about an unexpected keyword argument, upgrade the
            # groq package (`pip install -U groq`) or just delete these two
            # lines and rely on the higher MAX_TOKENS alone.
            if REASONING_EFFORT:
                kwargs["reasoning_effort"] = REASONING_EFFORT
            if REASONING_FORMAT:
                kwargs["reasoning_format"] = REASONING_FORMAT

            response = client.chat.completions.create(**kwargs)
            text = response.choices[0].message.content.strip()

            # Strip accidental markdown fences if the model adds them anyway
            if text.startswith("```"):
                text = text.strip("`")
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()

            if not text:
                raise ValueError("Model returned empty content (likely truncated / all tokens spent on reasoning)")

            return json.loads(text)

        except (APIError, APIStatusError) as e:
            last_error = e
            status_code = getattr(e, "status_code", None)

            if status_code == 429:
                # Rate limit: don't burn a normal retry on this. Wait a long,
                # escalating amount of time instead, since retrying quickly
                # will just get another 429.
                idx = min(rate_limit_hits, RATE_LIMIT_MAX_RETRIES - 1)
                wait_seconds = RATE_LIMIT_BACKOFF_SECONDS[idx]
                rate_limit_hits += 1
                print(f"  Rate limited (429). Waiting {wait_seconds}s before retrying "
                      f"(rate-limit hit #{rate_limit_hits})...")
                _print_raw_error_body(e)
                time.sleep(wait_seconds)

                if rate_limit_hits > RATE_LIMIT_MAX_RETRIES:
                    raise RuntimeError(
                        f"Still rate limited after {rate_limit_hits} escalating waits: {e}"
                    )
                continue  # retry this same row without counting it against attempt

            print(f"  API error on attempt {attempt}/{MAX_RETRIES}: {e}")
            _print_raw_error_body(e)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
        except (json.JSONDecodeError, ValueError) as e:
            last_error = e
            print(f"  JSON parse error on attempt {attempt}/{MAX_RETRIES}: {e}")
            time.sleep(RETRY_BACKOFF_SECONDS)

        attempt += 1

    raise RuntimeError(f"Failed after {MAX_RETRIES} attempts: {last_error}")


def apply_hard_gate(analysis: dict) -> dict:
    """Enforce, IN CODE, the rules from Section 4 of the plan:
      - No quantified task_frequency / time_cost / business_impact ->
        problem_confidence auto-caps at Low.
      - hiring_type weighting: "exploring AI/tech upgrade" posts default to
        a lower confidence ceiling than "replacing human labor" posts unless
        evidence is fully quantified, per Section 4's "lower default
        confidence unless evidence is quantified" rule.
    This never upgrades the model's own confidence - only downgrades it.
    """
    quantified_count = sum(1 for f in QUANTIFICATION_FIELDS if is_quantified(analysis.get(f)))
    fully_quantified = quantified_count == len(QUANTIFICATION_FIELDS)
    any_quantified = quantified_count > 0

    hiring_type = analysis.get("hiring_type")
    confidence = analysis.get("problem_confidence")
    if confidence not in CONFIDENCE_ORDER:
        confidence = "Low"  # unrecognized/missing value - treat as worst case

    def cap(value, ceiling):
        if CONFIDENCE_ORDER.get(value, 0) > CONFIDENCE_ORDER[ceiling]:
            return ceiling
        return value

    # Base hard gate: zero quantified evidence at all -> Low, no exceptions.
    if not any_quantified:
        confidence = cap(confidence, "Low")
    # hiring_type weighting: curiosity/exploratory posts need FULL
    # quantification to earn anything above Medium; partial quantification
    # only earns Medium at most. "Replacing human labor" posts are proven,
    # funded pain, so partial quantification is enough to reach Medium/High
    # based on the model's own judgement.
    elif hiring_type == HIRING_TYPE_CURIOSITY and not fully_quantified:
        confidence = cap(confidence, "Medium")

    analysis["problem_confidence"] = confidence

    # If confidence got capped to Low, automation_fit claims of "High" are
    # also suspect - the underlying problem itself is not well-established
    # yet, so don't let a strong automation_fit rating stand unchallenged.
    if confidence == "Low" and analysis.get("automation_fit") == "High":
        analysis["automation_fit"] = "Medium"

    return analysis


def make_source_job_id(job_url: str, job_title: str) -> str:
    """Try to pull Upwork's own job id out of the URL (e.g. the ~0123abc...
    segment). Falls back to a short stable hash of url+title so every row
    still gets a consistent, deterministic id even without a clean URL."""
    if job_url:
        match = re.search(r"~[0-9a-fA-F]{10,}", job_url)
        if match:
            return match.group(0)
        match = re.search(r"/jobs/([\w-]+)", job_url)
        if match:
            return match.group(1)
    basis = f"{job_url}|{job_title}".encode("utf-8", errors="ignore")
    return "h" + hashlib.sha1(basis).hexdigest()[:16]


def load_occurrence_store() -> dict:
    if os.path.isfile(OCCURRENCE_STORE_PATH):
        try:
            with open(OCCURRENCE_STORE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            print(f"  WARNING: could not read {OCCURRENCE_STORE_PATH}, starting fresh.")
    return {}


def save_occurrence_store(store: dict):
    os.makedirs(os.path.dirname(OCCURRENCE_STORE_PATH), exist_ok=True)
    with open(OCCURRENCE_STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2, sort_keys=True)


def normalize_problem_pattern(problem_pattern: str) -> str:
    """Normalize the model's problem_pattern label to lower_snake_case so
    minor formatting differences ("Lead Follow-Up Delay" vs
    "lead_follow_up_delay") don't fragment the count. This does NOT solve
    genuine semantic drift (the model calling the same real-world pattern
    two different names across posts) - that needs either a controlled
    vocabulary you maintain over time, or an embeddings-based similarity
    match once volume justifies the added complexity. This is the
    string-level normalization half of that fix."""
    if is_empty(problem_pattern):
        return "unclassified_problem"
    s = str(problem_pattern).strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "unclassified_problem"


def occurrence_key(problem_pattern: str) -> str:
    """Groups by the normalized problem_pattern itself - e.g.
    "lead_follow_up_delay" - so the count reflects how many Upwork posts
    have exhibited THIS problem, regardless of which industry it showed up
    in, per the plan's definition: 'how often this problem pattern appears
    across Upwork posts' (e.g. 17 relevant posts this month)."""
    return normalize_problem_pattern(problem_pattern)


def add_provenance_and_occurrence(analysis: dict, row: dict, occurrence_store: dict, row_index: int) -> dict:
    analysis["id"] = row_index
    analysis["source"] = "upwork"
    analysis["source_url"] = row.get("job_url", "")
    analysis["source_job_id"] = make_source_job_id(row.get("job_url", ""), row.get("job_title", ""))
    analysis["date_collected"] = datetime.date.today().isoformat()
    analysis["client_spend_signal"] = row.get("client_total_spend", "")

    # Normalize problem_pattern in place so the CSV column and the occurrence
    # key always agree (e.g. "Lead Follow-Up Delay" -> "lead_follow_up_delay").
    analysis["problem_pattern"] = normalize_problem_pattern(analysis.get("problem_pattern"))

    key = occurrence_key(analysis["problem_pattern"])
    occurrence_store[key] = occurrence_store.get(key, 0) + 1
    analysis["problem_occurrence"] = occurrence_store[key]

    return analysis


def normalize_for_csv(analysis: dict) -> dict:
    """solution_type comes back as a JSON array from the model - flatten it
    to a readable string for the CSV cell."""
    solution_type = analysis.get("solution_type")
    if isinstance(solution_type, list):
        analysis["solution_type"] = "; ".join(str(x) for x in solution_type)
    elif solution_type is None:
        analysis["solution_type"] = ""
    return analysis


def validate_row(input_row: dict) -> list:
    missing = [c for c in REQUIRED_INPUT_COLUMNS if c not in input_row]
    return missing


def print_summary(enriched_rows: list):
    total = len(enriched_rows)
    if total == 0:
        print("\nNo rows processed.")
        return

    high = sum(1 for r in enriched_rows if r.get("problem_confidence") == "High")
    medium = sum(1 for r in enriched_rows if r.get("problem_confidence") == "Medium")
    low = sum(1 for r in enriched_rows if r.get("problem_confidence") == "Low")

    labor = sum(1 for r in enriched_rows if r.get("hiring_type") == HIRING_TYPE_LABOR)
    curiosity = sum(1 for r in enriched_rows if r.get("hiring_type") == HIRING_TYPE_CURIOSITY)

    high_automation_fit = sum(1 for r in enriched_rows if r.get("automation_fit") == "High")

    print("\n" + "=" * 60)
    print("FINAL SUMMARY")
    print("=" * 60)
    print(f"Total posts analyzed:                 {total}")
    print(f"problem_confidence High / Medium / Low: {high} / {medium} / {low}")
    print(f"hiring_type - replacing human labor:    {labor}")
    print(f"hiring_type - exploring AI/tech upgrade: {curiosity}")
    print(f"automation_fit = High:                  {high_automation_fit}")

    print("\nTop rows by problem_confidence:")
    ranked = sorted(
        enriched_rows,
        key=lambda r: -CONFIDENCE_ORDER.get(r.get("problem_confidence"), 0),
    )
    for r in ranked[:10]:
        print(
            f"  [{r.get('problem_confidence', '?'):6s}] "
            f"({r.get('hiring_type', '?')}) "
            f"- {r.get('job_title', '')[:55]}"
        )
    print("=" * 60)


def main():
    args = sys.argv[1:]

    if len(args) >= 1:
        input_path = args[0]
    else:
        input_path = input("Enter the path to your input CSV: ").strip().strip('"')

    if not os.path.isfile(input_path):
        print(f"ERROR: file not found: {input_path}")
        sys.exit(1)

    if len(args) >= 2:
        output_path = args[1]
    else:
        base = os.path.splitext(os.path.basename(input_path))[0]
        # Avoid "..._enriched_enriched.csv" if you accidentally run this on
        # a file that's already an enriched output.
        while base.endswith("_enriched"):
            base = base[: -len("_enriched")]
        os.makedirs(DEFAULT_OUTPUT_DIR, exist_ok=True)
        output_path = os.path.join(DEFAULT_OUTPUT_DIR, f"{base}_enriched.csv")

    df = pd.read_csv(input_path, dtype=str).fillna("")

    # Guard against re-running on a file that's already been enriched (e.g.
    # you accidentally pointed this at an old output, or an output once
    # overwrote your input). Drop any prior analysis columns - including
    # pandas-style duplicate suffixes like problem_confidence.1, .2, etc. -
    # and redo the analysis fresh from the original job data.
    already_present = [
        c for c in df.columns
        if c in ANALYSIS_COLUMNS
        or any(c == f"{a}.{n}" for a in ANALYSIS_COLUMNS for n in range(1, 10))
    ]
    if already_present:
        print(f"NOTE: input already contains analysis columns {already_present} — "
              f"dropping them and re-analyzing fresh (this looks like an already-enriched file).")
        df = df.drop(columns=already_present)

    input_rows = df.to_dict(orient="records")

    missing_cols = validate_row(input_rows[0]) if input_rows else REQUIRED_INPUT_COLUMNS
    if missing_cols:
        print(f"ERROR: input CSV is missing required columns: {missing_cols}")
        print(f"Required columns: {REQUIRED_INPUT_COLUMNS}")
        sys.exit(1)

    client = get_client()
    occurrence_store = load_occurrence_store()

    enriched_rows = []
    total = len(input_rows)
    print(f"Processing {total} rows from {input_path} ...")

    for i, row in enumerate(input_rows, start=1):
        title_preview = str(row.get("job_title", ""))[:50]
        print(f"[{i}/{total}] {title_preview}")

        try:
            analysis = call_model(client, row)
            analysis = apply_hard_gate(analysis)
            analysis = add_provenance_and_occurrence(analysis, row, occurrence_store, i)
            analysis = normalize_for_csv(analysis)
        except Exception as e:
            print(f"  FAILED to analyze row {i}: {e}")
            analysis = {col: "ERROR" for col in ANALYSIS_COLUMNS}

        merged = dict(row)
        for col in ANALYSIS_COLUMNS:
            merged[col] = analysis.get(col, "")
        enriched_rows.append(merged)

        # Be polite to the API between calls
        time.sleep(0.5)

    save_occurrence_store(occurrence_store)

    fieldnames = REQUIRED_INPUT_COLUMNS + ANALYSIS_COLUMNS
    # Preserve any extra input columns beyond the required ones
    extra_cols = [c for c in df.columns if c not in REQUIRED_INPUT_COLUMNS]
    fieldnames = REQUIRED_INPUT_COLUMNS + extra_cols + ANALYSIS_COLUMNS

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv_module.DictWriter(f, fieldnames=fieldnames, quoting=csv_module.QUOTE_ALL)
        writer.writeheader()
        for r in enriched_rows:
            writer.writerow({k: r.get(k, "") for k in fieldnames})

    print(f"\nDone. problem_library CSV written to: {output_path}")
    print(f"Running problem_occurrence counts saved to: {OCCURRENCE_STORE_PATH}")
    print_summary(enriched_rows)


if __name__ == "__main__":
    main()