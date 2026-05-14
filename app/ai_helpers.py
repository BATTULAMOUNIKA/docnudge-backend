import json
import logging
import re
from urllib import error, request

from app.config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL

logger = logging.getLogger(__name__)


COMMON_DRUGS = {
    "pcm": "Paracetamol",
    "paracetamol": "Paracetamol",
    "mtf": "Metformin",
    "metformin": "Metformin",
    "amox": "Amoxicillin",
    "amoxicillin": "Amoxicillin",
    "pan": "Pantoprazole",
    "pantoprazole": "Pantoprazole",
    "cet": "Cetirizine",
    "cetirizine": "Cetirizine",
}

FREQ_MAP = {
    "od": "Once daily",
    "bd": "Twice daily",
    "tds": "Three times daily",
    "qid": "Four times daily",
    "sos": "As needed",
    "hs": "At bedtime",
}

KNOWN_INTERACTIONS = {
    frozenset({"Ibuprofen", "Diclofenac"}): ("moderate", "Avoid combining multiple NSAIDs because GI irritation risk increases."),
    frozenset({"Aspirin", "Ibuprofen"}): ("moderate", "Concurrent use can increase bleeding risk and reduce aspirin's antiplatelet effect."),
    frozenset({"Metformin", "Prednisolone"}): ("moderate", "Steroids may worsen blood glucose control."),
    frozenset({"Azithromycin", "Ondansetron"}): ("moderate", "Both can prolong QT interval in susceptible patients."),
}


EMPTY_LAB_ANALYSIS = {
    "abnormals": [],
    "normal": [],
    "clinicalContext": "",
    "watchList": [],
    "medicationConsiderations": [],
    "followupSuggested": False,
    "urgentFlag": False,
}


def _call_anthropic(system_prompt: str, user_prompt, max_tokens: int = 900):
    if not ANTHROPIC_API_KEY:
        return None

    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    req = request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "content-type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with request.urlopen(req, timeout=25) as response:
            raw = response.read().decode("utf-8")
            data = json.loads(raw)
            return data.get("content", [{}])[0].get("text", "").strip()
    except error.HTTPError as exc:
        logger.warning("Anthropic request failed: %s %s", exc.code, exc.read().decode("utf-8", errors="replace"))
    except Exception:
        logger.exception("Anthropic request failed")
    return None


def _fallback_parse(shorthand: str):
    rows = []
    parts = [part.strip() for part in shorthand.replace("\n", ",").split(",") if part.strip()]
    for part in parts:
        tokens = part.split()
        name = ""
        dosage = ""
        frequency = ""
        duration = ""
        notes = ""
        for token in tokens:
            key = token.lower().strip()
            if not name and key in COMMON_DRUGS:
                name = COMMON_DRUGS[key]
            elif any(ch.isdigit() for ch in token) and not dosage and any(unit in key for unit in ["mg", "ml", "gm", "g"]):
                dosage = token
            elif key in FREQ_MAP and not frequency:
                frequency = FREQ_MAP[key]
            elif key.endswith("d") and token[:-1].isdigit() and not duration:
                duration = f"{token[:-1]} days"
            elif key.endswith("w") and token[:-1].isdigit() and not duration:
                duration = f"{token[:-1]} weeks"
            elif not dosage and any(ch.isdigit() for ch in token):
                dosage = token
            elif not notes and key not in {"tab", "cap", "syp", "inj"}:
                notes = token if not notes else f"{notes} {token}"
        rows.append(
            {
                "name": name or (tokens[0] if tokens else "Medicine"),
                "dosage": dosage or "",
                "frequency": frequency or "",
                "duration": duration or "",
                "notes": notes.strip(),
            }
        )
    return rows or [{"name": "", "dosage": "", "frequency": "", "duration": "", "notes": ""}]


