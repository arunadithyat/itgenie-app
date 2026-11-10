"""Permanent installation binding for the custom Frappe app (not Safe Exec).

Deploy to apps/itgenie/itgenie/mobile_auth.py; run install() before replacing api.login.
MariaDB advisory locks intentionally survive commits made inside Frappe login.
"""
from contextlib import contextmanager, nullcontext
import hashlib
import hmac
import re

import frappe
from frappe.auth import LoginManager
from frappe.sessions import delete_session
from frappe.utils.password import get_decrypted_password

DOCTYPE = "FCM Device"
HASH_FIELD = "custom_mobile_device_hash"
ANDROID_HASH_FIELD = "custom_mobile_android_id_hash"
SESSION_FIELD = "custom_mobile_session_id"
DEVICE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


def install():
    """Add protected fields to the existing FCM Device; never create a DocType.

    No legacy Genie Mobile Session records are read, copied or deleted.
    Run from bench as Administrator during the backend rollout.
    """
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    frappe.only_for("System Manager")
    if not frappe.db.exists("DocType", DOCTYPE):
        frappe.throw("FCM Device must already exist.", frappe.ValidationError)
    meta = frappe.get_meta(DOCTYPE)
    for name in ("user", "employee", "fcm_token", "is_active"):
        if not meta.has_field(name):
            frappe.throw("FCM Device schema is missing: " + name, frappe.ValidationError)
    # Hidden/read-only alone do not prevent API reads. No existing role is
    # granted permission level 1; ordinary FCM readers cannot see these fields.
    if any(p.permlevel == 1 for p in meta.permissions):
        frappe.throw("Review FCM Device permission level 1 before installation.", frappe.ValidationError)
    for name in (HASH_FIELD, ANDROID_HASH_FIELD, SESSION_FIELD):
        field = meta.get_field(name)
        if field and field.fieldtype != "Data":
            frappe.throw("Unexpected mobile login field type: " + name, frappe.ValidationError)
    create_custom_fields({DOCTYPE: [
        {"fieldname": HASH_FIELD, "label": "Mobile Installation Hash",
         "fieldtype": "Data", "length": 64, "insert_after": "device_id",
         "hidden": 1, "read_only": 1, "permlevel": 1, "no_copy": 1, "reqd": 0},
        {"fieldname": SESSION_FIELD, "label": "Mobile Session ID",
         "fieldtype": "Data", "length": 255, "insert_after": HASH_FIELD,
         "hidden": 1, "read_only": 1, "permlevel": 1, "no_copy": 1, "reqd": 0},
        {"fieldname": ANDROID_HASH_FIELD, "label": "Mobile Android ID Hash",
         "fieldtype": "Data", "length": 64, "insert_after": HASH_FIELD,
         "hidden": 1, "read_only": 1, "permlevel": 1, "no_copy": 1, "reqd": 0,
         "search_index": 1},
    ]})
    frappe.db.commit()


def _setup_ready():
    if not frappe.db.exists("DocType", DOCTYPE):
        return False
    meta = frappe.get_meta(DOCTYPE)
    return all(meta.has_field(name) for name in (HASH_FIELD, ANDROID_HASH_FIELD, SESSION_FIELD))


def _binding(user):
    # FCM Device is named by Employee, not by User. Reject ambiguous mappings.
    rows = frappe.get_all(
        DOCTYPE, filters={"user": user},
        fields=["name", "employee", HASH_FIELD, ANDROID_HASH_FIELD, SESSION_FIELD], limit_page_length=2,
    )
    if len(rows) > 1:
        frappe.throw("Multiple FCM Device records are linked to this user. Contact your administrator.",
                     frappe.ValidationError)
    if not rows:
        return None
    row = rows[0]
    return {"name": row.name, "employee": row.employee,
            "device_hash": row.get(HASH_FIELD), "android_hash": row.get(ANDROID_HASH_FIELD),
            "session_id": row.get(SESSION_FIELD)}


def _save_binding(user, employee, device_hash, sid, binding, platform=None, android_hash=None):
    # Serialize first-row creation with register_device(), including requests
    # authenticated before the API secret was rotated by this login.
    frappe.db.get_value("Employee", employee, "name", for_update=True)
    existing = frappe.db.get_value(
        DOCTYPE, {"employee": employee}, ["name", "user", "platform"], as_dict=True, for_update=True,
    )
    if existing and existing.user not in (None, "", user):
        frappe.throw("FCM Device belongs to another user. Contact your administrator.", frappe.ValidationError)
    if binding and (not existing or existing.name != binding["name"]):
        frappe.throw("FCM Device Employee mapping changed. Contact your administrator.", frappe.ValidationError)
    values = {"user": user, HASH_FIELD: device_hash, SESSION_FIELD: sid}
    if android_hash:
        values[ANDROID_HASH_FIELD] = android_hash
    # Use the validated platform reported by this login. This also repairs
    # previously blank or incorrect values before FCM registration runs.
    if platform is not None:
        values["platform"] = platform
    elif not existing or not existing.platform:
        # Compatibility for older Android builds that omit this new parameter.
        values["platform"] = "android"
    if not binding or not binding["device_hash"] or binding["device_hash"] != device_hash:
        # Old notification tokens must not be active for a newly claimed phone.
        # Registration restores the current token and is_active after login.
        values["is_active"] = 0
        # A verified reinstall gets a new notification ID. Only this login path
        # may release old metadata; the document save guard stays unchanged.
        values["device_id"] = None
    if existing:
        # Preserve notification token and version metadata.
        frappe.db.set_value(DOCTYPE, existing.name, values)
    else:
        frappe.get_doc({"doctype": DOCTYPE, "employee": employee, **values}).insert(
            ignore_permissions=True,
        )


