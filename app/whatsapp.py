import json
import logging
from datetime import date, datetime
from urllib import error, request

from app.config import (
    WHATSAPP_ACCESS_TOKEN,
    WHATSAPP_PHONE_NUMBER_ID,
    INTERAKT_LANGUAGE_CODE,
    INTERAKT_TEMPLATE_DAY_BEFORE,
    INTERAKT_TEMPLATE_MISSED_FOLLOWUP,
    INTERAKT_TEMPLATE_MORNING,
    INTERAKT_TEMPLATE_THANK_YOU,
    INTERAKT_TEMPLATE_TWO_DAYS_BEFORE,
    INTERAKT_TEMPLATE_WEEKLY_REPORT,
    INTERAKT_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

DEFAULT_CLINIC_NAME = "your clinic"
META_API_VERSION = "v20.0"
META_API_BASE = f"https://graph.facebook.com/{META_API_VERSION}"

TEMPLATE_BY_MESSAGE_TYPE = {
    "thank_you": INTERAKT_TEMPLATE_THANK_YOU,
    "two_days_before": INTERAKT_TEMPLATE_TWO_DAYS_BEFORE,
    "day_before": INTERAKT_TEMPLATE_DAY_BEFORE,
    "morning": INTERAKT_TEMPLATE_MORNING,
    "missed_followup": INTERAKT_TEMPLATE_MISSED_FOLLOWUP,
    "weekly_report": INTERAKT_TEMPLATE_WEEKLY_REPORT,
}


# ── Phone helpers ───────────────────────────────────────────

def _digits_only(value: str | None) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _to_e164(phone: str) -> str:
    """Return full international digits (no + or spaces) for Meta API."""
    digits = _digits_only(phone)
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 10:
        return f"91{digits}"
    return digits


def split_country_code(phone: str) -> tuple[str, str]:
    digits = _digits_only(phone)
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 10:
        return "+91", digits
    if digits.startswith("91") and len(digits) == 12:
        return "+91", digits[-10:]
    if len(digits) > 10:
        return f"+{digits[:-10]}", digits[-10:]
    return "+91", digits


# ── Date / context helpers ──────────────────────────────────

def _format_date(value) -> str:
    if not value:
        return "not set"
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.strftime("%d %b %Y").lstrip("0")
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text).date()
        return parsed.strftime("%d %b %Y").lstrip("0")
    except ValueError:
        return text


def care_tip_for(condition: str | None) -> str:
    normalized = (condition or "").strip().lower()
    if "diabet" in normalized:
        return "Monitor sugar regularly and take medicines on time."
    if "bp" in normalized or "hypertension" in normalized or "blood pressure" in normalized:
        return "Check your BP regularly and reduce extra salt."
    if "preg" in normalized:
        return "Keep your follow-up schedule and stay hydrated."
    if "dental" in normalized or "tooth" in normalized:
        return "Brush gently twice daily and avoid very hard foods."
    return "Follow the doctor's advice and keep your next visit."


def _body_values_for(message_type: str, patient_name: str, context: dict | None = None) -> list[str]:
    context = context or {}
    clinic_name = context.get("clinic_name") or DEFAULT_CLINIC_NAME
    condition = context.get("condition") or "Follow-up"
    next_visit = _format_date(context.get("next_visit") or context.get("followup_date"))
    care_tip = context.get("care_tip") or care_tip_for(condition)
    appointment_time = context.get("appointment_time") or "your scheduled time"
    clinic_address = context.get("clinic_address") or "the clinic"
    clinic_phone = context.get("clinic_phone") or "the clinic"

    values_by_type = {
        "thank_you": [patient_name, clinic_name, condition, next_visit, care_tip],
        "two_days_before": [patient_name, clinic_name, next_visit, care_tip],
        "day_before": [patient_name, clinic_name, next_visit],
        "morning": [patient_name, appointment_time, clinic_address],
        "missed_followup": [patient_name, clinic_name, next_visit, clinic_phone],
        "weekly_report": [
            clinic_name,
            context.get("total_patients", "0"),
            context.get("visits_completed", "0"),
            context.get("missed_count", "0"),
            context.get("return_rate", "0%"),
            context.get("summary", "No action needed."),
        ],
    }
    return [str(v) for v in values_by_type.get(message_type, [patient_name, clinic_name, next_visit])]


