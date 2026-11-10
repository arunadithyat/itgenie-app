import re

import frappe
from frappe.core.doctype.user.user import (
    update_password as frappe_update_password,
)


def validate_password(password):
    if not isinstance(password, str):
        frappe.throw("Please enter a password.")

    errors = []

    if len(password) < 8:
        errors.append("Password must contain at least 8 characters.")

    if not re.search(r"[A-Z]", password):
        errors.append("Include at least one uppercase letter (A-Z).")

    if not re.search(r"[a-z]", password):
        errors.append("Include at least one lowercase letter (a-z).")

    if not re.search(r"[0-9]", password):
        errors.append("Include at least one number (0-9).")

    special_chars = "!@#$%^&*()-_=+[]{};:,.?/|~"

    if not any(character in special_chars for character in password):
        errors.append("Include at least one special character, such as @, # or !.")

    if any(character.isspace() for character in password):
        errors.append("Password must not contain spaces or other whitespace.")

    # Reject any three consecutive digits:
    # 123, 234, 456, 789, 321, 654, etc.
    sequences = ("0123456789012", "9876543210987")

    blocked_sequences = {
        sequence[index:index + 3]
        for sequence in sequences
        for index in range(len(sequence) - 2)
    }

    if any(sequence in password for sequence in blocked_sequences):
        errors.append(
            "Password must not contain consecutive numbers "
            "such as 123, 456, 789 or 321."
        )

    if errors:
        frappe.throw(
            "<br>".join(errors),
            title="Password requirements",
        )


def validate_user_password(doc, method=None):
    """Validate a new password entered on the User form."""
    password = doc.get("new_password")

    if password:
        validate_password(password)


@frappe.whitelist(allow_guest=True)
def update_password(
    new_password,
    logout_all_sessions=0,
    key=None,
    old_password=None,
):
    """Validate first, then use Frappe's standard password-update process."""
    validate_password(new_password)

    return frappe_update_password(
        new_password=new_password,
        logout_all_sessions=logout_all_sessions,
        key=key,
        old_password=old_password,
    )
