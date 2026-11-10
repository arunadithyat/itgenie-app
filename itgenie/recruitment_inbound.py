"""Sarvam inbound recruitment webhook for ERPNext/Frappe v15.

Copy to apps/itgenie/itgenie/recruitment_inbound.py. This module is independent
of the Bioman sales caller. Fill in the constants at the top of this file.
No Custom Fields, new DocTypes, or site_config.json changes are needed.

Configure the Sarvam deployment's webhook URL as:
    https://YOUR_ERP_HOST/api/method/itgenie.recruitment_inbound.receive?token=TOKEN

Treat TOKEN like a password. Sarvam's deployment webhook configuration
documents a URL but no custom authentication header or signature. This endpoint
therefore requires a shared secret and validates the deployment and agent IDs.
Keep the URL out of logs, screenshots, and tickets; rotate the token if exposed.
Keep the populated file out of Git because it contains a secret.

Required Sarvam output variables:
    call_disposition: Enum SCREENED, CALLBACK_REQUESTED, DECLINED,
                      WRONG_NUMBER, INCOMPLETE
    screening_json:   String containing a JSON object in English
    candidate_email:  String, confirmed address or Not Provided
    email_confirmed:  Enum YES or NO
    phone_confirmed:  Enum YES or NO (the inbound caller number is a suitable
                      number for HR to call back)

This endpoint does not decide whether a candidate is hired or shortlisted.
It creates new Job Applicants as Lead, or appends a Comment to a confidently
matched existing applicant. Other calls create an HR ToDo for manual review.
"""

import hashlib
import hmac
import html
import json
import re

import frappe


# Populate these three constants on the server. Do not commit the populated file.
WEBHOOK_TOKEN = "Token"
SARVAM_AGENT_ID = "Recruitment-cebe5229-804a"
SARVAM_DEPLOYMENT_ID = "Homegenie-R-5b748134-eb68"


ROLES = {
    "sales executive": "Sales Executive",
    "senior sales executive": "Senior Sales Executive",
    "area sales manager": "Area Sales Manager",
}
DISPOSITIONS = {
    "SCREENED", "CALLBACK_REQUESTED", "DECLINED", "WRONG_NUMBER", "INCOMPLETE"
}
NOT_PROVIDED = {"", "not provided", "not_provided", "unknown", "none", "null"}
EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")
INTERACTION_RE = re.compile(r"^[A-Za-z0-9_./:\-]{8,150}$")
MARKER_PREFIX = "[Homegenie-Recruitment-Sarvam:"

SCREENING_FIELDS = (
    "candidate_name", "role_applied", "current_location", "preferred_location",
    "total_experience_years", "sales_experience_years", "current_company",
    "current_designation", "products_sold", "sales_channels",
    "territories_handled", "field_sales_experience", "monthly_target",
    "average_monthly_achievement", "highest_monthly_achievement",
    "lead_generation_and_conversion", "sales_cycle_responsibilities",
    "team_size", "management_experience", "reason_for_change",
    "current_annual_ctc", "current_monthly_take_home", "incentives",
    "expected_annual_ctc", "expected_monthly_take_home",
    "salary_negotiable", "notice_period", "last_working_day",
    "earliest_joining_date", "travel_and_fieldwork", "driving_licence",
    "own_vehicle", "interview_interest", "interview_availability",
    "callback_preference", "candidate_questions", "role_relevant_evidence",
    "missing_information", "hr_next_action", "screening_summary",
)


def _configured(value, placeholder, label):
    value = str(value or "").strip()
    if not value or value == placeholder:
        raise RuntimeError("Configure " + label + " in recruitment_inbound.py")
    return value


def _text(value, limit=500):
    if value is None or isinstance(value, (dict, list)):
        return "Not Provided"
    value = str(value).strip()
    return value[:limit] if value else "Not Provided"


def _provided(value):
    return str(value or "").strip().lower() not in NOT_PROVIDED


def _marker(interaction_id):
    return MARKER_PREFIX + interaction_id + "]"


def _already_processed(marker):
    applicant_note = frappe.db.exists("Comment", {
        "reference_doctype": "Job Applicant",
        "content": ["like", "%" + marker + "%"],
    })
    hr_review = frappe.db.exists("ToDo", {
        "description": ["like", "%" + marker + "%"],
    })
    return bool(applicant_note or hr_review)


def _screening_data(variables):
    raw = variables.get("screening_json")
    if isinstance(raw, str):
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
            raw = re.sub(r"\s*```$", "", raw)
        if len(raw) > 40000:
            raise ValueError("screening_json exceeds 40,000 characters")
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    if not isinstance(raw, dict):
        return {}
    return {key: _text(raw.get(key), 5000 if key == "screening_summary" else 500)
            for key in SCREENING_FIELDS}


