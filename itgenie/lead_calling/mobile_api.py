import frappe


@frappe.whitelist()
def register_device(
    fcm_token,
    device_id=None,
    platform="android",
    app_version=None,
    build_version=None,
):
    user = frappe.session.user

    if not user or user == "Guest":
        frappe.throw("Login required", frappe.PermissionError)

    if not isinstance(fcm_token, str) or not fcm_token.strip():
        frappe.throw("Missing or invalid FCM token")

    fcm_token = fcm_token.strip()

    if platform is None:
        platform = "android"

    if not isinstance(platform, str):
        frappe.throw("Invalid platform")

    platform = platform.strip().lower()

    if platform not in ("android", "ios"):
        frappe.throw("Platform must be android or ios")

    try:
        build_version = int(build_version or 0)
    except (TypeError, ValueError):
        frappe.throw("Invalid build version")

    # Lock the employee row so simultaneous registration requests
    # cannot both attempt to create the employee's first FCM record.
    employee = frappe.db.get_value(
        "Employee",
        {"user_id": user},
        "name",
        for_update=True,
    )

    if not employee:
        frappe.throw(
            "No Employee is linked to the logged-in user"
        )

    # This deployment uses one FCM Device record per employee.
    # Platform, device ID and FCM token can change, so they must
    # not determine whether that employee's record already exists.
    existing = frappe.db.get_value(
        "FCM Device",
        {"employee": employee},
        ["name", "user"],
        as_dict=True,
        for_update=True,
    )

    if existing and existing.get("user") != user:
        frappe.throw(
            "FCM Device belongs to another user",
            frappe.PermissionError,
        )

    data = {
        "user": user,
        "employee": employee,
        "fcm_token": fcm_token,
        "device_id": device_id,
        "platform": platform,
        "is_active": 1,
        "last_seen": frappe.utils.now(),
        "app_version": app_version,
        "build_version": build_version,
    }

    if existing:
        name = existing["name"]

        # Preserve the existing notification-metadata update method.
        # Custom login-binding fields are not included or modified.
        frappe.db.set_value(
            "FCM Device",
            name,
            data,
        )
    else:
        doc = frappe.get_doc({
            "doctype": "FCM Device",
            **data,
        })

        doc.insert(ignore_permissions=True)
        name = doc.name

    frappe.db.commit()

    return {
        "status": "success",
        "device": name,
        "app_version": app_version,
        "build_version": build_version,
    }



# import frappe


# @frappe.whitelist()
# def register_device(
#     fcm_token,
#     device_id=None,
#     platform="android",
#     app_version=None,
#     build_version=None
# ):
#     user = frappe.session.user

#     employee = frappe.db.get_value(
#         "Employee", {"user_id": user}, "name"
#     )

#     existing = None

#     # Check 1 — match by device_id (most reliable, survives reinstall)
#     if device_id:
#         existing = frappe.db.get_value(
#             "FCM Device",
#             {"user": user, "device_id": device_id},
#             "name"
#         )

#     # Check 2 — match by fcm_token (same install, token unchanged)
#     if not existing:
#         existing = frappe.db.get_value(
#             "FCM Device",
#             {"user": user, "fcm_token": fcm_token},
#             "name"
#         )

#     # Check 3 — fallback: if only one device for this user, update it
#     # This handles old entries that were created before device_id was added
#     if not existing:
#         all_devices = frappe.db.get_all(
#             "FCM Device",
#             filters={"user": user, "platform": platform},
#             fields=["name"],
#             order_by="creation desc"
#         )
#         if len(all_devices) == 1:
#             # Only one entry exists — safe to update it
#             existing = all_devices[0]["name"]

#     data = {
#         "user": user,
#         "employee": employee,
#         "fcm_token": fcm_token,
#         "device_id": device_id,  # always save device_id now
#         "platform": platform,
#         "is_active": 1,
#         "last_seen": frappe.utils.now(),
#         "app_version": app_version,
#         "build_version": int(build_version or 0)
#     }

#     if existing:
#         frappe.db.set_value("FCM Device", existing, data)
#         name = existing
#     else:
#         doc = frappe.get_doc({"doctype": "FCM Device", **data})
#         doc.insert(ignore_permissions=True)
#         name = doc.name

#     frappe.db.commit()
#     return {"status": "success", "device": name,  "app_version": app_version, "build_version": int(build_version or 0)}




















# # @frappe.whitelist()
# # def register_device(
# #     fcm_token,
# #     device_id=None,
# #     platform="android",
# #     app_version=None
# # ):
# #     user = frappe.session.user

# #     employee = frappe.db.get_value(
# #         "Employee",
# #         {"user_id": user},
# #         "name"
# #     )

# #     existing = None

# #     if device_id:
# #         existing = frappe.db.get_value(
# #             "FCM Device",
# #             {
# #                 "user": user,
# #                 "device_id": device_id
# #             },
# #             "name"
# #         )

# #     if not existing:
# #         existing = frappe.db.get_value(
# #             "FCM Device",
# #             {
# #                 "user": user,
# #                 "fcm_token": fcm_token
# #             },
# #             "name"
# #         )

# #     data = {
# #         "user": user,
# #         "employee": employee,
# #         "fcm_token": fcm_token,
# #         "device_id": device_id,
# #         "platform": platform,
# #         "is_active": 1,
# #         "last_seen": frappe.utils.now(),
# #         "app_version": app_version
# #     }

# #     if existing:
# #         frappe.db.set_value(
# #             "FCM Device",
# #             existing,
# #             data
# #         )
# #         name = existing
# #     else:
# #         doc = frappe.get_doc({
# #             "doctype": "FCM Device",
# #             **data
# #         })
# #         doc.insert(ignore_permissions=True)
# #         name = doc.name

# #     frappe.db.commit()

# #     return {
# #         "status": "success",
# #         "device": name
# #     }


    # @frappe.whitelist()
    # def heartbeat(device_id=None):
    #     user = frappe.session.user

    #     filters = {
    #         "user": user,
    #         "is_active": 1
    #     }

    #     if device_id:
    #         filters["device_id"] = device_id

    #     device = frappe.db.get_value(
    #         "FCM Device",
    #         filters,
    #         "name"
    #     )

    #     if device:
    #         frappe.db.set_value(
    #             "FCM Device",
    #             device,
    #             "last_seen",
    #             frappe.utils.now()
    #         )

    #     frappe.db.commit()

    #     return {"status": "success"}


# # @frappe.whitelist()
# # def call_started(doctype, docname):
# #     frappe.db.set_value(
# #         doctype,
# #         docname,
# #         {
# #             "custom_call_status": "Started",
# #             "custom_first_call_started_at": frappe.utils.now()
# #         }
# #     )

# #     frappe.db.commit()

# #     return {"status": "success"}


# # @frappe.whitelist()
# # def call_completed(doctype, docname, status):
# #     frappe.db.set_value(
# #         doctype,
# #         docname,
# #         {
# #             "custom_call_status": status
# #         }
# #     )

# #     frappe.db.commit()

# #     return {"status": "success"}