def _device_hash(device_key):
    if not isinstance(device_key, str) or not DEVICE_PATTERN.fullmatch(device_key):
        frappe.throw("Update the Genie app before signing in.", frappe.ValidationError)
    return hashlib.sha256(device_key.encode()).hexdigest()


def _android_hash(android_id, platform):
    if android_id is None:
        return None  # Legacy Android and iOS keep the private-key-only flow.
    if platform != "android" or not isinstance(android_id, str):
        frappe.throw("Invalid Android device identifier.", frappe.ValidationError)
    value = android_id.strip().lower()
    if (not re.fullmatch(r"[0-9a-f]{1,16}", value) or not value.strip("0")
            or value == "9774d56d682e549c"):
        frappe.throw("Invalid Android device identifier.", frappe.ValidationError)
    return hashlib.sha256(("genie-android-v1:" + value).encode()).hexdigest()


@contextmanager
def _binding_lock(identity, namespace="genie"):
    # Connection-scoped MariaDB mutex: unlike SELECT FOR UPDATE it survives
    # the commits in Session.start(), session cleanup, and CSRF initialization.
    name = hashlib.sha256((frappe.local.site + ":" + namespace + ":" + identity).encode()).hexdigest()
    result = frappe.db.sql("SELECT GET_LOCK(%s, 10)", (name,))
    if not result or result[0][0] != 1:
        frappe.throw("Another sign-in is being processed. Please retry.", frappe.ValidationError)
    try:
        # Authentication may already have opened a repeatable-read snapshot.
        # Discard it so a waiter sees the preceding login's committed binding.
        frappe.db.commit()
        yield
    finally:
        frappe.db.sql("SELECT RELEASE_LOCK(%s)", (name,))


def _user_lock(user):
    # Preserve the established user-lock name for existing deployments.
    return _binding_lock(user)


def _device_lock(device_hash):
    # Two different users claiming one installation must also serialize.
    return _binding_lock(device_hash, namespace="genie-device")


def _error(message, code):
    frappe.local.response["http_status_code"] = code
    frappe.local.response["message"] = {"key": 0, "message": message}


def _revoke_session(sid, user):
    if sid and sid != "Guest":
        delete_session(sid, user=user, reason="Genie mobile sign out")


def _write_login_diagnostic(diagnostic):
    """Best-effort diagnostics only; never commit the login transaction.

    Deferred insertion keeps the log outside the request rollback path when
    Redis is available. The scheduler must run to flush it into Error Log.
    Never include request bodies, passwords, tokens, keys, SIDs or tracebacks.
    """
    try:
        frappe.log_error(
            title="Genie Mobile Login: " + diagnostic.get("outcome", "unknown"),
            message=frappe.as_json(diagnostic),
            defer_insert=True,
        )
    except Exception:
        # A logging failure must never change the authentication result.
        try:
            frappe.logger("mobile_auth").error(
                "Could not queue Genie mobile login diagnostic"
            )
        except Exception:
            pass


def _login_rejected(diagnostic, message, code, reason):
    diagnostic.update(outcome="rejected", http_status=code, reason=reason)
    return _error(message, code)


def login(usr, pwd, device_key=None, platform=None, android_id=None):
    """Keep the public login contract; record only safe diagnostic metadata."""
    diagnostic = {
        "stage": "started",
        "outcome": "started",
        "user": "not_authenticated",
        "platform": "not_validated",
        "first_device_enrollment": None,
    }
    try:
        return _login(usr, pwd, device_key=device_key, platform=platform,
                      android_id=android_id, diagnostic=diagnostic)
    except Exception as error:
        # Exception text/traceback can contain credentials; record type only.
        diagnostic.update(outcome="exception", exception_type=type(error).__name__)
        raise
    finally:
        _write_login_diagnostic(diagnostic)


