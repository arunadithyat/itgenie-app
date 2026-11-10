"""Custom Frappe app Python, NOT a restricted Server Script.

Install as apps/itgenie/itgenie/fcm/fcm_utils.py. Place the private credential
beside this file as automategenie-service-account.json and exclude it from Git.
FCM Notification rules use a trusted, after-commit worker. Generic deliveries
cannot be invoked over HTTP. No DocType-specific payload builders are used.
"""

import json
import hashlib
import inspect
import traceback
from functools import wraps
from importlib import import_module
from contextvars import ContextVar
from pathlib import Path
from threading import Lock

import firebase_admin
from firebase_admin import credentials, messaging
import frappe


PROJECT_ID = "automategenie-mobile"
_APP_LOCK = Lock()
_NOTIFICATION_DISPATCH = ContextVar("genie_fcm_notification_dispatch", default=None)


def _log_fcm_errors(stage):
    """Record otherwise unhandled failures without changing exception behavior."""
    def decorate(function):
        @wraps(function)
        def logged(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except Exception as error:
                _log_notification_failure(None, error, stage=stage)
                raise
        return logged
    return decorate


@_log_fcm_errors("firebase_initialization")
def get_firebase_app():
    """Use a site-specific named app without changing the default Firebase app."""
    app_name = PROJECT_ID + ":" + str(frappe.local.site)
    with _APP_LOCK:
        try:
            app = firebase_admin.get_app(app_name)
        except ValueError:
            credential_path = Path(__file__).with_name(
                "automategenie-service-account.json"
            )
            credential = credentials.Certificate(str(credential_path))
            if credential.project_id != PROJECT_ID:
                raise ValueError("Credential must belong to automategenie-mobile")
            app = firebase_admin.initialize_app(
                credential,
                options={"projectId": PROJECT_ID, "httpTimeout": 10},
                name=app_name,
            )
        if app.project_id != PROJECT_ID:
            raise ValueError("Unexpected Firebase project configuration")
        return app


@_log_fcm_errors("recipient_validation_or_delivery")
def send_fcm_to_user(user, payload):
    """Internal sender for generic Notification payloads, never a public API."""
    if isinstance(payload, str):
        if len(payload) > 8192:
            frappe.throw("Invalid FCM notification payload.")
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            frappe.throw("Invalid FCM notification payload.")
    if not isinstance(payload, dict) or payload.get("type") != "DOCUMENT_NOTIFICATION":
        frappe.throw("Only generic DOCUMENT_NOTIFICATION payloads are supported.")
    return _send_document_notification(user, payload)


class FCMNotificationMixin:
    """Shared override channel behavior. Other channels use super()."""

    def send(self, doc):
        # Older v15 dispatches its built-in channels directly inside send(),
        # without ever calling send_notification_by_channel(). On newer builds
        # let the native send() reach our channel handler, avoiding two jobs.
        if self.get("channel") != "FCM" or callable(
            getattr(super(), "send_notification_by_channel", None)
        ):
            return super().send(doc)
        if not self.get("enabled") or self.get("event") == "Custom":
            return
        self._enqueue_fcm_notification(doc)
        # Keep v15's standard context, optional ERP bell and property-after-alert
        # handling. Its built-in channel switch does not send FCM itself.
        return super().send(doc)

    def validate(self):
        if self.get("channel") == "FCM":
            from frappe.utils.jinja import validate_template

            if self.get("event") == "Custom":
                frappe.throw("Manual/custom FCM notifications are not enabled.")
            if not (self.get("subject") or "").strip():
                frappe.throw("Subject is required for an FCM Notification.")
            if not (self.get("message") or "").strip():
                frappe.throw("Message is required for an FCM Notification.")
            validate_template(self.subject)
        return super().validate()

    def send_notification_by_channel(self, doc, context):
        if self.get("channel") != "FCM":
            return super().send_notification_by_channel(doc, context)
        if not self.get("enabled") or self.get("event") == "Custom":
            return
        self._enqueue_fcm_notification(doc)
        # Preserve the standard optional ERP bell notification.
        if self.get("send_system_notification"):
            try:
                self.create_system_notification(doc, context)
            except Exception as error:
                _log_notification_failure(self.name, error, stage="erp_bell_notification", document=doc)

    def _enqueue_fcm_notification(self, doc):
        try:
            # Frappe already evaluated the Notification event and condition.
            frappe.enqueue(
                "itgenie.fcm.fcm_utils.dispatch_document_notification",
                queue="default", timeout=600, enqueue_after_commit=True,
                notification_name=self.name, doctype=doc.doctype, docname=doc.name,
                document_version=str(doc.get("modified") or doc.get("creation") or ""),
            )
        except Exception as error:
            _log_notification_failure(self.name, error, stage="enqueue_after_commit", document=doc)


def _notification_context(rule, document):
    """Match the installed Notification implementation's template context."""
    notification_module = import_module("frappe.email.doctype.notification.notification")

    context = notification_module.get_context(document)
    comments_helper = getattr(notification_module, "get_comments_for_context", None)
    if callable(comments_helper):
        comments = comments_helper(document)
    else:
        # v15.69.3 reads the document's existing _comments JSON in send().
        raw_comments = document.get("_comments")
        comments = json.loads(raw_comments) if raw_comments else None
    context.update({"doc": document, "alert": rule, "comments": comments})
    if rule.get("is_standard"):
        rule.load_standard_properties(context)
    return context


def dispatch_document_notification(notification_name, doctype, docname, document_version=""):
    """Internal queue target. Intentionally NOT whitelisted/public."""
    stage = "load_safe_exec"
    try:
        from frappe.utils.safe_exec import safe_exec

        stage = "load_notification_rule"
        rule = frappe.get_doc("Notification", notification_name)
        if not rule.get("enabled") or rule.get("channel") != "FCM" or rule.get("event") == "Custom":
            return {"status": "skipped"}
        stage = "validate_document_type"
        if rule.get("document_type") != doctype:
            frappe.throw("Notification DocType mismatch.")
        stage = "load_document"
        document = frappe.get_doc(doctype, docname)
        stage = "find_server_script"
        script_name = frappe.db.get_value("Server Script", {
            "api_method": "mobile_fcm_notification", "script_type": "API", "disabled": 0,
        }, "name")
        if not script_name:
            _log_notification_failure(rule.name, "Generic Server Script missing or disabled",
                stage=stage, document=document)
            return {"status": "failed", "message": "Generic Server Script missing or disabled"}
        stage = "load_server_script"
        script = frappe.get_doc("Server Script", script_name)
    except Exception as error:
        _log_notification_failure(notification_name, error, stage=stage,
            document={"doctype": doctype, "name": docname})
        # These failures previously propagated to the queue worker.
        raise
    dispatch = {"rule": rule, "document": document, "version": document_version}
    marker = _NOTIFICATION_DISPATCH.set(dispatch)
    try:
        stage = "build_template_context"
        context = _notification_context(rule, document)
        # Pass the Python callable directly. Restricted frappe.call/safe_enqueue
        # may invoke HTTP-method checks in a worker where no request exists.
        execution = {"_locals": {"fcm_trigger": {
            "notification_name": rule.name, "doctype": doctype, "docname": docname,
            "context": context, "send_fcm_to_user": send_fcm_to_user,
            "render_template": render_notification_template,
        }}}
        if "script_filename" in inspect.signature(safe_exec).parameters:
            execution["script_filename"] = script.name
        stage = "execute_mobile_fcm_notification"
        scope, unused_locals = safe_exec(script.script, **execution)
        stage = "read_server_script_result"
        result = scope["frappe"].flags.get("fcm_notification_result") or {"status": "processed"}
        if (isinstance(result, dict) and result.get("status") == "processed"
                and all(result.get(key) == 0 for key in ("sent", "failed", "skipped"))):
            _log_notification_failure(rule.name, "Server Script processed without any recipient deliveries",
                stage="recipient_resolution", document=document)
        return result
    except Exception as error:
        _log_notification_failure(rule.name, error, stage=stage, document=document)
        return {"status": "failed", "message": "FCM Notification dispatch failed"}
    finally:
        _NOTIFICATION_DISPATCH.reset(marker)


@_log_fcm_errors("render_notification_template")
def render_notification_template(template, context):
    """Use restricted globals where supported, including v15 backports.

    Earlier v15 Jinja renderers use their standard sandbox and do not expose
    the restrict_globals keyword. Detect the signature instead of retrying a
    failed render (which could hide an actual template error).
    """
    if "restrict_globals" in inspect.signature(frappe.render_template).parameters:
        return frappe.render_template(template, context, restrict_globals=True)
    return frappe.render_template(template, context)


def _log_notification_failure(notification_name, error, *, stage="dispatch", document=None, user=None):
    """Safe diagnostics: frame locations only, never exception text or locals.

    Open ERP Error Log and search for FCM Notification. A logger failure must
    not turn an accepted Firebase delivery into a failure or mask its cause.
    """
    try:
        dispatch = _NOTIFICATION_DISPATCH.get() or {}
        rule = dispatch.get("rule")
        if notification_name is None and rule is not None:
            notification_name = rule.get("name")
        if document is None:
            document = dispatch.get("document")
        details = {"stage": stage, "notification": str(notification_name or ""),
            "error": type(error).__name__ if isinstance(error, BaseException) else str(error)}
        if document is not None:
            details.update({"doctype": document.get("doctype"), "docname": document.get("name")})
        if rule is not None:
            details["event"] = rule.get("event")
        if user:
            details["recipient_hash"] = hashlib.sha256(user.encode()).hexdigest()[:16]
        if isinstance(error, BaseException):
            details["frames"] = [
                {"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
                for frame in traceback.extract_tb(error.__traceback__)[-15:]
            ]
        frappe.log_error(title="FCM Notification: " + stage,
            message=json.dumps(details, ensure_ascii=True, default=str))
    except Exception:
        # Diagnostics must not affect existing send/return/exception behavior.
        pass


def _send_document_notification(user, payload):
    dispatch = _NOTIFICATION_DISPATCH.get()
    if not dispatch:
        frappe.throw("Generic notifications can only be sent by the Notification worker.", frappe.PermissionError)
    rule, document = dispatch["rule"], dispatch["document"]
    if not isinstance(user, str) or not user.strip() or len(user) > 140 or user == "Guest":
        frappe.throw("Invalid FCM recipient.")
    user = user.strip()
    if (payload.get("notification_name") != rule.name or payload.get("doctype") != document.doctype
            or payload.get("docname") != document.name or payload.get("for_user") != user):
        frappe.throw("FCM notification references do not match the trusted trigger.")
    if (not rule.get("enabled") or rule.get("channel") != "FCM"
            or rule.get("document_type") != document.doctype or rule.get("event") == "Custom"):
        return {"status": "skipped"}
    if not frappe.db.get_value("User", user, "enabled") or not document.has_permission("read", user=user):
        _log_notification_failure(rule.name, "Recipient is disabled or has no document access",
            stage="recipient_access", document=document, user=user)
        return {"status": "skipped", "message": "Recipient is disabled or has no document access"}
    if any(not isinstance(payload.get(key), str) or not payload[key].strip() for key in ["title", "body"]):
        frappe.throw("FCM title and body are required.")
    # Retry/duplicate protection per rule, document version and recipient.
    identity = json.dumps([str(frappe.local.site), rule.name, document.doctype, document.name,
        dispatch["version"], rule.get("event"), user], separators=(",", ":"))
    delivery_key = "genie_fcm_notification:" + hashlib.sha256(identity.encode()).hexdigest()
    cache = frappe.cache()
    with cache.lock(delivery_key + ":lock", timeout=30, blocking_timeout=5):
        if cache.get_value(delivery_key):
            return {"status": "skipped", "message": "Already accepted by FCM"}
        devices = frappe.get_all("FCM Device", filters={"user": user, "is_active": 1},
            fields=["fcm_token"], order_by="modified desc, name desc", limit_page_length=1)
        token = devices[0].get("fcm_token") if devices else None
        if not isinstance(token, str) or not token.strip():
            _log_notification_failure(rule.name, "No active device token",
                stage="device_lookup", document=document, user=user)
            return {"status": "failed", "message": "No active device token"}
        alarm_enabled = bool(frappe.db.exists("Has Role", {
            "parent": user, "parenttype": "User", "parentfield": "roles",
            "role": ["in", ["Sales User", "Designer"]],
        }))
        # The trusted Server Script may select a supported sound mode. Omit it
        # for the role-based default; requesting an alarm cannot bypass roles.
        sound_mode = payload.get("sound_mode")
        if sound_mode is None:
            sound_mode = "alarm" if alarm_enabled else "default"
        elif not isinstance(sound_mode, str) or sound_mode not in ("alarm", "default", "silent"):
            frappe.throw("Invalid FCM notification sound mode.")
        if sound_mode == "alarm" and not alarm_enabled:
            sound_mode = "default"
        data = {
            "type": "DOCUMENT_NOTIFICATION",
            "notification_name": rule.name,
            "doctype": document.doctype,
            "docname": document.name,
            "assignment_id": "",
            "for_user": user,
            "sound_mode": sound_mode,
            "alarm_enabled": "1" if sound_mode == "alarm" else "0",
            "title": payload["title"].strip()[:200],
            "body": payload["body"].strip()[:240],
        }
        try:
            response = messaging.send(messaging.Message(token=token.strip(), data=data,
                android=messaging.AndroidConfig(priority="high")), app=get_firebase_app())
        except Exception as error:
            _log_notification_failure(rule.name, error, stage="firebase_send", document=document, user=user)
            return {"status": "failed", "message": "FCM sending failed"}
        # Firebase acceptance is recorded; it is not proof of phone delivery.
        cache.set_value(delivery_key, str(response), expires_in_sec=86400)
        return {"status": "sent", "response": response}


def setup_fcm_notification_channel():
    """Install/repair FCM field conditions from the bench console, not HTTP."""
    frappe.only_for("System Manager")
    from frappe.custom.doctype.property_setter.property_setter import make_property_setter

    meta = frappe.get_meta("Notification")
    channels = [value for value in (meta.get_field("channel").options or "").splitlines() if value]
    if "FCM" not in channels:
        channels.append("FCM")
        make_property_setter("Notification", "channel", "options", "\n".join(channels), "Text")
    for fieldname, property_name in [("subject", "depends_on"), ("subject", "mandatory_depends_on"), ("message", "depends_on")]:
        previous = meta.get_field(fieldname).get(property_name) or ""
        expression = previous
        fcm_prefix = "eval:doc.channel == 'FCM'"
        # Repair only expressions generated by earlier versions of this helper.
        # Property Setter conditions execute as JavaScript in the browser.
        legacy_prefix = fcm_prefix + " or "
        if expression.startswith(legacy_prefix):
            expression = fcm_prefix + " || " + expression[len(legacy_prefix):]
        legacy_field_prefix = fcm_prefix + " || doc.get("
        if expression.startswith(legacy_field_prefix) and expression.endswith(")"):
            expression = fcm_prefix + " || doc[" + expression[len(legacy_field_prefix):-1] + "]"
        if "doc.channel == 'FCM'" in previous:
            if expression == previous:
                continue
        elif previous.startswith("eval:"):
            expression = fcm_prefix + " || (" + previous[5:] + ")"
        elif previous:
            expression = fcm_prefix + " || doc[" + json.dumps(previous) + "]"
        elif property_name == "mandatory_depends_on":
            expression = fcm_prefix
        else:
            continue
        make_property_setter("Notification", fieldname, property_name, expression, "Code")
    frappe.clear_cache(doctype="Notification")


@_log_fcm_errors("validate_controller_registration")
def validate_fcm_notification_installation():
    """Console check after registration; detects another app masking the hook."""
    from frappe.model.base_document import get_controller

    controller = get_controller("Notification")
    if not issubclass(controller, FCMNotificationMixin):
        frappe.throw("The FCM Notification controller is not active. Check hooks and installed-app order.")
    return {"status": "ready", "controller": controller.__name__}


@frappe.whitelist(methods=["POST"])
def test_fcm_document_notification(data=None):
    """Send a test FCM notification to a user."""

    data = frappe.parse_json(data) if isinstance(data, str) else data
    if not isinstance(data, dict):
        frappe.throw("Invalid test payload.")

    user = (data.get("for_user") or "").strip()
    if not frappe.db.get_value("User", user, "enabled"):
        frappe.throw("A valid, enabled recipient is required in for_user.")

    title = (data.get("title") or "").strip()
    body = (data.get("body") or "").strip()
    if not title or not body:
        frappe.throw("A title and body are required.")

    sound_mode = data.get("sound_mode") or "default"
    if sound_mode == "alarm" and not frappe.db.exists(
        "Has Role", {"parent": user, "role": ["in", ["Sales User", "Designer"]]}
    ):
        sound_mode = "default"

    token = frappe.db.get_value(
        "FCM Device", {"user": user, "is_active": 1}, "fcm_token", order_by="modified desc"
    )
    if not token:
        return {"status": "failed", "message": "No active FCM device token."}

    try:
        response = messaging.send(
            messaging.Message(
                token=token.strip(),
                data={
                    "type": "DOCUMENT_NOTIFICATION",
                    "notification_name": str(data.get("notification_name") or "")[:140],
                    "doctype": str(data.get("doctype") or ""),
                    "docname": str(data.get("doctype") or ""),
                    "assignment_id": str(data.get("assignment_id") or ""),
                    "for_user": user,
                    "sound_mode": sound_mode,
                    "alarm_enabled": "1" if sound_mode == "alarm" else "0",
                    "title": title[:200],
                    "body": body[:240],
                },
                android=messaging.AndroidConfig(priority="high"),
            ),
            app=get_firebase_app(),
        )
    except Exception as error:
        _log_notification_failure(
            data.get("notification_name"), error, stage="test_firebase_send", user=user
        )
        return {"status": "failed", "message": "FCM sending failed. Check Error Log."}

    return {"status": "sent", "message": "Accepted by Firebase.", "response": response}






















# """Custom Frappe app Python, NOT a restricted Server Script.

# Install as apps/itgenie/itgenie/fcm/fcm_utils.py. Place the private credential
# beside this file as automategenie-service-account.json and exclude it from Git.
# FCM Notification rules use a trusted, after-commit worker. Generic deliveries
# cannot be invoked over HTTP. No DocType-specific payload builders are used.
# """

# import json
# import hashlib
# import inspect
# from contextvars import ContextVar
# from pathlib import Path
# from threading import Lock

# import firebase_admin
# from firebase_admin import credentials, messaging
# import frappe


# PROJECT_ID = "automategenie-mobile"
# _APP_LOCK = Lock()
# _NOTIFICATION_DISPATCH = ContextVar("genie_fcm_notification_dispatch", default=None)


# def get_firebase_app():
#     """Use a site-specific named app without changing the default Firebase app."""
#     app_name = PROJECT_ID + ":" + str(frappe.local.site)
#     with _APP_LOCK:
#         try:
#             app = firebase_admin.get_app(app_name)
#         except ValueError:
#             credential_path = Path(__file__).with_name(
#                 "automategenie-service-account.json"
#             )
#             credential = credentials.Certificate(str(credential_path))
#             if credential.project_id != PROJECT_ID:
#                 raise ValueError("Credential must belong to automategenie-mobile")
#             app = firebase_admin.initialize_app(
#                 credential,
#                 options={"projectId": PROJECT_ID, "httpTimeout": 10},
#                 name=app_name,
#             )
#         if app.project_id != PROJECT_ID:
#             raise ValueError("Unexpected Firebase project configuration")
#         return app


# def send_fcm_to_user(user, payload):
#     """Internal sender for generic Notification payloads, never a public API."""
#     if isinstance(payload, str):
#         if len(payload) > 8192:
#             frappe.throw("Invalid FCM notification payload.")
#         try:
#             payload = json.loads(payload)
#         except (TypeError, ValueError):
#             frappe.throw("Invalid FCM notification payload.")
#     if not isinstance(payload, dict) or payload.get("type") != "DOCUMENT_NOTIFICATION":
#         frappe.throw("Only generic DOCUMENT_NOTIFICATION payloads are supported.")
#     return _send_document_notification(user, payload)


# class FCMNotificationMixin:
#     """Shared override channel behavior. Other channels use super()."""

#     def validate(self):
#         if self.get("channel") == "FCM":
#             from frappe.utils.jinja import validate_template

#             if self.get("event") == "Custom":
#                 frappe.throw("Manual/custom FCM notifications are not enabled.")
#             if not (self.get("subject") or "").strip():
#                 frappe.throw("Subject is required for an FCM Notification.")
#             if not (self.get("message") or "").strip():
#                 frappe.throw("Message is required for an FCM Notification.")
#             validate_template(self.subject)
#         return super().validate()

#     def send_notification_by_channel(self, doc, context):
#         if self.get("channel") != "FCM":
#             return super().send_notification_by_channel(doc, context)
#         if not self.get("enabled") or self.get("event") == "Custom":
#             return
#         try:
#             # Frappe already evaluated the Notification event and condition.
#             frappe.enqueue(
#                 "itgenie.fcm.fcm_utils.dispatch_document_notification",
#                 queue="default", timeout=600, enqueue_after_commit=True,
#                 notification_name=self.name, doctype=doc.doctype, docname=doc.name,
#                 document_version=str(doc.get("modified") or doc.get("creation") or ""),
#             )
#         except Exception as error:
#             _log_notification_failure(self.name, type(error).__name__)
#         # Preserve the standard optional ERP bell notification.
#         if self.get("send_system_notification"):
#             try:
#                 self.create_system_notification(doc, context)
#             except Exception as error:
#                 _log_notification_failure(self.name, type(error).__name__)


# def dispatch_document_notification(notification_name, doctype, docname, document_version=""):
#     """Internal queue target. Intentionally NOT whitelisted/public."""
#     from frappe.email.doctype.notification.notification import get_context, get_comments_for_context
#     from frappe.utils.safe_exec import safe_exec

#     rule = frappe.get_doc("Notification", notification_name)
#     if not rule.get("enabled") or rule.get("channel") != "FCM" or rule.get("event") == "Custom":
#         return {"status": "skipped"}
#     if rule.get("document_type") != doctype:
#         frappe.throw("Notification DocType mismatch.")
#     document = frappe.get_doc(doctype, docname)
#     script_name = frappe.db.get_value("Server Script", {
#         "api_method": "mobile_fcm_notification", "script_type": "API", "disabled": 0,
#     }, "name")
#     if not script_name:
#         _log_notification_failure(rule.name, "Generic Server Script missing or disabled")
#         return {"status": "failed", "message": "Generic Server Script missing or disabled"}
#     script = frappe.get_doc("Server Script", script_name)
#     context = get_context(document)
#     context.update({"alert": rule, "comments": get_comments_for_context(document)})
#     dispatch = {"rule": rule, "document": document, "version": document_version}
#     marker = _NOTIFICATION_DISPATCH.set(dispatch)
#     try:
#         # Pass the Python callable directly. Restricted frappe.call/safe_enqueue
#         # may invoke HTTP-method checks in a worker where no request exists.
#         execution = {"_locals": {"fcm_trigger": {
#             "notification_name": rule.name, "doctype": doctype, "docname": docname,
#             "context": context, "send_fcm_to_user": send_fcm_to_user,
#             "render_template": render_notification_template,
#         }}}
#         if "script_filename" in inspect.signature(safe_exec).parameters:
#             execution["script_filename"] = script.name
#         scope, unused_locals = safe_exec(script.script, **execution)
#         return scope["frappe"].flags.get("fcm_notification_result") or {"status": "processed"}
#     except Exception as error:
#         _log_notification_failure(rule.name, type(error).__name__)
#         return {"status": "failed", "message": "FCM Notification dispatch failed"}
#     finally:
#         _NOTIFICATION_DISPATCH.reset(marker)


# def render_notification_template(template, context):
#     """Use restricted globals where supported, including v15 backports.

#     Earlier v15 Jinja renderers use their standard sandbox and do not expose
#     the restrict_globals keyword. Detect the signature instead of retrying a
#     failed render (which could hide an actual template error).
#     """
#     if "restrict_globals" in inspect.signature(frappe.render_template).parameters:
#         return frappe.render_template(template, context, restrict_globals=True)
#     return frappe.render_template(template, context)


# def _log_notification_failure(notification_name, error_name):
#     # Do not log SDK messages, device tokens, rendered customer data or locals.
#     frappe.log_error(title="FCM Notification dispatch failed",
#         message="Notification " + str(notification_name) + "; " + str(error_name))


# def _send_document_notification(user, payload):
#     dispatch = _NOTIFICATION_DISPATCH.get()
#     if not dispatch:
#         frappe.throw("Generic notifications can only be sent by the Notification worker.", frappe.PermissionError)
#     rule, document = dispatch["rule"], dispatch["document"]
#     if not isinstance(user, str) or not user.strip() or len(user) > 140 or user == "Guest":
#         frappe.throw("Invalid FCM recipient.")
#     user = user.strip()
#     if (payload.get("notification_name") != rule.name or payload.get("doctype") != document.doctype
#             or payload.get("docname") != document.name or payload.get("for_user") != user):
#         frappe.throw("FCM notification references do not match the trusted trigger.")
#     if (not rule.get("enabled") or rule.get("channel") != "FCM"
#             or rule.get("document_type") != document.doctype or rule.get("event") == "Custom"):
#         return {"status": "skipped"}
#     if not frappe.db.get_value("User", user, "enabled") or not document.has_permission("read", user=user):
#         return {"status": "skipped", "message": "Recipient is disabled or has no document access"}
#     if any(not isinstance(payload.get(key), str) or not payload[key].strip() for key in ["title", "body"]):
#         frappe.throw("FCM title and body are required.")
#     # Retry/duplicate protection per rule, document version and recipient.
#     identity = json.dumps([str(frappe.local.site), rule.name, document.doctype, document.name,
#         dispatch["version"], rule.get("event"), user], separators=(",", ":"))
#     delivery_key = "genie_fcm_notification:" + hashlib.sha256(identity.encode()).hexdigest()
#     cache = frappe.cache()
#     with cache.lock(delivery_key + ":lock", timeout=30, blocking_timeout=5):
#         if cache.get_value(delivery_key):
#             return {"status": "skipped", "message": "Already accepted by FCM"}
#         devices = frappe.get_all("FCM Device", filters={"user": user, "is_active": 1},
#             fields=["fcm_token"], order_by="modified desc, name desc", limit_page_length=1)
#         token = devices[0].get("fcm_token") if devices else None
#         if not isinstance(token, str) or not token.strip():
#             _log_notification_failure(rule.name, "No active device token")
#             return {"status": "failed", "message": "No active device token"}
#         alarm_enabled = bool(frappe.db.exists("Has Role", {
#             "parent": user, "parenttype": "User", "parentfield": "roles",
#             "role": ["in", ["Sales User", "Designer"]],
#         }))
#         data = {
#             "type": "DOCUMENT_NOTIFICATION",
#             "notification_name": rule.name,
#             "doctype": document.doctype,
#             "docname": document.name,
#             "assignment_id": "",
#             "for_user": user,
#             "alarm_enabled": "1" if alarm_enabled else "0",
#             "title": payload["title"].strip()[:200],
#             "body": payload["body"].strip()[:240],
#         }
#         try:
#             response = messaging.send(messaging.Message(token=token.strip(), data=data,
#                 android=messaging.AndroidConfig(priority="high")), app=get_firebase_app())
#         except Exception as error:
#             _log_notification_failure(rule.name, type(error).__name__)
#             return {"status": "failed", "message": "FCM sending failed"}
#         # Firebase acceptance is recorded; it is not proof of phone delivery.
#         cache.set_value(delivery_key, str(response), expires_in_sec=86400)
#         return {"status": "sent", "response": response}


# def setup_fcm_notification_channel():
#     """Run once from bench console as Administrator/System Manager, not HTTP."""
#     frappe.only_for("System Manager")
#     from frappe.custom.doctype.property_setter.property_setter import make_property_setter

#     meta = frappe.get_meta("Notification")
#     channels = [value for value in (meta.get_field("channel").options or "").splitlines() if value]
#     if "FCM" not in channels:
#         channels.append("FCM")
#         make_property_setter("Notification", "channel", "options", "\n".join(channels), "Text")
#     for fieldname, property_name in [("subject", "depends_on"), ("subject", "mandatory_depends_on"), ("message", "depends_on")]:
#         previous = meta.get_field(fieldname).get(property_name) or ""
#         if "doc.channel == 'FCM'" in previous:
#             continue
#         if previous.startswith("eval:"):
#             expression = "eval:doc.channel == 'FCM' or (" + previous[5:] + ")"
#         elif previous:
#             expression = "eval:doc.channel == 'FCM' or doc.get(" + json.dumps(previous) + ")"
#         elif property_name == "mandatory_depends_on":
#             expression = "eval:doc.channel == 'FCM'"
#         else:
#             continue
#         make_property_setter("Notification", fieldname, property_name, expression, "Code")
#     frappe.clear_cache(doctype="Notification")


# def validate_fcm_notification_installation():
#     """Console check after registration; detects another app masking the hook."""
#     from frappe.model.base_document import get_controller

#     controller = get_controller("Notification")
#     if not issubclass(controller, FCMNotificationMixin):
#         frappe.throw("The FCM Notification controller is not active. Check hooks and installed-app order.")
#     return {"status": "ready", "controller": controller.__name__}
