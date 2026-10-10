"""Service categories, workflow objects, buyer-intent levels and the seed problem-pattern library.
Pattern identity = the problem, not the solution. 'manual' / tool names never go in a NEW pattern name."""
import re

CATEGORIES = {
 "workflow_automation": "Automating repeated business tasks, tool-to-tool integrations, scheduled or triggered workflows (n8n, Zapier, Make, scripts).",
 "ai_implementation":   "Building or integrating LLM/AI features: chatbots, agents, RAG, custom GPTs, AI extraction/classification.",
 "crm":                 "CRM setup, migration, configuration, pipelines (HubSpot, Salesforce, GoHighLevel, Zoho).",
 "web_development":     "Websites, landing pages, web apps, WordPress/Shopify/Webflow builds, fixes and performance.",
 "software_development":"Custom software, APIs, mobile apps, scrapers, databases, DevOps, bug fixing.",
 "data_analysis":       "Analysis, dashboards, reporting, spreadsheets/BI, data cleaning and visualisation.",
 "marketing":           "SEO, paid ads, email/social marketing, content strategy, campaign management.",
 "sales":               "Lead generation, outreach, appointment setting, cold email/calling, sales operations.",
 "customer_support":    "Handling customer messages/tickets/chat, helpdesk setup, support staffing, reply drafting.",
 "design":              "Graphic, UI/UX, brand, presentation, video or 3D design.",
 "accounting":          "Bookkeeping, invoicing, reconciliation, payroll, tax preparation, financial reporting.",
 "research":            "Market/competitor/academic research, list building, information gathering and summarising.",
 "admin_support":       "General virtual-assistant and administrative work: inbox, calendar, data entry, document handling.",
 "other":               "Any other paid freelance work (writing, translation, legal, etc.).",
}

INTENT_LEVELS = {
 "explicit_hire":    "Writer says they want to hire/engage someone now for stated work.",
 "defined_project":  "A concrete deliverable or scope is described, without explicit hiring language.",
 "exploratory":      "Open to ideas, 'not sure what we need', 'maybe', exploring what AI/automation could do.",
 "research_inquiry": "Asking questions, advice, opinions, price checks, or whether something is possible.",
 "vague":            "Wants 'help', 'automation' or 'a developer' with no concrete work described.",
 "unknown":          "Cannot tell from the text.",
}

WORKFLOW_OBJECTS = ["lead","appointment","booking","order","invoice","payment","expense","inventory",
  "customer_message","support_ticket","document","report","dataset","candidate","onboarding_task",
  "content_post","campaign","website","design_asset","research_topic","software_system","data_record","other"]

# name tokens that describe a mode or a solution, not a problem -> stripped from NEW pattern names
BAD_NAME_TOKENS = {"manual","manually","automation","automated","automate","chatbot","zapier","n8n","gpt","tool"}

def snake(s):
    s = re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")
    return s or "unclassified_problem"