def _login(usr, pwd, device_key=None, platform=None, android_id=None, diagnostic=None):
    """Called only through api.login. Passwords are checked before lock details."""
    diagnostic["stage"] = "validate_installation"
    device_hash = _device_hash(device_key)
    diagnostic["stage"] = "validate_platform"
    if platform is not None:
        if not isinstance(platform, str):
            frappe.throw("Invalid mobile platform.", frappe.ValidationError)
        platform = platform.strip().lower()
        if platform not in ("android", "ios"):
            frappe.throw("Platform must be android or ios.", frappe.ValidationError)
    diagnostic["platform"] = platform or "legacy"
    diagnostic["stage"] = "validate_android_identifier"
    android_hash = _android_hash(android_id, platform)
    diagnostic["stage"] = "authenticate_password"
    manager = LoginManager()
    manager.authenticate(usr, pwd)
    user = manager.user  # Canonical ID, including logins using Employee/User aliases.
    diagnostic["user"] = user
    diagnostic["stage"] = "check_backend_setup"
    if not _setup_ready():
        return _login_rejected(diagnostic, "Mobile login setup is incomplete. Contact your administrator.", 503, "backend_setup_incomplete")
    # Always take the device lock first. Logout/reset take only the user lock;
    # neither waits on a device lock while holding a user lock.
    # The extra Android mutex serializes two fresh installation keys claiming
    # the same Android ID under different accounts. All callers use this order.
    android_lock = (_binding_lock(android_hash, namespace="genie-android")
                    if android_hash else nullcontext())
    diagnostic["stage"] = "acquire_binding_locks"
    with _device_lock(device_hash), android_lock, _user_lock(user):
        # Read after mutex acquisition; don't use a stale cached document.
        diagnostic["stage"] = "check_device_ownership"
        binding = _binding(user)
        diagnostic["first_device_enrollment"] = bool(not binding or not binding["device_hash"])
        same_key = bool(binding and binding["device_hash"]
                        and hmac.compare_digest(binding["device_hash"], device_hash))
        if binding:
            if binding["android_hash"]:
                if android_hash:
                    if not hmac.compare_digest(binding["android_hash"], android_hash):
                        return _login_rejected(diagnostic, "Wrong User", 409, "android_id_differs_from_user_binding")
                elif not same_key:
                    # Omitting/changing the platform cannot bypass ownership.
                    return _login_rejected(diagnostic, "Wrong User", 409, "android_id_missing_and_installation_key_differs")
            elif binding["device_hash"] and not same_key:
                # Legacy migration requires the original private installation
                # key. Password alone must never enroll a replacement phone.
                return _login_rejected(diagnostic, "Wrong User", 409, "installation_key_differs_from_legacy_binding")
        other_device = frappe.get_all(
            DOCTYPE,
            filters={HASH_FIELD: device_hash, "user": ["!=", user]},
            fields=["name"], limit_page_length=1,
        )
        if other_device:
            return _login_rejected(diagnostic, "Wrong User", 409, "installation_already_bound_to_another_user")
        if android_hash and frappe.get_all(
            DOCTYPE,
            filters={ANDROID_HASH_FIELD: android_hash, "user": ["!=", user]},
            fields=["name"], limit_page_length=1,
        ):
            return _login_rejected(diagnostic, "Wrong User", 409, "android_id_already_bound_to_another_user")
        diagnostic["stage"] = "check_employee_mapping"
        employees = frappe.get_all(
            "Employee", filters={"user_id": user, "status": "Active"},
            fields=["name"], limit_page_length=2,
        )
        if len(employees) != 1:
            return _login_rejected(diagnostic, "An active Employee profile is required. Contact your administrator.", 403, "active_employee_mapping_invalid")
        # The mapping is a Child Table, not a column on tabEmployee.
        # Load its rows through the document, matching the existing API.
        diagnostic["stage"] = "check_office_type_mapping"
        employee = frappe.get_doc("Employee", employees[0].name)
        if not employee.custom_office_type_mapping:
            return _login_rejected(diagnostic, "Access Denied: No office type mapped to your profile.", 403, "office_type_mapping_missing")

        # Use Frappe's full login flow, including password policy, 2FA and IP/hour
        # checks. A rejected second phone never reaches session creation.
        diagnostic["stage"] = "frappe_login"
        if manager.login() is False:
            diagnostic.update(outcome="not_completed", reason="frappe_login_returned_false")
            return
        diagnostic["stage"] = "check_authenticated_session"
        if frappe.session.user != user or frappe.session.sid == "Guest":
            return _login_rejected(diagnostic, "Sign-in was not completed. Please retry.", 401, "session_not_established")
        sid = frappe.session.sid
        try:
            diagnostic["stage"] = "prepare_api_credentials"
            account = frappe.get_doc("User", user)
            secret = get_decrypted_password("User", user, "api_secret", raise_exception=False)
            # Rotate on a NEW claim, never on a same-installation retry. An old
            # build/token from before this rollout cannot retain API access.
            if not same_key or not account.api_key or not secret:
                if not account.api_key:
                    account.api_key = frappe.generate_hash(length=15)
                # Keep the generated value separately: Frappe masks Password
                # fields on the document during save (e.g. "********").
                secret = frappe.generate_hash(length=32)
                account.api_secret = secret
                account.save(ignore_permissions=True)
            diagnostic["stage"] = "save_device_binding"
            _save_binding(user, employee.name, device_hash, sid, binding,
                          platform=platform, android_hash=android_hash)
            diagnostic["stage"] = "commit_device_binding"
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            _revoke_session(sid, user)
            raise
        diagnostic["stage"] = "revoke_previous_session"
        if binding and binding["session_id"] != sid:
            _revoke_session(binding["session_id"], user)
        frappe.local.response.update({
            "message": "Logged In", "email": user, "full_name": account.full_name,
            "name": account.full_name, "employee_id": employee.name,
            "token": "token " + account.api_key + ":" + secret,
            "api_key": account.api_key, "api_secret": secret,
            "single_device_login": True,
            "android_device_binding": bool(android_hash),
        })

        diagnostic.update(stage="completed", outcome="success", reason="login_completed")


