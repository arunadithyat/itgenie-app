
import hmac
import json

import html

import re

from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import frappe

import requests

from frappe.rate_limiter import rate_limit

from frappe.utils import now_datetime, strip_html

# Paste your rotated Sarvam API key here.

SARVAM_API_KEY = "sk_samvaad_5uppgzdn_aOzV3Z8PGAa5e7Ct5jY4usZb"

# Leave empty until the Sarvam webhook is connected. While empty, the

# webhook endpoint rejects every request. This is NOT provided by Sarvam;

# it is a random value we will create ourselves when wiring the webhook.

SARVAM_WEBHOOK_SECRET = ""

SARVAM_ORG_ID = "01a110c3-98c4-767f-83e2-55bea65759e2"

SARVAM_WORKSPACE_ID = "01a110c3-98c9-71a0-8b4d-85b630618af0"

SARVAM_AGENT_ID = "Bioman-RNR-34e5735e-d72a"

# Version 12 produced the 25 October 2026 Follow-up pilot call; update after publishing changes.

SARVAM_AGENT_VERSION = 12

# Used when the Opportunity Owner has no valid Employee custom_contact_number.

DEFAULT_EXECUTIVE_PHONE = "7387773877"

SMARTFLO_CONNECTION_ID = "Smartflo-Ra-e050f3a5-5f85"

SMARTFLO_PHONE_NUMBER = "+918065108701"

TEST_OPPORTUNITY = "BOS-OPP-2026-00754"

# The Follow-up test uses this Opportunity's context, but NEVER its phone.
TEST_FOLLOWUP_OPPORTUNITY = "BOS-OPP-2026-00053"
TEST_FOLLOWUP_MOBILE = "+919514930546"
FOLLOWUP_TEST_MARKER = "[Bioman-Followup-Test]"

# Pilot controls. Set ENABLE_LIVE_FOLLOWUP_CALLS=True only after preview/review.
ENABLE_LIVE_FOLLOWUP_CALLS = True
FOLLOWUP_PILOT_DAILY_CAP = 13
FOLLOWUP_PILOT_PER_RUN = 1
FOLLOWUP_START_HOUR_IST = 9
FOLLOWUP_START_MINUTE_IST = 30
FOLLOWUP_END_HOUR_IST = 17
FOLLOWUP_END_MINUTE_IST = 30
FOLLOWUP_CLAIM_MARKER = "[Bioman-Followup-Claim]"
FOLLOWUP_ATTEMPT_MARKER = "[Bioman-Followup-Pilot-Attempt]"
FOLLOWUP_OUTCOME_MARKER = "[Bioman-Followup-Outcome]"
FOLLOWUP_SUPPRESSION_MARKER = "[Bioman-No-AI-Calls]"
FOLLOWUP_DUE_MARKER = "[Bioman-Followup-Due]"
FOLLOWUP_SNAPSHOT_BATCH = 100

CALL_LOCK_SECONDS = 3600

# Matching / retry settings

ATTEMPT_MARKER = "[Bioman-Sarvam-Attempt]"

PENDING_WINDOW_MINUTES = 90      # stop looking for a call's summary after this

MATCH_BEFORE_SECONDS = 120       # clock-skew tolerance before our request time

MATCH_AFTER_MINUTES = 20         # max delay between our request and Sarvam dialing

SUMMARY_GRACE_MINUTES = 20       # log once if summary still missing after call end

IST = timezone(timedelta(hours=5, minutes=30))

UTC = timezone.utc

SARVAM_API_URL = (
    f"https://apps.sarvam.ai/api/outbounds/v1/orgs/{SARVAM_ORG_ID}/"
    f"workspaces/{SARVAM_WORKSPACE_ID}/outbounds"
)

SARVAM_INTERACTIONS_URL = (
    f"https://apps.sarvam.ai/api/analytics/v1/{SARVAM_ORG_ID}/"
    f"{SARVAM_WORKSPACE_ID}/{SARVAM_AGENT_ID}/interactions"
)

SARVAM_ATTEMPTS_URL = (
    f"https://apps.sarvam.ai/api/analytics/v1/{SARVAM_ORG_ID}/"
    f"{SARVAM_WORKSPACE_ID}/{SARVAM_AGENT_ID}/attempts"
)

ATTEMPT_RE = re.compile(
    r"Attempt:\s*(\S+)\s*\|\s*Mobile:\s*(\+\d+)\s*\|\s*Requested UTC:\s*([0-9T:\-]+)"
)

# ---------------------------------------------------------------------------

# Helpers

# ---------------------------------------------------------------------------

def _api_key():
    if not SARVAM_API_KEY or SARVAM_API_KEY.startswith("PASTE_"):
        frappe.throw("Set SARVAM_API_KEY in sarvam_outbound.py before testing.")
    return SARVAM_API_KEY

def _check_key():
    _api_key()

def normalize_mobile(number):
    number = re.sub(r"\D", "", str(number or ""))
    if len(number) == 10:
        return "+91" + number
    if len(number) == 12 and number.startswith("91"):
        return "+" + number
    raise ValueError("Invalid Indian mobile number.")

def _safe_normalize(number):
    try:
        return normalize_mobile(number)
    except ValueError:
        return None

