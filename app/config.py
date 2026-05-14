from dotenv import load_dotenv
import os

load_dotenv()

DATABASE_URL         = os.getenv("DATABASE_URL")
INTERAKT_API_KEY = os.getenv("INTERAKT_API_KEY", "").strip()
INTERAKT_BASE_URL = os.getenv("INTERAKT_BASE_URL", "https://api.interakt.ai").strip()
INTERAKT_LANGUAGE_CODE = os.getenv("INTERAKT_LANGUAGE_CODE", "en").strip()
INTERAKT_CAMPAIGN_ID = os.getenv("INTERAKT_CAMPAIGN_ID", "").strip()
INTERAKT_TEMPLATE_THANK_YOU = os.getenv("INTERAKT_TEMPLATE_THANK_YOU", "docnudge_thank_you_after_visit").strip()
INTERAKT_TEMPLATE_TWO_DAYS_BEFORE = os.getenv("INTERAKT_TEMPLATE_TWO_DAYS_BEFORE", "docnudge_two_days_before").strip()
INTERAKT_TEMPLATE_DAY_BEFORE = os.getenv("INTERAKT_TEMPLATE_DAY_BEFORE", "docnudge_day_before").strip()
INTERAKT_TEMPLATE_MORNING = os.getenv("INTERAKT_TEMPLATE_MORNING", "docnudge_morning_of_visit").strip()
INTERAKT_TEMPLATE_MISSED_FOLLOWUP = os.getenv("INTERAKT_TEMPLATE_MISSED_FOLLOWUP", "docnudge_missed_followup").strip()
INTERAKT_TEMPLATE_WEEKLY_REPORT = os.getenv("INTERAKT_TEMPLATE_WEEKLY_REPORT", "docnudge_weekly_report").strip()
INTERAKT_TIMEOUT_SECONDS = int(os.getenv("INTERAKT_TIMEOUT_SECONDS", "12"))
WEBHOOK_VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-latest").strip()
_default_secret = "change-this-in-production"
SECRET_KEY = (os.getenv("SECRET_KEY") or _default_secret).strip()
