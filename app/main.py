from fastapi import FastAPI, Depends, Request, Query, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from sqlalchemy import inspect, or_, text
from apscheduler.schedulers.background import BackgroundScheduler
from app.database import Base, engine, get_db, SessionLocal
from app.models import (
    AIConversation,
    Appointment,
    Clinic,
    DemoRequest,
    Patient,
    ReminderLog,
    LabResult,
    Prescription,
    Service,
    User,
    Visit,
    WeeklyReport,
)
from app.scheduler import trigger_two_days_before, trigger_day_before, trigger_morning, trigger_missed_followups
from app.auth import (
    hash_password,
    verify_password,
    create_token,
    decode_token,
    get_current_user,
    require_admin,
    require_clinic_access,
)
from app.config import WEBHOOK_VERIFY_TOKEN
from app.ai_helpers import (
    parse_prescription_shorthand,
    check_drug_interactions,
    analyze_lab_report,
    analyze_lab_image,
    draft_prescription_assist,
)
from pydantic import BaseModel
from datetime import date, datetime, timedelta
import json
import logging
import os
import random
import uuid

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="Clinic Reminder Engine")

def _dt(value):
    return value.isoformat() if value else None

def _model_data(model: BaseModel, exclude_unset: bool = False) -> dict:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_unset=exclude_unset)
    return model.dict(exclude_unset=exclude_unset)

def _digits_only(value: str | None) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())

def _phone_local(value: str | None) -> str:
    digits = _digits_only(value)
    return digits[-10:] if len(digits) >= 10 else digits

def _patient_mrn_value(clinic_id: int | None, patient_id: int | None) -> str | None:
    if clinic_id is None or patient_id is None:
        return None
    return f"DN{int(clinic_id):03d}{int(patient_id):06d}"

def _ensure_patient_mrn(db: Session, patient: Patient) -> str | None:
    if patient.mrn:
        return patient.mrn
    patient.mrn = _patient_mrn_value(patient.clinic_id, patient.id)
    db.flush()
    return patient.mrn

def _doctor_name_for_user(user: User | None, clinic: Clinic | None = None) -> str | None:
    if user and user.name:
        return user.name
    if clinic and clinic.doctor_name:
        return clinic.doctor_name
    return None

def _designation_for_user(user: User | None, clinic: Clinic | None = None) -> str | None:
    if user and user.designation:
        return user.designation
    if clinic and clinic.designation:
        return clinic.designation
    return None

def clinic_out(clinic: Clinic) -> dict:
    billing_status = clinic.billing_status or "trialing"
    subscription_plan = clinic.subscription_plan or "trial"
    if billing_status in {"cancelled", "inactive"}:
        status = "inactive"
    elif subscription_plan == "trial":
        status = "trial"
    else:
        status = "active"
    return {
        "id": clinic.id,
        "name": clinic.name,
        "city": clinic.city,
        "email": clinic.email,
        "phone": clinic.phone,
        "doctor_name": clinic.doctor_name,
        "designation": clinic.designation,
        "speciality": clinic.designation,
        "address": clinic.address,
        "website_url": clinic.website_url,
        "whatsapp_number": clinic.whatsapp_number,
        "logo_url": clinic.logo_url,
        "subscription_plan": subscription_plan,
        "plan": subscription_plan,
        "trial_ends_at": _dt(clinic.trial_ends_at),
        "billing_status": billing_status,
        "status": status,
        "widget_primary_color": clinic.widget_primary_color or "#0f766e",
        "widget_welcome_text": clinic.widget_welcome_text or "Hi, I am DocNudge AI. What would you like to book?",
        "widget_enabled": bool(clinic.widget_enabled),
        "patient_count": len(clinic.patients or []),
        "created_at": _dt(clinic.created_at),
    }

def patient_out(patient: Patient) -> dict:
    clinic = patient.clinic
    latest_visit = None
    if patient.visits:
        latest_visit = max(
            patient.visits,
            key=lambda visit: (visit.visit_date or date.min, visit.created_at or datetime.min),
        )
    return {
        "id": patient.id,
        "clinic_id": patient.clinic_id,
        "mrn": patient.mrn or _patient_mrn_value(patient.clinic_id, patient.id),
        "name": patient.name,
        "phone": patient.phone,
        "condition": patient.condition,
        "age": patient.age,
        "gender": patient.gender,
        "followup_type": patient.followup_type,
        "blood_group": patient.blood_group,
        "allergies": patient.allergies or [],
        "conditions": patient.conditions or [],
        "emergency_name": patient.emergency_name,
        "emergency_phone": patient.emergency_phone,
        "emergency_relation": patient.emergency_relation,
        "emergency_token": patient.emergency_token,
        "source": patient.source,
        "last_visit_at": _dt(patient.last_visit_at),
        "preferred_language": patient.preferred_language or "en",
        "opted_out": bool(patient.opted_out),
        "reminder_enabled": bool(patient.reminder_enabled),
        "followup_enabled": bool(patient.followup_enabled),
        "doctor_name": (latest_visit.doctor_name if latest_visit and latest_visit.doctor_name else (clinic.doctor_name if clinic else None)),
        "doctor_designation": (latest_visit.doctor_designation if latest_visit and latest_visit.doctor_designation else (clinic.designation if clinic else None)),
        "created_at": _dt(patient.created_at),
    }

def visit_out(visit: Visit) -> dict:
    followup_date = visit.followup_date or visit.next_visit
    followup_status = visit.followup_status or visit.status or "due"
    return {
        "id": visit.id,
        "patient_id": visit.patient_id,
        "condition": visit.condition,
        "doctor_name": visit.doctor_name,
        "doctor_designation": visit.doctor_designation,
        "visit_date": _dt(visit.visit_date),
        "followup_date": _dt(followup_date),
        "followup_status": followup_status,
        "next_visit": _dt(visit.next_visit),
        "notes": visit.notes,
        "prescription_text": visit.prescription_text,
        "doctor_notes": visit.doctor_notes,
        "status": visit.status,
        "created_at": _dt(visit.created_at),
    }

def user_out(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "phone": user.phone,
        "designation": user.designation,
        "role": user.role,
        "clinic_id": user.clinic_id,
        "created_at": _dt(user.created_at),
    }


def user_out_with_clinic(user: User, db: Session) -> dict:
    payload = user_out(user)
    clinic = db.query(Clinic).filter(Clinic.id == user.clinic_id).first() if user.clinic_id else None
    if clinic:
        payload.update(
            {
                "clinic_name": clinic.name,
                "doctor_name": _doctor_name_for_user(user, clinic),
                "designation": _designation_for_user(user, clinic),
            }
        )
    return payload

def reminder_log_out(log: ReminderLog) -> dict:
    return {
        "id": log.id,
        "patient_id": log.patient_id,
        "reminder_type": log.reminder_type,
        "sent_at": _dt(log.sent_at),
        "success": bool(log.success),
        "error": log.error,
    }

def service_out(service: Service) -> dict:
    return {
        "id": service.id,
        "clinic_id": service.clinic_id,
        "name": service.name,
        "duration_minutes": service.duration_minutes,
        "price": service.price,
        "is_active": bool(service.is_active),
        "created_at": _dt(service.created_at),
    }

def appointment_out(appointment: Appointment) -> dict:
    return {
        "id": appointment.id,
        "clinic_id": appointment.clinic_id,
        "patient_name": appointment.patient_name,
        "patient_phone": appointment.patient_phone,
        "service_type": appointment.service_type,
        "appointment_date": _dt(appointment.appointment_date),
        "appointment_time": appointment.appointment_time,
        "status": appointment.status,
        "booked_via": appointment.booked_via,
        "notes": appointment.notes,
        "created_at": _dt(appointment.created_at),
    }

def weekly_report_out(report: WeeklyReport) -> dict:
    return {
        "id": report.id,
        "clinic_id": report.clinic_id,
        "week_start": _dt(report.week_start),
        "total_patients": report.total_patients,
        "visits_completed": report.visits_completed,
        "missed_count": report.missed_count,
        "return_rate": report.return_rate,
        "ai_summary": report.ai_summary,
        "sent_at": _dt(report.sent_at),
        "created_at": _dt(report.created_at),
    }

def demo_request_out(request: DemoRequest) -> dict:
    return {
        "id": request.id,
        "name": request.name,
        "clinic": request.clinic,
        "email": request.email,
        "phone": request.phone,
        "role": request.role,
        "city": request.city,
        "message": request.message,
        "source": request.source or "landing",
        "status": request.status or "new",
        "created_at": _dt(request.created_at),
    }

# ── CORS ───────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Preflight handler ──────────────────────────────────────
@app.options("/{full_path:path}")
async def preflight_handler(full_path: str):
    return Response(
        status_code=200,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        },
    )

# ── Create tables ──────────────────────────────────────────
Base.metadata.create_all(bind=engine)

# ── Migrations ─────────────────────────────────────────────
def _columns_for(conn, table_name: str) -> set[str]:
    return {column["name"] for column in inspect(conn).get_columns(table_name)}

def _add_column_if_missing(conn, table_name: str, column_name: str, column_sql: str):
    if column_name not in _columns_for(conn, table_name):
        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}"))

def _drop_column_if_present(conn, table_name: str, column_name: str):
    if column_name in _columns_for(conn, table_name):
        try:
            conn.execute(text(f"ALTER TABLE {table_name} DROP COLUMN {column_name}"))
        except Exception:
            logging.exception("Could not drop legacy column %s.%s", table_name, column_name)

