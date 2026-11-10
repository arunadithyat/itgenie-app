# ============================================================
# GENIE ONE CANTEEN BOOKING API — real app method, not a Server Script
#
# Deploy this as an actual file in your app, e.g.:
#   apps/<your_app>/<your_app>/api/canteen_booking.py
#
# Then run:
#   bench --site <your-site> migrate   (not strictly needed, but safe)
#   bench restart
#
# Requires the razorpay package in your bench's Python environment:
#   bench pip install razorpay
#
# API Method (unchanged from the frontend's point of view):
# /api/method/<your_app>.api.canteen_booking.canteen_booking
#
# IMPORTANT: update the `method` string in canteen_booking.js to the
# dotted path above (it currently just says "canteen_booking", which
# only worked because it matched a Server Script's name).
# ============================================================

import hashlib
import hmac
import json

import frappe
import razorpay


RAZORPAY_KEY_ID = "rzp_test_TZqDpj3nZcBRHs"
RAZORPAY_KEY_SECRET = "YIsN5L0zMvmJWWEhnodNQFA7"


MENU = {
    "Break Fast": {
        "item_code": "Breakfast Combo",
        "item_name": "Breakfast Combo",
        "description": "Poori, Pongal and Kesari",
        "rate": 5,
    },
    "Lunch": {
        "item_code": "Lunch Combo",
        "item_name": "Lunch Combo",
        "description": "Meals and Sambar",
        "rate": 10,
    },
    "Dinner": {
        "item_code": "Dinner Combo",
        "item_name": "Dinner Combo",
        "description": "Chapati and Veg Kuruma",
        "rate": 5,
    },
}


def get_tomorrow():
    return frappe.utils.add_days(frappe.utils.today(), 1)


# ============================================================
# SHARED VALIDATION — used before payment AND again at verification
# so pricing always comes from the server, never the browser.
# ============================================================

def validate_and_price_items(employee, selected_items):
    if not employee:
        frappe.throw("Please choose an employee.")

    if not selected_items:
        frappe.throw("Please choose at least one meal.")

    if isinstance(selected_items, str):
        selected_items = json.loads(selected_items)

    if not isinstance(selected_items, list):
        frappe.throw("Invalid menu item format.")

    if not frappe.db.exists("Employee", {"name": employee, "status": "Active"}):
        frappe.throw("Please choose a valid active employee.")

    if len(selected_items) == 0:
        frappe.throw("Please choose at least one meal.")

    if len(selected_items) > 3:
        frappe.throw("A maximum of three meal types can be booked.")

    tomorrow = get_tomorrow()
    validated_items = []
    selected_meals = []
    total_amount = 0

    for selected_item in selected_items:
        meal_type = str(selected_item.get("meal_type") or "").strip()
        quantity = frappe.utils.cint(selected_item.get("quantity") or 1)

        if meal_type not in MENU:
            frappe.throw(f"Invalid meal type selected: {meal_type}")

        if meal_type in selected_meals:
            frappe.throw(f"{meal_type} was selected more than once.")

        if quantity < 1 or quantity > 10:
            frappe.throw("Quantity must be between 1 and 10.")

        selected_meals.append(meal_type)

        meal = MENU[meal_type]
        item_code = meal["item_code"]
        rate = meal["rate"]
        amount = rate * quantity

        if not frappe.db.exists("Item", item_code):
            frappe.throw(
                f"Item {item_code} does not exist. Please create it in Item master."
            )

        duplicate_booking = frappe.db.sql(
            """
                SELECT
                    CT.name AS canteen_booking
                FROM `tabCanteen` CT
                INNER JOIN `tabSales Order Item` CTI
                    ON CTI.parent = CT.name
                    AND CTI.parenttype = 'Canteen'
                    AND CTI.parentfield = 'canteen_items'
                WHERE
                    CT.employee = %s
                    AND CTI.item_code = %s
                    AND CTI.delivery_date = %s
                LIMIT 1
            """,
            (employee, item_code, tomorrow),
            as_dict=True,
        )

        if duplicate_booking:
            frappe.throw(f"{meal_type} is already booked for this employee for tomorrow.")

        validated_items.append({
            "meal_type": meal_type,
            "item_code": item_code,
            "quantity": quantity,
            "rate": rate,
            "amount": amount,
        })

        total_amount += amount

    if total_amount <= 0:
        frappe.throw("Invalid order amount.")

    return {"items": validated_items, "total_amount": total_amount}