def _parse_utc(value):
    """Sarvam returns naive UTC timestamps like 2026-10-11T09:38:20."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)

def _note_exists(marker):
    return frappe.db.exists(
        "CRM Note",
        {"parenttype": "Opportunity", "note": ["like", f"%{marker}%"]},
    )

def get_executive_phone(opportunity):
    """Opportunity Owner -> active Employee (user_id) -> custom_contact_number."""
    owner = opportunity.get("opportunity_owner")
    if owner:
        number = frappe.db.get_value(
            "Employee",
            {"user_id": owner, "status": "Active"},
            "custom_contact_number",
        )
        normalized = _safe_normalize(number)
        if normalized:
            return normalized
    fallback = _safe_normalize(DEFAULT_EXECUTIVE_PHONE)
    frappe.logger().warning(
        "Bioman Sarvam: no valid executive number for Opportunity %s (owner %s); using fallback",
        opportunity.name, owner,
    )
    return fallback or ""

def get_opportunity_details(opportunity_id):
    # The public preview below checks write permission; dispatch runs as Administrator.
    opportunity = frappe.get_doc("Opportunity", opportunity_id)
    if opportunity.get("custom_product_enquired") != "Bioman":
        frappe.throw("Product Enquired must be Bioman.")
    status = opportunity.get("status")
    if status not in ("RNR", "Follow-up"):
        frappe.throw("Opportunity status must be RNR or Follow-up.")
    notes = frappe.get_all(
        "CRM Note",
        filters={"parenttype": "Opportunity", "parent": opportunity.name,
                 "parentfield": "notes"},
        fields=["name", "note", "added_on", "creation"],
        order_by="added_on desc, creation desc, name desc",
        limit_page_length=100,
    )
    notes = [row for row in notes if "[Bioman Follow-up Test]" not in (row.get("note") or "")][:10]
    history = []
    for note in reversed(notes):
        content = html.unescape(strip_html(note.get("note") or "")).strip()
        if not content:
            continue
        if len(content) > 2500:
            content = content[:2500] + " [Note shortened]"
        recorded = note.get("added_on") or note.get("creation")
        history.append("Recorded on: " + str(recorded or "Not provided")
                       + "\n" + content)
    requirements = html.unescape(strip_html(
        opportunity.get("custom_customer_requirements") or ""
    )).strip()
    return {
        "opportunity_id": opportunity.name,
        "status": status,
        "call_purpose": "FOLLOWUP" if status == "Follow-up" else "RNR",
        "customer_name": str(opportunity.get("customer_name")
                             or opportunity.get("party_name") or "Customer"),
        "mobile": opportunity.get("contact_mobile") or opportunity.get("phone"),
        "executive_phone": get_executive_phone(opportunity),
        "customer_requirements": requirements[:5000] or "Not provided",
        "previous_conversation_notes": "\n\n---\n\n".join(history)
                                       or "No previous CRM Notes available.",
    }

@frappe.whitelist()

def preview_call_context(opportunity_id):
    """Read-only preview; never places a call."""
    frappe.get_doc("Opportunity", opportunity_id).check_permission("write")
    return get_opportunity_details(opportunity_id)

# ---------------------------------------------------------------------------

# Outbound call (existing working flow + attempt recording)

# ---------------------------------------------------------------------------

def _record_attempt(opportunity_id, attempt_id, mobile, requested_at):
    content = (
        f"{ATTEMPT_MARKER} Bioman AI call placed. "
        f"Attempt: {attempt_id} | Mobile: {mobile} | "
        f"Requested UTC: {requested_at.strftime('%Y-%m-%dT%H:%M:%S')}"
    )
    frappe.get_doc({
        "doctype": "Comment",
        "comment_type": "Info",
        "reference_doctype": "Opportunity",
        "reference_name": opportunity_id,
        "content": content,
    }).insert(ignore_permissions=True)

@frappe.whitelist()

def initiate_outbound_call(opportunity_id, test_mobile=None):
    """Existing RNR outbound flow."""
    if opportunity_id != TEST_OPPORTUNITY:
        frappe.throw("This Opportunity is not allowed for AI testing.")
    if not frappe.has_permission("Opportunity", "write", opportunity_id):
        frappe.throw("You do not have permission to initiate this AI call.", frappe.PermissionError)
    details = get_opportunity_details(opportunity_id)
    if details["status"] not in ("RNR", "Follow-up"):
        frappe.throw("Opportunity status must be RNR or Follow-up.")
    api_key = _api_key()
    mobile = test_mobile or details["mobile"]
    if not mobile:
        frappe.throw("Customer mobile number is missing in contact_mobile and phone.")
    mobile = normalize_mobile(mobile)
    lock = frappe.cache().lock(
        "bioman_sarvam_outbound:" + opportunity_id,
        timeout=CALL_LOCK_SECONDS,
    )
    if not lock.acquire(blocking=False):
        frappe.throw("An AI call request for this Opportunity is already processing.")
    try:
        payload = {
            "app_config": {
                "app_id": SARVAM_AGENT_ID,
                "app_version": SARVAM_AGENT_VERSION,
                "app_type": "agent",
                "connection_config": {
                    "connection_id": SMARTFLO_CONNECTION_ID,
                    "agent_phone_number": SMARTFLO_PHONE_NUMBER,
                },
                "agent_variables": {
                    "user_name": details["customer_name"],
                    "executive_phone": details["executive_phone"],
                    "opportunity_id": details["opportunity_id"],
                    "call_purpose": details["call_purpose"],
                    "customer_requirements": details["customer_requirements"],
                    "previous_conversation_notes": details["previous_conversation_notes"],
                },
            },
            "user_config": {"user_phone_number": mobile},
        }
        requested_at = datetime.now(UTC)
        response = requests.post(
            SARVAM_API_URL,
            headers={"Content-Type": "application/json", "X-API-Key": api_key},
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        try:
            sarvam_response = response.json()
        except ValueError:
            sarvam_response = {"raw_response": response.text[:2000]}
        attempt_id = None
        if isinstance(sarvam_response, dict):
            attempt_id = sarvam_response.get("attempt_id")
        if not attempt_id:
            attempt_id = "local-" + frappe.generate_hash(length=12)
        # The call is already placed; never fail the job because of bookkeeping.
        try:
            _record_attempt(opportunity_id, attempt_id, mobile, requested_at)
        except Exception:
            frappe.log_error(
                title="Bioman Sarvam Attempt Record Error",
                message=frappe.get_traceback(),
            )
        frappe.logger().info(
            "Bioman Sarvam outbound accepted for Opportunity %s, attempt %s",
            opportunity_id, attempt_id,
        )
        return {
            "success": True,
            "opportunity": opportunity_id,
            "customer_name": details["customer_name"],
            "mobile": mobile,
            "attempt_id": attempt_id,
            "sarvam_response": sarvam_response,
        }
    except requests.RequestException as error:
        error_body = error.response.text[:2000] if error.response is not None else ""
        frappe.log_error(
            title="Bioman Sarvam Outbound API Error",
            message=f"Opportunity: {opportunity_id}\nError: {error}\nResponse: {error_body}",
        )
        raise
    finally:
        lock.release()

@frappe.whitelist()

def queue_outbound_call(opportunity_id):
    if opportunity_id != TEST_OPPORTUNITY:
        frappe.throw("Opportunity not allowed for AI testing.")
    if not frappe.has_permission("Opportunity", "write", opportunity_id):
        frappe.throw("Not permitted to initiate AI calls.")
    if frappe.db.get_value("Opportunity", opportunity_id, "status") != "RNR":
        frappe.throw("Opportunity must be RNR.")
    frappe.enqueue(
        "itgenie.sarvam_outbound.initiate_outbound_call",
        queue="short",
        enqueue_after_commit=True,
        opportunity_id=opportunity_id,
    )
    return {"queued": True}

# ---------------------------------------------------------------------------

# Completed call -> CRM Note

# ---------------------------------------------------------------------------

def _fetch_interactions(start_utc, end_utc):
    items = []
    offset = 0
    while True:
        response = requests.get(
            SARVAM_INTERACTIONS_URL,
            headers={"X-API-Key": _api_key()},
            params={
                "start_datetime": start_utc.astimezone(IST).isoformat(timespec="seconds"),
                "end_datetime": end_utc.astimezone(IST).isoformat(timespec="seconds"),
                "limit": 100,
                "offset": offset,
            },
            timeout=30,
        )
        response.raise_for_status()
        batch = response.json().get("items", []) or []
        items.extend(batch)
        if len(batch) < 100 or offset >= 900:
            break
        offset += 100
    return items

def _pending_attempts():
    since = now_datetime() - timedelta(minutes=PENDING_WINDOW_MINUTES)
    rows = frappe.get_all(
        "Comment",
        filters={
            "reference_doctype": "Opportunity",
            "comment_type": "Info",
            "content": ["like", f"%{ATTEMPT_MARKER}%"],
            "creation": [">=", since],
        },
        fields=["reference_name", "content"],
        order_by="creation asc",
    )
    attempts = []
    for row in rows:
        if row.reference_name != TEST_OPPORTUNITY:
            continue
        match = ATTEMPT_RE.search(html.unescape(strip_html(row.content or "")))
        if not match:
            continue
        attempt_id, mobile, requested = match.groups()
        if _note_exists("Sarvam Attempt: " + attempt_id):
            continue
        try:
            requested_at = datetime.strptime(requested, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            continue
        attempts.append({
            "opportunity_id": row.reference_name,
            "attempt_id": attempt_id,
            "mobile": mobile,
            "requested_at": requested_at,
        })
    return attempts

def _build_note_html(item, variables, summary, attempt_id):
    def safe(value):
        return html.escape(str(value if value is not None else ""), quote=True)
    started = _parse_utc(item.get("start_datetime"))
    started_text = started.astimezone(IST).strftime("%d-%m-%Y %I:%M %p IST") if started else "Unknown"
    parts = [
        "<h4>Bioman AI Call Summary</h4>",
        f"<p><b>Lead Interest:</b> {safe(variables.get('lead_interest') or 'Not provided')}</p>",
        f"<p><b>Call Time:</b> {safe(started_text)}<br>"
        f"<b>Duration:</b> {safe(item.get('duration_in_seconds'))} seconds<br>"
        f"<b>Ended By:</b> {safe(item.get('ended_by'))}</p>",
    ]
    ordered_keys = ["call_outcome", "next_recommended_action"]
    ordered_keys += [key for key in summary if key not in ordered_keys]
    parts.append("<ul>")
    for key in ordered_keys:
        if key not in summary:
            continue
        label = str(key).replace("_", " ").title()
        parts.append(f"<li><b>{safe(label)}:</b> {safe(summary[key])}</li>")
    parts.append("</ul>")
    parts.append(
        f"<p>Sarvam Interaction: {safe(item.get('interaction_id'))}<br>"
        f"Sarvam Attempt: {safe(attempt_id)}</p>"
    )
    return "\n".join(parts)

def _parse_call_summary(value):
    """Accept a native object or a JSON object encoded as text."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*```$", "", text).strip()
        value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("call_summary must be a JSON object")
    return value or None