def logout(user, device_key):
    """A 256-bit installation credential allows logout even after SID expiry.

    The app calls this without cookies or API headers; the installation key is
    the bearer credential. A different phone cannot sign this account out.
    Logout revokes the session/token but retains permanent device ownership.
    A repeated logout with no active SID is idempotent.
    """
    device_hash = _device_hash(device_key)
    if not isinstance(user, str) or not user or len(user) > 140:
        frappe.throw("Invalid user.", frappe.ValidationError)
    if not _setup_ready():
        return _error("Mobile login setup is incomplete. Contact your administrator.", 503)
    with _user_lock(user):
        binding = _binding(user)
        if binding and binding["device_hash"]:
            if not hmac.compare_digest(binding["device_hash"], device_hash):
                return _error("This phone cannot sign out that account.", 403)
            _release(user, binding)
    frappe.local.response["message"] = {"status": "success"}


def _release(user, binding, clear_binding=False):
    if not binding["session_id"] and not clear_binding:
        return
    # Revoke the token too: merely deleting an SID leaves API-token calls valid.
    account = frappe.get_doc("User", user)
    account.api_secret = frappe.generate_hash(length=32)
    account.save(ignore_permissions=True)
    _revoke_session(binding["session_id"], user)
    # Normal logout keeps ownership. Only the administrator reset clears it.
    values = {SESSION_FIELD: None, "is_active": 0}
    if clear_binding:
        values[HASH_FIELD] = None
        values[ANDROID_HASH_FIELD] = None
        # Permit the approved replacement's first notification registration.
        # Keep the FCM token/version metadata, with delivery disabled above.
        values["device_id"] = None
    frappe.db.set_value(DOCTYPE, binding["name"], values)
    frappe.db.commit()


def reset_device(user):
    """Administrator console recovery for a lost/reinstalled phone; never whitelist."""
    frappe.only_for("System Manager")
    with _user_lock(user):
        binding = _binding(user)
        if binding and (binding["device_hash"] or binding["android_hash"]):
            _release(user, binding, clear_binding=True)


# Attendance and visit writes in the public mobile module. A user with a bound
# phone must reach these from that phone, not with a SID created through
# Frappe's standard /api/method/login on another device or script.
BOUND_SESSION_METHODS = frozenset(
    "itgenie.api_utils." + name
    for name in ("checkin", "checkout", "event_checkin", "event_checkout")
)
METHOD_PATH_PREFIXES = ("/api/method/", "/api/v1/method/", "/api/v2/method/")


def validate_bound_session():
    """auth_hooks entry; runs after Frappe has authenticated every request.

    Token calls are already bound: the API secret rotates on each new phone
    claim. SID calls must use the SID recorded by the guarded mobile login.
    Users without a managed binding keep existing behavior.
    """
    request = getattr(frappe.local, "request", None)
    path = (getattr(request, "path", None) or "").rstrip("/")
    if not path.startswith(METHOD_PATH_PREFIXES):
        return
    if path.rsplit("/", 1)[-1] not in BOUND_SESSION_METHODS:
        return
    user = frappe.session.user
    if not user or user == "Guest":
        return
    authorization = (request.headers.get("Authorization") or "").strip().lower()
    if authorization.startswith("token "):
        return
    if not _setup_ready():
        return
    binding = _binding(user)
    if not binding or not binding["device_hash"]:
        return
    sid = frappe.session.sid or ""
    if binding["session_id"] and hmac.compare_digest(binding["session_id"], sid):
        return
    frappe.throw("Use the Genie app on your registered phone for this action.",
                 frappe.AuthenticationError)