def parse_prescription_shorthand(shorthand: str, patient_context: str = ""):
    system_prompt = (
        "You are a clinical documentation assistant. Format ONLY what the clinician typed as shorthand "
        "into structured rows—do not invent medicines, doses, or indications that are not clearly implied. "
        "If shorthand is ambiguous, output best-effort rows and put a short clarification request in that row's "
        "notes field (e.g. 'confirm dose'). "
        "Return only a JSON array of objects with keys name, dosage, frequency, duration, notes. "
        "Expand frequency abbreviations (OD, BD, TDS, QID, SOS, HS, AC, PC). "
        "Do not include markdown or prose outside the JSON array."
    )
    user_prompt = (
        f"Patient context (demographics/condition summary only, for phrasing—not for adding new drugs): {patient_context}\n"
        f"Doctor shorthand (source of truth): {shorthand}\n"
        "Parse each comma- or line-separated medicine into one object."
    )
    text = _call_anthropic(system_prompt, user_prompt)
    if text:
        try:
            return json.loads(text.replace("```json", "").replace("```", "").strip())
        except Exception:
            logger.exception("Could not parse Anthropic JSON for prescription shorthand")
    return _fallback_parse(shorthand)


def check_drug_interactions(medicines: list[dict]):
    names = [str(item.get("name", "")).strip() for item in medicines if item.get("name")]
    if len(names) < 2:
        return {"interactions": [], "safe": True}

    system_prompt = (
        "You are a cautious medication interaction checker. "
        "Return only JSON with keys interactions and safe. "
        "interactions is an array of objects with drugs, severity, note."
    )
    user_prompt = f"Check clinically meaningful interactions for: {', '.join(names)}"
    text = _call_anthropic(system_prompt, user_prompt, max_tokens=500)
    if text:
        try:
            parsed = json.loads(text.replace("```json", "").replace("```", "").strip())
            if isinstance(parsed, dict) and "interactions" in parsed and "safe" in parsed:
                return parsed
        except Exception:
            logger.exception("Could not parse Anthropic JSON for interactions")

    interactions = []
    normalized = {name.title(): name for name in names}
    title_names = list(normalized.keys())
    for index, first in enumerate(title_names):
        for second in title_names[index + 1 :]:
            key = frozenset({first, second})
            if key in KNOWN_INTERACTIONS:
                severity, note = KNOWN_INTERACTIONS[key]
                interactions.append({"drugs": f"{first} + {second}", "severity": severity, "note": note})
    return {"interactions": interactions, "safe": len(interactions) == 0}


def _normalize_medicine_row(row: object) -> dict:
    if not isinstance(row, dict):
        return {"name": "", "dosage": "", "frequency": "", "duration": "", "notes": ""}
    return {
        "name": str(row.get("name", "") or "").strip(),
        "dosage": str(row.get("dosage", "") or "").strip(),
        "frequency": str(row.get("frequency", "") or "").strip(),
        "duration": str(row.get("duration", "") or "").strip(),
        "notes": str(row.get("notes", "") or "").strip(),
    }


def draft_prescription_assist(shorthand: str, patient_context: str = "") -> dict:
    """
    Single-call prescription draft: parse shorthand to structured rows, then interaction check.
    Output is decision-support only until a clinician reviews and saves.
    """
    raw = parse_prescription_shorthand(shorthand, patient_context)
    if not isinstance(raw, list):
        raw = []
    medicines = [_normalize_medicine_row(item) for item in raw]
    if not medicines:
        medicines = [{"name": "", "dosage": "", "frequency": "", "duration": "", "notes": ""}]

    interactions = check_drug_interactions(medicines)

    warnings: list[str] = []
    if not ANTHROPIC_API_KEY:
        warnings.append(
            "ANTHROPIC_API_KEY is not set on the server; parsing uses offline rules only. "
            "Set ANTHROPIC_API_KEY (and optionally ANTHROPIC_MODEL) on Railway for full AI formatting."
        )

    disclaimer = (
        "This output is clinical decision support only. It does not replace the treating clinician's judgment "
        "and is not a legal prescription until reviewed and signed by a licensed prescriber."
    )

    return {
        "medicines": medicines,
        "interactions": interactions,
        "disclaimer": disclaimer,
        "warnings": warnings,
    }


