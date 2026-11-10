
import json
import requests
import frappe

from frappe.utils import get_datetime, add_to_date


SMARTFLO_API_URL = (
    "https://api-smartflo.tatateleservices.com/v1/call/records"
)


# ---------------------------------------------------------
# GET SMARTFLO API TOKEN
# ---------------------------------------------------------

def get_smartflo_token():

    settings_doctype = "Smartflo Settings"
    meta = frappe.get_meta(settings_doctype)

    if meta.issingle:
        settings = frappe.get_doc(settings_doctype)

    else:
        rows = frappe.get_all(
            settings_doctype,
            fields=["name"],
            limit_page_length=2
        )

        if len(rows) != 1:
            frappe.throw(
                "Specify the correct Smartflo Settings document."
            )

        settings = frappe.get_doc(
            settings_doctype,
            rows[0].name
        )

    token_field = meta.get_field("api_token")

    if not token_field:
        frappe.throw("api_token field not found.")

    if token_field.fieldtype == "Password":
        token = settings.get_password("api_token")
    else:
        token = settings.get("api_token")

    if not token:
        frappe.throw("Smartflo API token is empty.")

    token = token.strip()

    if not token.lower().startswith("bearer "):
        token = "Bearer " + token

    return token


# ---------------------------------------------------------
# FETCH RECORDING URL FOR ONE CALL LOG
# ---------------------------------------------------------

def fetch_recording_url(call_log_name, token):

    call_log = frappe.get_doc(
        "Call Log",
        call_log_name
    )

    # Verify that user can update this Call Log
    call_log.check_permission("write")

    # Skip records with existing recording URLs
    if call_log.get("custom_url"):
        return "already_exists"

    # ERPNext Call Log ID = Smartflo Call ID
    smartflo_call_id = call_log.name

    # Determine the call date
    call_datetime = (
        call_log.get("start_time")
        or call_log.get("custom_initiate_time")
        or call_log.creation
    )

    call_datetime = get_datetime(call_datetime)

    # Search the call date and adjacent dates to handle
    # midnight / timezone boundary differences
    from_datetime = add_to_date(
        call_datetime,
        days=-1
    )

    to_datetime = add_to_date(
        call_datetime,
        days=1
    )

    from_date = (
        from_datetime.strftime("%Y-%m-%d")
        + " 00:00:00"
    )

    to_date = (
        to_datetime.strftime("%Y-%m-%d")
        + " 23:59:59"
    )

    # Smartflo API parameters
    params = {
        "from_date": from_date,
        "to_date": to_date,
        "call_id": smartflo_call_id,
        "page": 1,
        "limit": 100
    }

    headers = {
        "Authorization": token,
        "Accept": "application/json"
    }

    # Request Smartflo API
    response = requests.get(
        SMARTFLO_API_URL,
        headers=headers,
        params=params,
        timeout=30
    )

    # Raise error for 4xx / 5xx responses
    response.raise_for_status()

    data = response.json()

    if not isinstance(data, dict):
        return "not_found"

    records = data.get("results") or []

    if not isinstance(records, list):
        return "not_found"

    # Match exact Smartflo call ID
    matches = []

    for record in records:

        if not isinstance(record, dict):
            continue

        if record.get("call_id") == smartflo_call_id:
            matches.append(record)

    # No matching record
    if len(matches) == 0:
        return "not_found"

    # Multiple matching records
    if len(matches) > 1:
        return "multiple_matches"

    selected = matches[0]

    recording_url = selected.get("recording_url")

    # Recording not available yet
    if not recording_url:
        return "not_ready"

    # Save recording URL
    # Recheck the current value before writing
    current_url = frappe.db.get_value(
        "Call Log",
        call_log_name,
        "custom_url"
    )

    if current_url:
        return "already_exists"

    frappe.db.set_value(
        "Call Log",
        call_log_name,
        "custom_url",
        recording_url,
        update_modified=True
    )

    return "updated"


# ---------------------------------------------------------
# BULK FETCH RECORDING URL
# ---------------------------------------------------------

@frappe.whitelist()
def bulk_fetch_recording_urls(call_log_names):

    if isinstance(call_log_names, str):
        try:
            call_log_names = json.loads(call_log_names)
        except (ValueError, TypeError):
            frappe.throw("Invalid Call Log selection.")

    if not isinstance(call_log_names, list):
        frappe.throw("Invalid Call Log selection.")

    if not call_log_names:
        frappe.throw("Please select Call Logs.")

    if len(call_log_names) > 100:
        frappe.throw(
            "Maximum 100 Call Logs per batch."
        )

    # Validate names
    for name in call_log_names:
        if not isinstance(name, str) or not name.strip():
            frappe.throw("Invalid Call Log name.")

    # Remove duplicate Call Log names
    call_log_names = list(dict.fromkeys(call_log_names))

    # Get Smartflo token once
    token = get_smartflo_token()

    results = {
        "total": len(call_log_names),
        "updated": 0,
        "already_exists": 0,
        "not_found": 0,
        "not_ready": 0,
        "multiple_matches": 0,
        "failed": 0,
        "errors": []
    }

    # Process each Call Log
    for call_log_name in call_log_names:

        try:

            # Savepoint allows failed records to roll back
            # without affecting earlier successful records
            frappe.db.savepoint("smartflo_record")

            status = fetch_recording_url(
                call_log_name,
                token
            )

            if status in results:
                results[status] += 1
            else:
                results["failed"] += 1
                results["errors"].append({
                    "call_log": call_log_name,
                    "error": "Unknown result: " + str(status)
                })

        except Exception as e:

            frappe.db.rollback(
                save_point="smartflo_record"
            )

            results["failed"] += 1

            results["errors"].append({
                "call_log": call_log_name,
                "error": str(e)
            })

    # Frappe commits successful changes when
    # the HTTP POST request completes successfully.
    return results
