from dotenv import load_dotenv
import os

load_dotenv()

DATABASE_URL                    = os.getenv("DATABASE_URL")

# ── WhatsApp / Meta ─────────────────────────────────────────
WHATSAPP_ACCESS_TOKEN           = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
WHATSAPP_PHONE_NUMBER_ID        = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
WHATSAPP_BUSINESS_ACCOUNT_ID    = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID", "").strip()
WHATSAPP_VERIFY_TOKEN           = os.getenv("WHATSAPP_VERIFY_TOKEN")
WEBHOOK_VERIFY_TOKEN            = os.getenv("WEBHOOK_VERIFY_TOKEN")          # legacy alias — keep for now

# ── Template names (must match Meta exactly) ────────────────
INTERAKT_LANGUAGE_CODE          = os.getenv("INTERAKT_LANGUAGE_CODE", "en").strip()
INTERAKT_TEMPLATE_THANK_YOU     = os.getenv("INTERAKT_TEMPLATE_THANK_YOU", "docnudge_thank_you_after_visit").strip()
INTERAKT_TEMPLATE_TWO_DAYS_BEFORE = os.getenv("INTERAKT_TEMPLATE_TWO_DAYS_BEFORE", "docnudge_two_days_before").strip()
INTERAKT_TEMPLATE_PRESCRIPTION  = os.getenv("INTERAKT_TEMPLATE_PRESCRIPTION",  "docnudge_prescription").strip()
INTERAKT_TEMPLATE_DAY_BEFORE    = os.getenv("INTERAKT_TEMPLATE_DAY_BEFORE",    "docnudge_day_before").strip()
INTERAKT_TEMPLATE_MORNING       = os.getenv("INTERAKT_TEMPLATE_MORNING", "docnudge_morning_of_visit").strip()
INTERAKT_TEMPLATE_MISSED_FOLLOWUP = os.getenv("INTERAKT_TEMPLATE_MISSED_FOLLOWUP", "docnudge_missed_followup").strip()
INTERAKT_TEMPLATE_WEEKLY_REPORT = os.getenv("INTERAKT_TEMPLATE_WEEKLY_REPORT", "docnudge_weekly_report").strip()
INTERAKT_TIMEOUT_SECONDS        = int(os.getenv("INTERAKT_TIMEOUT_SECONDS", "12"))

# ── AI ──────────────────────────────────────────────────────
ANTHROPIC_API_KEY               = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL                 = os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-latest").strip()

# ── Auth ────────────────────────────────────────────────────
_default_secret                 = "change-this-in-production"
SECRET_KEY                      = (os.getenv("SECRET_KEY") or _default_secret).strip()

# ── Patient portal base URL (for prescription links) ────────
PATIENT_PORTAL_URL              = os.getenv("PATIENT_PORTAL_URL", "https://patient.docnudge.in").strip()