def create_canteen_bookings(employee, validated_items):
    tomorrow = get_tomorrow()
    created_bookings = []

    for item in validated_items:
        meal = MENU[item["meal_type"]]
        item_code = item["item_code"]

        item_details = frappe.db.get_value(
            "Item", item_code, ["item_name", "description", "stock_uom"], as_dict=True
        )

        if not item_details:
            frappe.throw(f"Unable to get details for Item {item_code}")

        canteen_doc = frappe.get_doc({
            "doctype": "Canteen",
            "employee": employee,
            "meal_type": item["meal_type"],
            "paid_amount": item["amount"],
            "canteen_items": [
                {
                    "item_code": item_code,
                    "item_name": item_details.get("item_name") or meal["item_name"],
                    "description": item_details.get("description") or meal["description"],
                    "delivery_date": tomorrow,
                    "qty": item["quantity"],
                    "uom": item_details.get("stock_uom") or "Nos",
                    "conversion_factor": 1,
                    "rate": item["rate"],
                    "amount": item["amount"],
                }
            ],
        })

        canteen_doc.insert()
        created_bookings.append(canteen_doc.name)

    return created_bookings


# ============================================================
# ENTRY POINT
# ============================================================

@frappe.whitelist()
def canteen_booking(
    action=None,
    employee=None,
    items=None,
    razorpay_order_id=None,
    razorpay_payment_id=None,
    razorpay_signature=None,
):
    if action == "list":
        return list_employees()

    if action == "create_payment_order":
        return create_payment_order(employee, items)

    if action == "verify_payment":
        return verify_payment(employee, items, razorpay_order_id, razorpay_payment_id, razorpay_signature)

    frappe.throw("Invalid API action.")


def list_employees():
    tomorrow = get_tomorrow()

    employees = frappe.get_list(
        "Employee",
        filters={"status": "Active", "grade": "Staff"},
        fields=["name", "employee_name"],
        order_by="employee_name asc",
        limit_page_length=0,
    )

    return {"success": True, "date": str(tomorrow), "employees": employees}


def create_payment_order(employee, items):
    priced = validate_and_price_items(employee, items)
    total_amount = priced["total_amount"]
    amount_paise = int(total_amount * 100)

    client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

    receipt = "CANTEEN-" + frappe.generate_hash(length=12)

    razorpay_order = client.order.create({
        "amount": amount_paise,
        "currency": "INR",
        "receipt": receipt,
        "notes": {
            "employee": employee,
            "canteen": "Genie One Canteen",
        },
    })

    employee_name = frappe.db.get_value("Employee", employee, "employee_name")

    return {
        "success": True,
        "key_id": RAZORPAY_KEY_ID,
        "order_id": razorpay_order["id"],
        "amount": amount_paise,
        "currency": "INR",
        "employee_name": employee_name,
    }


def verify_payment(employee, items, razorpay_order_id, razorpay_payment_id, razorpay_signature):
    if not razorpay_order_id:
        return {"success": False, "message": "Razorpay Order ID missing."}

    if not razorpay_payment_id:
        return {"success": False, "message": "Razorpay Payment ID missing."}

    if not razorpay_signature:
        return {"success": False, "message": "Razorpay signature missing."}

    generated_signature = hmac.new(
        RAZORPAY_KEY_SECRET.encode("utf-8"),
        (razorpay_order_id + "|" + razorpay_payment_id).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(generated_signature, razorpay_signature):
        return {"success": False, "message": "Payment verification failed."}

    client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
    payment = client.payment.fetch(razorpay_payment_id)

    if payment.get("order_id") != razorpay_order_id:
        return {"success": False, "message": "Payment order mismatch."}

    if payment.get("status") != "captured":
        return {"success": False, "message": "Payment has not been captured."}

    priced = validate_and_price_items(employee, items)
    validated_items = priced["items"]
    total_amount = priced["total_amount"]
    amount_paise = int(total_amount * 100)

    if payment.get("amount") != amount_paise:
        return {"success": False, "message": "Paid amount does not match the order amount."}

    created_bookings = create_canteen_bookings(employee, validated_items)

    return {
        "success": True,
        "message": "Payment verified. Tomorrow's meals have been booked successfully.",
        "date": str(get_tomorrow()),
        "employee": employee,
        "bookings": created_bookings,
    }
