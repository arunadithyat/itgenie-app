from datetime import datetime
import frappe
import json
import requests
import random
from frappe.utils import today,add_days, flt, nowdate,now
from frappe.utils import now, now_datetime
from frappe.utils import nowdate,getdate
from frappe.core.doctype.user.user import generate_keys

@frappe.whitelist(allow_guest=True, methods=["POST"])
def login(usr, pwd, device_key=None, platform=None, android_id=None):
    # Keep this public method path; implementation is isolated from visits and attendance.
    from itgenie.mobile_auth import login as mobile_login
    return mobile_login(usr, pwd, device_key, platform=platform, android_id=android_id)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def mobile_logout(user, device_key):
    from itgenie.mobile_auth import logout as release_mobile_login
    return release_mobile_login(user, device_key)


@frappe.whitelist(allow_guest = False)
def event_list(user):
    assigned_todos = frappe.get_all("ToDo",
        filters={"status":"Open","reference_type": "Event", "allocated_to": user},
        fields=["reference_name"]
    )

    event_names = [todo["reference_name"] for todo in assigned_todos]

    if not event_names:
        return []

    assigned_events = frappe.get_all("Event",
        filters={"name": ["in", event_names],"status":"Open"},
        fields=["name", "subject", "starts_on", "ends_on","custom_location","event_type", "custom_distance"]
    )
    return assigned_events

@frappe.whitelist(allow_guest=False)
def history_list(user, from_date=None, to_date=None):
    if not user:
        frappe.throw("User is required.")

    filters = {
        "status": "Open",
        "reference_type": "Event",
        "allocated_to": user
    }

    assigned_todos = frappe.get_all("ToDo", filters=filters, fields=["reference_name"])
    event_names = [todo["reference_name"] for todo in assigned_todos]

    if not event_names:
        return []

    event_filters = {
        "name": ["in", event_names],
        "status": "Completed"
    }

    # Apply date filters if provided
    if from_date and to_date:
        event_filters["starts_on"] = ["between", [from_date, to_date]]

    assigned_events = frappe.get_all("Event", filters=event_filters, fields=["*"])
    return assigned_events



@frappe.whitelist(allow_guest = False)
def event_details(id):
    doc = frappe.get_doc("Event",id)
    response = {
    "event": doc.as_dict(),
    "requires_geofence": (doc.get("event_category") or "").strip().lower() not in (
        "installation", "measurement", "other", "others", "bank",
        "head office ch", "timbe / bioman office ch", "collection",
    ),
    "reference_details": []
    }

    for participant in doc.get("event_participants", []):
        reference_doctype = participant.reference_doctype
        reference_docname = participant.reference_docname

        if reference_doctype and reference_docname:
            ref_doc = frappe.get_doc(reference_doctype, reference_docname)
            

            if reference_doctype == "Opportunity":
                if ref_doc.opportunity_from == "Lead":
                    opporutnity_doc = frappe.get_doc(ref_doc.opportunity_from, ref_doc.party_name)
                    response["reference_details"].append({
                        "doctype": reference_doctype,
                        "name": reference_docname,
                        "opportunity_from": ref_doc.opportunity_from,
                        "party_name": opporutnity_doc.first_name,
                        "custom_product_enquired": ref_doc.custom_product_enquired,
                        "contact_mobile": ref_doc.contact_mobile
                    })
                else:
                    response["reference_details"].append({
                        "doctype": reference_doctype,
                        "name": reference_docname,
                        "opportunity_from": ref_doc.opportunity_from,
                        "party_name": ref_doc.party_name,
                        "custom_product_enquired": ref_doc.custom_product_enquired,
                        "contact_mobile": ref_doc.contact_mobile
                    })


    return response

@frappe.whitelist(allow_guest = False)
def send_notification():
    user = "prabhu@gmail.com"
    message = "test"
    frappe.publish_realtime(event="new_notification", message={"msg": message}, user=user)