def _import_interaction(opportunity_id, item, attempt_id):
    if opportunity_id not in (TEST_OPPORTUNITY, TEST_FOLLOWUP_OPPORTUNITY):
        if not frappe.db.exists("Comment", {
            "reference_doctype": "Opportunity",
            "reference_name": opportunity_id,
            "comment_type": "Info",
            "content": ["like", f"%{FOLLOWUP_ATTEMPT_MARKER}%Attempt: {attempt_id} |%"],
        }):
            return {"status": "blocked", "message": "No matching Follow-up attempt Comment."}
    interaction_id = item.get("interaction_id")
    variables = item.get("agent_variables") or {}
    if not isinstance(variables, dict):
        return {"status": "invalid_agent_variables", "interaction_id": interaction_id}
    try:
        summary = _parse_call_summary(variables.get("call_summary"))
    except (ValueError, TypeError):
        frappe.log_error(
            title="Bioman Sarvam Summary Format Error",
            message=f"Opportunity: {opportunity_id}\nInteraction: {interaction_id}\n"
                    "call_summary must contain a valid JSON object.",
        )
        return {"status": "invalid_summary_format", "interaction_id": interaction_id}
    if not isinstance(summary, dict) or not summary:
        ended = _parse_utc(item.get("end_datetime"))
        if ended and datetime.now(UTC) - ended > timedelta(minutes=SUMMARY_GRACE_MINUTES):
            flag = "bioman_sarvam_nosummary:" + str(interaction_id)
            if not frappe.cache().get_value(flag):
                frappe.cache().set_value(flag, 1, expires_in_sec=86400)
                frappe.log_error(
                    title="Bioman Sarvam Summary Missing",
                    message=(
                        f"Opportunity: {opportunity_id}\nInteraction: {interaction_id}\n"
                        f"Ended by: {item.get('ended_by')}\n"
                        f"Failure reason: {item.get('failure_reason')}"
                    ),
                )
        return {"status": "summary_not_ready", "interaction_id": interaction_id}
    note_lock = frappe.cache().lock("bioman_sarvam_note:" + str(interaction_id), timeout=60)
    if not note_lock.acquire(blocking=False):
        return {"status": "in_progress", "interaction_id": interaction_id}
    try:
        if _note_exists("Sarvam Interaction: " + str(interaction_id)):
            return {"status": "already_exists", "interaction_id": interaction_id}
        opportunity = frappe.get_doc("Opportunity", opportunity_id)
        note_html = _build_note_html(item, variables, summary, attempt_id)
        if opportunity_id == TEST_FOLLOWUP_OPPORTUNITY:
            note_html = ("<p><b>[Bioman Follow-up Test] Internal number only. "
                         "This was not a customer conversation.</b></p>\n" + note_html)
        opportunity.append("notes", {
            "note": note_html,
            "added_by": "Administrator",
            "added_on": now_datetime(),
        })
        opportunity.save(ignore_permissions=True)
        frappe.logger().info(
            "Bioman Sarvam CRM Note added to %s for interaction %s",
            opportunity_id, interaction_id,
        )
        return {"status": "created", "interaction_id": interaction_id}
    finally:
        note_lock.release()

def process_pending_calls():
    """Runs every minute (Scheduler Event Server Script) and on webhook.
    Does nothing if no call is pending."""
    attempts = _pending_attempts()
    if not attempts:
        return {"pending": 0}
    lock = frappe.cache().lock("bioman_sarvam_process", timeout=300)
    if not lock.acquire(blocking=False):
        return {"skipped": "already running"}
    try:
        now_utc = datetime.now(UTC)
        start = min(a["requested_at"] for a in attempts) - timedelta(seconds=MATCH_BEFORE_SECONDS)
        try:
            items = _fetch_interactions(start, now_utc)
        except requests.RequestException as error:
            body = error.response.text[:1000] if error.response is not None else ""
            frappe.log_error(
                title="Bioman Sarvam Analytics API Error",
                message=f"Error: {error}\nResponse: {body}",
            )
            return {"error": "analytics_fetch_failed"}
        candidates = []
        for item in items:
            if item.get("channel_direction") != "outbound" or str(item.get("is_debug_call") or "").lower() in ("true", "1"):
                continue
            attempted = _parse_utc(item.get("attempted_at") or item.get("start_datetime"))
            if attempted and item.get("interaction_id"):
                candidates.append((attempted, item))
        candidates.sort(key=lambda pair: pair[0])
        used = set()
        results = []
        for attempt in attempts:
            earliest = attempt["requested_at"] - timedelta(seconds=MATCH_BEFORE_SECONDS)
            latest = attempt["requested_at"] + timedelta(minutes=MATCH_AFTER_MINUTES)
            match = None
            for attempted, item in candidates:
                interaction_id = item["interaction_id"]
                if interaction_id in used:
                    continue
                if not (earliest <= attempted <= latest):
                    continue
                if _safe_normalize(item.get("user_contact")) != attempt["mobile"]:
                    continue
                if _note_exists("Sarvam Interaction: " + interaction_id):
                    used.add(interaction_id)
                    continue
                match = item
                break
            if not match:
                results.append({"attempt_id": attempt["attempt_id"], "status": "waiting_for_interaction"})
                continue
            used.add(match["interaction_id"])
            result = _import_interaction(attempt["opportunity_id"], match, attempt["attempt_id"])
            result["attempt_id"] = attempt["attempt_id"]
            results.append(result)
        return {"pending": len(attempts), "results": results}
    finally:
        lock.release()

@frappe.whitelist()

def run_pending_calls_job():
    """Entry point for the Scheduler Event Server Script (runs as Administrator)."""
    if frappe.session.user != "Administrator" and "System Manager" not in frappe.get_roles():
        frappe.throw("Not permitted.", frappe.PermissionError)
    result = process_pending_calls()
    frappe.db.commit()
    return result

def backfill_interaction(interaction_id, opportunity_id=TEST_OPPORTUNITY, hours=48):
    """Manual test: import one known interaction into the Opportunity's Notes."""
    if opportunity_id != TEST_OPPORTUNITY:
        frappe.throw("Opportunity not allowed for AI testing.")
    now_utc = datetime.now(UTC)
    for item in _fetch_interactions(now_utc - timedelta(hours=int(hours)), now_utc):
        if item.get("interaction_id") != interaction_id:
            continue
        if item.get("channel_direction") != "outbound" or str(item.get("is_debug_call") or "").lower() in ("true", "1"):
            return {"status": "rejected", "message": "Not a normal outbound call."}
        return _import_interaction(opportunity_id, item, "manual-backfill")
    return {"status": "not_found", "message": "Interaction not found in the time window."}

# ---------------------------------------------------------------------------

# Webhook (secured trigger only; payload is never trusted for content)

# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True, methods=["POST"])

@rate_limit(limit=30, seconds=60)