# ── Meta Graph API core ─────────────────────────────────────

def _meta_post(payload: dict) -> dict:
    """POST a message payload to Meta's messages endpoint."""
    if not WHATSAPP_ACCESS_TOKEN:
        return {"error": "WHATSAPP_ACCESS_TOKEN is not configured", "provider": "meta"}
    if not WHATSAPP_PHONE_NUMBER_ID:
        return {"error": "WHATSAPP_PHONE_NUMBER_ID is not configured", "provider": "meta"}

    url = f"{META_API_BASE}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
    )

    try:
        with request.urlopen(req, timeout=INTERAKT_TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw) if raw else {}
    except error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        logger.warning("Meta send failed: %s %s", exc.code, body_text)
        return {"error": f"Meta API {exc.code}: {body_text}", "provider": "meta"}
    except Exception as exc:
        logger.exception("Meta send failed")
        return {"error": str(exc), "provider": "meta"}

    msg_id = None
    try:
        msg_id = data["messages"][0]["id"]
    except (KeyError, IndexError, TypeError):
        pass

    return {
        "id": msg_id,
        "status": "queued" if msg_id else "unknown",
        "provider": "meta",
        "raw": data,
    }


def send_meta_template(
    phone: str,
    template_name: str,
    body_values: list[str],
    language: str | None = None,
) -> dict:
    """Send a WhatsApp template message via Meta Graph API."""
    if not template_name:
        return {"error": "Template name is not configured", "provider": "meta"}

    to = _to_e164(phone)
    if not to:
        return {"error": "Phone number is empty", "provider": "meta"}

    lang = language or INTERAKT_LANGUAGE_CODE or "en"

    components = []
    if body_values:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": str(v)} for v in body_values],
        })

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": lang},
            "components": components,
        },
    }
    result = _meta_post(payload)
    result["template"] = template_name
    result["mode"] = "template"
    return result


def send_meta_text(phone: str, text: str) -> dict:
    """Send a free-form text message (only works within the 24-hour service window)."""
    to = _to_e164(phone)
    if not to:
        return {"error": "Phone number is empty", "provider": "meta"}

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    result = _meta_post(payload)
    result["mode"] = "text"
    return result


def send_hello_world(phone: str) -> dict:
    """Send the pre-approved hello_world template — use for connectivity testing."""
    return send_meta_template(
        phone=phone,
        template_name="hello_world",
        body_values=[],
        language="en_US",
    )


# ── Public API (same signatures as before) ──────────────────

def send_whatsapp_message(
    phone: str,
    patient_name: str,
    reminder_type: str,
    context: dict | None = None,
) -> dict:
    template_name = TEMPLATE_BY_MESSAGE_TYPE.get(reminder_type)
    body_values = _body_values_for(reminder_type, patient_name, context)
    return send_meta_template(
        phone=phone,
        template_name=template_name,
        body_values=body_values,
    )


def send_visit_thank_you_message(
    phone: str,
    patient_name: str,
    condition: str | None,
    next_visit,
    clinic_name: str | None = None,
) -> dict:
    return send_whatsapp_message(
        phone=phone,
        patient_name=patient_name,
        reminder_type="thank_you",
        context={
            "clinic_name": clinic_name or DEFAULT_CLINIC_NAME,
            "condition": condition,
            "next_visit": next_visit,
            "care_tip": care_tip_for(condition),
        },
    )


def handle_opt_out(message: str) -> bool:
    return message.lower().strip() in {"stop", "unsubscribe", "cancel", "quit"}


def build_prescription_message(patient, prescription):
    lines = [
        f"Hello {patient.name}!",
        "Here is your prescription from today's visit:",
        "",
    ]
    for m in prescription.medicines:
        lines.append(f"- {m.get('name', '')} - {m.get('dosage', '')}")
        lines.append(f"  Timing: {m.get('frequency', '')} | Duration: {m.get('duration', '')}")
    if prescription.notes:
        lines.append("")
        lines.append(f"Notes: {prescription.notes}")
    lines.append("")
    lines.append("Take care and get well soon.")
    return "\n".join(lines)