@frappe.whitelist(allow_guest = False)
def officetype(user):
    employee = frappe.get_doc("Employee",{"user_id":user,"status":"Active"})
    if employee:
        office_type_list = []
        for row in employee.custom_office_type_mapping:
            office_type = frappe.db.get_value("Employee Office Type Mapping", {"name": row.name}, "office_type")
            if office_type:
                office_type_list.append(office_type)
        
        return office_type_list


@frappe.whitelist(allow_guest = False)
def head_office_list():
    head_office_list = frappe.get_all("Homegenie Head Office",pluck = "name")
    return head_office_list

@frappe.whitelist(allow_guest = False)
def head_office_details(head_office):
    head_office = frappe.get_doc("Homegenie Head Office",head_office)
    return head_office

@frappe.whitelist(allow_guest = False)
def checkin(user, lat, lng, address=None, office_type=None, photo_url=None, face_verification=None, include_timings=None):
    # Genie checkin timings v1
    from time import perf_counter

    timing_enabled = include_timings in (True, 1, "1", "true") or frappe.form_dict.get("include_timings") in (True, 1, "1", "true")
    checkin_started = perf_counter()

    def checkin_timed(stage, operation, *args, **kwargs):
        if not timing_enabled:
            return operation(*args, **kwargs)
        started = perf_counter()
        try:
            return operation(*args, **kwargs)
        finally:
            timings = frappe.local.response.setdefault("attendance_timings_ms", {})
            timings[stage] = round((perf_counter() - started) * 1000, 3)

    try:
        employeeid = checkin_timed("checkin_employee_lookup", frappe.db.get_value, "Employee", {"user_id": user, "status": "Active"}, "name")
        if not employeeid:
            frappe.local.response["message"] = "Employee not found or inactive"
            frappe.local.response["http_status_code"] = 404
            return

        existing_checkin = checkin_timed("checkin_existing_in", frappe.db.exists, "Employee Checkin", {
            "employee": employeeid,
            "log_type": "IN",
            "time": ["between", [f"{frappe.utils.nowdate()} 00:00:00", f"{frappe.utils.nowdate()} 23:59:59"]]
        })

        if existing_checkin:
            frappe.local.response["message"] = "Employee already checked in today!"
            frappe.local.response["http_status_code"] = 200
            return

        doc = frappe.new_doc('Employee Checkin')
        doc.employee = employeeid
        doc.log_type = 'IN'
        doc.custom_office_type = office_type
        current_time = now_datetime().strftime("%Y-%m-%d %H:%M:%S")
        doc.time = current_time
        doc.device_id = address
        doc.custom_latitude = lat
        doc.custom_longitude = lng
        doc.custom_created_using_mobile_app = 1
        checkin_timed("checkin_checkin_save", doc.save, ignore_permissions=True)

        visit = frappe.new_doc('Visit')
        visit.latitude = lat
        visit.longitude = lng
        visit.source = address
        visit.event_id = "Employee Checked In"
        visit.employee = employeeid
        visit.date = frappe.utils.nowdate()
        visit.custom_employee_checkin = doc.name
        visit.custom_created_using_mobile_app = 1
        checkin_timed("checkin_visit_save", visit.save, ignore_permissions=True)

        checkin_timed("checkin_commit", frappe.db.commit)
        frappe.local.response["message"] = 'Check-In Created Successfully'
        frappe.local.response["checkin_id"] = doc.name
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(title = f"Error on checkin-{user}",message = frappe.get_traceback())
        frappe.local.response["message"] = "Check In Denied,Check Error Log"
        frappe.local.response["http_status_code"] = 500
    finally:
        if timing_enabled:
            timings = frappe.local.response.setdefault("attendance_timings_ms", {})
            timings["checkin_total"] = round((perf_counter() - checkin_started) * 1000, 3)


