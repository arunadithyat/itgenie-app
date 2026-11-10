import frappe
import requests
from frappe import _

@frappe.whitelist(allow_guest=True)
#def get_five_analytics():
    token ="18494e-019d-4d25-ba37-53081ef514ff"  # your Fleetx token
    url = "https://api.fleetx.io/api/v1/analytics/live"
    headers = {
        "Authorization": f"bearer {token}"
    }

    try:
        res = requests.get(url, headers=headers, timeout=10)

        if res.status_code == 200:
            return res.json()
        else:
            frappe.log_error("Fleetx API Error", f"{res.status_code} - {res.text}")
            return {
                "error": "Fleetx API failed",
                "status_code": res.status_code,
                "details": res.text
            }

    except Exception as e:
        frappe.log_error("Fleetx Script Crash", frappe.get_traceback())
        return {
            "error": "Unexpected error occurred",
            "details": str(e)
        }

@frappe.whitelist(allow_guest=True)
#def gehicle_details(vehicle_number):
    token = "7491e58fc5-4ab4-b555-f9cdb3fff802"
    url = "https://api.fleetx.io/api/v1/analytics/live"
    headers = {
        "Authorization": f"bearer {token}"
    }

    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            data = res.json()
            vehicles = data.get("vehicles", [])
            # Match either vehicleName or vehicleNumber
            for vehicle in vehicles:
                if vehicle.get("vehicleName") == vehicle_number or vehicle.get("vehicleNumber") == vehicle_number:
                    return vehicle

            return {
                "error": f"Vehicle '{vehicle_number}' not found"
            }

        else:
            frappe.log_error("Fleetx Vehicle API Error", f"{res.status_code} - {res.text}")
            return {
                "error": "Fleetx API failed",
                "status_code": res.status_code,
                "details": res.text
            }

    except Exception as e:
        frappe.log_error("Fleetx Vehicle Script Crash", frappe.get_traceback())
        return {
            "error": "Unexpected error occurred",
            "details": str(e)
        }

# in itgenie/api/fleetx/generate_tracking_link.py

import frappe
import uuid
from frappe.utils import now_datetime, add_to_date

@frappe.whitelist()
def generate_tracking_link(vehicle):
    tracking_id = str(uuid.uuid4())
    expiry = add_to_date(now_datetime(), hours=1)

    doc = frappe.get_doc({
        "doctype": "Vehicle Tracking Link",
        "vehicle": vehicle,
        "uuid": tracking_id,
        "expiry_time": expiry
    })
    doc.insert(ignore_permissions=True)

    return {
        "link": f"https://erp.cargogenie.in/fleet-vehicle?track={tracking_id}",
        "expiry": expiry
    }

# in itgenie/api/fleetx/validate_tracking.py

@frappe.whitelist(allow_guest=True)
def validate_tracking(uuid):
    try:
        doc = frappe.get_doc("Vehicle Tracking Link", {"uuid": uuid})
    except frappe.DoesNotExistError:
        return {"error": "Tracking ID not found"}

    if now_datetime() > doc.expiry_time:
        return {"error": "This link has expired"}

    return {
        "vehicle": doc.vehicle
    }

# get_eta_from_google.py

@frappe.whitelist(allow_guest=True)
def get_eta(origin_lat, origin_lon, dest_lat, dest_lon):
    api_key = "AIzaSyDwgBZOPiXS-vpyu9raHgJQ8vKrcDGrFH0"
    
    origin = f"{origin_lat},{origin_lon}"
    destination = f"{dest_lat},{dest_lon}"
    url = f"https://maps.googleapis.com/maps/api/directions/json?origin={origin}&destination={destination}&key={api_key}"

    try:
        response = requests.get(url)
        return response.json()
    except Exception as e:
        return {"error": f"Request failed: {str(e)}"}


import requests
import re
import frappe

@frappe.whitelist()
def extract_coordinates(short_url):
    try:
        response = requests.get(short_url, allow_redirects=True, timeout=5)
        final_url = response.url

        # Extract coordinates from '@' URL format
        match = re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', final_url)
        if match:
            lat, lon = match.groups()
            return {"lat": lat, "lon": lon}
        else:
            return None
    except Exception as e:
        frappe.log_error(f"Error in extract_coordinates: {e}", "Extract Coordinates Failed")
        return None


import json
import frappe
import requests

@frappe.whitelist()
def create_fleetx_job(delivery_trip):
    doc = frappe.get_doc("Delivery Trip", delivery_trip)
    url = "https://api.fleetx.io/api/v1/jobs"  # Assuming this is your correct endpoint
    token = "7497b-8fc5-4ab4-b555-f9cdb3fff802"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    payload = {
        "vehicle_id": doc.custom_fleetx_vehicle_id,
        "name": doc.name,
        "departure_time": doc.departure_time.isoformat() if doc.departure_time else None,
        "job_notes": f"ERP Trip: {doc.name}",
        "driver_name": doc.driver_name or ""
    }

    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code == 200:
            return {"success": True, "data": response.json()}
        else:
            # LOG FULL ERROR for debugging
            return {
                "success": False,
                "error": "Fleetx API error",
                "status_code": response.status_code,
                "message": response.text  # <<< KEY LINE
            }
    except Exception as e:
        return {
            "success": False,
            "error": "Exception",
            "message": str(e)
        }
