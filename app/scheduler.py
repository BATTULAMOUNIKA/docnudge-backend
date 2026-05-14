from datetime import date, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import or_
from app.database import SessionLocal
from app.models import Patient, Visit, ReminderLog
from app.whatsapp import send_whatsapp_message
import logging

logger = logging.getLogger(__name__)

def get_patients_for_reminder(db: Session, target_date: date):
    return (
        db.query(Patient, Visit)
        .join(Visit, Visit.patient_id == Patient.id)
        .filter(or_(Visit.followup_date == target_date, Visit.next_visit == target_date))
        .filter(or_(Visit.followup_status.in_(["due", "upcoming"]), Visit.followup_status.is_(None)))
        .filter(Patient.opted_out == False)
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

def run_reminders(reminder_type: str):
    today = date.today()

    # Map reminder type to how many days before the visit
    days_before = {
        "two_days_before": 2,
        "day_before":      1,
        "morning":         0,
    }
    target_date = today + timedelta(days=days_before[reminder_type])

    db = SessionLocal()
    try:
        mark_missed_followups(db, today)
        patients = get_patients_for_reminder(db, target_date)
        logger.info(f"[{reminder_type}] Found {len(patients)} patient(s) for {target_date}")

        for patient, visit in patients:
            if already_sent(db, patient.id, reminder_type, today):
                logger.info(f"Already sent {reminder_type} to {patient.name}, skipping.")
                continue

            result = send_whatsapp_message(
                phone=patient.phone,
                patient_name=patient.name,
                reminder_type=reminder_type,
                context={
                    "clinic_name": patient.clinic.name if patient.clinic else "your clinic",
                    "condition": visit.condition or patient.condition,
                    "followup_date": visit.followup_date or visit.next_visit,
                    "next_visit": visit.followup_date or visit.next_visit,
                },
            )

            success = "error" not in result
            error_msg = result.get("error") if not success else None

            log = ReminderLog(
                patient_id=patient.id,
                reminder_type=reminder_type,
                success=success,
                error=error_msg,
            )
            db.add(log)
            db.commit()

            status = f"✅ sent (sid: {result.get('sid')})" if success else f"❌ failed: {error_msg}"
            logger.info(f"{patient.name} ({patient.phone}) — {status}")

    finally:
        db.close()

def trigger_two_days_before():
    run_reminders("two_days_before")

def trigger_day_before():
    run_reminders("day_before")

def trigger_morning():
    run_reminders("morning")

def trigger_missed_followups():
    today = date.today()
    target_date = today - timedelta(days=3)
    db = SessionLocal()
    try:
        rows = (
            db.query(Patient, Visit)
            .join(Visit, Visit.patient_id == Patient.id)
            .filter(or_(Visit.followup_date == target_date, Visit.next_visit == target_date))
            .filter(or_(Visit.followup_status == "missed", Visit.status == "missed"))
            .filter(Patient.opted_out == False)
            .all()
        )
        for patient, visit in rows:
            if already_sent(db, patient.id, "missed_followup", today):
                continue
            result = send_whatsapp_message(
                phone=patient.phone,
                patient_name=patient.name,
                reminder_type="missed_followup",
                context={
                    "clinic_name": patient.clinic.name if patient.clinic else "your clinic",
                    "condition": visit.condition or patient.condition,
                    "followup_date": visit.followup_date or visit.next_visit,
                    "next_visit": visit.followup_date or visit.next_visit,
                    "clinic_phone": patient.clinic.phone if patient.clinic else "the clinic",
                },
            )
            db.add(
                ReminderLog(
                    patient_id=patient.id,
                    reminder_type="missed_followup",
                    success="error" not in result,
                    error=result.get("error"),
                )
            )
            db.commit()
    finally:
        db.close()