def _parse_number(raw_value: str) -> float | None:
    match = re.search(r"-?\d+(?:\.\d+)?", str(raw_value or ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _normalize_lab_name(name: str) -> str:
    lowered = str(name or "").strip().lower()
    aliases = {
        "hba1c": "hba1c",
        "a1c": "hba1c",
        "fasting glucose": "fasting_glucose",
        "fasting sugar": "fasting_glucose",
        "fbs": "fasting_glucose",
        "pp glucose": "pp_glucose",
        "pp sugar": "pp_glucose",
        "post prandial glucose": "pp_glucose",
        "creatinine": "creatinine",
        "serum creatinine": "creatinine",
        "egfr": "egfr",
        "total cholesterol": "total_cholesterol",
        "cholesterol": "total_cholesterol",
        "ldl": "ldl",
        "hdl": "hdl",
        "triglycerides": "triglycerides",
        "tg": "triglycerides",
        "bp": "bp",
        "blood pressure": "bp",
    }
    return aliases.get(lowered, lowered)


def _lab_ranges():
    return {
        "hba1c": {"high": 6.5, "range": "< 5.7%"},
        "fasting_glucose": {"high": 126, "range": "70-99 mg/dL"},
        "pp_glucose": {"high": 200, "range": "< 140 mg/dL"},
        "creatinine": {"high": 1.3, "range": "0.7-1.3 mg/dL"},
        "egfr": {"low": 60, "range": "> 90 mL/min"},
        "total_cholesterol": {"high": 200, "range": "< 200 mg/dL"},
        "ldl": {"high": 130, "range": "< 100 mg/dL"},
        "hdl": {"low": 40, "range": "> 40 mg/dL"},
        "triglycerides": {"high": 150, "range": "< 150 mg/dL"},
    }


def _add_lab_flag(abnormals: list, normal: list, label: str, value: str, range_text: str, flag: str | None, severity: str = "mild"):
    if flag:
        abnormals.append(
            {
                "test": label,
                "value": value,
                "range": range_text,
                "flag": flag,
                "severity": severity,
            }
        )
    else:
        normal.append({"test": label, "value": value})


def _fallback_lab_analysis(lab_text: str):
    abnormals = []
    normal = []
    watch_list = []
    medication_considerations = []
    urgent_flag = False
    parsed = {}

    for raw_line in str(lab_text or "").splitlines():
        if ":" not in raw_line:
            continue
        name, raw_value = raw_line.split(":", 1)
        key = _normalize_lab_name(name)
        value = raw_value.strip()
        parsed[key] = value

    ranges = _lab_ranges()
    for key, value in parsed.items():
        number = _parse_number(value)
        label = key.replace("_", " ").title()
        if key == "bp":
            bp_match = re.search(r"(\d{2,3})\s*/\s*(\d{2,3})", value)
            if not bp_match:
                continue
            systolic = int(bp_match.group(1))
            diastolic = int(bp_match.group(2))
            flag = None
            severity = "mild"
            if systolic >= 180 or diastolic >= 110:
                flag = "high"
                severity = "significant"
                urgent_flag = True
            elif systolic >= 140 or diastolic >= 90:
                flag = "high"
                severity = "moderate"
            elif systolic >= 130 or diastolic >= 80:
                flag = "high"
                severity = "mild"
            _add_lab_flag(abnormals, normal, "Blood Pressure", value, "< 120/80 mmHg", flag, severity)
            continue
        if key not in ranges or number is None:
            continue

        threshold = ranges[key]
        flag = None
        severity = "mild"
        if "high" in threshold and number > threshold["high"]:
            flag = "high"
            if key == "hba1c" and number >= 10:
                severity = "significant"
                urgent_flag = True
            elif key in {"fasting_glucose", "pp_glucose"} and number >= 250:
                severity = "significant"
                urgent_flag = True
            elif key == "creatinine" and number >= 2:
                severity = "significant"
                urgent_flag = True
            elif number >= threshold["high"] * 1.2:
                severity = "moderate"
        elif "low" in threshold and number < threshold["low"]:
            flag = "low"
            if key == "egfr" and number < 30:
                severity = "significant"
                urgent_flag = True
            elif key == "egfr" and number < 60:
                severity = "moderate"
            elif key == "hdl":
                severity = "moderate"
        _add_lab_flag(abnormals, normal, label, value, threshold["range"], flag, severity)

    if any(item["test"] == "Hba1C" for item in abnormals) or any(item["test"] == "Fasting Glucose" for item in abnormals):
        watch_list.append("Review adherence, diet pattern, symptoms of hyperglycemia, and recent home glucose readings.")
        medication_considerations.append("Values suggest reviewing glycemic control before adjusting antidiabetic therapy.")
    if any(item["test"] in {"Creatinine", "Egfr"} for item in abnormals):
        watch_list.append("Ask about urine output, edema, dehydration, NSAID use, and prior kidney disease.")
        medication_considerations.append("Consider reviewing renal dosing and nephrotoxic exposure before finalizing medicines.")
    if any(item["test"] in {"Ldl", "Triglycerides", "Total Cholesterol"} for item in abnormals):
        watch_list.append("Check cardiovascular risk factors, diet pattern, and treatment adherence.")
        medication_considerations.append("Values suggest reviewing lipid-lowering therapy intensity and follow-up timing.")
    if any(item["test"] == "Blood Pressure" for item in abnormals):
        watch_list.append("Repeat blood pressure if needed and ask about headache, dizziness, chest pain, or missed doses.")
        medication_considerations.append("Consider reviewing antihypertensive coverage and timing.")

    if abnormals:
        summary = "Abnormal values suggest the doctor should review metabolic control and correlate with current symptoms, examination findings, and adherence history."
    elif normal:
        summary = "Available values look broadly reassuring. Clinical correlation is still needed before finalizing treatment."
    else:
        summary = "The uploaded values could not be interpreted reliably. Please review the report manually."

    return {
        "abnormals": abnormals,
        "normal": normal,
        "clinicalContext": summary,
        "watchList": watch_list,
        "medicationConsiderations": medication_considerations,
        "followupSuggested": bool(abnormals),
        "urgentFlag": urgent_flag,
    }


def _coerce_lab_analysis(parsed):
    if not isinstance(parsed, dict):
        return dict(EMPTY_LAB_ANALYSIS)
    return {
        "abnormals": parsed.get("abnormals") or [],
        "normal": parsed.get("normal") or [],
        "clinicalContext": parsed.get("clinicalContext") or "",
        "watchList": parsed.get("watchList") or [],
        "medicationConsiderations": parsed.get("medicationConsiderations") or [],
        "followupSuggested": bool(parsed.get("followupSuggested")),
        "urgentFlag": bool(parsed.get("urgentFlag")),
    }


def analyze_lab_report(lab_text: str, patient_context: str = ""):
    system_prompt = (
        "You are a clinical decision support assistant for Indian doctors. "
        "Return only JSON with keys abnormals, normal, clinicalContext, watchList, "
        "medicationConsiderations, followupSuggested, urgentFlag. "
        "Provide clinical context, not diagnosis, and do not recommend a specific prescription."
    )
    user_prompt = (
        f"Patient context: {patient_context or 'not provided'}\n\n"
        f"Lab report values:\n{lab_text}\n\n"
        "Flag abnormal values with high or low and add severity as mild, moderate, or significant."
    )
    text = _call_anthropic(system_prompt, user_prompt, max_tokens=1200)
    if text:
        try:
            return _coerce_lab_analysis(json.loads(text.replace('```json', '').replace('```', '').strip()))
        except Exception:
            logger.exception("Could not parse Anthropic JSON for lab analysis")
    return _fallback_lab_analysis(lab_text)


def analyze_lab_image(base64_image: str, media_type: str, patient_context: str = ""):
    system_prompt = (
        "You read lab report images for Indian doctors and return only JSON with keys "
        "abnormals, normal, clinicalContext, watchList, medicationConsiderations, "
        "followupSuggested, urgentFlag. Do not diagnose. Do not recommend a specific prescription."
    )
    user_content = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": base64_image},
        },
        {
            "type": "text",
            "text": (
                f"Patient context: {patient_context or 'not provided'}.\n"
                "Read the lab report image, extract visible values, flag abnormal results, "
                "and provide concise clinical context. Return JSON only."
            ),
        },
    ]
    text = _call_anthropic(system_prompt, user_content, max_tokens=1400)
    if text:
        try:
            return _coerce_lab_analysis(json.loads(text.replace('```json', '').replace('```', '').strip()))
        except Exception:
            logger.exception("Could not parse Anthropic JSON for lab image analysis")
    return dict(EMPTY_LAB_ANALYSIS)