def on_device_trash(doc, method=None):
    """FCM Device on_trash hook. Custom app Python; never a public API.

    Revoke managed mobile access atomically with an administrator's deletion.
    Do NOT call delete_session/_user_lock here: both commit, which would break
    the document deletion transaction. Direct db.delete bypasses this hook and
    is not an approved reset path.
    """
    if not (doc.get(HASH_FIELD) or doc.get(ANDROID_HASH_FIELD)):
        return  # Unmanaged notification records retain existing behavior.
    frappe.only_for("System Manager")
    user = doc.get("user")
    if not user:
        frappe.throw("Bound device has no user. Contact your administrator.", frappe.ValidationError)
    name = hashlib.sha256((frappe.local.site + ":genie:" + user).encode()).hexdigest()
    result = frappe.db.sql("SELECT GET_LOCK(%s, 10)", (name,))
    if not result or result[0][0] != 1:
        frappe.throw("Another sign-in is being processed. Please retry deletion.", frappe.ValidationError)

    def unlock():
        frappe.db.sql("SELECT RELEASE_LOCK(%s)", (name,))

    # Hold through commit/rollback so a concurrent login cannot recreate a
    # binding before the row deletion is visible. No commits inside this hook.
    frappe.db.after_rollback.add(unlock)
    try:
        row = frappe.db.get_value(
            DOCTYPE, doc.name, ["user", HASH_FIELD, ANDROID_HASH_FIELD, SESSION_FIELD],
            as_dict=True, for_update=True,
        )
        if not row or row.user != user:
            frappe.throw("Device ownership changed. Please reload and retry.", frappe.ValidationError)
        account = frappe.get_doc("User", user)
        account.api_secret = frappe.generate_hash(length=32)
        account.save(ignore_permissions=True)
        sid = row.get(SESSION_FIELD)
        if sid and sid != "Guest":
            frappe.db.delete("Sessions", {"sid": sid, "user": user})

        def finish_delete():
            try:
                if sid and sid != "Guest":
                    frappe.cache.hdel("session", sid)
                    frappe.cache.hdel("last_db_session_update", sid)
            finally:
                unlock()

        frappe.db.after_commit.add(finish_delete)
    except Exception:
        # Frappe rolls the request back and then releases the mutex above.
        raise












####EXISTING SCRIPT: mobile_auth.py


# """Permanent installation binding for the custom Frappe app (not Safe Exec).

# Deploy to apps/itgenie/itgenie/mobile_auth.py; run install() before replacing api.login.
# MariaDB advisory locks intentionally survive commits made inside Frappe login.
# """
# from contextlib import contextmanager, nullcontext
# import hashlib
# import hmac
# import re

# import frappe
# from frappe.auth import LoginManager
# from frappe.sessions import delete_session
# from frappe.utils.password import get_decrypted_password

# DOCTYPE = "FCM Device"
# HASH_FIELD = "custom_mobile_device_hash"
# ANDROID_HASH_FIELD = "custom_mobile_android_id_hash"
# SESSION_FIELD = "custom_mobile_session_id"
# DEVICE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


# def install():
#     """Add protected fields to the existing FCM Device; never create a DocType.

#     No legacy Genie Mobile Session records are read, copied or deleted.
#     Run from bench as Administrator during the backend rollout.
#     """
#     from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

#     frappe.only_for("System Manager")
#     if not frappe.db.exists("DocType", DOCTYPE):
#         frappe.throw("FCM Device must already exist.", frappe.ValidationError)
#     meta = frappe.get_meta(DOCTYPE)
#     for name in ("user", "employee", "fcm_token", "is_active"):
#         if not meta.has_field(name):
#             frappe.throw("FCM Device schema is missing: " + name, frappe.ValidationError)
#     # Hidden/read-only alone do not prevent API reads. No existing role is
#     # granted permission level 1; ordinary FCM readers cannot see these fields.
#     if any(p.permlevel == 1 for p in meta.permissions):
#         frappe.throw("Review FCM Device permission level 1 before installation.", frappe.ValidationError)
#     for name in (HASH_FIELD, ANDROID_HASH_FIELD, SESSION_FIELD):
#         field = meta.get_field(name)
#         if field and field.fieldtype != "Data":
#             frappe.throw("Unexpected mobile login field type: " + name, frappe.ValidationError)
#     create_custom_fields({DOCTYPE: [
#         {"fieldname": HASH_FIELD, "label": "Mobile Installation Hash",
#          "fieldtype": "Data", "length": 64, "insert_after": "device_id",
#          "hidden": 1, "read_only": 1, "permlevel": 1, "no_copy": 1, "reqd": 0},
#         {"fieldname": SESSION_FIELD, "label": "Mobile Session ID",
#          "fieldtype": "Data", "length": 255, "insert_after": HASH_FIELD,
#          "hidden": 1, "read_only": 1, "permlevel": 1, "no_copy": 1, "reqd": 0},
#         {"fieldname": ANDROID_HASH_FIELD, "label": "Mobile Android ID Hash",
#          "fieldtype": "Data", "length": 64, "insert_after": HASH_FIELD,
#          "hidden": 1, "read_only": 1, "permlevel": 1, "no_copy": 1, "reqd": 0,
#          "search_index": 1},
#     ]})
#     frappe.db.commit()


