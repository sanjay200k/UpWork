"""Pattern assignment. Embeddings only rank candidates. Humans decide merges/splits/new."""
import re

T_AUTO, T_SYNONYM, T_NEW = 0.80, 0.75, 0.65   # GUESSES. Calibrate on ~100 labeled pairs.

def snake(s):
    s = re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")
    return s or "unclassified_problem"

def assign_pattern(obs, vocab_ids, top_k):
    """top_k: list of (pattern_id, sim) from embedder over canonical definitions, sorted desc."""
    p = obs.get("pattern") or {}
    llm_id = p.get("existing_id")
    top_id, top_sim = top_k[0] if top_k else (None, 0.0)
    if llm_id and llm_id in vocab_ids and llm_id == top_id and top_sim >= T_AUTO:
        return {"action": "assign", "pattern_id": llm_id, "method": "auto", "similarity": top_sim}
    if not llm_id and p.get("proposed_name"):
        if top_sim >= T_SYNONYM: return {"action": "review", "reason": "likely_synonym", "candidates": top_k}
        if top_sim < T_NEW:      return {"action": "review", "reason": "new_pattern_candidate", "proposed": snake(p["proposed_name"])}
    return {"action": "review", "reason": "disagreement_or_midrange", "candidates": top_k, "llm_id": llm_id}

# Human decisions: approve | merge(into=id) | new(canonical, definition, same_prototype_test)
# Merge: patterns.merged_into=id; bump taxonomy_version; re-run assignment; never mutate old rows.
# Split trigger: members cluster by differing required_capabilities / solution_modes.
# Pattern identity = (workflow_object, pain_type) + definition. "manual" is a mode, not part of the name.

SEED_PATTERNS = [
 ("lead_follow_up_delay","lead_handling","Inbound leads contacted late or inconsistently.","One follow-up automation covers all members."),
 ("lead_qualification_manual","lead_handling","People manually screen leads for fit.","Needs scoring/rules, not messaging."),
 ("appointment_scheduling_manual","scheduling","Booking/rescheduling done by hand over chat/phone.","One booking flow covers members."),
 ("order_data_entry_manual","order_processing","Orders re-keyed from messages/email into a system.","One intake->record flow."),
 ("whatsapp_order_handling","order_processing","Orders taken and tracked inside WhatsApp chats.","Needs WhatsApp API specifically."),
 ("inventory_sync_errors","order_processing","Stock counts differ across systems.","Needs two-way sync, not data entry."),
 ("invoice_chasing_manual","financial_admin","Staff chase unpaid invoices manually.","One reminder flow."),
 ("transaction_reconciliation_manual","financial_admin","Manual matching of transactions/records.","Needs matching rules."),
 ("support_response_slow","customer_support","Customer messages answered slowly.","Triage+reply flow."),
]
