import json
import logging
from datetime import date, datetime
from urllib import error, request

from app.config import (
    INTERAKT_API_KEY,
    INTERAKT_BASE_URL,
    INTERAKT_CAMPAIGN_ID,
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

TEMPLATE_BY_MESSAGE_TYPE = {
    "thank_you": INTERAKT_TEMPLATE_THANK_YOU,
    "two_days_before": INTERAKT_TEMPLATE_TWO_DAYS_BEFORE,
    "day_before": INTERAKT_TEMPLATE_DAY_BEFORE,
    "morning": INTERAKT_TEMPLATE_MORNING,
    "missed_followup": INTERAKT_TEMPLATE_MISSED_FOLLOWUP,
    "weekly_report": INTERAKT_TEMPLATE_WEEKLY_REPORT,
}


def _digits_only(value: str | None) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


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
    return [str(value) for value in values_by_type.get(message_type, [patient_name, clinic_name, next_visit])]


def send_interakt_template(
    phone: str,
    template_name: str,
    body_values: list[str],
    callback_data: str | None = None,
) -> dict:
    if not INTERAKT_API_KEY:
        return {"error": "INTERAKT_API_KEY is not configured", "provider": "interakt"}
    if not template_name:
        return {"error": "Interakt template name is not configured", "provider": "interakt"}

    country_code, phone_number = split_country_code(phone)
    if not phone_number:
        return {"error": "Phone number is empty", "provider": "interakt"}

    payload = {
        "countryCode": country_code,
        "phoneNumber": phone_number,
        "type": "Template",
        "template": {
            "name": template_name,
            "languageCode": INTERAKT_LANGUAGE_CODE,
            "bodyValues": body_values,
        },
    }
    if callback_data:
        payload["callbackData"] = callback_data[:512]
    if INTERAKT_CAMPAIGN_ID:
        payload["campaignId"] = INTERAKT_CAMPAIGN_ID

    url = f"{INTERAKT_BASE_URL.rstrip('/')}/v1/public/message/"
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Basic {INTERAKT_API_KEY}",
            "Content-Type": "application/json",
        },
    )

    try:
        with request.urlopen(req, timeout=INTERAKT_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8")
            data = json.loads(raw) if raw else {}
    except error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        logger.warning("Interakt send failed: %s %s", exc.code, response_body)
        return {"error": f"Interakt API {exc.code}: {response_body}", "provider": "interakt"}
    except Exception as exc:
        logger.exception("Interakt send failed")
        return {"error": str(exc), "provider": "interakt"}

    if data.get("result") is False:
        return {"error": data.get("message") or "Interakt rejected the message", "provider": "interakt"}

    return {
        "id": data.get("id"),
        "status": data.get("message") or "queued",
        "mode": "template",
        "provider": "interakt",
        "template": template_name,
    }


def send_whatsapp_message(
    phone: str,
    patient_name: str,
    reminder_type: str,
    context: dict | None = None,
) -> dict:
    template_name = TEMPLATE_BY_MESSAGE_TYPE.get(reminder_type)
    body_values = _body_values_for(reminder_type, patient_name, context)
    return send_interakt_template(
        phone=phone,
        template_name=template_name,
        body_values=body_values,
        callback_data=f"docnudge:{reminder_type}",
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