def run_migrations():
    try:
        with engine.begin() as conn:
            tables = set(inspect(conn).get_table_names())

            if "patients" in tables:
                patient_columns = _columns_for(conn, "patients")

                if "opted_out" not in patient_columns and "whatsapp_opt_out" in patient_columns:
                    conn.execute(text("ALTER TABLE patients RENAME COLUMN whatsapp_opt_out TO opted_out"))
                elif "opted_out" not in patient_columns:
                    conn.execute(text("ALTER TABLE patients ADD COLUMN opted_out BOOLEAN DEFAULT FALSE"))
                elif "whatsapp_opt_out" in patient_columns:
                    _drop_column_if_present(conn, "patients", "whatsapp_opt_out")

                _add_column_if_missing(conn, "patients", "age", "INTEGER")
                _add_column_if_missing(conn, "patients", "gender", "VARCHAR")
                _add_column_if_missing(conn, "patients", "followup_type", "VARCHAR")
                _add_column_if_missing(conn, "patients", "blood_group", "VARCHAR")
                _add_column_if_missing(conn, "patients", "allergies", "JSON")
                _add_column_if_missing(conn, "patients", "conditions", "JSON")
                _add_column_if_missing(conn, "patients", "emergency_name", "VARCHAR")
                _add_column_if_missing(conn, "patients", "emergency_phone", "VARCHAR")
                _add_column_if_missing(conn, "patients", "emergency_relation", "VARCHAR")
                _add_column_if_missing(conn, "patients", "emergency_token", "VARCHAR")
                _add_column_if_missing(conn, "patients", "source", "VARCHAR DEFAULT 'walk_in'")
                _add_column_if_missing(conn, "patients", "last_visit_at", "DATE")
                _add_column_if_missing(conn, "patients", "preferred_language", "VARCHAR DEFAULT 'en'")
                _add_column_if_missing(conn, "patients", "mrn", "VARCHAR")
                _add_column_if_missing(conn, "patients", "reminder_enabled", "BOOLEAN DEFAULT TRUE")
                _add_column_if_missing(conn, "patients", "followup_enabled", "BOOLEAN DEFAULT TRUE")
                conn.execute(text("UPDATE patients SET opted_out = FALSE WHERE opted_out IS NULL"))
                conn.execute(text("UPDATE patients SET source = 'walk_in' WHERE source IS NULL"))
                conn.execute(text("UPDATE patients SET preferred_language = 'en' WHERE preferred_language IS NULL"))
                conn.execute(text("UPDATE patients SET reminder_enabled = TRUE WHERE reminder_enabled IS NULL"))
                conn.execute(text("UPDATE patients SET followup_enabled = TRUE WHERE followup_enabled IS NULL"))
                conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_patients_mrn ON patients (mrn)"))

            if "visits" in tables:
                _add_column_if_missing(conn, "visits", "condition", "VARCHAR")
                _add_column_if_missing(conn, "visits", "doctor_name", "VARCHAR")
                _add_column_if_missing(conn, "visits", "doctor_designation", "VARCHAR")
                _add_column_if_missing(conn, "visits", "followup_date", "DATE")
                _add_column_if_missing(conn, "visits", "followup_status", "VARCHAR DEFAULT 'due'")
                _add_column_if_missing(conn, "visits", "status", "VARCHAR DEFAULT 'upcoming'")
                _add_column_if_missing(conn, "visits", "prescription_text", "TEXT")
                _add_column_if_missing(conn, "visits", "doctor_notes", "TEXT")
                conn.execute(text("UPDATE visits SET followup_date = next_visit WHERE followup_date IS NULL AND next_visit IS NOT NULL"))
                conn.execute(text("UPDATE visits SET followup_status = status WHERE followup_status IS NULL AND status IS NOT NULL"))
                conn.execute(text("UPDATE visits SET followup_status = 'due' WHERE followup_status IS NULL"))
                conn.execute(text("UPDATE visits SET status = 'upcoming' WHERE status IS NULL"))
                conn.execute(text("""
                    UPDATE visits
                    SET doctor_name = clinics.doctor_name
                    FROM patients, clinics
                    WHERE visits.patient_id = patients.id
                      AND patients.clinic_id = clinics.id
                      AND visits.doctor_name IS NULL
                """))
                conn.execute(text("""
                    UPDATE visits
                    SET doctor_designation = clinics.designation
                    FROM patients, clinics
                    WHERE visits.patient_id = patients.id
                      AND patients.clinic_id = clinics.id
                      AND visits.doctor_designation IS NULL
                """))

            if "clinics" in tables:
                _add_column_if_missing(conn, "clinics", "city", "VARCHAR")
                _add_column_if_missing(conn, "clinics", "email", "VARCHAR")
                _add_column_if_missing(conn, "clinics", "phone", "VARCHAR")
                _add_column_if_missing(conn, "clinics", "doctor_name", "VARCHAR")
                _add_column_if_missing(conn, "clinics", "designation", "VARCHAR")
                _add_column_if_missing(conn, "clinics", "address", "TEXT")
                _add_column_if_missing(conn, "clinics", "website_url", "VARCHAR")
                _add_column_if_missing(conn, "clinics", "whatsapp_number", "VARCHAR")
                _add_column_if_missing(conn, "clinics", "logo_url", "VARCHAR")
                _add_column_if_missing(conn, "clinics", "subscription_plan", "VARCHAR DEFAULT 'trial'")
                _add_column_if_missing(conn, "clinics", "trial_ends_at", "TIMESTAMP")
                _add_column_if_missing(conn, "clinics", "billing_status", "VARCHAR DEFAULT 'trialing'")
                _add_column_if_missing(conn, "clinics", "widget_primary_color", "VARCHAR DEFAULT '#0f766e'")
                _add_column_if_missing(conn, "clinics", "widget_welcome_text", "VARCHAR")
                _add_column_if_missing(conn, "clinics", "widget_enabled", "BOOLEAN DEFAULT TRUE")
                conn.execute(text("UPDATE clinics SET subscription_plan = 'trial' WHERE subscription_plan IS NULL"))
                conn.execute(text("UPDATE clinics SET billing_status = 'trialing' WHERE billing_status IS NULL"))
                conn.execute(text("UPDATE clinics SET widget_primary_color = '#0f766e' WHERE widget_primary_color IS NULL"))
                conn.execute(text("UPDATE clinics SET widget_enabled = TRUE WHERE widget_enabled IS NULL"))

            if "users" in tables:
                _add_column_if_missing(conn, "users", "name", "VARCHAR")
                _add_column_if_missing(conn, "users", "phone", "VARCHAR")
                _add_column_if_missing(conn, "users", "designation", "VARCHAR")
                conn.execute(text("""
                    UPDATE users
                    SET designation = clinics.designation
                    FROM clinics
                    WHERE users.clinic_id = clinics.id
                      AND users.designation IS NULL
                """))

            if "demo_requests" in tables:
                _add_column_if_missing(conn, "demo_requests", "city", "VARCHAR")
                _add_column_if_missing(conn, "demo_requests", "message", "TEXT")
                _add_column_if_missing(conn, "demo_requests", "source", "VARCHAR DEFAULT 'landing'")
                _add_column_if_missing(conn, "demo_requests", "status", "VARCHAR DEFAULT 'new'")
                conn.execute(text("UPDATE demo_requests SET source = 'landing' WHERE source IS NULL"))
                conn.execute(text("UPDATE demo_requests SET status = 'new' WHERE status IS NULL"))

            if "patients" in tables:
                logging.info("patients columns after migration: %s", sorted(_columns_for(conn, "patients")))
            if "visits" in tables:
                logging.info("visits columns after migration: %s", sorted(_columns_for(conn, "visits")))
            print("Migrations done.")
    except Exception:
        logging.exception("Migration error")

run_migrations()

def backfill_patient_metadata():
    db = SessionLocal()
    try:
        changed = False
        clinics_by_id = {clinic.id: clinic for clinic in db.query(Clinic).all()}
        for user in db.query(User).all():
            clinic = clinics_by_id.get(user.clinic_id)
            if not user.name and user.role == "doctor":
                user.name = clinic.doctor_name if clinic and clinic.doctor_name else user.email.split("@")[0].replace(".", " ").title()
                changed = True
            if not user.designation and clinic and clinic.designation:
                user.designation = clinic.designation
                changed = True
        for patient in db.query(Patient).all():
            next_mrn = _patient_mrn_value(patient.clinic_id, patient.id)
            if next_mrn and patient.mrn != next_mrn:
                patient.mrn = next_mrn
                changed = True
            if patient.reminder_enabled is None:
                patient.reminder_enabled = True
                changed = True
            if patient.followup_enabled is None:
                patient.followup_enabled = True
                changed = True
        for visit in db.query(Visit).join(Patient, Visit.patient_id == Patient.id).all():
            clinic = clinics_by_id.get(visit.patient.clinic_id if visit.patient else None)
            if not visit.doctor_name and clinic and clinic.doctor_name:
                visit.doctor_name = clinic.doctor_name
                changed = True
            if not visit.doctor_designation and clinic and clinic.designation:
                visit.doctor_designation = clinic.designation
                changed = True
        if changed:
            db.commit()
    except Exception:
        db.rollback()
        logging.exception("Could not backfill patient metadata")
    finally:
        db.close()

backfill_patient_metadata()