@frappe.whitelist(allow_guest = False)
def checkout(user, lat=None, lng=None, address=None,lastCheckoutAddress= None,distance=None, office_type=None,location_logs=None,with_event=None,last_lat_lng=None, photo_url=None, face_verification=None, include_timings=None):
    # Genie checkout performance v1
    from time import perf_counter

    timing_enabled = frappe.form_dict.get("include_timings") in [True, 1, "1", "true"]
    checkout_started = perf_counter()
    route_responses = {}  # Request-local only; never shared between employees.

    def checkout_timed(stage, operation, *args, **kwargs):
        if not timing_enabled:
            return operation(*args, **kwargs)
        started = perf_counter()
        try:
            return operation(*args, **kwargs)
        finally:
            timings = frappe.local.response.setdefault("attendance_timings_ms", {})
            timings[stage] = round(timings.get(stage, 0) + (perf_counter() - started) * 1000, 3)

    def checkout_maps_response(url, headers, payload):
        # Both original branches can request exactly the same driving route.
        # Reuse that response, retaining their existing Google status checks.
        if url not in route_responses:
            try:
                route_responses[url] = checkout_timed(
                    "checkout_google_distance", requests.request,
                    "GET", url, headers=headers, data=payload, timeout=(3.05, 10),
                )
            except requests.exceptions.Timeout:
                # The outer handler rolls back both attendance and receipt use.
                raise RuntimeError("Google distance lookup timed out. Please retry checkout.") from None
        return route_responses[url]

    try:
        employeeid = checkout_timed("checkout_employee_lookup", frappe.db.get_value, "Employee", {"user_id": user, "status": "Active"}, "name")
        if not employeeid:
            frappe.local.response["message"] = "Employee not found or inactive"
            frappe.local.response["http_status_code"] = 404
            return

        # Block checkout if event visit is still in progress
        event_visit_in_progress = checkout_timed("checkout_active_visit", frappe.db.exists, "Visit", {
            "employee": employeeid,
            "event_id": ["!=", ""],
            "custom_check_in_time": ["is", "set"],
            "custom_check_out_time": ["is", "not set"],
        })

        if event_visit_in_progress:
            frappe.local.response["message"] = "You cannot checkout from home while an event is still active. Please check out from the event first."
            frappe.local.response["http_status_code"] = 403
            return

        # Check if already checked out (i.e., last log was OUT today)
        
        
        # Get latest IN
        latest_checkin = checkout_timed("checkout_latest_in", frappe.get_all, "Employee Checkin", filters={
            "employee": employeeid,
            "log_type": "IN"
        }, fields=["name", "time", "custom_office_type"], order_by="time desc", limit=1)

        # Get latest OUT
        latest_checkout = checkout_timed("checkout_latest_out", frappe.get_all, "Employee Checkin", filters={
            "employee": employeeid,
            "log_type": "OUT"
        }, fields=["name", "time"], order_by="time desc", limit=1)

        if latest_checkin:
            in_time = latest_checkin[0]["time"]
            if latest_checkout and latest_checkout[0]["time"] > in_time:
                frappe.local.response["message"] = "Employee Already checked out!"
                frappe.local.response["http_status_code"] = 200
                return

        

        # Get latest office type from check-in
        custom_office_type = (latest_checkin[0].get("custom_office_type") if latest_checkin else None)

        # if office_type and custom_office_type and custom_office_type != office_type:
        #     frappe.local.response["message"] = f"Check-out Office Type mismatch! Checked in at '{custom_office_type}', trying to check out at '{office_type}'."
        #     frappe.local.response["http_status_code"] = 400
        #     return
        # Create Employee Checkin OUT entry
        doc = frappe.new_doc('Employee Checkin')
        doc.employee = employeeid
        doc.log_type = 'OUT'
        doc.custom_office_type = custom_office_type or office_type
        current_time = now_datetime().strftime("%Y-%m-%d %H:%M:%S")
        doc.time = current_time
        doc.device_id = address
        doc.custom_latitude = lat
        doc.custom_longitude = lng
        doc.custom_created_using_mobile_app = 1
        checkout_timed("checkout_checkin_save", doc.save, ignore_permissions=True)

        # Create Visit log for checkout
        visit = frappe.new_doc('Visit')
        
        visit.destination_lat = lat
        visit.destination_lng = lng
        visit.custom_created_using_mobile_app = 1

        previous_visit = checkout_timed("checkout_previous_visit", frappe.db.sql, """
            SELECT event_id, creation, latitude, longitude, destination_lat, destination_lng, source, destination
            FROM `tabVisit`
            WHERE employee = %(emp)s
            ORDER BY creation DESC
            LIMIT 1
        """, {"user": user,"emp": employeeid}, as_dict=True)
        
        if len(previous_visit) == 0:
            frappe.throw('No Visit Records Found For this User')
            
        prev = previous_visit[0]

        if prev['event_id'] != "Employee Checked In" and prev['event_id'] !="Employee Checked Out":

            # if last_lat_lng:
                
            #     lat_str, lng_str = last_lat_lng.split(',') if ',' in last_lat_lng else (None, None)

            # else:
                
            if prev:
                if prev.event_id == "Employee Checked In":
                    lat_str = prev.get("latitude")
                    lng_str = prev.get("longitude")
                else:
                    lat_str = prev.get("destination_lat")
                    lng_str = prev.get("destination_lng")

            visit.source = prev['destination']
            
            if not lat_str and not lng_str:
                frappe.throw('In Previous Visit latitude and longitude Missing')
                
            if not lat and not lng:
                frappe.throw('Current latitude and longitude missing. Please remove the app from recent tabs and reopen it.')

            origin = f"{lat_str},{lng_str}"
            destination = f"{lat},{lng}"
            
            api_key = "AIzaSyCEkjoOnXRf6c9Xzml2QPNvZoq2LuG06JI"
            visit.custom_from_google_api = 1
            url = (
                    f"https://maps.googleapis.com/maps/api/distancematrix/json"
                    f"?origins={origin}&destinations={destination}&key={api_key}&mode=driving"
                )
            payload = {}
            headers = {}

            if getdate(prev['creation']) == getdate(nowdate()):
                response = checkout_maps_response(url, headers, payload)
                data = json.loads(response.text)
                if data['status'] == 'OK':
                    if data['rows'][0]['elements'][0]['status'] == 'OK':
                        visit.custom_distance = data['rows'][0]['elements'][0]['distance']['text']
                    else:
                        frappe.log_error(title='Google Api Error',message = data)
                        frappe.throw('Google Api Error - Check Error Log')
                else:
                    frappe.log_error(title='Google Api Error',message = data)
                    frappe.throw('Google Api Error - Check Error Log')

            visit.latitude = prev['destination_lat']

            visit.longitude = prev['destination_lng']
        
        elif prev['event_id'] == "Employee Checked In":

            visit.latitude = prev['latitude']

            visit.longitude = prev['longitude']

            visit.source = prev['source']
            
        elif prev['event_id'] == "Employee Checked Out":
            
            frappe.throw("Your previous visit type is invalid (must be Employee Check-In Visit or Event Visit), so you are not allowed to check out.")
            


     
        # logs_str = ""
        # if location_logs:
        #     try:
        #         if isinstance(location_logs, str):
        #             location_logs = json.loads(location_logs)
        #         logs_str = json.dumps(location_logs)
        #     except Exception as e:
        #         logs_str = "[]"
        #         frappe.log_error(title = f"Error parsing location logs",message = frappe.get_traceback())

        # Combine address + newline + logs into destination
        # visit.destination = f"{address}\n{logs_str}"
        visit.destination = address
        visit.event_id = "Employee Checked Out"
        visit.employee = employeeid
        # visit.custom_distance = distance
        visit.date = frappe.utils.nowdate()
        visit.custom_employee_checkin = doc.name
        if with_event == "true" and getdate(prev['creation']) == getdate(nowdate()):

            # lat_str, lng_str = last_lat_lng.split(',') if last_lat_lng and ',' in last_lat_lng else (None, None)

            origin = f"{visit.latitude},{visit.longitude}"
            destination = f"{lat},{lng}"
            
            api_key = "AIzaSyCEkjoOnXRf6c9Xzml2QPNvZoq2LuG06JI"
            visit.custom_from_google_api = 1
            url = (
                    f"https://maps.googleapis.com/maps/api/distancematrix/json"
                    f"?origins={origin}&destinations={destination}&key={api_key}&mode=driving"
                )
            payload = {}
            headers = {}

            response = checkout_maps_response(url, headers, payload)

            data = json.loads(response.text)
            
            if data['status'] == 'OK':
                if data['rows'][0]['elements'][0]['status'] == 'OK':
                    visit.custom_distance = data['rows'][0]['elements'][0]['distance']['text']
                else:
                    frappe.log_error(title='Google Api Error',message = data)
                    frappe.throw('Google Api Error - Check Error Log')
            else:
                frappe.log_error(title='Google Api Error',message = data)
                frappe.throw('Google Api Error - Check Error Log')


        checkout_timed("checkout_visit_save", visit.save, ignore_permissions=True)

        checkout_timed("checkout_commit", frappe.db.commit)
        frappe.local.response["message"] = 'Check-Out Created Successfully'
        frappe.local.response["http_status_code"] = 200

    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(title = f"Error on checkout-{user}",message = frappe.get_traceback())
        frappe.local.response["message"] = (str(e) if isinstance(e, frappe.ValidationError) or str(e) == "Google distance lookup timed out. Please retry checkout." else "Unable to complete the request. Please try again.")
        frappe.local.response["http_status_code"] = 500
    finally:
        if timing_enabled:
            timings = frappe.local.response.setdefault("attendance_timings_ms", {})
            timings["checkout_total"] = round((perf_counter() - checkout_started) * 1000, 3)


