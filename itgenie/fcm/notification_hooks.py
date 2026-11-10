"""Notification override AND registration, shared by Frappe v15 and v16.

The controller is loaded lazily: hooks.py imports this module while Frappe is
still collecting hooks, so reading get_hooks() at module import would recurse.
"""


def configure_notification_hooks(hooks):
    overrides = hooks.setdefault("override_doctype_class", {})
    previous = overrides.get("Notification", [])
    previous = [previous] if isinstance(previous, str) else list(previous)
    own = "itgenie.fcm.notification_hooks.FCMNotification"
    obsolete = {
        "itgenie.fcm.notification_override.FCMNotification",
        "itgenie.fcm.notification_v15.FCMNotification",
    }
    # Keep other overrides available to the shared controller's base resolver.
    overrides["Notification"] = [path for path in previous if path != own and path not in obsolete] + [own]

    # Remove only OUR previous v16 registration if this hooks.py still has it.
    # Other apps' extensions and all other DocType entries are preserved.
    extensions = hooks.get("extend_doctype_class", {})
    previous = extensions.get("Notification", [])
    previous = [previous] if isinstance(previous, str) else list(previous)
    old_mixin = "itgenie.fcm.fcm_utils.FCMNotificationMixin"
    if old_mixin in previous:
        remaining = [path for path in previous if path != old_mixin]
        if remaining:
            extensions["Notification"] = remaining
        else:
            extensions.pop("Notification", None)


def _build_notification_controller():
    import frappe
    from frappe.email.doctype.notification.notification import Notification
    from .fcm_utils import FCMNotificationMixin

    own_paths = {
        "itgenie.fcm.notification_hooks.FCMNotification",
        "itgenie.fcm.notification_override.FCMNotification",
        "itgenie.fcm.notification_v15.FCMNotification",
    }
    overrides = frappe.get_hooks("override_doctype_class", {}).get("Notification", [])
    overrides = [overrides] if isinstance(overrides, str) else overrides
    base = Notification
    for path in reversed(overrides):
        if path in own_paths:
            continue
        base = frappe.get_attr(path)
        if not isinstance(base, type) or not issubclass(base, Notification):
            raise TypeError("The existing Notification override must inherit Frappe Notification")
        break
    return type("FCMNotification", (FCMNotificationMixin, base), {
        "__module__": __name__,
        "__doc__": "Handle FCM while preserving the existing implementation of other channels.",
    })


def __getattr__(name):
    if name != "FCMNotification":
        raise AttributeError("module " + __name__ + " has no attribute " + name)
    controller = _build_notification_controller()
    globals()[name] = controller
    return controller