def sarvam_call_webhook():
    secret = str(SARVAM_WEBHOOK_SECRET or "")
    token = str(
        frappe.request.args.get("token")
        or frappe.get_request_header("X-Bioman-Webhook-Token")
        or ""
    )
    if not secret or not hmac.compare_digest(token.encode(), secret.encode()):
        frappe.local.response.http_status_code = 403
        return {"ok": False}
    try:
        payload = frappe.request.get_json(silent=True) or {}
    except Exception:
        payload = {}
    keys = sorted(payload.keys()) if isinstance(payload, dict) else type(payload).__name__
    frappe.logger("bioman_sarvam").info("Sarvam webhook received; payload keys: %s", keys)
    frappe.enqueue(
        "itgenie.sarvam_outbound.process_pending_calls",
        queue="short",
        job_id="bioman_sarvam_process",
        deduplicate=True,
    )
    return {"ok": True}


# Administrative console helpers. Call only as standalone console operations.
def _check_test_admin():
    if frappe.session.user != "Administrator" and "System Manager" not in frappe.get_roles():
        frappe.throw("Not permitted.", frappe.PermissionError)
    frappe.get_doc("Opportunity", TEST_OPPORTUNITY).check_permission("write")


def _followup_test_details():
    _check_test_admin()
    frappe.get_doc("Opportunity", TEST_FOLLOWUP_OPPORTUNITY).check_permission("write")
    opportunity = frappe.get_doc("Opportunity", TEST_FOLLOWUP_OPPORTUNITY)
    today = datetime.now(IST).date().isoformat()
    if opportunity.status != "Follow-up":
        frappe.throw("The test Opportunity is no longer in Follow-up status.")
    if opportunity.get("custom_product_enquired") != "Bioman":
        frappe.throw("The test Opportunity is no longer a Bioman enquiry.")
    if str(opportunity.get("custom_next_followup_date") or "") != today:
        frappe.throw("The test Opportunity's next follow-up date is not today (IST).")
    details = get_opportunity_details(opportunity.name)
    if _safe_normalize(details.get("mobile")) == TEST_FOLLOWUP_MOBILE:
        frappe.throw("Test number matches the customer number; choose another test record.")
    return details, today


@frappe.whitelist()
def preview_followup_test():
    """Read-only inspection: the configured Opportunity and destination."""
    details, today = _followup_test_details()
    return {
        "opportunity": details["opportunity_id"],
        "date_ist": today,
        "call_purpose": details["call_purpose"],
        "test_destination_last_four": TEST_FOLLOWUP_MOBILE[-4:],
        "requirements_available": details["customer_requirements"] != "Not provided",
        "history_available": details["previous_conversation_notes"] != "No previous CRM Notes available.",
        "agent_version_configured": SARVAM_AGENT_VERSION,
        "customer_number_will_be_used": False,
    }


def _followup_test_comment(content):
    frappe.get_doc({
        "doctype": "Comment",
        "comment_type": "Info",
        "reference_doctype": "Opportunity",
        "reference_name": TEST_FOLLOWUP_OPPORTUNITY,
        "content": content,
    }).insert(ignore_permissions=True)


@frappe.whitelist()
def run_followup_test_call():
    """Call the internal test number once for today's eligible Opportunity.

    Invoke standalone from System Console as an administrator. A Comment is
    committed BEFORE contacting Sarvam; an ambiguous timeout must be reviewed,
    not automatically redialled. Never invoke this from an Opportunity hook.
    """
    details, today = _followup_test_details()
    _api_key()
    marker = f"{FOLLOWUP_TEST_MARKER} Date: {today}"
    lock = frappe.cache().lock(
        f"bioman_followup_test:{frappe.local.site}:{details['opportunity_id']}:{today}",
        timeout=120,
    )
    if not lock.acquire(blocking=False):
        return {"status": "already_processing"}
    try:
        if frappe.db.exists("Comment", {
            "reference_doctype": "Opportunity",
            "reference_name": details["opportunity_id"],
            "comment_type": "Info",
            "content": ["like", f"%{marker}%"],
        }):
            return {"status": "already_tested_today", "opportunity": details["opportunity_id"]}

        _followup_test_comment(f"{marker} Submitting to internal test number ending {TEST_FOLLOWUP_MOBILE[-4:]}.")
        frappe.db.commit()  # Intentional pre-network durability for this standalone test.

        payload = {
            "app_config": {
                "app_id": SARVAM_AGENT_ID,
                "app_version": SARVAM_AGENT_VERSION,
                "app_type": "agent",
                "connection_config": {
                    "connection_id": SMARTFLO_CONNECTION_ID,
                    "agent_phone_number": SMARTFLO_PHONE_NUMBER,
                },
                "agent_variables": {
                    "user_name": details["customer_name"],
                    "executive_phone": details["executive_phone"],
                    "opportunity_id": details["opportunity_id"],
                    "call_purpose": "FOLLOWUP",
                    "customer_requirements": details["customer_requirements"],
                    "previous_conversation_notes": details["previous_conversation_notes"],
                },
            },
            "user_config": {"user_phone_number": TEST_FOLLOWUP_MOBILE},
        }
        requested_at = datetime.now(UTC)
        try:
            response = requests.post(
                SARVAM_API_URL,
                headers={"Content-Type": "application/json", "X-API-Key": _api_key()},
                json=payload,
                timeout=(10, 30),
            )
            response.raise_for_status()
        except requests.RequestException as error:
            code = error.response.status_code if error.response is not None else "unknown"
            _followup_test_comment(f"{marker} Submission uncertain or failed. HTTP: {code}. Review Sarvam attempts before retrying.")
            frappe.db.commit()
            frappe.log_error(
                title="Bioman Follow-up Test Submit Failed",
                message=f"Opportunity: {details['opportunity_id']}\nHTTP: {code}\nError type: {type(error).__name__}",
            )
            frappe.db.commit()
            return {"status": "needs_review", "http_status": code}

        try:
            body = response.json()
        except ValueError:
            body = None
        attempt_id = body.get("attempt_id") if isinstance(body, dict) else None
        if not attempt_id:
            _followup_test_comment(f"{marker} Sarvam response lacked attempt_id. Review Sarvam attempts; do not redial automatically.")
            frappe.db.commit()
            return {"status": "needs_review", "reason": "missing_attempt_id"}

        _record_attempt(details["opportunity_id"], attempt_id, TEST_FOLLOWUP_MOBILE, requested_at)
        _followup_test_comment(f"{marker} Submitted. Sarvam Attempt: {attempt_id}.")
        frappe.db.commit()
        return {
            "status": "submitted",
            "opportunity": details["opportunity_id"],
            "purpose": "FOLLOWUP",
            "test_destination_last_four": TEST_FOLLOWUP_MOBILE[-4:],
            "attempt_id": attempt_id,
        }
    finally:
        lock.release()