@frappe.whitelist(allow_guest = False)
def checkin_status(user):
    employeeid = frappe.db.get_value("Employee", {"user_id": user, "status": "Active"}, "name")
    if not employeeid:
        return False

    # Check if there's a check-in log today
    checkin_exists = frappe.db.exists("Employee Checkin", {
        "employee": employeeid,
        "log_type": "IN",
        "time": ["between", [f"{frappe.utils.nowdate()} 00:00:00", f"{frappe.utils.nowdate()} 23:59:59"]]
    })

    return bool(checkin_exists)

@frappe.whitelist(allow_guest = False)
def checkout_status(user):
    employeeid = frappe.db.get_value("Employee", {"user_id": user, "status": "Active"}, "name")
    if not employeeid:
        return False

    # Check if there's a checkout log today
    checkout_exists = frappe.db.exists("Employee Checkin", {
        "employee": employeeid,
        "log_type": "OUT",
        "time": ["between", [f"{frappe.utils.nowdate()} 00:00:00", f"{frappe.utils.nowdate()} 23:59:59"]]
    })
    
    existing_checkin = frappe.db.exists("Employee Checkin", {
            "employee": employeeid,
            "log_type": "IN",
            "time": ["between", [f"{frappe.utils.nowdate()} 00:00:00", f"{frappe.utils.nowdate()} 23:59:59"]]
        })
        
    if existing_checkin and checkout_exists:
        check_out = frappe.get_doc("Employee Checkin", checkout_exists)
        check_in = frappe.get_doc("Employee Checkin", existing_checkin)
        if check_out.time > check_in.time:
            frappe.local.response["message"] = "Employee already checked Exist!"
            frappe.local.response["http_status_code"] = 200
            return False

    return bool(checkout_exists)


