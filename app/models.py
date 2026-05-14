from sqlalchemy import Column, Integer, String, Date, DateTime, Boolean, ForeignKey, Text, Float, JSON
from sqlalchemy.orm import relationship
from datetime import datetime
from app.database import Base

class Clinic(Base):
    __tablename__ = "clinics"
    id         = Column(Integer, primary_key=True)
    name       = Column(String, nullable=False)
    city       = Column(String)
    email      = Column(String)
    phone      = Column(String)
    doctor_name = Column(String)
    designation = Column(String)
    address    = Column(Text)
    website_url = Column(String)
    whatsapp_number = Column(String)
    logo_url = Column(String)
    subscription_plan = Column(String, default="trial")
    trial_ends_at = Column(DateTime)
    billing_status = Column(String, default="trialing")
    widget_primary_color = Column(String, default="#0f766e")
    widget_welcome_text = Column(String, default="Hi, I am DocNudge AI. What would you like to book?")
    widget_enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    patients   = relationship("Patient", back_populates="clinic")

class Patient(Base):
    __tablename__ = "patients"
    id            = Column(Integer, primary_key=True)
    clinic_id     = Column(Integer, ForeignKey("clinics.id"), nullable=False)
    mrn           = Column(String, unique=True, nullable=True)
    name          = Column(String, nullable=False)
    phone         = Column(String, nullable=False)
    condition     = Column(String)
    age           = Column(Integer, nullable=True)        # NEW
    gender        = Column(String, nullable=True)         # NEW
    followup_type = Column(String, nullable=True)         # NEW
    blood_group   = Column(String, nullable=True)
    allergies     = Column(JSON, nullable=True)
    conditions    = Column(JSON, nullable=True)
    emergency_name = Column(String, nullable=True)
    emergency_phone = Column(String, nullable=True)
    emergency_relation = Column(String, nullable=True)
    emergency_token = Column(String, unique=True, nullable=True)
    source        = Column(String, default="walk_in")
    last_visit_at = Column(Date)
    preferred_language = Column(String, default="en")
    opted_out     = Column(Boolean, default=False)
    reminder_enabled = Column(Boolean, default=True)
    followup_enabled = Column(Boolean, default=True)
    created_at    = Column(DateTime, default=datetime.utcnow)
    clinic        = relationship("Clinic", back_populates="patients")
    visits        = relationship("Visit", back_populates="patient")

class Visit(Base):
    __tablename__ = "visits"
    id         = Column(Integer, primary_key=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False)
    condition  = Column(String)
    doctor_name = Column(String)
    doctor_designation = Column(String)
    visit_date = Column(Date, nullable=False)
    next_visit = Column(Date)
    followup_date = Column(Date)
    followup_status = Column(String, default="due")
    notes      = Column(String)
    prescription_text = Column(Text)
    doctor_notes = Column(Text)
    status     = Column(String, default="upcoming")  # NEW: upcoming / completed / missed
    created_at = Column(DateTime, default=datetime.utcnow)
    patient    = relationship("Patient", back_populates="visits")

class ReminderLog(Base):
    __tablename__ = "reminder_logs"
    id            = Column(Integer, primary_key=True)
    patient_id    = Column(Integer, ForeignKey("patients.id"), nullable=False)
    reminder_type = Column(String)
    sent_at       = Column(DateTime, default=datetime.utcnow)
    success       = Column(Boolean, default=True)
    error         = Column(String)

class User(Base):
    __tablename__ = "users"
    id         = Column(Integer, primary_key=True)
    email      = Column(String, nullable=False)
    login_id   = Column(String, unique=True, nullable=True)
    password   = Column(String, nullable=False)
    name       = Column(String)
    phone      = Column(String)
    designation = Column(String)
    role       = Column(String, default="receptionist")
    clinic_id  = Column(Integer, ForeignKey("clinics.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class Service(Base):
    __tablename__ = "services"
    id = Column(Integer, primary_key=True)
    clinic_id = Column(Integer, ForeignKey("clinics.id"), nullable=False)
    name = Column(String, nullable=False)
    duration_minutes = Column(Integer, default=30)
    price = Column(Float, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class Appointment(Base):
    __tablename__ = "appointments"
    id = Column(Integer, primary_key=True)
    clinic_id = Column(Integer, ForeignKey("clinics.id"), nullable=False)
    patient_name = Column(String, nullable=False)
    patient_phone = Column(String, nullable=False)
    service_type = Column(String)
    appointment_date = Column(Date, nullable=False)
    appointment_time = Column(String, nullable=False)
    status = Column(String, default="booked")
    booked_via = Column(String, default="manual")
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

class AIConversation(Base):
    __tablename__ = "ai_conversations"
    id = Column(Integer, primary_key=True)
    clinic_id = Column(Integer, ForeignKey("clinics.id"), nullable=False)
    session_id = Column(String, nullable=False)
    messages_json = Column(Text, default="[]")
    appointment_id = Column(Integer, ForeignKey("appointments.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class WeeklyReport(Base):
    __tablename__ = "weekly_reports"
    id = Column(Integer, primary_key=True)
    clinic_id = Column(Integer, ForeignKey("clinics.id"), nullable=False)
    week_start = Column(Date, nullable=False)
    total_patients = Column(Integer, default=0)
    visits_completed = Column(Integer, default=0)
    missed_count = Column(Integer, default=0)
    return_rate = Column(Float, default=0)
    ai_summary = Column(Text)
    sent_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)


class Prescription(Base):
    __tablename__ = "prescriptions"
    id = Column(Integer, primary_key=True, index=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False)
    medicines = Column(JSON, nullable=False)
    notes = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class LabResult(Base):
    __tablename__ = "lab_results"
    id = Column(Integer, primary_key=True, index=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False)
    test_name = Column(String(200), nullable=False)
    result = Column(String(200), nullable=False)
    reference_range = Column(String(200), default="")
    test_date = Column(String(20), nullable=False)
    status = Column(String(20), default="normal")
    created_at = Column(DateTime, default=datetime.utcnow)


class DemoRequest(Base):
    __tablename__ = "demo_requests"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    clinic = Column(String(200), nullable=False)
    email = Column(String(200), nullable=False)
    phone = Column(String(50), nullable=False)
    role = Column(String(100), nullable=False)
    city = Column(String(120), nullable=True)
    message = Column(Text, nullable=True)
    source = Column(String(50), default="landing")
    status = Column(String(50), default="new")
    created_at = Column(DateTime, default=datetime.utcnow)
