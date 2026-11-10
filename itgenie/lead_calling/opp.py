import frappe
from itgenie.lead_calling.fcm_utils import send_fcm_to_user

@frappe.whitelist()
def send_opportunity_call_fcm(docname):

    opp = frappe.get_doc("Opportunity", docname)

    if not opp.opportunity_owner:
        frappe.throw("Opportunity Owner is missing")

    if not opp.contact_mobile:
        frappe.throw("Contact Mobile is missing")

    payload = {
        "type": "LEAD_CALL",
        "doctype": "Opportunity",
        "docname": opp.name,
        "customer_name": opp.customer_name or opp.party_name or "",
        "mobile_no": opp.contact_mobile,
        "auto_call": "1"
    }

    send_fcm_to_user(
        opp.opportunity_owner,
        payload
    )

    return {
        "status": "success",
        "sent_to": opp.opportunity_owner,
        "payload": payload
    }
