"""EDIT THIS FILE. It holds the judgments only you can make: what you sell, what is worth your time,
and how much each scoring component counts. Everything marked HEURISTIC is a starting guess.
Calibrate it with outcomes.py once you have real applications (see README_v4.md)."""

# How well does each service category match what YOU deliver?  core | adjacent | out
SERVICE_FIT = {
    "workflow_automation": "core",
    "ai_implementation":   "core",
    "crm":                 "adjacent",
    "software_development":"adjacent",
    "web_development":     "adjacent",
    "data_analysis":       "adjacent",
    "customer_support":    "adjacent",
    "admin_support":       "out",
    "marketing":           "out",
    "sales":               "out",
    "design":              "out",
    "accounting":          "out",
    "research":            "out",
    "other":               "out",
}
FIT_POINTS = {"core": 100, "adjacent": 60, "out": 10}

# Smallest budget that is worth a proposal (same currency as the posts; Upwork = USD).
MIN_FIXED_BUDGET = 150.0     # HEURISTIC
MIN_HOURLY_RATE = 20.0       # HEURISTIC

# Component weights (renormalised over the components that have data).  HEURISTIC
WEIGHTS = {
    "buyer_intent":        0.25,
    "service_fit":         0.20,
    "problem_clarity":     0.20,
    "budget_evidence":     0.15,
    "client_signals":      0.10,
    "recency_competition": 0.10,
}

# Tier cut-offs on the 0-100 lead score.  HEURISTIC
TIERS = [("A", 75), ("B", 55), ("C", 35), ("D", 0)]

# Hard caps so a weak job can never rank in the top tiers.
CAP_BY_INTENT = {"research_inquiry": 54, "vague": 54, "unknown": 54, "exploratory": 74}
CAP_OUT_OF_FIT = 54
STALE_DAYS = 30             # older than this -> capped at tier C (probably filled)