def _normal_role(value):
    normalized = re.sub(r"\s+", " ", str(value or "").strip()).lower()
    return ROLES.get(normalized)


def _opening_for(role, data):
    """Link only an unambiguous Open vacancy for the stated role/location."""
    meta = frappe.get_meta("Job Opening")
    fields = ["name", "job_title"]
    if meta.has_field("designation"):
        fields.append("designation")
    if meta.has_field("location"):
        fields.append("location")
    openings = frappe.get_all(
        "Job Opening", filters={"status": "Open"}, fields=fields,
        limit_page_length=1000,
    )
    normalize = lambda value: re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
    role_key = normalize(role)
    matches = [row for row in openings if role_key in {
        normalize(row.get("job_title")), normalize(row.get("designation"))
    }]
    preferred = data.get("preferred_location")
    if _provided(preferred) and "location" in fields:
        located = [row for row in matches if normalize(row.get("location")) == normalize(preferred)]
        if len(located) == 1:
            return located[0].name
        if len(located) > 1:
            return None
    return matches[0].name if len(matches) == 1 else None


def _summary_html(data, *, marker, disposition, interaction_id, phone, duration):
    parts = [
        "<h4>Homegenie AI Recruitment Screening</h4>",
        "<p><b>Call disposition:</b> " + html.escape(disposition) + "<br>",
        "<b>Duration:</b> " + html.escape(str(round(duration, 2))) + " seconds<br>",
        "<b>Caller number:</b> " + html.escape(phone or "Not Provided") + "</p>",
        "<ul>",
    ]
    for key in SCREENING_FIELDS:
        label = key.replace("_", " ").title()
        value = data.get(key, "Not Provided")
        parts.append("<li><b>" + html.escape(label) + ":</b> "
                     + html.escape(value) + "</li>")
    parts.extend([
        "</ul>",
        "<p><b>HR decision:</b> Pending human review. "
        "This screening does not approve or reject the applicant.</p>",
        "<p>Sarvam Interaction: " + html.escape(interaction_id) + "<br>"
        + html.escape(marker) + "</p>",
    ])
    return "".join(parts)


def _review_todo(*, marker, reason, data, disposition, interaction_id,
                 caller_phone, duration):
    description = (
        "<h4>AI Recruitment Call — HR Review Required</h4>"
        "<p><b>Reason:</b> " + html.escape(reason) + "</p>"
        + _summary_html(data, marker=marker, disposition=disposition,
                        interaction_id=interaction_id, phone=caller_phone,
                        duration=duration)
    )
    todo = frappe.get_doc({
        "doctype": "ToDo",
        "status": "Open",
        "priority": "Medium",
        "description": description,
    })
    todo.insert(ignore_permissions=True)
    return {"status": "hr_review", "todo": todo.name, "interaction_id": interaction_id}


