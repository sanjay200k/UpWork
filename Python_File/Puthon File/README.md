# Upwork Problem-Discovery Extractor

Reads a CSV of raw Upwork job posts and enriches each row with a full
problem-discovery analysis (root problem, scores, financial impact,
priority) using Claude.

## Input CSV format

Required columns (any extra columns are preserved and passed through):

```
job_title, job_description, job_url, client_payment_verified, client_total_spend, date_posted
```

## Option A: Run locally with Python

```bash
pip install -r requirements.txt
export GROQ_API_KEY=your-key-here
# get a key at https://console.groq.com/keys

python3 extract.py
# it will prompt: Enter the path to your input CSV:

# or pass the path directly:
python3 extract.py /path/to/input.csv
python3 extract.py /path/to/input.csv /path/to/output.csv
```

## Option B: Run with Docker (no local Python setup needed)

```bash
# Build the image once
docker build -t upwork-extractor .

# Put your input CSV in some folder, e.g. ~/upwork-data/input.csv
# Then run, mounting that folder into the container:
docker run --rm -it \
  -e GROQ_API_KEY=$GROQ_API_KEY \
  -v ~/upwork-data:/data \
  upwork-extractor /data/input.csv /data/output.csv
```

The enriched CSV will appear at `~/upwork-data/output.csv` on your host
machine.

## What it does

1. Reads each row of the input CSV.
2. Sends the job title/description to Claude with a strict problem-discovery
   prompt (root-problem extraction, scoring, evidence requirement, no
   invented numbers).
3. Applies a **hard gate in code** (not just in the prompt): if a post has
   no quantified business impact / time waste / frequency at all, the
   script forcibly caps `CONFIDENCE_SCORE` and `PROBLEM_SCORE`, downgrades
   `LEAD_TYPE` away from `HOT_PROBLEM`, and downgrades `FINAL_PRIORITY` away
   from `P1` — regardless of what the model returned. This guarantees the
   rule holds even if the model is inconsistent about self-enforcing it.
4. Writes an enriched CSV with all original columns preserved, plus the
   analysis columns appended after them.
5. Prints a summary (strong vs weak leads, average scores, top 10 ranked
   leads) to the terminal.

## Notes

- Retries API calls up to 3 times on transient errors.
- If a single row fails after all retries, its analysis columns are filled
  with `"ERROR"` so the run doesn't stop — you can re-run just that row later.
- Model used: `llama-3.3-70b-versatile` via Groq. Change the `MODEL` constant
  in `extract.py` if you want a different Groq-hosted model (e.g.
  `llama-3.1-8b-instant` for a faster/cheaper run).
