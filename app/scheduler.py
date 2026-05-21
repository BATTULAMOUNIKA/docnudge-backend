from datetime import date, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import or_
from app.database import SessionLocal
from app.models import Patient, Visit, ReminderLog
from app.whatsapp import send_whatsapp_message
import logging

logger = logging.getLogger(__name__)


# ── Helpers ─────────────────────────────────────────────────

def get_patients_for_reminder(db: Session, target_date: date):
    return (
        db.query(Patient, Visit)
        .join(Visit, Visit.patient_id == Patient.id)
        .filter(or_(Visit.followup_date == target_date, Visit.next_visit == target_date))
        .filter(or_(Visit.followup_status.in_(["due", "upcoming"]), Visit.followup_status.is_(None)))
        .filter(Patient.opted_out == False)
        .filter(Patient.reminder_enabled == True)
        .filter(Patient.followup_enabled == True)
        .all()
    )


def mark_missed_followups(db: Session, today: date):
    visits = (
        db.query(Visit)
        .filter(Visit.followup_date.isnot(None))
        .filter(Visit.followup_date < today)
        .filter(or_(Visit.followup_status.in_(["due", "upcoming"]), Visit.followup_status.is_(None)))
        .all()
    )
    for visit in visits:
        visit.followup_status = "missed"
        visit.status = "missed"
    if visits:
        db.commit()


def already_sent(db: Session, patient_id: int, reminder_type: str, today: date) -> bool:
    from sqlalchemy import func
    return (
        db.query(ReminderLog)
        .filter(
            ReminderLog.patient_id == patient_id,
            ReminderLog.reminder_type == reminder_type,
            func.date(ReminderLog.sent_at) == today,
        )
        .first()
        is not None
    )


# ── Day Before Reminder ─────────────────────────────────────
# Only 1 reminder type now — day_before
# Replaces old: two_days_before + morning (those templates don't exist in Meta)

def run_day_before_reminder():
    """
    Sends docnudge_day_before template to all patients
    whose follow-up is tomorrow.
    Runs daily at 9 AM via APScheduler.
    """
    today       = date.today()
    target_date = today + timedelta(days=1)

    db = SessionLocal()
    try:
        mark_missed_followups(db, today)
        patients = get_patients_for_reminder(db, target_date)
        logger.info("[day_before] Found %d patient(s) for %s", len(patients), target_date)

        for patient, visit in patients:
            if already_sent(db, patient.id, "day_before", today):
                logger.info("Already sent day_before to %s, skipping.", patient.name)
                continue

            clinic       = patient.clinic
            clinic_name  = clinic.name  if clinic else "your clinic"
            clinic_phone = clinic.phone if clinic else ""

            result = send_whatsapp_message(
                phone=patient.phone,
                patient_name=patient.name,
                reminder_type="day_before",
                context={
                    "clinic_name":  clinic_name,
                    "next_visit":   visit.followup_date or visit.next_visit,
                    "clinic_phone": clinic_phone,
                },
            )

            success   = "error" not in result
            error_msg = result.get("error") if not success else None

            db.add(ReminderLog(
                patient_id=patient.id,
                reminder_type="day_before",
                success=success,
                error=error_msg,
                message_id=result.get("id"),   # Meta returns 'id' not 'sid'
            ))
            db.commit()

            status = f"✅ sent (id: {result.get('id')})" if success else f"❌ failed: {error_msg}"
            logger.info("%s (%s) — %s", patient.name, patient.phone, status)

    finally:
        db.close()


# ── Missed Follow-up Recovery ───────────────────────────────

def trigger_missed_followups():
    """
    Sends docnudge_missed_followup template to patients
    who missed their follow-up 3 days ago.
    Runs daily at 10 AM via APScheduler.
    """
    today       = date.today()
    target_date = today - timedelta(days=3)

    db = SessionLocal()
    try:
        rows = (
            db.query(Patient, Visit)
            .join(Visit, Visit.patient_id == Patient.id)
            .filter(or_(Visit.followup_date == target_date, Visit.next_visit == target_date))
            .filter(or_(Visit.followup_status == "missed", Visit.status == "missed"))
            .filter(Patient.opted_out == False)
            .filter(Patient.reminder_enabled == True)
            .filter(Patient.followup_enabled == True)
            .all()
        )
        logger.info("[missed_followup] Found %d patient(s) for %s", len(rows), target_date)

        for patient, visit in rows:
            if already_sent(db, patient.id, "missed_followup", today):
                continue

            clinic       = patient.clinic
            clinic_name  = clinic.name  if clinic else "your clinic"
            clinic_phone = clinic.phone if clinic else ""

            result = send_whatsapp_message(
                phone=patient.phone,
                patient_name=patient.name,
                reminder_type="missed_followup",
                context={
                    "clinic_name":  clinic_name,
                    "missed_date":  visit.followup_date or visit.next_visit,
                    "clinic_phone": clinic_phone,
                },
            )

            success = "error" not in result
            db.add(ReminderLog(
                patient_id=patient.id,
                reminder_type="missed_followup",
                success=success,
                error=result.get("error") if not success else None,
                message_id=result.get("id"),
            ))
            db.commit()

            status = f"✅ sent" if success else f"❌ failed: {result.get('error')}"
            logger.info("%s (%s) — %s", patient.name, patient.phone, status)

    finally:
        db.close()


# ── Trigger functions (called by APScheduler in main.py) ───

def trigger_day_before():
    run_day_before_reminder()


# ── Keep old names so main.py doesn't break ────────────────
# These now do nothing — old templates don't exist in Meta
# Remove them from APScheduler in main.py when convenient

def trigger_two_days_before():
    logger.info("[two_days_before] Skipped — template removed. Using day_before only.")

def trigger_morning():
    logger.info("[morning] Skipped — template removed. Using day_before only.")