# def _setup_ready():
#     if not frappe.db.exists("DocType", DOCTYPE):
#         return False
#     meta = frappe.get_meta(DOCTYPE)
#     return all(meta.has_field(name) for name in (HASH_FIELD, ANDROID_HASH_FIELD, SESSION_FIELD))


# def _binding(user):
#     # FCM Device is named by Employee, not by User. Reject ambiguous mappings.
#     rows = frappe.get_all(
#         DOCTYPE, filters={"user": user},
#         fields=["name", "employee", HASH_FIELD, ANDROID_HASH_FIELD, SESSION_FIELD], limit_page_length=2,
#     )
#     if len(rows) > 1:
#         frappe.throw("Multiple FCM Device records are linked to this user. Contact your administrator.",
#                      frappe.ValidationError)
#     if not rows:
#         return None
#     row = rows[0]
#     return {"name": row.name, "employee": row.employee,
#             "device_hash": row.get(HASH_FIELD), "android_hash": row.get(ANDROID_HASH_FIELD),
#             "session_id": row.get(SESSION_FIELD)}


# def _save_binding(user, employee, device_hash, sid, binding, platform=None, android_hash=None):
#     # Serialize first-row creation with register_device(), including requests
#     # authenticated before the API secret was rotated by this login.
#     frappe.db.get_value("Employee", employee, "name", for_update=True)
#     existing = frappe.db.get_value(
#         DOCTYPE, {"employee": employee}, ["name", "user", "platform"], as_dict=True, for_update=True,
#     )
#     if existing and existing.user not in (None, "", user):
#         frappe.throw("FCM Device belongs to another user. Contact your administrator.", frappe.ValidationError)
#     if binding and (not existing or existing.name != binding["name"]):
#         frappe.throw("FCM Device Employee mapping changed. Contact your administrator.", frappe.ValidationError)
#     values = {"user": user, HASH_FIELD: device_hash, SESSION_FIELD: sid}
#     if android_hash:
#         values[ANDROID_HASH_FIELD] = android_hash
#     # Use the validated platform reported by this login. This also repairs
#     # previously blank or incorrect values before FCM registration runs.
#     if platform is not None:
#         values["platform"] = platform
#     elif not existing or not existing.platform:
#         # Compatibility for older Android builds that omit this new parameter.
#         values["platform"] = "android"
#     if not binding or not binding["device_hash"] or binding["device_hash"] != device_hash:
#         # Old notification tokens must not be active for a newly claimed phone.
#         # Registration restores the current token and is_active after login.
#         values["is_active"] = 0
#         # A verified reinstall gets a new notification ID. Only this login path
#         # may release old metadata; the document save guard stays unchanged.
#         values["device_id"] = None
#     if existing:
#         # Preserve notification token and version metadata.
#         frappe.db.set_value(DOCTYPE, existing.name, values)
#     else:
#         frappe.get_doc({"doctype": DOCTYPE, "employee": employee, **values}).insert(
#             ignore_permissions=True,
#         )


# def _device_hash(device_key):
#     if not isinstance(device_key, str) or not DEVICE_PATTERN.fullmatch(device_key):
#         frappe.throw("Update the Genie app before signing in.", frappe.ValidationError)
#     return hashlib.sha256(device_key.encode()).hexdigest()


# def _android_hash(android_id, platform):
#     if android_id is None:
#         return None  # Legacy Android and iOS keep the private-key-only flow.
#     if platform != "android" or not isinstance(android_id, str):
#         frappe.throw("Invalid Android device identifier.", frappe.ValidationError)
#     value = android_id.strip().lower()
#     if (not re.fullmatch(r"[0-9a-f]{1,16}", value) or not value.strip("0")
#             or value == "9774d56d682e549c"):
#         frappe.throw("Invalid Android device identifier.", frappe.ValidationError)
#     return hashlib.sha256(("genie-android-v1:" + value).encode()).hexdigest()


# @contextmanager
# def _binding_lock(identity, namespace="genie"):
#     # Connection-scoped MariaDB mutex: unlike SELECT FOR UPDATE it survives
#     # the commits in Session.start(), session cleanup, and CSRF initialization.
#     name = hashlib.sha256((frappe.local.site + ":" + namespace + ":" + identity).encode()).hexdigest()
#     result = frappe.db.sql("SELECT GET_LOCK(%s, 10)", (name,))
#     if not result or result[0][0] != 1:
#         frappe.throw("Another sign-in is being processed. Please retry.", frappe.ValidationError)
#     try:
#         # Authentication may already have opened a repeatable-read snapshot.
#         # Discard it so a waiter sees the preceding login's committed binding.
#         frappe.db.commit()
#         yield
#     finally:
#         frappe.db.sql("SELECT RELEASE_LOCK(%s)", (name,))