def _find_test_attempt(attempt_id):
    rows = frappe.get_all(
        "Comment",
        filters={
            "reference_doctype": "Opportunity",
            "reference_name": TEST_FOLLOWUP_OPPORTUNITY,
            "comment_type": "Info",
            "content": ["like", f"%{ATTEMPT_MARKER}%"],
        },
        fields=["content"],
        order_by="creation desc",
        limit_page_length=100,
    )
    for row in rows:
        match = ATTEMPT_RE.search(html.unescape(strip_html(row.content or "")))
        if match and match.group(1) == attempt_id and match.group(2) == TEST_FOLLOWUP_MOBILE:
            requested_at = datetime.strptime(match.group(3), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
            return requested_at
    return None


def _get_sarvam_attempt(attempt_id, requested_at):
    start = requested_at - timedelta(minutes=2)
    end = max(datetime.now(UTC), requested_at + timedelta(minutes=1))
    offset = 0
    while offset < 1000:
        response = requests.get(
            SARVAM_ATTEMPTS_URL,
            headers={"X-API-Key": _api_key()},
            params={
                "start_datetime": start.astimezone(IST).isoformat(timespec="seconds"),
                "end_datetime": end.astimezone(IST).isoformat(timespec="seconds"),
                "limit": 100,
                "offset": offset,
            },
            timeout=30,
        )
        response.raise_for_status()
        batch = response.json().get("items") or []
        for row in batch:
            if row.get("attempt_id") == attempt_id:
                return row
        if len(batch) < 100:
            return None
        offset += 100
    raise RuntimeError("More than 1,000 Sarvam attempts in the test window; inspect manually.")


@frappe.whitelist()
def poll_followup_test_call(attempt_id):
    """Check an exact Sarvam attempt and save a test-labelled outcome note."""
    _check_test_admin()
    attempt_id = str(attempt_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", attempt_id):
        frappe.throw("Enter the attempt_id returned by run_followup_test_call.")
    requested_at = _find_test_attempt(attempt_id)
    if not requested_at:
        frappe.throw("No matching test attempt Comment was found on the Opportunity.")
    if _note_exists("Sarvam Attempt: " + attempt_id):
        return {"status": "already_recorded", "attempt_id": attempt_id}

    item = _get_sarvam_attempt(attempt_id, requested_at)
    if not item:
        return {"status": "waiting_for_attempt", "attempt_id": attempt_id}
    if _safe_normalize(item.get("user_contact")) != TEST_FOLLOWUP_MOBILE:
        frappe.throw("Sarvam attempt destination does not match the internal test number.")
    outcome = str(item.get("connectivity_status") or "").lower()
    if outcome == "connected":
        interaction_id = item.get("interaction_id")
        if not interaction_id:
            return {"status": "waiting_for_interaction", "attempt_id": attempt_id}
        for interaction in _fetch_interactions(
            requested_at - timedelta(minutes=2), datetime.now(UTC)
        ):
            if interaction.get("interaction_id") != interaction_id:
                continue
            if _safe_normalize(interaction.get("user_contact")) != TEST_FOLLOWUP_MOBILE:
                frappe.throw("Interaction destination does not match the internal test number.")
            variables = interaction.get("agent_variables") or {}
            if not isinstance(variables, dict) or variables.get("opportunity_id") != TEST_FOLLOWUP_OPPORTUNITY:
                frappe.throw("Interaction Opportunity does not match the selected test.")
            result = _import_interaction(TEST_FOLLOWUP_OPPORTUNITY, interaction, attempt_id)
            frappe.db.commit()
            result["attempt_id"] = attempt_id
            return result
        return {"status": "waiting_for_interaction", "attempt_id": attempt_id}

    if outcome not in ("no_answer", "busy", "failed"):
        return {"status": "waiting_for_outcome", "attempt_id": attempt_id,
                "provider_status": outcome or "not_available"}
    lock = frappe.cache().lock("bioman_sarvam_note:" + attempt_id, timeout=60)
    if not lock.acquire(blocking=False):
        return {"status": "in_progress", "attempt_id": attempt_id}
    try:
        if _note_exists("Sarvam Attempt: " + attempt_id):
            return {"status": "already_recorded", "attempt_id": attempt_id}
        readable = {"no_answer": "No Answer", "busy": "Busy", "failed": "Failed"}[outcome]
        attempted_ist = requested_at.astimezone(IST).strftime("%d-%m-%Y %I:%M %p IST")
        failure = str(item.get("failure_reason") or "")[:500]
        failure_html = f"<p><b>Provider reason:</b> {html.escape(failure)}</p>" if failure and outcome == "failed" else ""
        note_html = (
            "<p><b>[Bioman Follow-up Test] Internal number only. "
            "The customer was not dialed.</b></p>"
            f"<h4>Bioman AI Call Attempt: {html.escape(readable)}</h4>"
            f"<p><b>Test Time:</b> {html.escape(attempted_ist)}</p>"
            f"{failure_html}"
            f"<p>Sarvam Attempt: {html.escape(attempt_id)}</p>"
        )
        opportunity = frappe.get_doc("Opportunity", TEST_FOLLOWUP_OPPORTUNITY)
        opportunity.append("notes", {
            "note": note_html,
            "added_by": "Administrator",
            "added_on": now_datetime(),
        })
        opportunity.save(ignore_permissions=True)
        frappe.db.commit()
        return {"status": "created", "outcome": outcome, "attempt_id": attempt_id}
    finally:
        lock.release()


def _pilot_today():
    return datetime.now(IST).date().isoformat()


def _check_pilot_admin():
    if frappe.session.user != "Administrator" and "System Manager" not in frappe.get_roles():
        frappe.throw("System Manager is required.", frappe.PermissionError)


def _pilot_claims(today):
    return frappe.get_all(
        "Comment",
        filters={
            "reference_doctype": "Opportunity",
            "comment_type": "Info",
            "content": ["like", f"%{FOLLOWUP_CLAIM_MARKER} Date: {today}%"],
        },
        fields=["reference_name", "content"],
        limit_page_length=2000,
    )


def _pilot_suppressed(opportunity_id):
    return bool(frappe.db.exists("Comment", {
        "reference_doctype": "Opportunity",
        "reference_name": opportunity_id,
        "content": ["like", f"%{FOLLOWUP_SUPPRESSION_MARKER}%"],
    }))


def _pilot_recent_phone_attempt(mobile):
    since = now_datetime() - timedelta(minutes=60)
    rows = frappe.get_all(
        "Comment",
        filters={
            "reference_doctype": "Opportunity",
            "comment_type": "Info",
            "creation": [">=", since],
            "content": ["like", f"%{ATTEMPT_MARKER}%"],
        },
        fields=["content"],
        limit_page_length=1000,
    )
    for row in rows:
        match = ATTEMPT_RE.search(html.unescape(strip_html(row.content or "")))
        if match and match.group(2) == mobile:
            return True
    return False


def _pilot_candidates(today):
    filters = {
        "status": "Follow-up",
        "custom_next_followup_date": today,
        "custom_product_enquired": "Bioman",
    }
    total = frappe.db.count("Opportunity", filters=filters)
    rows = frappe.get_all(
        "Opportunity",
        filters=filters,
        fields=["name", "contact_mobile", "phone"],
        order_by="name asc",
        limit_page_length=5000,
    )
    if total > len(rows):
        raise RuntimeError("More than 5,000 due-today Opportunities; review paging.")
    return total, rows


def _pilot_due_rows():
    """Read the durable queue from standard Opportunity timeline Comments."""
    rows = frappe.get_all(
        "Comment",
        filters={
            "reference_doctype": "Opportunity",
            "comment_type": "Info",
            "content": ["like", f"%{FOLLOWUP_DUE_MARKER}%"],
        },
        fields=["reference_name", "content", "creation"],
        order_by="creation asc",
        limit_page_length=5001,
    )
    if len(rows) > 5000:
        raise RuntimeError("Follow-up Comment queue exceeds 5,000 entries; review paging.")
    queue = []
    seen = set()
    for row in rows:
        found = re.search(r"\[Bioman-Followup-Due\] Due:\s*(\d{4}-\d{2}-\d{2})", row.content or "")
        if not found:
            continue
        pair = (row.reference_name, found.group(1))
        if pair not in seen:
            queue.append({"opportunity": pair[0], "due_date": pair[1]})
            seen.add(pair)
    return queue


def _pilot_claimed_due_pairs():
    rows = frappe.get_all(
        "Comment",
        filters={
            "reference_doctype": "Opportunity",
            "comment_type": "Info",
            "content": ["like", f"%{FOLLOWUP_CLAIM_MARKER}%"],
        },
        fields=["reference_name", "content"],
        limit_page_length=5001,
    )
    if len(rows) > 5000:
        raise RuntimeError("Follow-up claims exceed 5,000 entries; review paging.")
    pairs = set()
    for row in rows:
        content = row.content or ""
        due = re.search(r"\bDue:\s*(\d{4}-\d{2}-\d{2})", content)
        day = re.search(r"\bDate:\s*(\d{4}-\d{2}-\d{2})", content)
        if due or day:
            # Legacy pilot claims had only Date, and were dialed on their due date.
            pairs.add((row.reference_name, (due or day).group(1)))
    return pairs


def _pilot_queued_eligible(today):
    claimed = _pilot_claimed_due_pairs()
    queue = [row for row in _pilot_due_rows()
             if row["due_date"] <= today
             and (row["opportunity"], row["due_date"]) not in claimed]
    if not queue:
        return []
    names = list(dict.fromkeys(row["opportunity"] for row in queue))
    current = {}
    for start in range(0, len(names), 200):
        docs = frappe.get_all(
            "Opportunity",
            filters={"name": ["in", names[start:start + 200]]},
            fields=["name", "status", "custom_product_enquired",
                    "custom_next_followup_date", "contact_mobile", "phone"],
            limit_page_length=200,
        )
        current.update({doc.name: doc for doc in docs})
    eligible = []
    for row in queue:
        doc = current.get(row["opportunity"])
        if (not doc or doc.status != "Follow-up"
                or doc.custom_product_enquired != "Bioman"
                or str(doc.custom_next_followup_date or "") != row["due_date"]
                or _pilot_suppressed(row["opportunity"])):
            continue
        mobile = _safe_normalize(doc.contact_mobile or doc.phone)
        if mobile:
            eligible.append({"opportunity": row["opportunity"],
                             "due_date": row["due_date"], "mobile": mobile})
    return eligible


@frappe.whitelist()
def capture_followups_due_today():
    """Save up to 100 due-today Opportunities as Comments. Never makes calls."""
    _check_pilot_admin()
    today = _pilot_today()
    lock = frappe.cache().lock(f"bioman_followup_capture:{frappe.local.site}:{today}", timeout=300)
    if not lock.acquire(blocking=False):
        return {"status": "already_running"}
    try:
        total, rows = _pilot_candidates(today)
        existing = {(row["opportunity"], row["due_date"]) for row in _pilot_due_rows()}
        claimed = _pilot_claimed_due_pairs()
        captured = 0
        skipped = 0
        for row in rows:
            pair = (row.name, today)
            if pair in existing or pair in claimed:
                continue
            if _pilot_suppressed(row.name) or not _safe_normalize(row.contact_mobile or row.phone):
                skipped += 1
                continue
            frappe.get_doc({
                "doctype": "Comment", "comment_type": "Info",
                "reference_doctype": "Opportunity", "reference_name": row.name,
                "content": f"{FOLLOWUP_DUE_MARKER} Due: {today} | Awaiting automated Follow-up",
            }).insert(ignore_permissions=True)
            captured += 1
            existing.add(pair)
            if captured % 25 == 0:
                frappe.db.commit()
            if captured >= FOLLOWUP_SNAPSHOT_BATCH:
                break
        frappe.db.commit()
        remaining = sum(
            1 for row in rows
            if (row.name, today) not in existing and (row.name, today) not in claimed
            and not _pilot_suppressed(row.name)
            and _safe_normalize(row.contact_mobile or row.phone)
        )
        return {"status": "captured", "date_ist": today,
                "eligible_today": total, "new_queue_comments": captured,
                "remaining_to_capture": remaining, "skipped_no_phone_or_suppressed": skipped}
    finally:
        lock.release()


def capture_all_followups_due_today():
    """Background worker: finish today's snapshot in small committed batches."""
    _check_pilot_admin()
    captured = 0
    for _ in range(50):
        result = capture_followups_due_today()
        if result.get("status") != "captured":
            return {"status": result.get("status"), "captured": captured}
        captured += result["new_queue_comments"]
        if result["remaining_to_capture"] == 0:
            return {"status": "complete", "captured": captured,
                    "date_ist": result["date_ist"]}
        if result["new_queue_comments"] == 0:
            raise RuntimeError("Snapshot has remaining Opportunities but made no progress.")
    raise RuntimeError("Snapshot exceeded 50 batches; review the eligible count.")


@frappe.whitelist()
def enqueue_followup_snapshot():
    """Schedule due-today Comment capture. Never dials a customer."""
    _check_pilot_admin()
    today = _pilot_today()
    frappe.enqueue(
        "itgenie.sarvam_outbound.capture_all_followups_due_today",
        queue="long",
        timeout=1200,
        enqueue_after_commit=True,
        job_id="bioman_followup_snapshot_" + today,
        deduplicate=True,
    )
    return {"status": "queued", "date_ist": today,
            "message": "Only Opportunity Comments will be created; no call will be placed."}


def _pilot_pending(today):
    rows = frappe.get_all(
        "Comment",
        filters={
            "reference_doctype": "Opportunity",
            "comment_type": "Info",
            "content": ["like", f"%{FOLLOWUP_ATTEMPT_MARKER} Date: {today}%"],
        },
        fields=["reference_name", "content"],
        order_by="creation asc",
        limit_page_length=1000,
    )
    pending = []
    for row in rows:
        match = ATTEMPT_RE.search(html.unescape(strip_html(row.content or "")))
        if not match:
            continue
        attempt_id, mobile, requested_text = match.groups()
        if _note_exists("Sarvam Attempt: " + attempt_id):
            continue
        requested_at = datetime.strptime(requested_text, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
        pending.append({
            "opportunity": row.reference_name,
            "attempt_id": attempt_id,
            "mobile": mobile,
            "requested_at": requested_at,
        })
    return pending


def _pilot_outstanding(today):
    rows = frappe.get_all(
        "Comment",
        filters={
            "reference_doctype": "Opportunity",
            "comment_type": "Info",
            "creation": [">=", now_datetime() - timedelta(days=30)],
            "content": ["like", f"%{FOLLOWUP_ATTEMPT_MARKER}%"],
        },
        fields=["reference_name", "content"],
        order_by="creation asc",
        limit_page_length=5001,
    )
    if len(rows) > 5000:
        raise RuntimeError("More than 5,000 recent Follow-up attempts; review paging.")
    pending = []
    for row in rows:
        found = ATTEMPT_RE.search(html.unescape(strip_html(row.content or "")))
        if not found:
            continue
        attempt_id, mobile, requested_text = found.groups()
        if _note_exists("Sarvam Attempt: " + attempt_id):
            continue
        pending.append({
            "opportunity": row.reference_name,
            "attempt_id": attempt_id,
            "mobile": mobile,
            "requested_at": datetime.strptime(requested_text, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC),
        })
    return pending


@frappe.whitelist()
def preview_followup_pilot():
    """Read-only daily count, eligibility, and first five candidate IDs."""
    _check_pilot_admin()
    today = _pilot_today()
    total, rows = _pilot_candidates(today)
    claimed = {row.reference_name for row in _pilot_claims(today)}
    queued = _pilot_queued_eligible(today)
    sample = [
        {"opportunity": row["opportunity"], "due_date": row["due_date"],
         "phone_last_four": row["mobile"][-4:]}
        for row in queued[:5]
    ]
    due_pairs = {(r["opportunity"], r["due_date"]) for r in _pilot_due_rows()}
    claimed_pairs = _pilot_claimed_due_pairs()
    uncaptured = sum(
        1 for row in rows
        if (row.name, today) not in due_pairs and (row.name, today) not in claimed_pairs
        and not _pilot_suppressed(row.name)
        and _safe_normalize(row.contact_mobile or row.phone)
    )
    return {
        "date_ist": today,
        "eligible_count": total,
        "queued_eligible_count": len(queued),
        "due_today_uncaptured": uncaptured,
        "claimed_today": len(claimed),
        "pending_outcome": len(_pilot_outstanding(today)),
        "customer_dialing_enabled": ENABLE_LIVE_FOLLOWUP_CALLS,
        "max_calls_per_day": FOLLOWUP_PILOT_DAILY_CAP,
        "max_calls_per_run": FOLLOWUP_PILOT_PER_RUN,
        "sample": sample,
    }


def _pilot_save_outcome(entry, outcome, item):
    attempt_id = entry["attempt_id"]
    if _note_exists("Sarvam Attempt: " + attempt_id):
        return {"status": "already_recorded", "attempt_id": attempt_id}
    label = {"no_answer": "No Answer", "busy": "Busy", "failed": "Failed"}[outcome]
    time_text = entry["requested_at"].astimezone(IST).strftime("%d-%m-%Y %I:%M %p IST")
    reason = str(item.get("failure_reason") or "")[:500]
    reason_html = f"<p><b>Provider reason:</b> {html.escape(reason)}</p>" if outcome == "failed" and reason else ""
    note = (
        f"<h4>Bioman AI Follow-up Call: {label}</h4>"
        f"<p><b>Call Time:</b> {html.escape(time_text)}<br>"
        f"<b>Outcome:</b> {label}. No customer conversation took place.</p>"
        f"{reason_html}<p><b>Next Recommended Action:</b> Salesperson to review this attempt.</p>"
        f"<p>Sarvam Attempt: {html.escape(attempt_id)}</p>"
    )
    opportunity = frappe.get_doc("Opportunity", entry["opportunity"])
    opportunity.append("notes", {
        "note": note,
        "added_by": "Administrator",
        "added_on": now_datetime(),
    })
    opportunity.save(ignore_permissions=True)
    frappe.get_doc({
        "doctype": "Comment",
        "comment_type": "Info",
        "reference_doctype": "Opportunity",
        "reference_name": entry["opportunity"],
        "content": f"{FOLLOWUP_OUTCOME_MARKER} Sarvam Attempt: {attempt_id} | Outcome: {label}",
    }).insert(ignore_permissions=True)
    frappe.db.commit()
    return {"status": "created", "attempt_id": attempt_id, "outcome": outcome}


def _pilot_save_no_customer_speech(entry, interaction):
    """Record a technically connected call with no captured customer speech."""
    attempt_id = entry["attempt_id"]
    interaction_id = interaction.get("interaction_id")
    if _note_exists("Sarvam Attempt: " + attempt_id):
        return {"status": "already_recorded", "attempt_id": attempt_id}
    lock = frappe.cache().lock("bioman_sarvam_note:" + attempt_id, timeout=120)
    if not lock.acquire(blocking=False):
        return {"status": "in_progress", "attempt_id": attempt_id}
    try:
        if _note_exists("Sarvam Attempt: " + attempt_id):
            return {"status": "already_recorded", "attempt_id": attempt_id}
        started = _parse_utc(interaction.get("start_datetime")) or entry["requested_at"]
        time_text = started.astimezone(IST).strftime("%d-%m-%Y %I:%M %p IST")
        note = (
            "<h4>Bioman AI Follow-up Call: No Customer Speech Captured</h4>"
            f"<p><b>Call Time:</b> {html.escape(time_text)}<br>"
            "<b>Outcome:</b> Sarvam reported a connected call, but its transcript "
            "contains only the assistant's opening turn. No customer response was "
            "captured, so no conversation summary is available.</p>"
            "<p><b>Next Recommended Action:</b> Salesperson to review the call "
            "recording or follow up separately.</p>"
            f"<p>Sarvam Interaction: {html.escape(str(interaction_id or ''))}<br>"
            f"Sarvam Attempt: {html.escape(attempt_id)}</p>"
        )
        opportunity = frappe.get_doc("Opportunity", entry["opportunity"])
        opportunity.append("notes", {
            "note": note,
            "added_by": "Administrator",
            "added_on": now_datetime(),
        })
        opportunity.save(ignore_permissions=True)
        frappe.get_doc({
            "doctype": "Comment", "comment_type": "Info",
            "reference_doctype": "Opportunity", "reference_name": entry["opportunity"],
            "content": (
                f"{FOLLOWUP_OUTCOME_MARKER} Sarvam Attempt: {attempt_id} | "
                "Outcome: Connected, no customer speech captured"
            ),
        }).insert(ignore_permissions=True)
        frappe.db.commit()
        return {"status": "created", "outcome": "no_customer_speech",
                "attempt_id": attempt_id, "interaction_id": interaction_id}
    finally:
        lock.release()


def _fetch_sarvam_transcript(interaction_id):
    base_url = SARVAM_INTERACTIONS_URL.rsplit("/interactions", 1)[0]
    response = requests.get(
        base_url + "/transcripts/" + quote(str(interaction_id), safe=""),
        headers={"X-API-Key": _api_key()},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or data.get("interaction_id") != interaction_id:
        raise ValueError("Transcript response does not match the interaction.")
    return data.get("messages") or []


def _pilot_reconcile(entry):
    item = _get_sarvam_attempt(entry["attempt_id"], entry["requested_at"])
    if not item:
        return {"status": "waiting_for_attempt", "attempt_id": entry["attempt_id"]}
    if _safe_normalize(item.get("user_contact")) != entry["mobile"]:
        raise ValueError("Provider phone mismatches stored Follow-up attempt.")
    outcome = str(item.get("connectivity_status") or "").lower()
    if outcome == "connected":
        interaction_id = item.get("interaction_id")
        if not interaction_id:
            return {"status": "waiting_for_interaction", "attempt_id": entry["attempt_id"]}
        interactions = _fetch_interactions(
            entry["requested_at"] - timedelta(minutes=2), datetime.now(UTC)
        )
        for interaction in interactions:
            if interaction.get("interaction_id") != interaction_id:
                continue
            variables = interaction.get("agent_variables") or {}
            if (not isinstance(variables, dict)
                    or variables.get("opportunity_id") != entry["opportunity"]
                    or _safe_normalize(interaction.get("user_contact")) != entry["mobile"]):
                raise ValueError("Provider interaction mismatches Follow-up claim.")
            if not variables.get("call_summary"):
                ended = _parse_utc(interaction.get("end_datetime"))
                if ended and datetime.now(UTC) - ended >= timedelta(minutes=5):
                    messages = _fetch_sarvam_transcript(interaction_id)
                    has_agent_turn = any(
                        str(message.get("role") or "").lower() in ("agent", "assistant")
                        and str(message.get("content") or "").strip()
                        for message in messages if isinstance(message, dict)
                    )
                    has_customer_speech = any(
                        str(message.get("role") or "").lower() == "user"
                        and str(message.get("content") or "").strip()
                        for message in messages if isinstance(message, dict)
                    )
                    if has_agent_turn and not has_customer_speech:
                        return _pilot_save_no_customer_speech(entry, interaction)
            result = _import_interaction(entry["opportunity"], interaction, entry["attempt_id"])
            frappe.db.commit()
            result["attempt_id"] = entry["attempt_id"]
            return result
        return {"status": "waiting_for_interaction", "attempt_id": entry["attempt_id"]}
    if outcome not in ("no_answer", "busy", "failed"):
        return {"status": "waiting_for_outcome", "attempt_id": entry["attempt_id"]}
    lock = frappe.cache().lock("bioman_sarvam_note:" + entry["attempt_id"], timeout=120)
    if not lock.acquire(blocking=False):
        return {"status": "in_progress", "attempt_id": entry["attempt_id"]}
    try:
        return _pilot_save_outcome(entry, outcome, item)
    finally:
        lock.release()


def _pilot_submit(opportunity_id, today, mobile, due_date):
    # This helper is only called after the scheduler checks the enable switch.
    doc = frappe.get_doc("Opportunity", opportunity_id)
    if (doc.status != "Follow-up" or doc.get("custom_product_enquired") != "Bioman"
            or str(doc.get("custom_next_followup_date") or "") != due_date):
        return {"status": "skipped_changed", "opportunity": opportunity_id}
    if _pilot_suppressed(opportunity_id) or _pilot_recent_phone_attempt(mobile):
        return {"status": "skipped_suppressed_or_recent", "opportunity": opportunity_id}
    if _safe_normalize(doc.get("contact_mobile") or doc.get("phone")) != mobile:
        return {"status": "skipped_phone_changed", "opportunity": opportunity_id}
    details = get_opportunity_details(opportunity_id)
    claim_content = (
        f"{FOLLOWUP_CLAIM_MARKER} Date: {today} | Due: {due_date} | "
        f"Phone: {mobile} | Submitting"
    )
    frappe.get_doc({
        "doctype": "Comment", "comment_type": "Info",
        "reference_doctype": "Opportunity", "reference_name": opportunity_id,
        "content": claim_content,
    }).insert(ignore_permissions=True)
    frappe.db.commit()  # Durable claim before the network request.
    payload = {
        "app_config": {
            "app_id": SARVAM_AGENT_ID, "app_version": SARVAM_AGENT_VERSION,
            "app_type": "agent",
            "connection_config": {
                "connection_id": SMARTFLO_CONNECTION_ID,
                "agent_phone_number": SMARTFLO_PHONE_NUMBER,
            },
            "agent_variables": {
                "user_name": details["customer_name"],
                "executive_phone": details["executive_phone"],
                "opportunity_id": opportunity_id,
                "call_purpose": "FOLLOWUP",
                "customer_requirements": details["customer_requirements"],
                "previous_conversation_notes": details["previous_conversation_notes"],
            },
        },
        "user_config": {"user_phone_number": mobile},
    }
    requested_at = datetime.now(UTC)
    try:
        response = requests.post(
            SARVAM_API_URL,
            headers={"Content-Type": "application/json", "X-API-Key": _api_key()},
            json=payload,
            timeout=(10, 30),
        )
        response.raise_for_status()
        body = response.json()
        attempt_id = body.get("attempt_id") if isinstance(body, dict) else None
        if not attempt_id:
            raise ValueError("Outbound response lacked an attempt_id.")
    except Exception as error:
        code = error.response.status_code if isinstance(error, requests.RequestException) and error.response is not None else "unknown"
        frappe.log_error(
            title="Bioman Follow-up Pilot Submit Error",
            message=f"Opportunity: {opportunity_id}\nHTTP: {code}\nError type: {type(error).__name__}",
        )
        frappe.db.commit()
        return {"status": "needs_review", "opportunity": opportunity_id, "http_status": code}
    _record_attempt(opportunity_id, attempt_id, mobile, requested_at)
    frappe.get_doc({
        "doctype": "Comment", "comment_type": "Info",
        "reference_doctype": "Opportunity", "reference_name": opportunity_id,
        "content": (
            f"{FOLLOWUP_ATTEMPT_MARKER} Date: {today} | "
            f"Attempt: {attempt_id} | Mobile: {mobile} | "
            f"Requested UTC: {requested_at.strftime('%Y-%m-%dT%H:%M:%S')}"
        ),
    }).insert(ignore_permissions=True)
    frappe.db.commit()
    return {"status": "submitted", "opportunity": opportunity_id, "attempt_id": attempt_id}


@frappe.whitelist()
def run_followup_pilot_job():
    """Run from a five-minute Scheduler Event; claims at most one customer."""
    _check_pilot_admin()
    lock = frappe.cache().lock(f"bioman_followup_pilot:{frappe.local.site}", timeout=300)
    if not lock.acquire(blocking=False):
        return {"status": "already_running"}
    try:
        today = _pilot_today()
        pending = _pilot_outstanding(today)
        results = []
        for entry in pending[:FOLLOWUP_PILOT_DAILY_CAP * 2]:
            try:
                results.append(_pilot_reconcile(entry))
            except Exception as error:
                frappe.log_error(
                    title="Bioman Follow-up Pilot Reconciliation Error",
                    message=f"Attempt: {entry['attempt_id']}\nError type: {type(error).__name__}",
                )
                frappe.db.commit()
                results.append({"status": "needs_review", "attempt_id": entry["attempt_id"]})
        pending = _pilot_outstanding(today)
        if not ENABLE_LIVE_FOLLOWUP_CALLS:
            return {"status": "customer_dialing_disabled", "results": results}
        now_ist = datetime.now(IST)
        clock = now_ist.hour * 60 + now_ist.minute
        first = FOLLOWUP_START_HOUR_IST * 60 + FOLLOWUP_START_MINUTE_IST
        last = FOLLOWUP_END_HOUR_IST * 60 + FOLLOWUP_END_MINUTE_IST
        if not first <= clock < last:
            return {"status": "outside_calling_window", "results": results}
        claims = _pilot_claims(today)
        if len(claims) >= FOLLOWUP_PILOT_DAILY_CAP or pending:
            return {"status": "daily_cap_or_active_call", "claims": len(claims),
                    "pending": len(pending), "results": results}
        snapshot = capture_followups_due_today()
        total, _ = _pilot_candidates(today)
        candidates = _pilot_queued_eligible(today)
        claimed_opportunities = {r.reference_name for r in claims}
        claimed_phones = set()
        for row in claims:
            found = re.search(r"Phone:\s*(\+\d+)", row.content or "")
            if found:
                claimed_phones.add(found.group(1))
        sent = []
        for candidate in candidates:
            if len(sent) >= FOLLOWUP_PILOT_PER_RUN:
                break
            if (candidate["opportunity"] in claimed_opportunities
                    or _pilot_suppressed(candidate["opportunity"])):
                continue
            mobile = candidate["mobile"]
            if not mobile or mobile in claimed_phones:
                continue
            if _pilot_recent_phone_attempt(mobile):
                continue
            result = _pilot_submit(candidate["opportunity"], today, mobile,
                                   candidate["due_date"])
            if result["status"] == "submitted":
                sent.append(result)
                claimed_phones.add(mobile)
            if result["status"] == "needs_review":
                break  # Never surge after an ambiguous submission.
        return {"status": "pilot_checked", "eligible": total,
                "snapshot": snapshot, "submitted": sent, "results": results}
    finally:
        lock.release()


@frappe.whitelist()
def run_test_call(test_mobile):
    """Place ONE call to an explicitly supplied test number and commit its record.
    Do not invoke this inside document save hooks: commit is intentional here.
    """
    _check_test_admin()
    if not str(test_mobile or "").strip():
        frappe.throw("Enter your own test mobile number.")
    result = initiate_outbound_call(TEST_OPPORTUNITY, test_mobile=test_mobile)
    frappe.db.commit()
    return result


@frappe.whitelist()
def recover_test_call_summary(interaction_id):
    """Import an existing call into the test Opportunity; never dials."""
    _check_test_admin()
    interaction_id = str(interaction_id or "").strip()
    if not interaction_id:
        frappe.throw("Enter the Sarvam Interaction ID.")
    result = backfill_interaction(interaction_id, TEST_OPPORTUNITY, hours=24)
    frappe.db.commit()
    return result


@frappe.whitelist()
def inspect_test_call_summary(interaction_id):
    """Read-only diagnostic for a known interaction in the last 24 hours."""
    _check_test_admin()
    now_utc = datetime.now(UTC)
    for item in _fetch_interactions(now_utc - timedelta(hours=24), now_utc):
        if item.get("interaction_id") != interaction_id:
            continue
        variables = item.get("agent_variables")
        summary = variables.get("call_summary") if isinstance(variables, dict) else None
        return {
            "interaction_id": interaction_id,
            "app_version": item.get("app_version"),
            "response_fields": sorted(item.keys()),
            "variables_type": type(variables).__name__,
            "variable_names": sorted(variables.keys()) if isinstance(variables, dict) else [],
            "summary_type": type(summary).__name__,
            "summary_value": summary,
            "ended_by": item.get("ended_by"),
            "end_datetime": item.get("end_datetime"),
        }
    return {"status": "not_found"}