@frappe.whitelist(allow_guest = False)
def event_list_date(user,date):
    event_list = []
    doc_list = frappe.db.get_all("ToDo",filters={'allocated_to':user,"reference_type":"Event"},pluck="reference_name")
    for doc in doc_list:
        doc = frappe.get_doc("Event",doc)
        if date == str(getdate(doc.starts_on)) and doc.status == "Open":
            event_list.append(doc.as_dict())

    return event_list


@frappe.whitelist(allow_guest = False)
def event_checkin(user, event_id, lat=None, lng=None, address= None):
    try:
        employeeid = frappe.db.get_value("Employee", {"user_id": user, "status": "Active"}, "name")

        from frappe.utils.safe_exec import call_whitelisted_function
        decision = call_whitelisted_function("mobile_business_policy", feature="visit", action="get", event_id=event_id)["policy"]
        action = decision["actions"]["check_in"]
        if user != frappe.session.user or not action["allowed"]:
            return {"status": "error", "message": action["reason"] or "This action is unavailable for your user."}
        doc = frappe.get_doc("Event", event_id)

        current_time = now_datetime().strftime("%Y-%m-%d %H:%M:%S")
        doc.custom_location = address
        doc.custom_check_in = current_time

        
        doc.save(ignore_permissions=True)

        visit = frappe.new_doc('Visit')
        visit.custom_created_using_mobile_app = 1
        visit.event_id = event_id
        if lat and lng:
            visit.latitude = lat
            visit.longitude = lng
        visit.source = address
        visit.employee = employeeid
        visit.custom_check_in_time = current_time
        visit.date = frappe.utils.nowdate()
        visit.save(ignore_permissions=True)



        return {"status": "success", "message": "Check-In successful", "check_in_time": current_time}

    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(title = f"Error on Event checkin-{user}",message = frappe.get_traceback())
        return {"status": "error", "message": (str(e) if isinstance(e, frappe.ValidationError) else "Unable to complete the request. Please try again.")}


