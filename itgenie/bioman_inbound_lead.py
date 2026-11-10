"""Bioman Sarvam inbound CRM integration for Frappe/ERPNext v15.

Endpoints:
  POST /api/method/itgenie.bioman_inbound_lead.lookup_customer
  POST /api/method/itgenie.bioman_inbound_lead.receive

Set in site_config.json (do not commit secrets):
  bioman_inbound_webhook_token
  bioman_inbound_agent_id
  bioman_inbound_deployment_id

Both endpoints accept token via X-Bioman-Token header or ?token= query.
The lookup must be called by a trusted server-side Sarvam tool/pre-call bridge,
not from an untrusted customer browser. Validate Sarvam's actual webhook format
and pre-call tool support before going live.

Does not modify the existing sarvam_outbound.py, statuses, or follow-up dates.
"""

import hashlib
import hmac
import html
import json
import re

import frappe
from frappe.utils import now_datetime, strip_html

LEAD_SOURCE = "Bio Leads"
MARKER_PREFIX = "[Bioman-Inbound-Sarvam:"
INTERACTION_RE = re.compile(r"^[A-Za-z0-9_./:\-]{8,150}$")
INDIA_PHONE_RE = re.compile(r"^[6-9]\d{9}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
MISSING = {"", "not provided", "not_provided", "unknown", "none", "null"}
CATEGORIES = {"B2B", "B2C", "B2Ch"}
DETAIL_FIELDS = (
    "customer_name", "customer_category", "whatsapp_number",
    "whatsapp_confirmed", "email", "project_location", "property_type",
    "requirement_type", "number_of_users", "construction_stage",
    "installation_timeline", "customer_requirements", "questions_asked",
    "quotation_requested", "site_visit_requested", "callback_preference",
    "next_recommended_action", "call_outcome",
)


def _settings(key):
    value = str(frappe.conf.get(key) or "").strip()
    if not value:
        raise RuntimeError("Missing site configuration: " + key)
    return value


def _authenticate():
    # Frappe authenticates the Authorization header before
    # calling this API method.
    if frappe.session.user == "Guest":
        frappe.throw(
            "ERPNext API authentication required",
            frappe.PermissionError
        )

    # Restrict CRM context access to an approved integration user.
    allowed_user = frappe.conf.get("bioman_inbound_api_user")

    if not allowed_user:
        frappe.throw(
            "Bioman integration user is not configured",
            frappe.PermissionError
        )

    if frappe.session.user != allowed_user:
        frappe.throw(
            "User is not authorized for Bioman integration",
            frappe.PermissionError
        )

    return True

def _require_post():
    if frappe.request.method != "POST":
        frappe.throw("POST required", frappe.PermissionError)


def _value(raw, limit=1000):
    if raw is None or isinstance(raw, (dict, list)):
        return "Not provided"
    text = str(raw).strip()
    return (text[:limit] if text else "Not provided")


def _provided(value):
    return str(value or "").strip().lower() not in MISSING


def _mobile(raw):
    text = re.sub(r"[\s()\-]", "", str(raw or ""))
    if text.startswith("+91"):
        text = text[3:]
    elif text.startswith("91") and len(text) == 12:
        text = text[2:]
    return text if INDIA_PHONE_RE.fullmatch(text) else None


def _details(variables):
    raw = variables.get("lead_details_json")
    if isinstance(raw, str):
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
            raw = re.sub(r"\s*```$", "", raw)
        if len(raw) > 30000:
            return {}
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    if not isinstance(raw, dict):
        return {}
    return {key: _value(raw.get(key), 5000 if key == "customer_requirements" else 1000)
            for key in DETAIL_FIELDS}


def _numbers(mobile):
    return [mobile, "+91" + mobile, "91" + mobile]


def _matching_records(mobile, whatsapp=None):
    numbers = set(_numbers(mobile))
    if whatsapp:
        numbers.update(_numbers(whatsapp))
    result = {"Opportunity": set(), "Lead": set()}
    for doctype, fields in (("Opportunity", ("contact_mobile", "phone")),
                            ("Lead", ("mobile_no", "whatsapp_no", "phone"))):
        for field in fields:
            for row in frappe.get_all(doctype, filters={field: ["in", list(numbers)]},
                                      fields=["name"], limit_page_length=200):
                result[doctype].add(row.name)
    # Never expose non-Bioman enquiries to the agent.
    for doctype in result:
        result[doctype] = sorted(name for name in result[doctype]
            if frappe.db.get_value(doctype, name, "custom_product_enquired") == "Bioman")
    return result


def _history(doctype, name):
    rows = frappe.get_all(
        "CRM Note",
        filters={"parenttype": doctype, "parent": name, "parentfield": "notes"},
        fields=["note", "added_on", "creation", "name"],
        order_by="added_on desc, creation desc, name desc",
        limit_page_length=100,
    )
    rows = [r for r in rows if "[Bioman Follow-up Test]" not in (r.note or "")][:10]
    history = []
    for row in reversed(rows):
        content = html.unescape(strip_html(row.note or "")).strip()
        if content:
            history.append("Recorded on: " + str(row.added_on or row.creation or "Unknown")
                           + "\n" + content[:2500])
    return "\n\n---\n\n".join(history) or "No previous CRM Notes available."


def _context(doctype, name):
    doc = frappe.get_doc(doctype, name)
    customer_name = (doc.get("customer_name") or doc.get("lead_name") or
                     doc.get("first_name") or doc.get("party_name") or "Customer")
    return {
        "record_type": doctype.upper(), "record_id": name,
        "opportunity_id": name if doctype == "Opportunity" else "",
        "lead_id": name if doctype == "Lead" else "",
        "user_name": str(customer_name)[:140],
        "customer_requirements": html.unescape(strip_html(
            doc.get("custom_customer_requirements") or "")).strip()[:5000] or "Not provided",
        "previous_conversation_notes": _history(doctype, name),
        "customer_category": doc.get("custom_customer_category") or "Not provided",
        "call_purpose": "INBOUND",
    }


def _lookup(mobile):
    found = _matching_records(mobile)
    if len(found["Opportunity"]) == 1:
        return {"status": "existing_opportunity", **_context("Opportunity", found["Opportunity"][0])}
    if len(found["Opportunity"]) > 1:
        # Do not disclose full history until the correct project is selected.
        return {"status": "multiple_opportunities", "record_type": "UNCLEAR",
                "call_purpose": "INBOUND", "message": "Ask which project or enquiry the customer means."}
    if len(found["Lead"]) == 1:
        return {"status": "existing_lead", **_context("Lead", found["Lead"][0])}
    if len(found["Lead"]) > 1:
        return {"status": "multiple_leads", "record_type": "UNCLEAR",
                "call_purpose": "INBOUND", "message": "Ask for identifying details; request staff review."}
    return {"status": "new_customer", "record_type": "NEW", "call_purpose": "INBOUND",
            "user_name": "", "customer_requirements": "Not provided",
            "previous_conversation_notes": "No previous CRM Notes available."}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def lookup_customer(phone=None):
    """Authenticated pre-call CRM context lookup. Never return details on ambiguous matches."""
    _require_post()
    _authenticate()
    phone = phone or frappe.form_dict.get("user_phone_number")
    mobile = _mobile(phone)
    if not mobile:
        return {"status": "invalid_phone", "record_type": "UNCLEAR", "call_purpose": "INBOUND"}
    return _lookup(mobile)


def _marker(interaction_id):
    return MARKER_PREFIX + interaction_id + "]"


def _already_processed(marker):
    return bool(
        frappe.db.exists("CRM Note", {"note": ["like", "%" + marker + "%"]})
        or frappe.db.exists("Comment", {"content": ["like", "%" + marker + "%"]})
        or frappe.db.exists("ToDo", {"description": ["like", "%" + marker + "%"]})
    )


def _summary_html(details, marker, interaction_id, duration, caller):
    parts = ["<h4>Bioman AI Inbound Call Summary</h4>",
             "<p><b>Caller:</b> " + html.escape(caller) +
             "<br><b>Duration:</b> " + html.escape(str(round(duration, 2))) + " seconds</p>",
             "<ul>"]
    for key in DETAIL_FIELDS:
        parts.append("<li><b>" + html.escape(key.replace("_", " ").title()) +
                     ":</b> " + html.escape(details.get(key, "Not provided")) + "</li>")
    parts.append("</ul><p>Sarvam Interaction: " + html.escape(interaction_id) +
                 "<br>" + html.escape(marker) + "</p>")
    return "".join(parts)


def _append_note(doctype, name, content):
    doc = frappe.get_doc(doctype, name)
    doc.append("notes", {"note": content, "added_by": "Administrator",
                         "added_on": now_datetime()})
    doc.save(ignore_permissions=True)


def _review(reason, details, marker, interaction_id, duration, caller):
    description = ("<p><b>Bioman inbound call needs review:</b> " +
                   html.escape(reason) + "</p>" +
                   _summary_html(details, marker, interaction_id, duration, caller))
    todo = frappe.get_doc({"doctype": "ToDo", "status": "Open",
                          "priority": "Medium", "description": description})
    todo.insert(ignore_permissions=True)
    return {"status": "needs_review", "todo": todo.name, "interaction_id": interaction_id}


def _customer_spoke(transcript):
    return any(isinstance(turn, dict) and turn.get("role") in ("user", "customer")
               and str(turn.get("en_text") or turn.get("indic_text") or
                       turn.get("text") or "").strip() for turn in transcript)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive():
    """Authenticated, idempotent post-call CRM writer; expects Sarvam payload shape."""
    _require_post()
    _authenticate()
    payload = frappe.request.get_json(silent=True)
    if not isinstance(payload, dict):
        frappe.throw("JSON object required")
    if payload.get("app_id") != _settings("bioman_inbound_agent_id"):
        frappe.throw("Unexpected Bioman agent", frappe.PermissionError)
    if payload.get("deployment_id") != _settings("bioman_inbound_deployment_id"):
        frappe.throw("Unexpected inbound deployment", frappe.PermissionError)
    interaction_id = str(payload.get("interaction_id") or "")
    if not INTERACTION_RE.fullmatch(interaction_id):
        frappe.throw("Invalid interaction ID")
    caller_raw = str(payload.get("user_phone_number") or "").strip()
    try:
        duration = max(0.0, min(float(payload.get("duration") or 0), 86400.0))
    except (ValueError, TypeError):
        duration = 0.0
    digest = hashlib.sha256(interaction_id.encode("utf-8")).hexdigest()
    lock = frappe.cache().lock("bioman_inbound:" + digest, timeout=180)
    if not lock.acquire(blocking=False):
        return {"status": "processing", "interaction_id": interaction_id}
    stage = "parse"
    try:
        marker = _marker(interaction_id)
        if _already_processed(marker):
            return {"status": "already_processed", "interaction_id": interaction_id}
        variables = {}
        for key in ("final_agent_variables", "output_agent_variables"):
            if isinstance(payload.get(key), dict):
                variables.update(payload[key])
        category = str(variables.get("call_category") or "UNCLEAR").strip().upper()
        details = _details(variables)
        transcript = payload.get("interaction_transcript")
        mobile = _mobile(caller_raw)
        if category == "OTHER":
            return {"status": "not_a_bioman_enquiry", "interaction_id": interaction_id}
        if isinstance(transcript, list) and not _customer_spoke(transcript):
            return {"status": "no_customer_speech", "interaction_id": interaction_id}
        if not isinstance(transcript, list):
            result = _review("Transcript missing; cannot verify customer speech", details,
                             marker, interaction_id, duration, caller_raw)
        elif not mobile:
            result = _review("Invalid or non-Indian caller number", details,
                             marker, interaction_id, duration, caller_raw)
        else:
            whatsapp = None
            if str(details.get("whatsapp_confirmed") or "").upper() == "YES":
                raw = details.get("whatsapp_number")
                whatsapp = (mobile if str(raw or "").strip().lower() in
                            {"same", "same as caller", "same as calling number", "caller number"}
                            else _mobile(raw))
            stage = "match"
            matches = _matching_records(mobile, whatsapp)
            content = _summary_html(details, marker, interaction_id, duration, mobile)
            if len(matches["Opportunity"]) == 1:
                name = matches["Opportunity"][0]
                stage = "append opportunity note"
                _append_note("Opportunity", name, content)
                result = {"status": "opportunity_updated", "opportunity": name,
                          "interaction_id": interaction_id}
            elif len(matches["Opportunity"]) > 1:
                result = _review("Multiple matching Bioman Opportunities; cannot safely choose",
                                 details, marker, interaction_id, duration, caller_raw)
            elif len(matches["Lead"]) == 1:
                name = matches["Lead"][0]
                stage = "append lead note"
                _append_note("Lead", name, content)
                result = {"status": "lead_updated", "lead": name,
                          "interaction_id": interaction_id}
            elif matches["Lead"]:
                result = _review("Multiple matching Bioman Leads", details, marker,
                                 interaction_id, duration, caller_raw)
            elif category != "NEW_ENQUIRY":
                result = _review("New caller not classified as NEW_ENQUIRY", details,
                                 marker, interaction_id, duration, caller_raw)
            elif not _provided(details.get("customer_name")):
                result = _review("Customer name missing", details, marker,
                                 interaction_id, duration, caller_raw)
            elif details.get("customer_category") not in CATEGORIES:
                result = _review("Customer category missing or invalid", details,
                                 marker, interaction_id, duration, caller_raw)
            elif not whatsapp:
                result = _review("Confirmed WhatsApp number missing or invalid", details,
                                 marker, interaction_id, duration, caller_raw)
            elif not frappe.db.exists("Lead Source", LEAD_SOURCE):
                raise RuntimeError("Lead Source does not exist: " + LEAD_SOURCE)
            else:
                stage = "create lead"
                name = _value(details.get("customer_name"), 140)
                fields = {"doctype": "Lead", "first_name": name, "lead_name": name,
                          "mobile_no": mobile, "whatsapp_no": whatsapp,
                          "source": LEAD_SOURCE, "status": "Lead",
                          "custom_product_enquired": "Bioman",
                          "custom_customer_category": details["customer_category"],
                          "custom_customer_requirements": _value(
                              details.get("customer_requirements"), 5000)}
                email = details.get("email")
                if _provided(email) and EMAIL_RE.fullmatch(email):
                    fields["email_id"] = email
                lead = frappe.get_doc(fields)
                lead.owner = "Administrator"
                lead.insert(ignore_permissions=True)
                stage = "append new lead note"
                _append_note("Lead", lead.name, content)
                result = {"status": "lead_created", "lead": lead.name,
                          "interaction_id": interaction_id}
        frappe.db.commit()
        return result
    except Exception:
        frappe.db.rollback()
        # Do not log the token, request headers, or raw webhook payload.
        frappe.log_error(title="Bioman inbound CRM webhook failed",
                         message="Interaction: " + interaction_id + "\nStage: " + stage +
                                 "\n" + frappe.get_traceback()[:10000])
        frappe.db.commit()
        raise
    finally:
        lock.release()