# (pattern_id, category, definition, same_prototype_test)
SEED_PATTERNS = [
 # ---- carried over from v3 (ids kept so earlier data still maps) ----
 ("lead_follow_up_delay","sales","Inbound leads contacted late or inconsistently.","One follow-up flow covers all members."),
 ("lead_qualification_manual","sales","People screen leads for fit by hand.","Needs scoring/rules, not messaging."),
 ("appointment_scheduling_manual","workflow_automation","Booking/rescheduling handled by hand over chat/phone.","One booking flow covers members."),
 ("order_data_entry_manual","workflow_automation","Orders re-keyed from messages/email into a system.","One intake->record flow."),
 ("whatsapp_order_handling","workflow_automation","Orders taken and tracked inside WhatsApp chats.","Needs WhatsApp API specifically."),
 ("inventory_sync_errors","workflow_automation","Stock counts differ across systems.","Needs two-way sync, not data entry."),
 ("invoice_chasing_manual","accounting","Staff chase unpaid invoices by hand.","One reminder flow."),
 ("transaction_reconciliation_manual","accounting","Matching transactions/records by hand.","Needs matching rules."),
 ("support_response_slow","customer_support","Customer messages answered slowly.","Triage+reply flow."),
 # ---- from v3 review queue (renamed to follow the naming rule) ----
 ("appointment_reminder_no_shows","workflow_automation","Missed or late reminders cause no-shows.","One reminder sequence covers members."),
 ("client_onboarding_admin","workflow_automation","Each new client needs the same set of setup steps/documents sent by hand.","One onboarding checklist flow."),
 # ---- web development ----
 ("website_missing_or_outdated","web_development","Business has no site or an outdated one and loses enquiries/credibility.","One new or rebuilt site covers members."),
 ("online_booking_or_ordering_gap","web_development","Customers cannot book/order online; it all goes through phone or messages.","One booking/ordering page plus backend."),
 ("site_broken_or_slow","web_development","Existing site is slow, broken, or failing forms/checkout.","Diagnose and fix, no rebuild."),
 # ---- marketing ----
 ("inconsistent_content_posting","marketing","Social/content posting is irregular or consumes owner time.","One content calendar and scheduling process."),
 ("low_traffic_or_enquiries","marketing","Not enough visitors or enquiries from SEO/ads.","One acquisition campaign/audit."),
 ("campaign_reporting_gap","marketing","No clear view of what marketing spend produces.","One tracking and reporting setup."),
 # ---- sales ----
 ("outbound_prospecting_capacity","sales","Nobody has time to find and contact prospects consistently.","One prospecting workflow or outsourced SDR."),
 ("appointment_setting_capacity","sales","Calls/meetings with prospects are not being booked at volume.","One setter process or script."),
 ("quote_proposal_turnaround","sales","Quotes/proposals take too long to produce.","One template/generator."),
 # ---- CRM ----
 ("crm_data_inconsistent","crm","Contact/deal records are duplicated, missing or out of date.","One cleanup plus entry rules."),
 ("crm_setup_or_migration","crm","Business needs a CRM configured or moved from another system.","One setup/migration project."),
 ("pipeline_visibility_gap","crm","Nobody can see deal stages, follow-ups or ownership.","One pipeline configuration."),
 # ---- data analysis ----
 ("report_assembly_time","data_analysis","Recurring reports are compiled by hand from several sources.","One automated report build."),
 ("data_cleaning_backlog","data_analysis","Messy datasets block analysis or decisions.","One cleaning pipeline."),
 ("dashboard_visibility_gap","data_analysis","No dashboard shows key numbers; decisions rely on guesses.","One dashboard build."),
 # ---- design ----
 ("brand_assets_missing","design","Business lacks logo/brand kit or has inconsistent branding.","One brand identity package."),
 ("ui_ux_improvement","design","Existing product/site has poor usability or look.","One UX audit and redesign."),
 ("design_asset_volume","design","Recurring need for many similar graphics/videos/presentations.","One template system or retained designer."),
 # ---- accounting ----
 ("bookkeeping_backlog","accounting","Books are behind or unreliable.","One catch-up and monthly process."),
 ("expense_receipt_processing","accounting","Receipts/expenses are collected and entered by hand.","One capture-to-ledger flow."),
 ("payroll_tax_prep","accounting","Payroll, VAT/GST or tax filing preparation is a recurring burden.","One compliance prep process."),
 # ---- research ----
 ("lead_list_building","research","Prospect/contact lists must be researched and compiled.","One list-building process."),
 ("market_competitor_research","research","Business needs market, competitor or pricing research.","One research report."),
 ("information_gathering_summarising","research","Large volumes of material must be gathered and summarised.","One research-and-summary workflow."),
 # ---- customer support ----
 ("support_coverage_gap","customer_support","Messages go unanswered after hours or at peak volume.","One coverage solution (staff or triage)."),
 ("repetitive_faq_handling","customer_support","Staff answer the same questions repeatedly.","One FAQ/knowledge-base responder."),
 ("knowledge_base_missing","customer_support","No help centre or internal answers library exists.","One knowledge-base build."),
 # ---- software development ----
 ("internal_tool_needed","software_development","Team runs on spreadsheets/email where a custom tool is needed.","One internal app."),
 ("system_integration_gap","software_development","Two systems cannot exchange data without human re-entry.","One integration/API connector."),
 ("data_extraction_scraping","software_development","Needed data lives on sites/files and must be extracted regularly.","One scraper/extractor."),
 # ---- AI implementation ----
 ("document_data_extraction_llm","ai_implementation","Structured data must be pulled from unstructured documents/emails.","One extraction pipeline."),
 ("knowledge_base_qa_assistant","ai_implementation","Staff or customers need answers from internal documents.","One retrieval-based assistant."),
 ("ai_content_generation_pipeline","ai_implementation","Recurring content/replies are drafted by hand and could be generated.","One generation workflow with review."),
 # ---- admin ----
 ("recurring_admin_overload","admin_support","Owner/staff time is eaten by recurring inbox, calendar and data-entry chores.","One VA or admin-routine fix."),
]

def seed_vocab():
    return {pid: cat for pid, cat, _, _ in SEED_PATTERNS}

def vocab_prompt_text():
    by = {}
    for pid, cat, d, t in SEED_PATTERNS:
        by.setdefault(cat, []).append(f"  - {pid}: {d} | same_prototype_test: {t}")
    return "\n".join(f"[{cat}]\n" + "\n".join(lines) for cat, lines in by.items())