@frappe.whitelist(allow_guest = False)
def event_checkout(user, event_id, lat=None, lng=None , address= None,working_hrs = None,distance = None,last_lat_lng = None,location_logs=None, custom_otp=None):
    try:


        from frappe.utils.safe_exec import call_whitelisted_function
        decision = call_whitelisted_function("mobile_business_policy", feature="visit", action="get", event_id=event_id)["policy"]
        action = decision["actions"]["check_out"]
        if user != frappe.session.user or not action["allowed"]:
            return {"status": "error", "message": action["reason"] or "This action is unavailable for your user."}
        doc = frappe.get_doc("Event", event_id)
        
        if not doc.custom_check_in:
            return {"status": "error", "message": "Cannot check out before check-in"}

        if doc.custom_check_out:
            return {"status": "error", "message": "Already Checked Out"}

        # OTP is no longer required. Keep the optional argument for older
        # callers. Temporarily allow checkout without a photo; uploads remain available.
        current_time = now_datetime().strftime("%Y-%m-%d %H:%M:%S")

        existing_visit_record = frappe.db.exists("Visit", {
            "event_id": event_id,            
        })

        if existing_visit_record:
            visit = frappe.get_doc("Visit",existing_visit_record)

            if last_lat_lng:
                lat_str, lng_str = last_lat_lng.split(',') if ',' in last_lat_lng else (None, None)

            else:
                previous_visit = frappe.db.sql("""
                SELECT *
                FROM `tabVisit`
                WHERE owner = %(user)s AND event_id != %(event_id)s
                ORDER BY creation DESC
                LIMIT 1
            """, {"user": user, "event_id": event_id}, as_dict=True)

                if previous_visit:
                    prev = previous_visit[0]
                    if prev.event_id == "Employee Checked In":
                        lat_str = prev.get("latitude")
                        lng_str = prev.get("longitude")
                    else:
                        lat_str = prev.get("destination_lat")
                        lng_str = prev.get("destination_lng")


            if lat and lng:
                visit.destination_lat = lat
                visit.destination_lng = lng
            visit.destination = address
            logs_str = ""
            if location_logs:
                try:
                    if isinstance(location_logs, str):
                        logs_str = location_logs
                        location_logs = json.loads(location_logs)
                    else:
                        logs_str = json.dumps(location_logs)
                except Exception as e:
                    logs_str = "[]"
                    frappe.log_error(f"Error parsing location logs: {str(e)}")

            # Combine address + newline + logs into destination
            # visit.destination = f"{address}\n{logs_str}"
            
            visit.custom_check_out_time = current_time
            if visit.latitude and visit.longitude and lat and lng:
                origin = f"{lat_str},{lng_str}"
                destination = f"{lat},{lng}"
                
                api_key = "AIzaSyCEkjoOnXRf6c9Xzml2QPNvZoq2LuG06JI"
                visit.custom_from_google_api = 1

                # url = f"https://maps.googleapis.com/maps/api/distancematrix/json?origins={origin}&destinations={destination}&key=AIzaSyCEkjoOnXRf6c9Xzml2QPNvZoq2LuG06JI&mode=driving"

                url = (
                    f"https://maps.googleapis.com/maps/api/distancematrix/json"
                    f"?origins={origin}&destinations={destination}&key={api_key}&mode=driving"
                )
                payload = {}
                headers = {}

                response = requests.request("GET", url, headers=headers, data=payload)

                data = json.loads(response.text)
                
                employeeid = frappe.db.get_value("Employee", {"user_id": user, "status": "Active"}, "name")
                if not employeeid:
                    frappe.local.response["message"] = "Employee not found or inactive"
                    frappe.local.response["http_status_code"] = 404
                    return
                
                visit_checkon = visit.custom_check_in_time.strftime("%Y-%m-%d")
                    
                current_date = now_datetime().strftime("%Y-%m-%d")
                if visit_checkon == current_date:
                    visit.custom_distance = data['rows'][0]['elements'][0]['distance']['text']

                    doc.custom_distance = data['rows'][0]['elements'][0]['distance']['text']



            visit.custom_created_using_mobile_app = 1
            visit.save(ignore_permissions=True)



        doc.custom_check_out = current_time
        doc.custom_check_out_location = address
        doc.custom_duration = working_hrs
        doc.ends_on = current_time
        doc.status = "Completed"

        
        doc.save(ignore_permissions=True)
        # frappe.db.commit()

        return {"status": "success", "message": "Check-Out successful", "check_out_time": current_time}

    except Exception as e:
        
        frappe.db.rollback()
        frappe.log_error(title = f"Error on Event checkout-{user}",message = frappe.get_traceback())
        return {"status": "error", "message": (str(e) if isinstance(e, frappe.ValidationError) else "Unable to complete the request. Please try again.")}