# def _user_lock(user):
#     # Preserve the established user-lock name for existing deployments.
#     return _binding_lock(user)


# def _device_lock(device_hash):
#     # Two different users claiming one installation must also serialize.
#     return _binding_lock(device_hash, namespace="genie-device")


# def _error(message, code):
#     frappe.local.response["http_status_code"] = code
#     frappe.local.response["message"] = {"key": 0, "message": message}


# def _revoke_session(sid, user):
#     if sid and sid != "Guest":
#         delete_session(sid, user=user, reason="Genie mobile sign out")


# def login(usr, pwd, device_key=None, platform=None, android_id=None):
#     """Called only through api.login. Passwords are checked before lock details."""
#     device_hash = _device_hash(device_key)
#     if platform is not None:
#         if not isinstance(platform, str):
#             frappe.throw("Invalid mobile platform.", frappe.ValidationError)
#         platform = platform.strip().lower()
#         if platform not in ("android", "ios"):
#             frappe.throw("Platform must be android or ios.", frappe.ValidationError)
#     android_hash = _android_hash(android_id, platform)
#     manager = LoginManager()
#     manager.authenticate(usr, pwd)
#     user = manager.user  # Canonical ID, including logins using Employee/User aliases.
#     if not _setup_ready():
#         return _error("Mobile login setup is incomplete. Contact your administrator.", 503)
#     # Always take the device lock first. Logout/reset take only the user lock;
#     # neither waits on a device lock while holding a user lock.
#     # The extra Android mutex serializes two fresh installation keys claiming
#     # the same Android ID under different accounts. All callers use this order.
#     android_lock = (_binding_lock(android_hash, namespace="genie-android")
#                     if android_hash else nullcontext())
#     with _device_lock(device_hash), android_lock, _user_lock(user):
#         # Read after mutex acquisition; don't use a stale cached document.
#         binding = _binding(user)
#         same_key = bool(binding and binding["device_hash"]
#                         and hmac.compare_digest(binding["device_hash"], device_hash))
#         if binding:
#             if binding["android_hash"]:
#                 if android_hash:
#                     if not hmac.compare_digest(binding["android_hash"], android_hash):
#                         return _error("Wrong User", 409)
#                 elif not same_key:
#                     # Omitting/changing the platform cannot bypass ownership.
#                     return _error("Wrong User", 409)
#             elif binding["device_hash"] and not same_key:
#                 # Legacy migration requires the original private installation
#                 # key. Password alone must never enroll a replacement phone.
#                 return _error("Wrong User", 409)
#         other_device = frappe.get_all(
#             DOCTYPE,
#             filters={HASH_FIELD: device_hash, "user": ["!=", user]},
#             fields=["name"], limit_page_length=1,
#         )
#         if other_device:
#             return _error("Wrong User", 409)
#         if android_hash and frappe.get_all(
#             DOCTYPE,
#             filters={ANDROID_HASH_FIELD: android_hash, "user": ["!=", user]},
#             fields=["name"], limit_page_length=1,
#         ):
#             return _error("Wrong User", 409)
#         employees = frappe.get_all(
#             "Employee", filters={"user_id": user, "status": "Active"},
#             fields=["name"], limit_page_length=2,
#         )
#         if len(employees) != 1:
#             return _error("An active Employee profile is required. Contact your administrator.", 403)
#         # The mapping is a Child Table, not a column on tabEmployee.
#         # Load its rows through the document, matching the existing API.
#         employee = frappe.get_doc("Employee", employees[0].name)
#         if not employee.custom_office_type_mapping:
#             return _error("Access Denied: No office type mapped to your profile.", 403)

#         # Use Frappe's full login flow, including password policy, 2FA and IP/hour
#         # checks. A rejected second phone never reaches session creation.
#         if manager.login() is False:
#             return
#         if frappe.session.user != user or frappe.session.sid == "Guest":
#             return _error("Sign-in was not completed. Please retry.", 401)
#         sid = frappe.session.sid
#         try:
#             account = frappe.get_doc("User", user)
#             secret = get_decrypted_password("User", user, "api_secret", raise_exception=False)
#             # Rotate on a NEW claim, never on a same-installation retry. An old
#             # build/token from before this rollout cannot retain API access.
#             if not same_key or not account.api_key or not secret:
#                 if not account.api_key:
#                     account.api_key = frappe.generate_hash(length=15)
#                 # Keep the generated value separately: Frappe masks Password
#                 # fields on the document during save (e.g. "********").
#                 secret = frappe.generate_hash(length=32)
#                 account.api_secret = secret
#                 account.save(ignore_permissions=True)
#             _save_binding(user, employee.name, device_hash, sid, binding,
#                           platform=platform, android_hash=android_hash)
#             frappe.db.commit()
#         except Exception:
#             frappe.db.rollback()
#             _revoke_session(sid, user)
#             raise
#         if binding and binding["session_id"] != sid:
#             _revoke_session(binding["session_id"], user)
#         frappe.local.response.update({
#             "message": "Logged In", "email": user, "full_name": account.full_name,
#             "name": account.full_name, "employee_id": employee.name,
#             "token": "token " + account.api_key + ":" + secret,
#             "api_key": account.api_key, "api_secret": secret,
#             "single_device_login": True,
#             "android_device_binding": bool(android_hash),
#         })


