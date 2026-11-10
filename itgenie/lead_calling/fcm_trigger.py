import frappe
import json

from itgenie.lead_calling.fcm_utils import send_fcm_to_user


def get_assigned_user(doc):
    assigned = doc.get("_assign")

    if not assigned:
        assigned = frappe.db.get_value(
            doc.doctype,
            doc.name,
            "_assign"
        )

    if not assigned:
        return None

    try:
        users = json.loads(assigned)
        if users:
            return users[0]
    except Exception:
        frappe.log_error(
            title="FCM Assign Parse Error",
            message=frappe.get_traceback()
        )

    return None


def get_mobile_no(doc):
    mobile_no = (
        doc.get("mobile_no")
        or doc.get("phone")
        or doc.get("contact_mobile")
        or doc.get("contact_phone")
        or doc.get("phone_no")
    )

    return mobile_no


def get_customer_name(doc):
    customer_name = (
        doc.get("lead_name")
        or doc.get("party_name")
        or doc.get("customer_name")
        or doc.get("contact_display")
        or doc.name
    )

    return customer_name


def trigger_fcm_call(doc):
    if doc.get("custom_fcm_call_triggered"):
        return

    assigned_user = get_assigned_user(doc)

    if not assigned_user:
        return

    mobile_no = get_mobile_no(doc)

    if not mobile_no:
        frappe.log_error(
            title="FCM Call Trigger Skipped",
            message="No mobile number found for " + doc.doctype + " " + doc.name
        )
        return

    payload = {
        "type": "NEW_LEAD_CALL",
        "doctype": doc.doctype,
        "docname": doc.name,
        "customer_name": get_customer_name(doc),
        "mobile_no": mobile_no,
        "auto_call": "1"
    }

    result = send_fcm_to_user(assigned_user, payload)

   # if result and result.get("status") == "sent":
    #    frappe.db.set_value(
     #       doc.doctype,
      #      doc.name,
       #     {
        #        "custom_fcm_call_triggered": 1,
         #       "custom_fcm_triggered_at": frappe.utils.now()
          #  }
       # )


def on_lead_insert(doc, method=None):
    trigger_fcm_call(doc)


def on_lead_update(doc, method=None):
    trigger_fcm_call(doc)


def on_opportunity_insert(doc, method=None):
    trigger_fcm_call(doc)


def on_opportunity_update(doc, method=None):
    trigger_fcm_call(doc)