@frappe.whitelist(methods=["POST"])
def generate_keys(user: str):
        user_details: User = frappe.get_doc("User", user)
        api_secret = frappe.generate_hash(length=15)
        # if api key is not set generate api key
        if not user_details.api_key:
                api_key = frappe.generate_hash(length=15)
                user_details.api_key = api_key
        user_details.api_secret = api_secret
        user_details.save(ignore_permissions = True)

        return {"api_secret": api_secret}


# @frappe.whitelist()
# def test():
#     url = "https://maps.googleapis.com/maps/api/distancematrix/json?origins=11.0226671,76.9986953&destinations=11.0226688,76.9986842&key=AIzaSyCEkjoOnXRf6c9Xzml2QPNvZoq2LuG06JI&mode=driving"

#     payload = {}
#     headers = {}

#     response = requests.request("GET", url, headers=headers, data=payload)
#     data = json.loads(response.text)
#     print(data['rows'][0]['elements'][0]['distance']['text'])
#     # print(response.text['destination_addresses'])


@frappe.whitelist(allow_guest = False)
def send_otp(event_id):
    try:
        event = frappe.get_doc("Event", event_id)
        otp = random.randint(1000, 9999)
        event.custom_otp = otp
        event.save(ignore_permissions = True)
        # frappe.db.set_value("Event",event_id,'custom_otp',otp)
        frappe.db.commit()
        frappe.msgprint(f"OTP sent: {otp}")
        return {"status": "success", "message": "OTP sent"}
    
    except frappe.DoesNotExistError:
        return {"status": "error", "message": "Event not found"}
    except Exception as e:
        frappe.log_error( title = f"OTP Verification Failed",message = frappe.get_traceback())
        return {"status": "error", "message": "Unable to complete the request. Please try again."}
    
