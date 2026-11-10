import os
import frappe
import firebase_admin

from firebase_admin import credentials, messaging


def get_firebase_app():
    if firebase_admin._apps:
        return firebase_admin.get_app()

    site_path = frappe.get_site_path()
    bench_path = os.path.abspath(os.path.join(site_path, "..", ".."))

    json_path = os.path.join(
        bench_path,
        "private",
        "firebase",
        "firebase-service-account.json"
    )

    cred = credentials.Certificate(json_path)

    return firebase_admin.initialize_app(cred)

@frappe.whitelist()
def send_fcm_to_user(user, payload):
    token = frappe.db.get_value(
        "FCM Device",
        {
            "user": user,
            "is_active": 1
        },
        "fcm_token"
    )

    if not token:
        frappe.log_error(
            title="FCM Token Missing",
            message="No active FCM token found for user: " + str(user)
        )
        return {"status": "failed", "message": "No token"}

    try:
        get_firebase_app()

        message = messaging.Message(
            token=token,
            data={k: str(v) for k, v in payload.items()},
            android=messaging.AndroidConfig(
                priority="high"
            )
        )

        response = messaging.send(message)

        frappe.log_error(
            title="FCM Push Response",
            message=str(response)
        )

        return {
            "status": "sent",
            "response": response
        }

    except Exception:
        frappe.log_error(
            title="FCM Push Failed",
            message=frappe.get_traceback()
        )
        return {"status": "failed"}