# ── Seed admin ─────────────────────────────────────────────
def seed_admin():
    print("Running seed_admin...")
    db = SessionLocal()
    try:
        admin_email = os.getenv("ADMIN_EMAIL", "admin@clinicremind.in").strip()
        admin_password = os.getenv("ADMIN_PASSWORD", "changeme123")
        reset_password = os.getenv("RESET_ADMIN_PASSWORD", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        existing = db.query(User).filter(User.email == admin_email).first()
        if not existing:
            admin = User(
                email=admin_email,
                password=hash_password(admin_password),
                role="admin",
                clinic_id=None,
            )
            db.add(admin)
            db.commit()
            print("Admin seeded successfully.")
        elif reset_password:
            existing.password = hash_password(admin_password)
            db.commit()
            print("Admin password reset from environment.")
        else:
            print("Admin already exists.")
    except Exception as e:
        print(f"Seed error: {e}")
    finally:
        db.close()

seed_admin()

# ── Scheduler ──────────────────────────────────────────────
scheduler = BackgroundScheduler(timezone="Asia/Kolkata")
scheduler.add_job(trigger_two_days_before, "cron", hour=10, minute=0)
scheduler.add_job(trigger_day_before,      "cron", hour=18, minute=0)
scheduler.add_job(trigger_morning,         "cron", hour=8,  minute=0)
scheduler.add_job(trigger_missed_followups, "cron", hour=10, minute=30)
scheduler.start()

# ── Pydantic schemas ───────────────────────────────────────
class PatientIn(BaseModel):
    clinic_id:     int
    name:          str
    phone:         str
    condition:     str | None = None
    age:           int | None = None
    gender:        str | None = None
    followup_type: str | None = None
    source:        str | None = "walk_in"
    preferred_language: str | None = "en"
    reminder_enabled: bool | None = True
    followup_enabled: bool | None = True

class PatientUpdateIn(BaseModel):
    name: str | None = None
    phone: str | None = None
    condition: str | None = None
    age: int | None = None
    gender: str | None = None
    followup_type: str | None = None
    source: str | None = None
    preferred_language: str | None = None
    reminder_enabled: bool | None = None
    followup_enabled: bool | None = None

class VisitIn(BaseModel):
    patient_id: int
    visit_date: date
    next_visit: date | None = None
    followup_date: date | None = None
    condition:  str | None = None
    notes:      str | None = None
    prescription_text: str | None = None
    doctor_notes: str | None = None
    status:     str | None = "upcoming"
    followup_status: str | None = None

class UserIn(BaseModel):
    email:     str
    password:  str
    name:      str | None = None
    phone:     str | None = None
    designation: str | None = None
    role:      str = "doctor"
    clinic_id: int | None = None


class UserUpdateIn(BaseModel):
    email: str | None = None
    password: str | None = None
    name: str | None = None
    phone: str | None = None
    designation: str | None = None
    role: str | None = None
    clinic_id: int | None = None

class RetentionPatientIn(BaseModel):
    clinic_id: int
    name: str
    phone: str
    condition: str
    visit_date: date
    followup_required: bool = True
    followup_date: date | None = None
    notes: str | None = None
    prescription_text: str | None = None
    doctor_notes: str | None = None
    source: str | None = "walk_in"
    preferred_language: str | None = "en"

class RetentionVisitIn(BaseModel):
    condition: str
    visit_date: date
    followup_required: bool = True
    followup_date: date | None = None
    notes: str | None = None
    prescription_text: str | None = None
    doctor_notes: str | None = None

class RetentionPatientUpdateIn(BaseModel):
    name: str | None = None
    phone: str | None = None
    condition: str | None = None

class CloseCaseIn(BaseModel):
    reason: str | None = None

class ClinicProfileIn(BaseModel):
    name: str | None = None
    city: str | None = None
    plan: str | None = None
    email: str | None = None
    phone: str | None = None
    doctor_name: str | None = None
    designation: str | None = None
    speciality: str | None = None
    address: str | None = None
    website_url: str | None = None
    whatsapp_number: str | None = None
    logo_url: str | None = None
    subscription_plan: str | None = None
    billing_status: str | None = None
    widget_primary_color: str | None = None
    widget_welcome_text: str | None = None
    widget_enabled: bool | None = None

class ServiceIn(BaseModel):
    clinic_id: int
    name: str
    duration_minutes: int = 30
    price: float = 0
    is_active: bool = True

class ServiceUpdateIn(BaseModel):
    name: str | None = None
    duration_minutes: int | None = None
    price: float | None = None
    is_active: bool | None = None

class AppointmentIn(BaseModel):
    clinic_id: int
    patient_name: str
    patient_phone: str
    service_type: str | None = None
    appointment_date: date
    appointment_time: str
    status: str = "booked"
    booked_via: str = "manual"
    notes: str | None = None

class AppointmentUpdateIn(BaseModel):
    patient_name: str | None = None
    patient_phone: str | None = None
    service_type: str | None = None
    appointment_date: date | None = None
    appointment_time: str | None = None
    status: str | None = None
    notes: str | None = None

class CompleteAppointmentIn(BaseModel):
    condition: str | None = None
    notes: str | None = None
    prescription_text: str | None = None

class PublicBookingIn(BaseModel):
    clinic_id: int
    patient_name: str
    patient_phone: str
    service_type: str
    appointment_date: date
    appointment_time: str
    session_id: str | None = None

class DemoRequestIn(BaseModel):
    name: str
    clinic: str
    email: str
    phone: str
    role: str
    city: str | None = None
    message: str | None = None
    source: str | None = "landing"

class WidgetChatIn(BaseModel):
    clinic_id: int
    session_id: str
    user_message: str

class PortalAccessIn(BaseModel):
    clinic_id: int
    phone: str

class PatientOtpIn(BaseModel):
    phone: str


class PatientPhoneLoginIn(BaseModel):
    phone: str


class PatientOtpVerifyIn(BaseModel):
    phone: str
    otp: str


class PatientProfileUpdateIn(BaseModel):
    name: str | None = None
    age: int | None = None
    gender: str | None = None
    blood_group: str | None = None
    allergies: list[str] | None = None
    conditions: list[str] | None = None
    emergency_name: str | None = None
    emergency_phone: str | None = None
    emergency_relation: str | None = None
    preferred_language: str | None = None


class MedicineItem(BaseModel):
    name: str
    dosage: str
    frequency: str
    duration: str
    notes: str = ""


class PrescriptionCreate(BaseModel):
    patient_id: int
    medicines: list[MedicineItem]
    notes: str | None = ""

class PrescriptionUpdateIn(BaseModel):
    medicines: list[MedicineItem] | None = None
    notes: str | None = None


class LabCreate(BaseModel):
    patient_id: int
    test_name: str
    result: str
    reference_range: str | None = ""
    test_date: str
    status: str | None = "normal"


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


class ReminderSettingsIn(BaseModel):
    two_days_time: str | None = None
    day_before_time: str | None = None
    morning_time: str | None = None
    missed_days: int | None = None


class ClinicCreateIn(BaseModel):
    name: str
    city: str | None = None
    plan: str | None = "trial"
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    doctor_name: str | None = None
    designation: str | None = None


class AIPrescriptionParseIn(BaseModel):
    shorthand: str
    patient_context: str | None = ""


class AIPrescriptionInteractionIn(BaseModel):
    medicines: list[dict]


class AIPrescriptionDraftIn(BaseModel):
    shorthand: str
    patient_context: str | None = ""


class AILabAnalyzeIn(BaseModel):
    lab_text: str
    patient_context: str | None = ""


class AILabImageAnalyzeIn(BaseModel):
    base64_image: str
    media_type: str
    patient_context: str | None = ""

# ── Auth routes ────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "Clinic Reminder Engine is running"}

@app.post("/auth/login")
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    try:
        user = db.query(User).filter(User.email == form.username).first()
        if not user or not verify_password(form.password, user.password):
            raise HTTPException(status_code=401, detail="Invalid email or password")
        clinic = db.query(Clinic).filter(Clinic.id == user.clinic_id).first() if user.clinic_id else None
        doctor_name = _doctor_name_for_user(user, clinic)
        designation = _designation_for_user(user, clinic)
        token = create_token(
            {
                "sub": str(user.id),
                "role": user.role,
                "clinic_id": user.clinic_id,
                "clinic_name": clinic.name if clinic else None,
                "doctor_name": doctor_name,
                "designation": designation,
            }
        )
        return {
            "access_token": token,
            "token_type": "bearer",
            "role": user.role,
            "clinic_id": user.clinic_id,
            "clinic_name": clinic.name if clinic else None,
            "doctor_name": doctor_name,
            "designation": designation,
            "name": user.name,
        }
    except HTTPException:
        raise
    except Exception:
        logging.exception("POST /auth/login failed")
        raise HTTPException(status_code=500, detail="Login failed")

@app.get("/auth/me")
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return user_out_with_clinic(user, db)


@app.put("/auth/change-password")
def change_password(
    data: ChangePasswordIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(data.current_password, user.password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    user.password = hash_password(data.new_password)
    db.commit()
    return {"success": True}


@app.put("/clinic/settings")
def clinic_settings(
    data: ClinicProfileIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    clinic_id = user.clinic_id
    if user.role == "admin" and not clinic_id:
        first = db.query(Clinic).order_by(Clinic.id.asc()).first()
        if not first:
            raise HTTPException(status_code=404, detail="Clinic not found")
        clinic_id = first.id
    if not clinic_id:
        raise HTTPException(status_code=400, detail="User is not assigned to a clinic")
    if user.role != "admin":
        require_clinic_access(clinic_id, user)
    clinic = db.query(Clinic).filter(Clinic.id == clinic_id).first()
    if not clinic:
        raise HTTPException(status_code=404, detail="Clinic not found")
    payload = _model_data(data, exclude_unset=True)
    user_designation = payload.pop("designation", None)
    if user_designation is None and "speciality" in payload:
        user_designation = payload.pop("speciality")
    user_name = payload.pop("doctor_name", None)
    for field, value in payload.items():
        setattr(clinic, field, value)
    if user_name is not None:
        user.name = user_name
    if user_designation is not None:
        user.designation = user_designation
    db.commit()
    db.refresh(clinic)
    db.refresh(user)
    payload = clinic_out(clinic)
    payload["doctor_name"] = _doctor_name_for_user(user, clinic)
    payload["designation"] = _designation_for_user(user, clinic)
    payload["user_name"] = user.name
    return payload


@app.put("/settings/reminders")
def reminder_settings(
    data: ReminderSettingsIn,
    user: User = Depends(get_current_user),
):
    return {"success": True, "settings": _model_data(data, exclude_unset=True), "clinic_id": user.clinic_id}


@app.post("/ai/prescriptions/parse")
def ai_parse_prescription(
    data: AIPrescriptionParseIn,
    user: User = Depends(get_current_user),
):
    return {"medicines": parse_prescription_shorthand(data.shorthand, data.patient_context or "")}


@app.post("/ai/prescriptions/draft")
def ai_prescription_draft(
    data: AIPrescriptionDraftIn,
    user: User = Depends(get_current_user),
):
    return draft_prescription_assist(data.shorthand or "", data.patient_context or "")


@app.post("/ai/prescriptions/interactions")
def ai_check_prescription_interactions(
    data: AIPrescriptionInteractionIn,
    user: User = Depends(get_current_user),
):
    return check_drug_interactions(data.medicines)


@app.post("/ai/labs/analyze")
def ai_analyze_lab_report(
    data: AILabAnalyzeIn,
    user: User = Depends(get_current_user),
):
    return analyze_lab_report(data.lab_text, data.patient_context or "")


@app.post("/ai/labs/analyze-image")
def ai_analyze_lab_image(
    data: AILabImageAnalyzeIn,
    user: User = Depends(get_current_user),
):
    return analyze_lab_image(data.base64_image, data.media_type, data.patient_context or "")

# ── Admin routes ───────────────────────────────────────────
@app.get("/admin/users", dependencies=[Depends(require_admin)])
def list_users(db: Session = Depends(get_db)):
    try:
        users = db.query(User).order_by(User.created_at.desc()).all()
        return [user_out(user) for user in users]
    except Exception as e:
        logging.exception("list_users failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/admin/demo-requests", dependencies=[Depends(require_admin)])
def list_demo_requests(db: Session = Depends(get_db)):
    try:
        requests = db.query(DemoRequest).order_by(DemoRequest.created_at.desc(), DemoRequest.id.desc()).all()
        return [demo_request_out(item) for item in requests]
    except Exception as e:
        logging.exception("list_demo_requests failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/admin/users", dependencies=[Depends(require_admin)])
def create_user(data: UserIn, db: Session = Depends(get_db)):
    try:
        if db.query(User).filter(User.email == data.email).first():
            raise HTTPException(status_code=400, detail="Email already exists")
        user = User(
            email=data.email,
            password=hash_password(data.password),
            name=data.name,
            phone=data.phone,
            designation=data.designation,
            role=data.role,
            clinic_id=data.clinic_id,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user_out(user)
    except HTTPException:
        raise
    except Exception as e:
        logging.exception("create_user failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/admin/users/{user_id}", dependencies=[Depends(require_admin)])
def update_user(user_id: int, data: UserUpdateIn, db: Session = Depends(get_db)):
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if user.role != "doctor":
            raise HTTPException(status_code=400, detail="Only doctor accounts can be edited here")
        if data.role is not None and data.role != "doctor":
            raise HTTPException(status_code=400, detail="Doctor accounts must keep the doctor role")
        if data.email:
            existing = db.query(User).filter(User.email == data.email, User.id != user_id).first()
            if existing:
                raise HTTPException(status_code=400, detail="Email already exists")

        payload = _model_data(data, exclude_unset=True)
        next_password = payload.pop("password", None)
        payload.pop("role", None)

        for field, value in payload.items():
            setattr(user, field, value)
        if next_password:
            user.password = hash_password(next_password)

        db.commit()
        db.refresh(user)
        return user_out(user)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logging.exception("update_user failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/admin/users/{user_id}", dependencies=[Depends(require_admin)])
def delete_user(user_id: int, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role != "doctor":
        raise HTTPException(status_code=400, detail="Only doctor accounts can be deleted here")
    try:
        db.delete(user)
        db.commit()
        return {"success": True}
    except Exception as e:
        db.rollback()
        logging.exception("delete_user failed")
        raise HTTPException(status_code=500, detail=str(e))

# ── Clinic routes ──────────────────────────────────────────
@app.post("/clinics")
def create_clinic(
    name: str | None = None,
    data: ClinicCreateIn | None = None,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        clinic_name = (data.name if data else name) or name
        if not clinic_name:
            raise HTTPException(status_code=400, detail="Clinic name is required")
        clinic = Clinic(
            name=clinic_name,
            city=data.city if data else None,
            email=data.email if data else None,
            phone=data.phone if data else None,
            doctor_name=data.doctor_name if data else None,
            designation=data.designation if data else None,
            address=data.address if data else None,
            subscription_plan=(data.plan if data and data.plan else "trial"),
        )
        db.add(clinic)
        db.commit()
        db.refresh(clinic)
        return clinic_out(clinic)
    except HTTPException:
        raise
    except Exception as e:
        logging.exception("create_clinic failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/clinics")
def list_clinics(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        if user.role == "admin":
            clinics = db.query(Clinic).all()
        else:
            clinics = db.query(Clinic).filter(Clinic.id == user.clinic_id).all()
        return [clinic_out(clinic) for clinic in clinics]
    except Exception as e:
        logging.exception("list_clinics failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/clinics/{clinic_id}")
def update_clinic_profile(
    clinic_id: int,
    data: ClinicProfileIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        require_clinic_access(clinic_id, user)
        clinic = db.query(Clinic).filter(Clinic.id == clinic_id).first()
        if not clinic:
            raise HTTPException(status_code=404, detail="Clinic not found")
        payload = _model_data(data, exclude_unset=True)
        if payload.get("plan") is not None:
            payload["subscription_plan"] = payload.pop("plan")
        for field, value in payload.items():
            if value is not None:
                setattr(clinic, field, value)
        if not clinic.trial_ends_at:
            clinic.trial_ends_at = datetime.utcnow() + timedelta(days=30)
        db.commit()
        db.refresh(clinic)
        return clinic_out(clinic)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logging.exception("update_clinic_profile failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/clinics/{clinic_id}")
def delete_clinic(
    clinic_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    clinic = db.query(Clinic).filter(Clinic.id == clinic_id).first()
    if not clinic:
        raise HTTPException(status_code=404, detail="Clinic not found")
    patient_count = db.query(Patient).filter(Patient.clinic_id == clinic_id).count()
    user_count = db.query(User).filter(User.clinic_id == clinic_id).count()
    appointment_count = db.query(Appointment).filter(Appointment.clinic_id == clinic_id).count()
    if patient_count or user_count or appointment_count:
        raise HTTPException(status_code=400, detail="Clinic has patients, staff, or appointments. Mark it inactive instead.")
    try:
        db.query(Service).filter(Service.clinic_id == clinic_id).delete(synchronize_session=False)
        db.delete(clinic)
        db.commit()
        return {"success": True, "id": clinic_id}
    except Exception as e:
        db.rollback()
        logging.exception("delete_clinic failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/services")
def list_services(
    clinic_id: int,
    include_inactive: bool = False,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        require_clinic_access(clinic_id, user)
        query = db.query(Service).filter(Service.clinic_id == clinic_id)
        if not include_inactive:
            query = query.filter(Service.is_active == True)
        services = query.order_by(Service.is_active.desc(), Service.name.asc()).all()
        return [service_out(service) for service in services]
    except HTTPException:
        raise
    except Exception as e:
        logging.exception("list_services failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/services")
def create_service(data: ServiceIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        require_clinic_access(data.clinic_id, user)
        service = Service(
            clinic_id=data.clinic_id,
            name=data.name.strip(),
            duration_minutes=data.duration_minutes,
            price=data.price,
            is_active=data.is_active,
        )
        db.add(service)
        db.commit()
        db.refresh(service)
        return service_out(service)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logging.exception("create_service failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/services/{service_id}")
def update_service(
    service_id: int,
    data: ServiceUpdateIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        service = db.query(Service).filter(Service.id == service_id).first()
        if not service:
            raise HTTPException(status_code=404, detail="Service not found")
        require_clinic_access(service.clinic_id, user)
        for field, value in _model_data(data, exclude_unset=True).items():
            if value is not None:
                setattr(service, field, value)
        db.commit()
        db.refresh(service)
        return service_out(service)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logging.exception("update_service failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/appointments")
def list_appointments(
    clinic_id: int,
    date_from: date | None = None,
    date_to: date | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        require_clinic_access(clinic_id, user)
        query = db.query(Appointment).filter(Appointment.clinic_id == clinic_id)
        if date_from:
            query = query.filter(Appointment.appointment_date >= date_from)
        if date_to:
            query = query.filter(Appointment.appointment_date <= date_to)
        appointments = query.order_by(Appointment.appointment_date.asc(), Appointment.appointment_time.asc()).all()
        return [appointment_out(appointment) for appointment in appointments]
    except HTTPException:
        raise
    except Exception as e:
        logging.exception("list_appointments failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/appointments/{clinic_id}")
def list_followup_appointments(
    clinic_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        require_clinic_access(clinic_id, user)
        rows = (
            db.query(
                Visit,
                Patient.name.label("patient_name"),
                Patient.phone.label("phone"),
                Patient.condition.label("patient_condition"),
                Patient.followup_type.label("patient_followup_type"),
            )
            .join(Patient, Visit.patient_id == Patient.id)
            .filter(Patient.clinic_id == clinic_id)
            .filter(Visit.next_visit.isnot(None))
            .order_by(Visit.next_visit.asc(), Visit.id.asc())
            .all()
        )
        result = []
        for visit, patient_name, phone, patient_condition, patient_followup_type in rows:
            row = visit_out(visit)
            row["patient_name"] = patient_name
            row["phone"] = phone
            row["condition"] = patient_condition or visit.condition
            row["followup_type"] = patient_followup_type
            result.append(row)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logging.exception("list_followup_appointments failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/appointments")
def create_appointment(data: AppointmentIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        require_clinic_access(data.clinic_id, user)
        appointment = Appointment(**_model_data(data))
        db.add(appointment)
        db.commit()
        db.refresh(appointment)
        return appointment_out(appointment)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logging.exception("create_appointment failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/appointments/{appointment_id}")
def update_appointment(
    appointment_id: int,
    data: AppointmentUpdateIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        appointment = db.query(Appointment).filter(Appointment.id == appointment_id).first()
        if not appointment:
            raise HTTPException(status_code=404, detail="Appointment not found")
        require_clinic_access(appointment.clinic_id, user)
        for field, value in _model_data(data, exclude_unset=True).items():
            if value is not None:
                setattr(appointment, field, value)
        db.commit()
        db.refresh(appointment)
        return appointment_out(appointment)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logging.exception("update_appointment failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/appointments/{appointment_id}/complete")
def complete_appointment(
    appointment_id: int,
    data: CompleteAppointmentIn | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        appointment = db.query(Appointment).filter(Appointment.id == appointment_id).first()
        if not appointment:
            raise HTTPException(status_code=404, detail="Appointment not found")
        require_clinic_access(appointment.clinic_id, user)

        local = _phone_local(appointment.patient_phone)
        patient = (
            db.query(Patient)
            .filter(Patient.clinic_id == appointment.clinic_id)
            .filter(or_(Patient.phone == appointment.patient_phone, Patient.phone.like(f"%{local}") if local else Patient.phone == ""))
            .first()
        )
        if not patient:
            patient = Patient(
                clinic_id=appointment.clinic_id,
                name=appointment.patient_name,
                phone=appointment.patient_phone,
                condition=(data.condition if data else None) or appointment.service_type,
                source="appointment",
                preferred_language="en",
                reminder_enabled=True,
                followup_enabled=True,
            )
            db.add(patient)
            db.flush()
            _ensure_patient_mrn(db, patient)
        elif data and data.condition:
            patient.condition = data.condition
        if patient.source == "appointment_queue":
            patient.source = "appointment"

        clinic = db.query(Clinic).filter(Clinic.id == appointment.clinic_id).first()

        visit = Visit(
            patient_id=patient.id,
            condition=(data.condition if data else None) or patient.condition or appointment.service_type,
            doctor_name=_doctor_name_for_user(user, clinic),
            doctor_designation=_designation_for_user(user, clinic),
            visit_date=appointment.appointment_date or date.today(),
            notes=(data.notes if data else None) or appointment.notes,
            prescription_text=data.prescription_text if data else None,
            status="completed",
            followup_status="completed",
        )
        patient.last_visit_at = visit.visit_date
        appointment.status = "completed"
        db.add(visit)
        db.commit()
        db.refresh(patient)
        db.refresh(appointment)
        return {"appointment": appointment_out(appointment), "patient": patient_out(patient), "visit": visit_out(visit)}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logging.exception("complete_appointment failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/appointments/{appointment_id}")
def delete_appointment(
    appointment_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        appointment = db.query(Appointment).filter(Appointment.id == appointment_id).first()
        if not appointment:
            raise HTTPException(status_code=404, detail="Appointment not found")
        require_clinic_access(appointment.clinic_id, user)
        db.delete(appointment)
        db.commit()
        return {"success": True, "id": appointment_id}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logging.exception("delete_appointment failed")
        raise HTTPException(status_code=500, detail=str(e))

def _available_slots(db: Session, clinic_id: int, service_name: str | None = None) -> list[dict]:
    base_times = ["10:00", "11:30", "14:00", "16:30"]
    slots = []
    for offset in range(1, 8):
        day = date.today() + timedelta(days=offset)
        booked = {
            row.appointment_time
            for row in db.query(Appointment)
            .filter(Appointment.clinic_id == clinic_id)
            .filter(Appointment.appointment_date == day)
            .filter(~Appointment.status.in_(["cancelled", "missed"]))
            .all()
        }
        for slot_time in base_times:
            if slot_time not in booked:
                slots.append({"date": _dt(day), "time": slot_time, "service_type": service_name})
        if len(slots) >= 8:
            break
    return slots[:8]

@app.get("/public/clinics/{clinic_id}/widget")
def public_widget_config(clinic_id: int, db: Session = Depends(get_db)):
    clinic = db.query(Clinic).filter(Clinic.id == clinic_id).first()
    if not clinic or not clinic.widget_enabled:
        raise HTTPException(status_code=404, detail="Widget unavailable")
    services = db.query(Service).filter(Service.clinic_id == clinic_id, Service.is_active == True).all()
    return {
        "clinic": clinic_out(clinic),
        "services": [service_out(service) for service in services],
        "available_slots": _available_slots(db, clinic_id),
        "embed_code": f"<script src=\"https://docnudge.in/widget.js\" data-clinic-id=\"{clinic_id}\"></script>",
    }

@app.post("/public/bookings")
def public_create_booking(data: PublicBookingIn, db: Session = Depends(get_db)):
    clinic = db.query(Clinic).filter(Clinic.id == data.clinic_id).first()
    if not clinic or not clinic.widget_enabled:
        raise HTTPException(status_code=404, detail="Clinic booking is unavailable")
    appointment = Appointment(
        clinic_id=data.clinic_id,
        patient_name=data.patient_name.strip(),
        patient_phone=data.patient_phone.strip(),
        service_type=data.service_type,
        appointment_date=data.appointment_date,
        appointment_time=data.appointment_time,
        status="booked",
        booked_via="widget",
    )
    db.add(appointment)
    db.flush()
    if data.session_id:
        conversation = db.query(AIConversation).filter(
            AIConversation.clinic_id == data.clinic_id,
            AIConversation.session_id == data.session_id,
        ).first()
        if conversation:
            conversation.appointment_id = appointment.id
    db.commit()
    db.refresh(appointment)
    return {"appointment": appointment_out(appointment), "message": "Appointment booked"}

@app.post("/public/demo-requests")
def create_demo_request(data: DemoRequestIn, db: Session = Depends(get_db)):
    payload = _model_data(data)
    for field in ("name", "clinic", "email", "phone", "role"):
        value = str(payload.get(field) or "").strip()
        if not value:
            raise HTTPException(status_code=400, detail=f"{field.replace('_', ' ').title()} is required")
        payload[field] = value

    payload["city"] = str(payload.get("city") or "").strip() or None
    payload["message"] = str(payload.get("message") or "").strip() or None
    payload["source"] = str(payload.get("source") or "landing").strip() or "landing"
    payload["status"] = "new"

    request = DemoRequest(**payload)
    db.add(request)
    db.commit()
    db.refresh(request)
    return {"request": demo_request_out(request), "message": "Demo request received"}

@app.post("/public/widget-chat")
def public_widget_chat(data: WidgetChatIn, db: Session = Depends(get_db)):
    clinic = db.query(Clinic).filter(Clinic.id == data.clinic_id).first()
    if not clinic or not clinic.widget_enabled:
        raise HTTPException(status_code=404, detail="Widget unavailable")
    services = db.query(Service).filter(Service.clinic_id == data.clinic_id, Service.is_active == True).all()
    service_names = [service.name for service in services] or ["General Checkup"]
    slots = _available_slots(db, data.clinic_id)
    text = data.user_message.strip()
    lowered = text.lower()
    if any(word in lowered for word in ["book", "appointment", "checkup", "visit", "tomorrow"]):
        reply = "Great. I can book that. Choose one of these slots, then share your name and WhatsApp number."
        intent = "book_appointment"
    elif any(word in lowered for word in ["name", "phone", "number"]):
        reply = "Perfect. Pick a slot above and confirm your WhatsApp number to finish booking."
        intent = "collect_details"
    else:
        reply = f"Hi, I can help with {', '.join(service_names[:3])}. What would you like to book?"
        intent = "ask_service"
    conversation = db.query(AIConversation).filter(
        AIConversation.clinic_id == data.clinic_id,
        AIConversation.session_id == data.session_id,
    ).first()
    messages = []
    if conversation and conversation.messages_json:
        try:
            messages = json.loads(conversation.messages_json)
        except json.JSONDecodeError:
            messages = []
    messages.extend([{"role": "patient", "content": text}, {"role": "assistant", "content": reply}])
    if not conversation:
        conversation = AIConversation(clinic_id=data.clinic_id, session_id=data.session_id)
        db.add(conversation)
    conversation.messages_json = json.dumps(messages[-20:])
    db.commit()
    return {"intent": intent, "reply": reply, "services": service_names, "available_slots": slots}

def _generate_weekly_report(db: Session, clinic: Clinic, week_start: date) -> WeeklyReport:
    week_end = week_start + timedelta(days=7)
    total_patients = db.query(Patient).filter(Patient.clinic_id == clinic.id).count()
    visits = (
        db.query(Visit)
        .join(Patient, Patient.id == Visit.patient_id)
        .filter(Patient.clinic_id == clinic.id)
        .filter(Visit.visit_date >= week_start)
        .filter(Visit.visit_date < week_end)
        .all()
    )
    completed = [visit for visit in visits if _followup_status_for(visit) == "completed"]
    missed = [visit for visit in visits if _followup_status_for(visit) == "missed"]
    return_rate = round((len(completed) / max(len(completed) + len(missed), 1)) * 100)
    summary = (
        f"{len(visits)} visits this week. {len(missed)} missed follow-ups. "
        f"Return rate is {return_rate}%. Focus on chronic patients inactive for 30+ days."
    )
    report = WeeklyReport(
        clinic_id=clinic.id,
        week_start=week_start,
        total_patients=total_patients,
        visits_completed=len(completed),
        missed_count=len(missed),
        return_rate=return_rate,
        ai_summary=summary,
    )
    return report

@app.get("/weekly-reports")
def list_weekly_reports(clinic_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    require_clinic_access(clinic_id, user)
    reports = db.query(WeeklyReport).filter(WeeklyReport.clinic_id == clinic_id).order_by(WeeklyReport.week_start.desc()).limit(12).all()
    return [weekly_report_out(report) for report in reports]

@app.post("/weekly-reports/generate")
def generate_weekly_report(
    clinic_id: int,
    send_whatsapp: bool = False,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        require_clinic_access(clinic_id, user)
        clinic = db.query(Clinic).filter(Clinic.id == clinic_id).first()
        if not clinic:
            raise HTTPException(status_code=404, detail="Clinic not found")
        week_start = date.today() - timedelta(days=date.today().weekday())
        report = _generate_weekly_report(db, clinic, week_start)
        if send_whatsapp and (clinic.whatsapp_number or clinic.phone):
            from app.whatsapp import send_whatsapp_message
            result = send_whatsapp_message(
                clinic.whatsapp_number or clinic.phone,
                clinic.name,
                "weekly_report",
                context={
                    "clinic_name": clinic.name,
                    "total_patients": str(report.total_patients),
                    "visits_completed": str(report.visits_completed),
                    "missed_count": str(report.missed_count),
                    "return_rate": f"{report.return_rate}%",
                    "summary": report.ai_summary,
                },
            )
            if "error" not in result:
                report.sent_at = datetime.utcnow()
        db.add(report)
        db.commit()
        db.refresh(report)
        return weekly_report_out(report)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logging.exception("generate_weekly_report failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/billing/{clinic_id}")
def billing_summary(clinic_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    require_clinic_access(clinic_id, user)
    clinic = db.query(Clinic).filter(Clinic.id == clinic_id).first()
    if not clinic:
        raise HTTPException(status_code=404, detail="Clinic not found")
    plans = [
        {"id": "basic", "name": "Basic", "price": 999, "features": ["Unlimited reminders", "Patient records", "Search", "Basic widget"]},
        {"id": "pro", "name": "Pro", "price": 1999, "features": ["AI booking widget", "AI weekly report", "WhatsApp bot", "Priority support"]},
        {"id": "chain", "name": "Clinic Chain", "price": 4999, "features": ["Up to 5 clinics", "Custom branding", "Dedicated support"]},
    ]
    return {"clinic": clinic_out(clinic), "plans": plans}

@app.post("/billing/{clinic_id}/checkout")
def billing_checkout(clinic_id: int, plan: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    require_clinic_access(clinic_id, user)
    clinic = db.query(Clinic).filter(Clinic.id == clinic_id).first()
    if not clinic:
        raise HTTPException(status_code=404, detail="Clinic not found")
    clinic.subscription_plan = plan
    clinic.billing_status = "checkout_pending"
    db.commit()
    return {"checkout_url": f"https://rzp.io/i/docnudge-{clinic_id}-{plan}", "plan": plan}

PATIENT_OTP_STORE: dict[str, dict] = {}

def _ensure_emergency_token(patient: Patient) -> str:
    if not patient.emergency_token:
        patient.emergency_token = uuid.uuid4().hex
    return patient.emergency_token

def _find_patient_by_phone(db: Session, phone: str) -> Patient | None:
    local = _phone_local(phone)
    if not local:
        return None
    return (
        db.query(Patient)
        .filter(or_(Patient.phone == local, Patient.phone.like(f"%{local}")))
        .order_by(Patient.created_at.desc(), Patient.id.desc())
        .first()
    )

def _find_testing_patient(db: Session) -> Patient | None:
    configured_phone = os.getenv("PATIENT_TEST_PHONE", "").strip()
    if configured_phone:
        patient = _find_patient_by_phone(db, configured_phone)
        if patient:
            return patient
    return db.query(Patient).order_by(Patient.created_at.desc(), Patient.id.desc()).first()

def _get_patient_from_token(token: str, db: Session) -> Patient:
    payload = decode_token(token)
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub.startswith("patient:"):
        raise HTTPException(status_code=401, detail="Invalid patient token")
    try:
        patient_id = int(sub.split(":", 1)[1])
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid patient token")
    patient = db.query(Patient).filter(Patient.id == patient_id).first()
    if not patient:
        raise HTTPException(status_code=401, detail="Patient not found")
    return patient

def get_current_patient(request: Request, db: Session = Depends(get_db)) -> Patient:
    auth_header = request.headers.get("Authorization") or ""
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Missing patient token")
    return _get_patient_from_token(token, db)

def _clinic_for_patient(db: Session, patient: Patient) -> Clinic | None:
    return db.query(Clinic).filter(Clinic.id == patient.clinic_id).first()

def _prescription_out(rx: Prescription, patient: Patient | None = None, clinic: Clinic | None = None) -> dict:
    medicines = rx.medicines or []
    return {
        "id": rx.id,
        "patient_id": rx.patient_id,
        "date": _dt(rx.created_at),
        "doctor": clinic.doctor_name if clinic else None,
        "clinic": clinic.name if clinic else None,
        "diagnosis": patient.condition if patient else "",
        "medicines": medicines,
        "advice": rx.notes or "",
        "followUp": None,
        "notes": rx.notes or "",
        "created_at": _dt(rx.created_at),
    }

def _lab_out(lab: LabResult) -> dict:
    return {
        "id": lab.id,
        "patient_id": lab.patient_id,
        "name": lab.test_name,
        "test_name": lab.test_name,
        "result": lab.result,
        "reference_range": lab.reference_range,
        "date": lab.test_date,
        "test_date": lab.test_date,
        "status": lab.status or "normal",
        "category": "general",
        "results": [
            {
                "test": lab.test_name,
                "value": lab.result,
                "range": lab.reference_range or "",
                "status": lab.status or "normal",
            }
        ],
        "aiNote": "",
        "created_at": _dt(lab.created_at),
    }

def _portal_patient_payload(patient: Patient, db: Session) -> dict:
    clinic = _clinic_for_patient(db, patient)
    visits = db.query(Visit).filter(Visit.patient_id == patient.id).order_by(Visit.visit_date.desc()).all()
    latest = visits[0] if visits else None
    _ensure_emergency_token(patient)
    return {
        **patient_out(patient),
        "bloodGroup": patient.blood_group,
        "emergencyContact": {
            "name": patient.emergency_name or "",
            "phone": patient.emergency_phone or "",
            "relation": patient.emergency_relation or "",
        },
        "clinic": clinic.name if clinic else "DocNudge Clinic",
        "lastDoctor": clinic.doctor_name if clinic and clinic.doctor_name else "Doctor",
        "latest_visit": visit_out(latest) if latest else None,
    }

@app.post("/portal/access")
def portal_access(data: PortalAccessIn, db: Session = Depends(get_db)):
    digits = _digits_only(data.phone)
    local = _phone_local(data.phone)
    patient = (
        db.query(Patient)
        .filter(Patient.clinic_id == data.clinic_id)
        .filter(or_(Patient.phone == digits, Patient.phone == local, Patient.phone.like(f"%{local}")))
        .first()
    )
    if not patient:
        raise HTTPException(status_code=404, detail="No patient found for this phone")
    visits = db.query(Visit).filter(Visit.patient_id == patient.id).order_by(Visit.visit_date.desc()).all()
    return {"patient": _patient_summary(patient, visits), "visits": [visit_out(visit) for visit in visits]}


@app.post("/patient/auth/login")
def patient_phone_login(data: PatientPhoneLoginIn, db: Session = Depends(get_db)):
    patient = _find_patient_by_phone(db, data.phone)
    if not patient:
        raise HTTPException(status_code=404, detail="No patient found for this phone")

    _ensure_emergency_token(patient)
    db.commit()
    clinic = _clinic_for_patient(db, patient)
    token = create_token(
        {
            "sub": f"patient:{patient.id}",
            "role": "patient",
            "clinic_id": patient.clinic_id,
            "clinic_name": clinic.name if clinic else None,
        }
    )
    return {"token": token, "patient": _portal_patient_payload(patient, db)}


@app.post("/patient/auth/test-login")
def patient_test_login(db: Session = Depends(get_db)):
    if os.getenv("PATIENT_TEST_LOGIN_ENABLED", "").strip().lower() not in {"1", "true", "yes"}:
        raise HTTPException(status_code=404, detail="Testing login is disabled")
    patient = _find_testing_patient(db)
    if not patient:
        raise HTTPException(status_code=404, detail="No patient records are available for testing")

    _ensure_emergency_token(patient)
    db.commit()
    clinic = _clinic_for_patient(db, patient)
    token = create_token(
        {
            "sub": f"patient:{patient.id}",
            "role": "patient",
            "clinic_id": patient.clinic_id,
            "clinic_name": clinic.name if clinic else None,
        }
    )
    return {"token": token, "patient": _portal_patient_payload(patient, db), "testing": True}


@app.post("/patient/auth/send-otp")
def patient_send_otp(data: PatientOtpIn, db: Session = Depends(get_db)):
    patient = _find_patient_by_phone(db, data.phone)
    if not patient:
        raise HTTPException(status_code=404, detail="No patient found for this phone")

    otp = f"{random.randint(100000, 999999)}"
    key = _phone_local(data.phone)
    PATIENT_OTP_STORE[key] = {
        "otp": otp,
        "patient_id": patient.id,
        "expires_at": datetime.utcnow() + timedelta(minutes=10),
    }

    clinic = _clinic_for_patient(db, patient)
    try:
        from app.whatsapp import send_whatsapp_message

        send_whatsapp_message(
            patient.phone,
            patient.name,
            "thank_you",
            {
                "clinic_name": clinic.name if clinic else "DocNudge",
                "condition": "Patient portal login",
                "next_visit": None,
                "care_tip": f"Your DocNudge OTP is {otp}. It is valid for 10 minutes.",
            },
        )
    except Exception:
        logging.exception("patient OTP WhatsApp send failed")

    return {"success": True, "message": "OTP sent if WhatsApp is configured for this clinic."}


@app.post("/patient/auth/verify-otp")
def patient_verify_otp(data: PatientOtpVerifyIn, db: Session = Depends(get_db)):
    key = _phone_local(data.phone)
    record = PATIENT_OTP_STORE.get(key)
    if not record or record["expires_at"] < datetime.utcnow() or str(record["otp"]) != str(data.otp).strip():
        raise HTTPException(status_code=401, detail="Invalid or expired OTP")

    patient = db.query(Patient).filter(Patient.id == record["patient_id"]).first()
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")

    _ensure_emergency_token(patient)
    db.commit()
    PATIENT_OTP_STORE.pop(key, None)
    clinic = _clinic_for_patient(db, patient)
    token = create_token(
        {
            "sub": f"patient:{patient.id}",
            "role": "patient",
            "clinic_id": patient.clinic_id,
            "clinic_name": clinic.name if clinic else None,
        }
    )
    return {"token": token, "patient": _portal_patient_payload(patient, db)}


@app.get("/patient/me")
def patient_me(patient: Patient = Depends(get_current_patient), db: Session = Depends(get_db)):
    _ensure_emergency_token(patient)
    db.commit()
    return _portal_patient_payload(patient, db)


@app.put("/patient/me")
def patient_update_me(
    data: PatientProfileUpdateIn,
    patient: Patient = Depends(get_current_patient),
    db: Session = Depends(get_db),
):
    payload = _model_data(data, exclude_unset=True)
    for field, value in payload.items():
        if field in {"allergies", "conditions"} and value is not None:
            value = [str(item).strip() for item in value if str(item).strip()]
        setattr(patient, field, value)
    _ensure_emergency_token(patient)
    db.commit()
    db.refresh(patient)
    return _portal_patient_payload(patient, db)


@app.get("/patient/visits")
def patient_visits(patient: Patient = Depends(get_current_patient), db: Session = Depends(get_db)):
    visits = db.query(Visit).filter(Visit.patient_id == patient.id).order_by(Visit.visit_date.desc()).all()
    return [visit_out(visit) for visit in visits]


@app.get("/patient/prescriptions")
def patient_prescriptions(patient: Patient = Depends(get_current_patient), db: Session = Depends(get_db)):
    clinic = _clinic_for_patient(db, patient)
    prescriptions = db.query(Prescription).filter(Prescription.patient_id == patient.id).order_by(Prescription.created_at.desc()).all()
    return [_prescription_out(rx, patient, clinic) for rx in prescriptions]


@app.get("/patient/prescriptions/{prescription_id}/pdf")
def patient_prescription_pdf(
    prescription_id: int,
    patient: Patient = Depends(get_current_patient),
    db: Session = Depends(get_db),
):
    rx = (
        db.query(Prescription)
        .filter(Prescription.id == prescription_id)
        .filter(Prescription.patient_id == patient.id)
        .first()
    )
    if not rx:
        raise HTTPException(status_code=404, detail="Prescription not found")
    clinic = _clinic_for_patient(db, patient)
    payload = _prescription_out(rx, patient, clinic)
    lines = [
        "DocNudge Prescription",
        f"Patient: {patient.name}",
        f"Clinic: {payload.get('clinic') or 'Clinic'}",
        f"Date: {payload.get('date') or ''}",
        "",
        "Medicines:",
    ]
    for medicine in payload["medicines"]:
        lines.append(
            f"- {medicine.get('name', '')}: {medicine.get('dosage', '')} "
            f"{medicine.get('frequency', '')} for {medicine.get('duration', '')}"
        )
    if payload["notes"]:
        lines.extend(["", f"Notes: {payload['notes']}"])
    return Response(
        "\n".join(lines),
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="prescription-{rx.id}.txt"'},
    )


@app.get("/patient/lab-reports")
def patient_lab_reports(patient: Patient = Depends(get_current_patient), db: Session = Depends(get_db)):
    labs = db.query(LabResult).filter(LabResult.patient_id == patient.id).order_by(LabResult.test_date.desc()).all()
    return [_lab_out(lab) for lab in labs]


@app.post("/patient/lab-reports")
async def patient_upload_lab_report(
    name: str = Form("Uploaded lab report"),
    file: UploadFile = File(...),
    patient: Patient = Depends(get_current_patient),
    db: Session = Depends(get_db),
):
    lab = LabResult(
        patient_id=patient.id,
        test_name=name or file.filename or "Uploaded lab report",
        result=file.filename or "Uploaded file",
        reference_range="",
        test_date=date.today().isoformat(),
        status="normal",
    )
    db.add(lab)
    db.commit()
    db.refresh(lab)
    return _lab_out(lab)


@app.get("/patient/appointments")
def patient_appointments(patient: Patient = Depends(get_current_patient), db: Session = Depends(get_db)):
    local = _phone_local(patient.phone)
    appointments = (
        db.query(Appointment)
        .filter(Appointment.clinic_id == patient.clinic_id)
        .filter(or_(Appointment.patient_phone == patient.phone, Appointment.patient_phone.like(f"%{local}")))
        .order_by(Appointment.appointment_date.desc(), Appointment.appointment_time.desc())
        .all()
    )
    rows = []
    for appointment in appointments:
        row = appointment_out(appointment)
        if appointment.status in {"cancelled", "missed"}:
            row["portal_status"] = "missed"
        elif appointment.appointment_date and appointment.appointment_date < date.today():
            row["portal_status"] = "completed"
        else:
            row["portal_status"] = "upcoming"
        rows.append(row)
    return rows


@app.post("/patient/appointments")
def patient_create_appointment(
    data: AppointmentUpdateIn,
    patient: Patient = Depends(get_current_patient),
    db: Session = Depends(get_db),
):
    if not data.appointment_date or not data.appointment_time:
        raise HTTPException(status_code=400, detail="Appointment date and time are required")
    appointment = Appointment(
        clinic_id=patient.clinic_id,
        patient_name=patient.name,
        patient_phone=patient.phone,
        service_type=data.service_type or "Consultation",
        appointment_date=data.appointment_date,
        appointment_time=data.appointment_time,
        status="booked",
        booked_via="patient_portal",
        notes=data.notes,
    )
    db.add(appointment)
    db.commit()
    db.refresh(appointment)
    return appointment_out(appointment)


@app.get("/emergency/{token}")
def emergency_profile(token: str, db: Session = Depends(get_db)):
    patient = db.query(Patient).filter(Patient.emergency_token == token).first()
    if not patient:
        raise HTTPException(status_code=404, detail="Emergency profile not found")
    clinic = _clinic_for_patient(db, patient)
    latest_rx = db.query(Prescription).filter(Prescription.patient_id == patient.id).order_by(Prescription.created_at.desc()).first()
    medicines = []
    if latest_rx:
        medicines = [m.get("name", "") for m in (latest_rx.medicines or []) if m.get("name")]
    return {
        "name": patient.name,
        "age": patient.age,
        "gender": patient.gender,
        "blood_group": patient.blood_group,
        "allergies": patient.allergies or [],
        "conditions": patient.conditions or ([patient.condition] if patient.condition else []),
        "current_medicines": medicines,
        "emergency_contact": {
            "name": patient.emergency_name,
            "phone": patient.emergency_phone,
            "relation": patient.emergency_relation,
        },
        "last_doctor": clinic.doctor_name if clinic else None,
        "clinic": clinic.name if clinic else None,
    }

@app.post("/admin/clinics/dedupe")
def dedupe_clinics(
    dry_run: bool = False,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        clinics = db.query(Clinic).order_by(Clinic.id.asc()).all()
        groups: dict[str, list[Clinic]] = {}
        for clinic in clinics:
            normalized = " ".join(clinic.name.casefold().split())
            groups.setdefault(normalized, []).append(clinic)

        merged = []
        for duplicates in groups.values():
            if len(duplicates) < 2:
                continue

            keep = duplicates[0]
            remove = duplicates[1:]
            for clinic in remove:
                patient_count = db.query(Patient).filter(Patient.clinic_id == clinic.id).count()
                user_count = db.query(User).filter(User.clinic_id == clinic.id).count()
                merged.append(
                    {
                        "from_id": clinic.id,
                        "to_id": keep.id,
                        "name": clinic.name,
                        "patients": patient_count,
                        "users": user_count,
                    }
                )

                if not dry_run:
                    db.query(Patient).filter(Patient.clinic_id == clinic.id).update(
                        {Patient.clinic_id: keep.id},
                        synchronize_session=False,
                    )
                    db.query(User).filter(User.clinic_id == clinic.id).update(
                        {User.clinic_id: keep.id},
                        synchronize_session=False,
                    )
                    db.delete(clinic)

        if not dry_run:
            db.commit()

        return {"dry_run": dry_run, "merged": merged}
    except Exception as e:
        db.rollback()
        logging.exception("dedupe_clinics failed")
        raise HTTPException(status_code=500, detail=str(e))

# ── Patient routes ─────────────────────────────────────────
# Retention product API
def _clinic_id_for(user: User, clinic_id: int | None = None) -> int:
    if user.role == "admin":
        if clinic_id is not None:
            return clinic_id
        raise HTTPException(status_code=400, detail="clinic_id is required for admin users")
    if user.clinic_id is None:
        raise HTTPException(status_code=403, detail="User is not assigned to a clinic")
    if clinic_id is not None and clinic_id != user.clinic_id:
        raise HTTPException(status_code=403, detail="Access denied for this clinic")
    return user.clinic_id

def _patient_for_user(patient_id: int, user: User, db: Session) -> Patient:
    patient = db.query(Patient).filter(Patient.id == patient_id).first()
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    require_clinic_access(patient.clinic_id, user)
    return patient

def _followup_date_for(visit: Visit):
    return visit.followup_date or visit.next_visit

def _followup_status_for(visit: Visit) -> str:
    return visit.followup_status or visit.status or "due"

def _set_visit_status(visit: Visit, status: str):
    visit.followup_status = status
    visit.status = status

def _sync_missed_followups(db: Session, clinic_id: int):
    rows = (
        db.query(Visit)
        .join(Patient, Patient.id == Visit.patient_id)
        .filter(Patient.clinic_id == clinic_id)
        .filter(Visit.followup_date.isnot(None))
        .filter(Visit.followup_date < date.today())
        .filter(~Visit.followup_status.in_(["completed", "closed", "missed", "none"]))
        .all()
    )
    for visit in rows:
        _set_visit_status(visit, "missed")
    if rows:
        db.commit()

def _patient_summary(patient: Patient, visits: list[Visit] | None = None) -> dict:
    visits = visits if visits is not None else patient.visits
    latest = sorted(visits, key=lambda visit: visit.visit_date or date.min, reverse=True)[0] if visits else None
    return {
        **patient_out(patient),
        "condition": (latest.condition if latest and latest.condition else patient.condition),
        "latest_visit": visit_out(latest) if latest else None,
    }

def _create_visit(db: Session, patient: Patient, data: RetentionVisitIn) -> Visit:
    has_followup = data.followup_required and data.followup_date is not None
    status = "due" if has_followup else "none"
    visit = Visit(
        patient_id=patient.id,
        condition=data.condition,
        visit_date=data.visit_date,
        next_visit=data.followup_date,
        followup_date=data.followup_date,
        followup_status=status,
        status=status,
        notes=data.notes,
        prescription_text=data.prescription_text,
        doctor_notes=data.doctor_notes,
    )
    patient.condition = data.condition
    patient.last_visit_at = data.visit_date
    db.add(visit)
    return visit

def _reminder_type_for(visit: Visit) -> str:
    followup_date = _followup_date_for(visit)
    if not followup_date:
        return "morning"
    days_until = (followup_date - date.today()).days
    if days_until >= 2:
        return "two_days_before"
    if days_until == 1:
        return "day_before"
    return "morning"

def _clinic_name_for(patient: Patient) -> str:
    if patient.clinic and patient.clinic.name:
        return patient.clinic.name
    return "your clinic"


def _clinic_info_for(patient: Patient) -> Clinic | None:
    return patient.clinic if patient and patient.clinic else None

def _whatsapp_context(patient: Patient, visit: Visit) -> dict:
    clinic = _clinic_info_for(patient)
    return {
        "clinic_name": _clinic_name_for(patient),
        "clinic_phone": clinic.phone if clinic else None,
        "clinic_address": clinic.address if clinic else None,
        "condition": visit.condition or patient.condition,
        "followup_date": _followup_date_for(visit),
        "next_visit": _followup_date_for(visit),
    }

def _save_message_log(db: Session, patient: Patient, message_type: str, result: dict) -> dict:
    success = "error" not in result
    log = ReminderLog(
        patient_id=patient.id,
        reminder_type=message_type,
        success=success,
        error=result.get("error") if not success else None,
    )
    db.add(log)
    db.commit()
    return reminder_log_out(log)

def _send_visit_thank_you(db: Session, patient: Patient, visit: Visit) -> dict:
    if patient.opted_out:
        return {"skipped": True, "reason": "patient_opted_out"}

    from app.whatsapp import send_visit_thank_you_message

    result = send_visit_thank_you_message(
        phone=patient.phone,
        patient_name=patient.name,
        condition=visit.condition or patient.condition,
        next_visit=_followup_date_for(visit),
        clinic_name=_clinic_name_for(patient),
    )
    try:
        result["reminder"] = _save_message_log(db, patient, "thank_you", result)
    except Exception:
        db.rollback()
        logging.exception("Could not save thank-you message log")
    return result

@app.get("/retention/dashboard")
def retention_dashboard(
    clinic_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        cid = _clinic_id_for(user, clinic_id)
        _sync_missed_followups(db, cid)
        today = date.today()
        patients = db.query(Patient).filter(Patient.clinic_id == cid).all()
        visits = (
            db.query(Visit)
            .join(Patient, Patient.id == Visit.patient_id)
            .filter(Patient.clinic_id == cid)
            .all()
        )
        visits_today = [visit for visit in visits if visit.visit_date == today]
        due_today = [
            visit
            for visit in visits
            if _followup_date_for(visit) == today and _followup_status_for(visit) in ["due", "upcoming"]
        ]
        missed = [visit for visit in visits if _followup_status_for(visit) == "missed"]
        completed = [visit for visit in visits if _followup_status_for(visit) == "completed"]
        patient_by_id = {patient.id: patient for patient in patients}
        return {
            "stats": {
                "total_patients": len(patients),
                "visits_today": len(visits_today),
                "followups_due": len(due_today),
                "missed_followups": len(missed),
                "return_rate": round((len(completed) / max(len(completed) + len(missed), 1)) * 100),
            },
            "today_patients": [
                {"patient": patient_out(patient_by_id[visit.patient_id]), "visit": visit_out(visit)}
                for visit in sorted(visits_today, key=lambda item: item.id, reverse=True)
            ],
            "followups_due": [
                {"patient": patient_out(patient_by_id[visit.patient_id]), "visit": visit_out(visit)}
                for visit in sorted(due_today, key=lambda item: item.id, reverse=True)
            ],
            "missed_followups": [
                {"patient": patient_out(patient_by_id[visit.patient_id]), "visit": visit_out(visit)}
                for visit in sorted(missed, key=lambda item: _followup_date_for(item) or date.min)
            ][:20],
        }
    except HTTPException:
        raise
    except Exception as e:
        logging.exception("retention_dashboard failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/retention/search")
def retention_search(
    q: str = "",
    clinic_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        cid = _clinic_id_for(user, clinic_id)
        query = f"%{q.strip()}%"
        patients_query = db.query(Patient).filter(Patient.clinic_id == cid)
        if q.strip():
            patients_query = (
                patients_query.outerjoin(Visit, Visit.patient_id == Patient.id)
                .filter(
                    or_(
                        Patient.name.ilike(query),
                        Patient.phone.like(query),
                        Patient.condition.ilike(query),
                        Visit.condition.ilike(query),
                    )
                )
                .distinct()
            )
        patients = patients_query.order_by(Patient.created_at.desc()).limit(30).all()
        return [_patient_summary(patient) for patient in patients]
    except HTTPException:
        raise
    except Exception as e:
        logging.exception("retention_search failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/retention/patients")
def retention_add_patient(
    data: RetentionPatientIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        require_clinic_access(data.clinic_id, user)
        patient = Patient(
            clinic_id=data.clinic_id,
            name=data.name.strip(),
            phone=data.phone.strip(),
            condition=data.condition.strip(),
            source=data.source or "walk_in",
            preferred_language=data.preferred_language or "en",
        )
        db.add(patient)
        db.flush()
        visit = _create_visit(
            db,
            patient,
            RetentionVisitIn(
                condition=data.condition,
                visit_date=data.visit_date,
                followup_required=data.followup_required,
                followup_date=data.followup_date,
                notes=data.notes,
                prescription_text=data.prescription_text,
                doctor_notes=data.doctor_notes,
            ),
        )
        db.commit()
        db.refresh(patient)
        db.refresh(visit)
        thank_you = _send_visit_thank_you(db, patient, visit)
        return {"patient": _patient_summary(patient, [visit]), "visit": visit_out(visit), "thank_you": thank_you}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logging.exception("retention_add_patient failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/retention/patients/{patient_id}")
def retention_patient_card(
    patient_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        patient = _patient_for_user(patient_id, user, db)
        visits = (
            db.query(Visit)
            .filter(Visit.patient_id == patient.id)
            .order_by(Visit.visit_date.desc(), Visit.id.desc())
            .all()
        )
        logs = (
            db.query(ReminderLog)
            .filter(ReminderLog.patient_id == patient.id)
            .order_by(ReminderLog.sent_at.desc())
            .limit(20)
            .all()
        )
        return {
            "patient": _patient_summary(patient, visits),
            "visits": [visit_out(visit) for visit in visits],
            "reminders": [reminder_log_out(log) for log in logs],
        }
    except HTTPException:
        raise
    except Exception as e:
        logging.exception("retention_patient_card failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/retention/patients/{patient_id}")
def retention_update_patient(
    patient_id: int,
    data: RetentionPatientUpdateIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        patient = _patient_for_user(patient_id, user, db)
        if data.name is not None:
            patient.name = data.name.strip()
        if data.phone is not None:
            patient.phone = data.phone.strip()
        if data.condition is not None:
            patient.condition = data.condition.strip()
        db.commit()
        db.refresh(patient)
        return _patient_summary(patient)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logging.exception("retention_update_patient failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/retention/patients/{patient_id}")
def retention_delete_patient(
    patient_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        patient = _patient_for_user(patient_id, user, db)
        db.query(ReminderLog).filter(ReminderLog.patient_id == patient.id).delete(synchronize_session=False)
        db.query(Visit).filter(Visit.patient_id == patient.id).delete(synchronize_session=False)
        db.delete(patient)
        db.commit()
        return {"deleted": True, "patient_id": patient_id}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logging.exception("retention_delete_patient failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/retention/patients/{patient_id}/visits")
def retention_add_visit(
    patient_id: int,
    data: RetentionVisitIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        patient = _patient_for_user(patient_id, user, db)
        visit = _create_visit(db, patient, data)
        db.commit()
        db.refresh(visit)
        thank_you = _send_visit_thank_you(db, patient, visit)
        return {**visit_out(visit), "thank_you": thank_you}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logging.exception("retention_add_visit failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/retention/patients/{patient_id}/close")
def retention_close_case(
    patient_id: int,
    data: CloseCaseIn | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        patient = _patient_for_user(patient_id, user, db)
        visits = db.query(Visit).filter(Visit.patient_id == patient.id).all()
        for visit in visits:
            if _followup_status_for(visit) in ["due", "upcoming", "missed"]:
                _set_visit_status(visit, "closed")
        db.commit()
        return {"status": "closed", "patient_id": patient.id}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logging.exception("retention_close_case failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/retention/visits/{visit_id}/complete")
def retention_complete_followup(
    visit_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        visit = db.query(Visit).filter(Visit.id == visit_id).first()
        if not visit:
            raise HTTPException(status_code=404, detail="Visit not found")
        _patient_for_user(visit.patient_id, user, db)
        _set_visit_status(visit, "completed")
        db.commit()
        return visit_out(visit)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logging.exception("retention_complete_followup failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/retention/visits/{visit_id}/send-reminder")
def retention_send_reminder(
    visit_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        visit = db.query(Visit).filter(Visit.id == visit_id).first()
        if not visit:
            raise HTTPException(status_code=404, detail="Visit not found")
        patient = _patient_for_user(visit.patient_id, user, db)
        if patient.opted_out:
            raise HTTPException(status_code=400, detail="Patient has opted out")
        if not patient.reminder_enabled or not patient.followup_enabled:
            raise HTTPException(status_code=400, detail="Reminders are disabled for this patient")

        from app.whatsapp import send_whatsapp_message

        reminder_type = _reminder_type_for(visit)
        result = send_whatsapp_message(
            patient.phone,
            patient.name,
            reminder_type,
            context=_whatsapp_context(patient, visit),
        )
        reminder = _save_message_log(db, patient, reminder_type, result)
        return {"success": "error" not in result, "result": result, "reminder": reminder}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logging.exception("retention_send_reminder failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/patients")
def add_patient(data: PatientIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        require_clinic_access(data.clinic_id, user)
        followup_enabled = True if data.followup_enabled is None else bool(data.followup_enabled)
        reminder_enabled = bool(data.reminder_enabled) if data.reminder_enabled is not None else True
        if not followup_enabled:
            reminder_enabled = False
        patient = Patient(
            clinic_id=data.clinic_id,
            name=data.name,
            phone=data.phone,
            condition=data.condition,
            age=data.age,
            gender=data.gender,
            followup_type=data.followup_type,
            source=data.source or "walk_in",
            preferred_language=data.preferred_language or "en",
            reminder_enabled=reminder_enabled,
            followup_enabled=followup_enabled,
        )
        db.add(patient)
        db.flush()
        _ensure_patient_mrn(db, patient)
        db.commit()
        db.refresh(patient)
        return patient_out(patient)
    except HTTPException:
        raise
    except Exception as e:
        logging.exception(f"add_patient failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/patients/{patient_id}")
def get_patient(patient_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    patient = db.query(Patient).filter(Patient.id == patient_id).first()
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    require_clinic_access(patient.clinic_id, user)
    return patient_out(patient)


@app.put("/patients/{patient_id}")
def update_patient(
    patient_id: int,
    data: PatientUpdateIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    patient = db.query(Patient).filter(Patient.id == patient_id).first()
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    require_clinic_access(patient.clinic_id, user)
    payload = _model_data(data, exclude_unset=True)
    if payload.get("followup_enabled") is False:
        payload["reminder_enabled"] = False
    for field, value in payload.items():
        setattr(patient, field, value)
    _ensure_patient_mrn(db, patient)
    db.commit()
    db.refresh(patient)
    return patient_out(patient)


@app.delete("/patients/{patient_id}")
def delete_patient(patient_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    patient = db.query(Patient).filter(Patient.id == patient_id).first()
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    require_clinic_access(patient.clinic_id, user)
    try:
        db.query(ReminderLog).filter(ReminderLog.patient_id == patient.id).delete(synchronize_session=False)
        db.query(Prescription).filter(Prescription.patient_id == patient.id).delete(synchronize_session=False)
        db.query(LabResult).filter(LabResult.patient_id == patient.id).delete(synchronize_session=False)
        db.query(Visit).filter(Visit.patient_id == patient.id).delete(synchronize_session=False)
        db.delete(patient)
        db.commit()
        return {"success": True, "id": patient_id}
    except Exception as e:
        db.rollback()
        logging.exception("delete_patient failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/patients/clinic/{clinic_id}")
def list_patients(
    clinic_id: int,
    search: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        require_clinic_access(clinic_id, user)
        query = db.query(Patient).filter(Patient.clinic_id == clinic_id).filter(Patient.source != "appointment_queue")
        if search:
            like = f"%{search.strip()}%"
            query = query.filter(
                or_(
                    Patient.name.ilike(like),
                    Patient.mrn.ilike(like),
                    Patient.phone.ilike(like),
                    Patient.condition.ilike(like),
                    Patient.followup_type.ilike(like),
                )
            )
        patients = query.order_by(Patient.last_visit_at.desc().nullslast(), Patient.created_at.desc(), Patient.id.desc()).all()
        return [patient_out(patient) for patient in patients]
    except HTTPException:
        raise
    except Exception as e:
        logging.exception(f"list_patients failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ── Visit routes ───────────────────────────────────────────
@app.post("/visits")
def add_visit(data: VisitIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        patient = _patient_for_user(data.patient_id, user, db)
        clinic = db.query(Clinic).filter(Clinic.id == patient.clinic_id).first()
        visit = Visit(
            patient_id=data.patient_id,
            condition=data.condition or patient.condition,
            doctor_name=_doctor_name_for_user(user, clinic),
            doctor_designation=_designation_for_user(user, clinic),
            visit_date=data.visit_date,
            next_visit=data.followup_date or data.next_visit,
            followup_date=data.followup_date or data.next_visit,
            notes=data.notes,
            prescription_text=data.prescription_text,
            doctor_notes=data.doctor_notes,
            status=data.followup_status or data.status,
            followup_status=data.followup_status or data.status,
        )
        if data.condition:
            patient.condition = data.condition
        if patient.source == "appointment_queue":
            patient.source = "appointment"
        patient.last_visit_at = data.visit_date
        db.add(visit)
        db.commit()
        db.refresh(visit)
        thank_you = _send_visit_thank_you(db, patient, visit)
        return {**visit_out(visit), "thank_you": thank_you}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logging.exception(f"add_visit failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/visits/{visit_id}/status")
def update_visit_status(visit_id: int, status: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        visit = db.query(Visit).filter(Visit.id == visit_id).first()
        if not visit:
            raise HTTPException(status_code=404, detail="Visit not found")
        visit.status = status
        visit.followup_status = status
        db.commit()
        return {"id": visit.id, "status": visit.status}
    except HTTPException:
        raise
    except Exception as e:
        logging.exception(f"update_visit_status failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/visits/{patient_id}")
def list_visits(patient_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        _patient_for_user(patient_id, user, db)
        visits = db.query(Visit).filter(Visit.patient_id == patient_id).order_by(Visit.visit_date.desc()).all()
        return [visit_out(visit) for visit in visits]
    except Exception as e:
        logging.exception(f"list_visits failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/visits/clinic/{clinic_id}")
def get_visits_by_clinic(clinic_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    require_clinic_access(clinic_id, user)
    today = date.today()
    visits = (
        db.query(Visit, Patient.name.label("patient_name"), Patient.condition.label("patient_condition"))
        .join(Patient, Visit.patient_id == Patient.id)
        .filter(Patient.clinic_id == clinic_id)
        .filter(Visit.visit_date == today)
        .order_by(Visit.created_at.asc())
        .all()
    )
    result = []
    for v, pname, condition in visits:
        row = visit_out(v)
        row["patient_name"] = pname
        row["condition"] = condition
        result.append(row)
    return result


@app.post("/prescriptions")
def create_prescription(data: PrescriptionCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    patient = db.query(Patient).filter(Patient.id == data.patient_id).first()
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    require_clinic_access(patient.clinic_id, current_user)
    rx = Prescription(patient_id=data.patient_id, medicines=[_model_data(m) for m in data.medicines], notes=data.notes or "")
    db.add(rx)
    db.commit()
    db.refresh(rx)
    return rx


@app.put("/prescriptions/{prescription_id}")
def update_prescription(
    prescription_id: int,
    data: PrescriptionUpdateIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rx = db.query(Prescription).filter(Prescription.id == prescription_id).first()
    if not rx:
        raise HTTPException(status_code=404, detail="Prescription not found")
    patient = db.query(Patient).filter(Patient.id == rx.patient_id).first()
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    require_clinic_access(patient.clinic_id, current_user)
    payload = _model_data(data, exclude_unset=True)
    if "medicines" in payload and payload["medicines"] is not None:
        rx.medicines = [_model_data(medicine) for medicine in data.medicines]
    if "notes" in payload:
        rx.notes = data.notes or ""
    db.commit()
    db.refresh(rx)
    return rx


@app.get("/prescriptions/{patient_id}")
def get_prescriptions(patient_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    patient = db.query(Patient).filter(Patient.id == patient_id).first()
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    require_clinic_access(patient.clinic_id, current_user)
    return db.query(Prescription).filter(Prescription.patient_id == patient_id).order_by(Prescription.created_at.desc()).all()


@app.post("/labs")
def create_lab(data: LabCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    patient = db.query(Patient).filter(Patient.id == data.patient_id).first()
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    require_clinic_access(patient.clinic_id, current_user)
    lab = LabResult(**_model_data(data))
    db.add(lab)
    db.commit()
    db.refresh(lab)
    return lab


@app.get("/labs/{patient_id}")
def get_labs(patient_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    patient = db.query(Patient).filter(Patient.id == patient_id).first()
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    require_clinic_access(patient.clinic_id, current_user)
    return db.query(LabResult).filter(LabResult.patient_id == patient_id).order_by(LabResult.test_date.desc()).all()


@app.post("/whatsapp/prescription/{patient_id}")
def send_whatsapp_prescription(patient_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    patient = db.query(Patient).filter(Patient.id == patient_id).first()
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    require_clinic_access(patient.clinic_id, current_user)
    latest_rx = db.query(Prescription).filter(Prescription.patient_id == patient_id).order_by(Prescription.created_at.desc()).first()
    if not latest_rx:
        raise HTTPException(status_code=404, detail="No prescription found")
    from app.whatsapp import build_prescription_message, send_whatsapp_message
    msg = build_prescription_message(patient, latest_rx)
    result = send_whatsapp_message(patient.phone, patient.name, "thank_you", {"clinic_name": "DocNudge", "condition": "Prescription", "next_visit": None, "care_tip": msg[:120]})
    return {"success": "error" not in result, "message": msg}


@app.post("/whatsapp/recovery/{patient_id}")
def send_recovery_message(
    patient_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    patient = db.query(Patient).filter(Patient.id == patient_id).first()
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    require_clinic_access(patient.clinic_id, current_user)
    if patient.opted_out:
        raise HTTPException(status_code=400, detail="Patient has opted out")
    if not patient.reminder_enabled or not patient.followup_enabled:
        raise HTTPException(status_code=400, detail="Reminders are disabled for this patient")

    visit = (
        db.query(Visit)
        .filter(Visit.patient_id == patient.id)
        .order_by(Visit.followup_date.desc(), Visit.next_visit.desc(), Visit.id.desc())
        .first()
    )
    if not visit:
        raise HTTPException(status_code=404, detail="No follow-up visit found")

    from app.whatsapp import send_whatsapp_message

    result = send_whatsapp_message(
        patient.phone,
        patient.name,
        "missed_followup",
        context=_whatsapp_context(patient, visit),
    )
    reminder = _save_message_log(db, patient, "missed_followup", result)
    return {"success": "error" not in result, "result": result, "reminder": reminder}

# ── Reminder logs ──────────────────────────────────────────
@app.get("/reminder-logs/{clinic_id}")
def reminder_logs(clinic_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        require_clinic_access(clinic_id, user)
        rows = (
            db.query(ReminderLog, Patient)
            .join(Patient, Patient.id == ReminderLog.patient_id)
            .filter(Patient.clinic_id == clinic_id)
            .order_by(ReminderLog.sent_at.desc())
            .limit(50)
            .all()
        )
        return [[reminder_log_out(log), patient_out(patient)] for log, patient in rows]
    except HTTPException:
        raise
    except Exception as e:
        logging.exception(f"reminder_logs failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ── Webhook ────────────────────────────────────────────────
def _phone_digits(value: str | None) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())

def _phone_match_filter(raw_phone: str):
    digits = _phone_digits(raw_phone)
    local = digits[-10:] if len(digits) >= 10 else digits
    candidates = {digits, local, f"+{digits}", f"91{local}" if local else ""}
    candidates = {item for item in candidates if item}
    return or_(
        Patient.phone.in_(candidates),
        Patient.phone.like(f"%{local}") if local else Patient.phone == "",
    )

def _extract_inbound_message(data: dict) -> tuple[str | None, str | None]:
    if data.get("type") == "message_received":
        customer = data.get("data", {}).get("customer", {})
        message = data.get("data", {}).get("message", {})
        return customer.get("channel_phone_number"), str(message.get("message") or "")

    try:
        msg = data["entry"][0]["changes"][0]["value"]["messages"][0]
        return msg.get("from"), msg.get("text", {}).get("body", "")
    except (KeyError, IndexError, TypeError):
        return None, None

@app.get("/webhook")
def verify_webhook(
    hub_mode: str = Query(alias="hub.mode"),
    hub_challenge: str = Query(alias="hub.challenge"),
    hub_verify_token: str = Query(alias="hub.verify_token"),
):
    if hub_mode == "subscribe" and hub_verify_token == WEBHOOK_VERIFY_TOKEN:
        return int(hub_challenge)
    raise HTTPException(status_code=403, detail="Verification failed")

@app.post("/webhook")
async def receive_webhook(request: Request, db: Session = Depends(get_db)):
    from app.whatsapp import handle_opt_out

    data = await request.json()
    phone, body = _extract_inbound_message(data)
    if phone and body and handle_opt_out(body):
        patients = db.query(Patient).filter(_phone_match_filter(phone)).all()
        for patient in patients:
            patient.opted_out = True
        if patients:
            db.commit()
        return {"status": "received", "action": "opt_out", "matched": len(patients)}
    return {"status": "received", "action": "ignored"}

# ── Test endpoints ─────────────────────────────────────────
@app.post("/test/two-days-before")
def test_two_days_before(user: User = Depends(require_admin)):
    trigger_two_days_before()
    return {"status": "two_days_before reminders triggered"}

@app.post("/test/day-before")
def test_day_before(user: User = Depends(require_admin)):
    trigger_day_before()
    return {"status": "day_before reminders triggered"}

@app.post("/test/morning")
def test_morning(user: User = Depends(require_admin)):
    trigger_morning()
    return {"status": "morning reminders triggered"}

@app.post("/test/missed-followups")
def test_missed_followups(user: User = Depends(require_admin)):
    trigger_missed_followups()
    return {"status": "missed follow-up messages triggered"}
