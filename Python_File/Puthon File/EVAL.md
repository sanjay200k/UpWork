# Evaluation methodology

## Benchmark (initial 150 REAL posts, labeled BEFORE running model)
50 relevant, 50 irrelevant, 20 vague, 20 subtle, 10 spam/duplicate.
Cover: obvious, subtle, irrelevant, vague, AI hype, duplicates, spam, thin, very long (>3000 words),
contradictory, automation-inappropriate, real-problem-low-fit.
Label per post: is_relevant, evidence spans, problem_statement ok (y/n), correct pattern, automation_fit, automation_inappropriate.
Self-agreement: relabel 30 items one week later; compute Cohen's kappa. Low kappa = fix guidelines first.

## Metrics
- Relevance precision = TP/(TP+FP); recall = TP/(TP+FN). Recall matters more early.
- Evidence: % spans verbatim (code); % spans human agrees support field (sample 40).
- Hallucination: unverified-span rate (code), number-not-in-source rate (code), human audit of unsupported inference labeled observation.
- Problem extraction: human y/n "text supports this statement".
- Normalization: 100 labeled pairs same/different; merge precision+recall; confusion between neighbor patterns.
- JSON validity: first-try %, after-retry %.
- Consistency: 3 runs per post, same settings, field agreement; 20 paraphrased posts, pattern stability.
- Version stability: frozen benchmark rerun on every prompt/model/schema/vocab change; diff vs previous; regression subset of past fixed errors.

## Noise
n=150 -> ~+-8 points at 95%. Differences under ~8 points = noise. Grow to 400+ by adding every production error.

## Rule
Change prompt only for error categories seen >=3 times. Log metrics per version in eval_runs.
Synthetic data = plumbing test only. Expect real numbers worse.