# def logout(user, device_key):
#     """A 256-bit installation credential allows logout even after SID expiry.

#     The app calls this without cookies or API headers; the installation key is
#     the bearer credential. A different phone cannot sign this account out.
#     Logout revokes the session/token but retains permanent device ownership.
#     A repeated logout with no active SID is idempotent.
#     """
#     device_hash = _device_hash(device_key)
#     if not isinstance(user, str) or not user or len(user) > 140:
#         frappe.throw("Invalid user.", frappe.ValidationError)
#     if not _setup_ready():
#         return _error("Mobile login setup is incomplete. Contact your administrator.", 503)
#     with _user_lock(user):
#         binding = _binding(user)
#         if binding and binding["device_hash"]:
#             if not hmac.compare_digest(binding["device_hash"], device_hash):
#                 return _error("This phone cannot sign out that account.", 403)
#             _release(user, binding)
#     frappe.local.response["message"] = {"status": "success"}


# def _release(user, binding, clear_binding=False):
#     if not binding["session_id"] and not clear_binding:
#         return
#     # Revoke the token too: merely deleting an SID leaves API-token calls valid.
#     account = frappe.get_doc("User", user)
#     account.api_secret = frappe.generate_hash(length=32)
#     account.save(ignore_permissions=True)
#     _revoke_session(binding["session_id"], user)
#     # Normal logout keeps ownership. Only the administrator reset clears it.
#     values = {SESSION_FIELD: None, "is_active": 0}
#     if clear_binding:
#         values[HASH_FIELD] = None
#         values[ANDROID_HASH_FIELD] = None
#         # Permit the approved replacement's first notification registration.
#         # Keep the FCM token/version metadata, with delivery disabled above.
#         values["device_id"] = None
#     frappe.db.set_value(DOCTYPE, binding["name"], values)
#     frappe.db.commit()


# def reset_device(user):
#     """Administrator console recovery for a lost/reinstalled phone; never whitelist."""
#     frappe.only_for("System Manager")
#     with _user_lock(user):
#         binding = _binding(user)
#         if binding and (binding["device_hash"] or binding["android_hash"]):
#             _release(user, binding, clear_binding=True)


# def on_device_trash(doc, method=None):
#     """FCM Device on_trash hook. Custom app Python; never a public API.

#     Revoke managed mobile access atomically with an administrator's deletion.
#     Do NOT call delete_session/_user_lock here: both commit, which would break
#     the document deletion transaction. Direct db.delete bypasses this hook and
#     is not an approved reset path.
#     """
#     if not (doc.get(HASH_FIELD) or doc.get(ANDROID_HASH_FIELD)):
#         return  # Unmanaged notification records retain existing behavior.
#     frappe.only_for("System Manager")
#     user = doc.get("user")
#     if not user:
#         frappe.throw("Bound device has no user. Contact your administrator.", frappe.ValidationError)
#     name = hashlib.sha256((frappe.local.site + ":genie:" + user).encode()).hexdigest()
#     result = frappe.db.sql("SELECT GET_LOCK(%s, 10)", (name,))
#     if not result or result[0][0] != 1:
#         frappe.throw("Another sign-in is being processed. Please retry deletion.", frappe.ValidationError)

#     def unlock():
#         frappe.db.sql("SELECT RELEASE_LOCK(%s)", (name,))

#     # Hold through commit/rollback so a concurrent login cannot recreate a
#     # binding before the row deletion is visible. No commits inside this hook.
#     frappe.db.after_rollback.add(unlock)
#     try:
#         row = frappe.db.get_value(
#             DOCTYPE, doc.name, ["user", HASH_FIELD, ANDROID_HASH_FIELD, SESSION_FIELD],
#             as_dict=True, for_update=True,
#         )
#         if not row or row.user != user:
#             frappe.throw("Device ownership changed. Please reload and retry.", frappe.ValidationError)
#         account = frappe.get_doc("User", user)
#         account.api_secret = frappe.generate_hash(length=32)
#         account.save(ignore_permissions=True)
#         sid = row.get(SESSION_FIELD)
#         if sid and sid != "Guest":
#             frappe.db.delete("Sessions", {"sid": sid, "user": user})

#         def finish_delete():
#             try:
#                 if sid and sid != "Guest":
#                     frappe.cache.hdel("session", sid)
#                     frappe.cache.hdel("last_db_session_update", sid)
#             finally:
#                 unlock()

#         frappe.db.after_commit.add(finish_delete)
#     except Exception:
#         # Frappe rolls the request back and then releases the mutex above.
#         raise