def _find_applicant(email, candidate_name, caller_phone):
    rows = frappe.get_all(
        "Job Applicant",
        filters={"email_id": email},
        fields=["name", "applicant_name", "phone_number"],
        limit_page_length=3,
    )
    if len(rows) > 1:
        return None, "Several Job Applicants share this email"
    if not rows:
        return None, None
    row = rows[0]
    if row.applicant_name and row.applicant_name.strip().casefold() != candidate_name.casefold():
        return None, "Existing applicant name differs from the confirmed name"
    existing_phone = (row.phone_number or "").strip()
    if existing_phone and caller_phone and re.sub(r"\D", "", existing_phone)[-10:] != caller_phone[-10:]:
        return None, "Existing applicant phone differs from the inbound caller"
    return row.name, None


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive():
    """Receive an authenticated inbound call completion. One record per interaction."""
    if frappe.request.method != "POST":
        frappe.throw("POST required", frappe.PermissionError)

    expected = _configured(
        WEBHOOK_TOKEN, "PASTE_LONG_RANDOM_TOKEN_HERE", "WEBHOOK_TOKEN"
    )
    supplied = str(frappe.request.args.get("token") or "")
    if not hmac.compare_digest(expected, supplied):
        frappe.throw("Invalid webhook credentials", frappe.PermissionError)

    payload = frappe.request.get_json(silent=True)
    if not isinstance(payload, dict):
        frappe.throw("JSON object required")
    agent_id = _configured(
        SARVAM_AGENT_ID, "PASTE_RECRUITMENT_AGENT_ID_HERE", "SARVAM_AGENT_ID"
    )
    deployment_id = _configured(
        SARVAM_DEPLOYMENT_ID, "PASTE_INBOUND_DEPLOYMENT_ID_HERE",
        "SARVAM_DEPLOYMENT_ID"
    )
    if payload.get("app_id") != agent_id:
        frappe.throw("Unexpected recruitment agent", frappe.PermissionError)
    if payload.get("deployment_id") != deployment_id:
        frappe.throw("Unexpected inbound deployment", frappe.PermissionError)

    interaction_id = str(payload.get("interaction_id") or "")
    if not INTERACTION_RE.fullmatch(interaction_id):
        frappe.throw("Invalid interaction ID")
    caller_phone = str(payload.get("user_phone_number") or "")
    if not PHONE_RE.fullmatch(caller_phone):
        frappe.throw("Invalid caller phone")
    try:
        duration = max(0.0, min(float(payload.get("duration") or 0), 86400.0))
    except (ValueError, TypeError):
        duration = 0.0

    digest = hashlib.sha256(interaction_id.encode("utf-8")).hexdigest()
    lock = frappe.cache().lock("homegenie_recruitment:" + digest, timeout=180)
    if not lock.acquire(blocking=False):
        return {"status": "processing", "interaction_id": interaction_id}
    try:
        marker = _marker(interaction_id)
        if _already_processed(marker):
            return {"status": "already_processed", "interaction_id": interaction_id}

        variables = {}
        final_variables = payload.get("final_agent_variables")
        output_variables = payload.get("output_agent_variables")
        if isinstance(final_variables, dict):
            variables.update(final_variables)
        if isinstance(output_variables, dict):
            variables.update(output_variables)
        data = _screening_data(variables)
        disposition = str(variables.get("call_disposition") or "INCOMPLETE").upper()
        if disposition not in DISPOSITIONS:
            disposition = "INCOMPLETE"
        candidate_name = _text(data.get("candidate_name"), 140)
        candidate_email = _text(variables.get("candidate_email") or data.get("candidate_email"), 140).lower()
        email_confirmed = str(variables.get("email_confirmed") or "").upper() == "YES"
        phone_confirmed = str(variables.get("phone_confirmed") or "").upper() == "YES"
        role = _normal_role(data.get("role_applied"))

        review_reason = None
        if disposition in {"DECLINED", "WRONG_NUMBER"}:
            review_reason = "Candidate did not complete a recruitment screening"
        elif not data:
            review_reason = "Structured screening output was missing or invalid"
        elif not _provided(candidate_name):
            review_reason = "Candidate name was not confirmed"
        elif not email_confirmed or not EMAIL_RE.fullmatch(candidate_email):
            review_reason = "Candidate email was not confirmed"
        elif not role:
            review_reason = "Role could not be matched to the three supported sales roles"

        if review_reason:
            result = _review_todo(
                marker=marker, reason=review_reason, data=data,
                disposition=disposition, interaction_id=interaction_id,
                caller_phone=caller_phone, duration=duration,
            )
        else:
            applicant_name, conflict = _find_applicant(candidate_email, candidate_name, caller_phone)
            if conflict:
                result = _review_todo(
                    marker=marker, reason=conflict, data=data,
                    disposition=disposition, interaction_id=interaction_id,
                    caller_phone=caller_phone, duration=duration,
                )
            else:
                if applicant_name is None:
                    applicant = frappe.get_doc({
                        "doctype": "Job Applicant",
                        "applicant_name": candidate_name,
                        "email_id": candidate_email,
                        "phone_number": caller_phone if phone_confirmed else None,
                        "status": "Lead",
                    })
                    opening = _opening_for(role, data)
                    if opening:
                        applicant.job_title = opening
                    if frappe.db.exists("Job Applicant Source", "AI Phone Screening"):
                        applicant.source = "AI Phone Screening"
                    applicant.insert(ignore_permissions=True)
                    applicant_name = applicant.name
                frappe.get_doc({
                    "doctype": "Comment",
                    "comment_type": "Info",
                    "reference_doctype": "Job Applicant",
                    "reference_name": applicant_name,
                    "content": _summary_html(
                        data, marker=marker, disposition=disposition,
                        interaction_id=interaction_id, phone=caller_phone,
                        duration=duration,
                    ),
                }).insert(ignore_permissions=True)
                result = {"status": "saved", "job_applicant": applicant_name,
                          "interaction_id": interaction_id}

        # The lock must outlive a durable idempotency marker. A POST webhook is
        # retried on failure; committing here makes duplicate deliveries safe.
        frappe.db.commit()
        return result
    except Exception as error:
        frappe.db.rollback()
        frappe.log_error(
            title="Recruitment inbound processing failed",
            message="Interaction: " + interaction_id + "\nError type: "
                    + type(error).__name__ + "\nReview application validation and configuration.",
        )
        frappe.db.commit()
        raise
    finally:
        lock.release()