@frappe.whitelist(allow_guest = False)
def verify_otp(otp,event_id):
    if not otp or not event_id:
        return {"status": "error", "message": "Missing OTP or Event ID"}

    if len(str(otp).strip()) != 4 or str(otp).strip().strip("0123456789"):
        return {"status": "error", "message": "OTP must contain exactly 4 digits."}

    try:
        event = frappe.get_doc("Event", event_id)

        if not event.custom_otp:
            return {"status": "error", "message": "OTP not set for this event"}
        
        if str(event.custom_otp).strip() == str(otp).strip():
            return {"status": "success", "message": "OTP verified"}
        else:
            return {"status": "error", "message": "Invalid OTP"}
        
    except frappe.DoesNotExistError:
        return {"status": "error", "message": "Event not found"}
    except Exception as e:
        frappe.log_error(title = f"OTP Verification Failed",message = frappe.get_traceback())
        return {"status": "error", "message": "Unable to complete the request. Please try again."}


@frappe.whitelist()
def get_checkin_checkout_status(user):
    from frappe.utils import getdate, nowdate

    employeeid = frappe.db.get_value("Employee", {"user_id": user, "status": "Active"}, "name")
    if not employeeid:
        return {
            "checked_in": False,
            "checked_out": False,
            "previous_checkout_pending": False,
            "last_checkin_date": None
        }

    today = nowdate()

    # ✅ Get today's check-in
    today_checkin_time = frappe.db.get_value(
        "Employee Checkin",
        filters={
            "employee": employeeid,
            "log_type": "IN",
            "time": ["between", [f"{today} 00:00:00", f"{today} 23:59:59"]]
        },
        fieldname="time"
    )

    # ✅ Get today's checkout only if there's an IN
    today_checkout_time = None
    if today_checkin_time:
        today_checkout_time = frappe.db.get_value(
            "Employee Checkin",
            filters={
                "employee": employeeid,
                "log_type": "OUT",
                "time": ["between", [f"{today} 00:00:00", f"{today} 23:59:59"]]
            },
            fieldname="time"
        )

    # ✅ Compute checked_out only if OUT is after today's IN
    checked_out = False
    if today_checkin_time and today_checkout_time:
        checked_out = today_checkout_time > today_checkin_time

    # ✅ Now: find most recent IN BEFORE today
    previous_in_time = frappe.db.get_value(
        "Employee Checkin",
        filters={
            "employee": employeeid,
            "log_type": "IN",
            "time": ["<", f"{today} 00:00:00"]
        },
        fieldname="time",
        order_by="time desc"
    )

    previous_checkout_pending = False
    if previous_in_time:
        # Was it closed?
        checkout_after_previous = frappe.db.exists("Employee Checkin", {
            "employee": employeeid,
            "log_type": "OUT",
            "time": [">", previous_in_time]
        })
        previous_checkout_pending = not checkout_after_previous

    # ✅ Get last IN for context
    last_checkin_time = frappe.db.get_value(
        "Employee Checkin",
        filters={"employee": employeeid, "log_type": "IN"},
        fieldname="time",
        order_by="time desc"
    )

    return {
        "checked_in": bool(today_checkin_time),
        "checked_out": checked_out,
        "previous_checkout_pending": previous_checkout_pending,
        "last_checkin_date": str(getdate(last_checkin_time)) if last_checkin_time else None
    }


@frappe.whitelist()
def is_otp_required(event_id):
    try:
        reference_doctypes = ["Opportunity", "Customer", "Installation Note"]
        references = frappe.get_all(
            "Event Participants",
            filters={
                "parent": event_id,
                "parenttype": "Event",
                "reference_doctype": ["in", reference_doctypes]
            },
            fields=["name"]
        )

        return False
    except Exception as e:
        frappe.log_error(title = "OTP Required",message = frappe.get_traceback())
