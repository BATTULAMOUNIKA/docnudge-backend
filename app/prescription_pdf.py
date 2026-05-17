"""
prescription_pdf.py
Generates a clean, professional prescription PDF for DocNudge.
Uses reportlab — add to requirements.txt: reportlab
"""

import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


# ── Colours ────────────────────────────────────────────────
TEAL        = colors.HexColor("#0f766e")
LIGHT_TEAL  = colors.HexColor("#f0fdfa")
DARK_GREY   = colors.HexColor("#1f2937")
MID_GREY    = colors.HexColor("#6b7280")
LIGHT_GREY  = colors.HexColor("#f9fafb")
WHITE       = colors.white


def generate_prescription_pdf(
    patient_name: str,
    patient_age: int | str | None,
    patient_gender: str | None,
    clinic_name: str,
    doctor_name: str | None,
    designation: str | None,
    clinic_phone: str | None,
    clinic_address: str | None,
    condition: str | None,
    medicines: list[dict],
    notes: str | None,
    next_visit: str | None,
    visit_date: str | None = None,
) -> bytes:
    """
    Returns PDF as bytes.
    medicines: list of dicts with keys: name, dosage, frequency, duration
    """

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=12 * mm,
        bottomMargin=15 * mm,
    )

    styles = getSampleStyleSheet()
    story  = []

    # ── Helper styles ───────────────────────────────────────
    def style(name, **kwargs) -> ParagraphStyle:
        return ParagraphStyle(name, parent=styles["Normal"], **kwargs)

    clinic_name_style  = style("ClinicName",  fontSize=18, textColor=TEAL,      fontName="Helvetica-Bold", leading=22)
    doctor_style       = style("Doctor",       fontSize=11, textColor=DARK_GREY, fontName="Helvetica-Bold", leading=14)
    sub_style          = style("Sub",          fontSize=9,  textColor=MID_GREY,  fontName="Helvetica",      leading=12)
    label_style        = style("Label",        fontSize=9,  textColor=MID_GREY,  fontName="Helvetica-Bold")
    value_style        = style("Value",        fontSize=10, textColor=DARK_GREY, fontName="Helvetica")
    section_style      = style("Section",      fontSize=11, textColor=TEAL,      fontName="Helvetica-Bold", leading=14, spaceAfter=4)
    med_name_style     = style("MedName",      fontSize=10, textColor=DARK_GREY, fontName="Helvetica-Bold", leading=13)
    med_detail_style   = style("MedDetail",    fontSize=9,  textColor=MID_GREY,  fontName="Helvetica",      leading=12)
    notes_style        = style("Notes",        fontSize=10, textColor=DARK_GREY, fontName="Helvetica",      leading=14)
    footer_style       = style("Footer",       fontSize=8,  textColor=MID_GREY,  fontName="Helvetica",      alignment=1)

    # ── Header ─────────────────────────────────────────────
    header_data = [[
        Paragraph(clinic_name or "DocNudge Clinic", clinic_name_style),
        Paragraph(
            f"{doctor_name or 'Doctor'}<br/>"
            f"<font color='#6b7280' size='9'>{designation or ''}</font>",
            doctor_style,
        ),
    ]]
    header_table = Table(header_data, colWidths=[100 * mm, 65 * mm])
    header_table.setStyle(TableStyle([
        ("VALIGN",      (0, 0), (-1, -1), "TOP"),
        ("ALIGN",       (1, 0), (1, 0),   "RIGHT"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(header_table)

    # Clinic contact line
    contact_parts = []
    if clinic_phone:
        contact_parts.append(f"📞 {clinic_phone}")
    if clinic_address:
        contact_parts.append(f"📍 {clinic_address}")
    if contact_parts:
        story.append(Paragraph(" &nbsp;&nbsp; ".join(contact_parts), sub_style))

    story.append(Spacer(1, 3 * mm))
    story.append(HRFlowable(width="100%", thickness=2, color=TEAL))
    story.append(Spacer(1, 2 * mm))

    # ── Rx stamp + date ────────────────────────────────────
    date_str = visit_date or datetime.now().strftime("%d %b %Y")
    rx_data = [[
        Paragraph("<font color='#0f766e' size='22'><b>Rx</b></font>", styles["Normal"]),
        Paragraph(f"<font color='#6b7280'>Date: </font>{date_str}", value_style),
    ]]
    rx_table = Table(rx_data, colWidths=[20 * mm, 145 * mm])
    rx_table.setStyle(TableStyle([
        ("VALIGN",  (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN",   (1, 0), (1, 0),   "RIGHT"),
    ]))
    story.append(rx_table)
    story.append(Spacer(1, 2 * mm))

    # ── Patient info box ───────────────────────────────────
    age_gender = " | ".join(filter(None, [
        str(patient_age) + " yrs" if patient_age else None,
        patient_gender,
    ]))
    patient_data = [
        [
            Paragraph("Patient", label_style),
            Paragraph(patient_name or "—", value_style),
            Paragraph("Age / Gender", label_style),
            Paragraph(age_gender or "—", value_style),
        ],
        [
            Paragraph("Diagnosis", label_style),
            Paragraph(condition or "General Consultation", value_style),
            Paragraph("", label_style),
            Paragraph("", value_style),
        ],
    ]
    patient_table = Table(patient_data, colWidths=[28 * mm, 60 * mm, 30 * mm, 47 * mm])
    patient_table.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), LIGHT_TEAL),
        ("GRID",          (0, 0), (-1, -1), 0.3, colors.HexColor("#ccfbf1")),
        ("FONTNAME",      (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE",      (0, 0), (-1, -1), 9),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("ROUNDEDCORNERS", [3, 3, 3, 3]),
    ]))
    story.append(patient_table)
    story.append(Spacer(1, 4 * mm))

    # ── Medicines ──────────────────────────────────────────
    story.append(Paragraph("Medicines", section_style))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#ccfbf1")))
    story.append(Spacer(1, 2 * mm))

    if medicines:
        for i, med in enumerate(medicines, 1):
            name      = med.get("name", "")
            dosage    = med.get("dosage", "")
            frequency = med.get("frequency", "")
            duration  = med.get("duration", "")

            detail_parts = []
            if dosage:
                detail_parts.append(f"Dose: {dosage}")
            if frequency:
                detail_parts.append(f"Timing: {frequency}")
            if duration:
                detail_parts.append(f"Duration: {duration}")

            med_data = [[
                Paragraph(f"{i}.", value_style),
                [
                    Paragraph(name, med_name_style),
                    Paragraph(" &nbsp;|&nbsp; ".join(detail_parts) if detail_parts else "", med_detail_style),
                ],
            ]]
            med_table = Table(med_data, colWidths=[8 * mm, 157 * mm])
            med_table.setStyle(TableStyle([
                ("VALIGN",        (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING",    (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("BACKGROUND",    (0, 0), (-1, -1), LIGHT_GREY if i % 2 == 0 else WHITE),
                ("LEFTPADDING",   (0, 0), (-1, -1), 4),
            ]))
            story.append(med_table)
    else:
        story.append(Paragraph("No medicines prescribed.", notes_style))

    story.append(Spacer(1, 4 * mm))

    # ── Notes / Instructions ───────────────────────────────
    if notes and notes.strip():
        story.append(Paragraph("Instructions & Notes", section_style))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#ccfbf1")))
        story.append(Spacer(1, 2 * mm))

        notes_box_data = [[Paragraph(notes.strip(), notes_style)]]
        notes_box = Table(notes_box_data, colWidths=[165 * mm])
        notes_box.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), LIGHT_TEAL),
            ("TOPPADDING",    (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING",   (0, 0), (-1, -1), 8),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
        ]))
        story.append(notes_box)
        story.append(Spacer(1, 4 * mm))

    # ── Next follow-up ─────────────────────────────────────
    if next_visit:
        story.append(Paragraph("Next Follow-up", section_style))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#ccfbf1")))
        story.append(Spacer(1, 2 * mm))
        story.append(Paragraph(f"📅 {next_visit}", value_style))
        story.append(Spacer(1, 4 * mm))

    # ── Doctor signature ───────────────────────────────────
    story.append(Spacer(1, 6 * mm))
    sig_data = [[
        Paragraph("", styles["Normal"]),
        [
            HRFlowable(width=50 * mm, thickness=0.8, color=DARK_GREY),
            Paragraph(doctor_name or "Doctor", style("SigName", fontSize=10, textColor=DARK_GREY, fontName="Helvetica-Bold", alignment=1)),
            Paragraph(designation or "", style("SigDes", fontSize=8, textColor=MID_GREY, alignment=1)),
        ],
    ]]
    sig_table = Table(sig_data, colWidths=[110 * mm, 55 * mm])
    sig_table.setStyle(TableStyle([
        ("VALIGN",  (0, 0), (-1, -1), "BOTTOM"),
        ("ALIGN",   (1, 0), (1, 0),   "RIGHT"),
    ]))
    story.append(sig_table)

    # ── Footer ─────────────────────────────────────────────
    story.append(Spacer(1, 4 * mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#e5e7eb")))
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph(
        "This prescription is generated digitally by DocNudge • docnudge.in • For queries contact your clinic",
        footer_style,
    ))

    doc.build(story)
    buffer.seek(0)
    return buffer.read()
