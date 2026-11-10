
import re
from datetime import datetime

import frappe
import requests

from google.oauth2 import service_account
from google.auth.transport.requests import Request


# ==================================================
# CONFIGURATION
# ==================================================

SERVICE_ACCOUNT_FILE = (
    "/home/frappe/google-analytics-service-account.json"
)

SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly"
]

ALLOWED_ROLES = {
    "System Manager",
    "Website Manager",
    "Trainee"
}

ADMIN_API = (
    "https://analyticsadmin.googleapis.com/v1beta/"
    "accountSummaries"
)

DATA_API = (
    "https://analyticsdata.googleapis.com/v1beta/"
    "properties/{property_id}:runReport"
)

PAGE_LIMIT = 10000
MAX_REPORT_ROWS = 100000


# ==================================================
# PERMISSIONS
# ==================================================

def check_analytics_permission():
    user = frappe.session.user

    if user == "Guest":
        frappe.throw(
            "Authentication required",
            frappe.PermissionError
        )

    roles = set(frappe.get_roles(user))

    if not roles.intersection(ALLOWED_ROLES):
        frappe.throw(
            "You do not have permission to access "
            "Google Analytics data.",
            frappe.PermissionError
        )


# ==================================================
# GOOGLE SERVICE ACCOUNT AUTHENTICATION
# ==================================================

def get_google_credentials():
    credentials = (
        service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE,
            scopes=SCOPES
        )
    )

    credentials.refresh(Request())

    return credentials


def _google_headers(credentials):
    if not credentials.valid:
        credentials.refresh(Request())

    return {
        "Authorization": "Bearer " + credentials.token,
        "Content-Type": "application/json"
    }


@frappe.whitelist()
def test_google_analytics_token():
    check_analytics_permission()

    credentials = get_google_credentials()

    return {
        "status": "success",
        "authenticated": credentials.valid,
        "token_expiry": str(credentials.expiry)
    }


# ==================================================
# DATE VALIDATION
# ==================================================

def _validate_date(value):
    if value in ("today", "yesterday"):
        return value

    if re.fullmatch(r"\d+daysAgo", value):
        return value

    try:
        datetime.strptime(value, "%Y-%m-%d")
        return value
    except ValueError:
        frappe.throw("Invalid date: " + value)


def _get_dates(from_date=None, to_date=None):
    from_date = _validate_date(
        str(from_date or "30daysAgo")
    )

    to_date = _validate_date(
        str(to_date or "today")
    )

    return from_date, to_date


# ==================================================
# PROPERTY DISCOVERY
# ==================================================

def _fetch_all_properties(credentials):
    properties = []
    page_token = None

    while True:
        params = {"pageSize": 200}

        if page_token:
            params["pageToken"] = page_token

        response = requests.get(
            ADMIN_API,
            headers=_google_headers(credentials),
            params=params,
            timeout=45
        )

        response.raise_for_status()
        data = response.json()

        for account in data.get(
            "accountSummaries", []
        ):
            for prop in account.get(
                "propertySummaries", []
            ):
                properties.append({
                    "account_name": account.get(
                        "displayName", ""
                    ),
                    "account_id": account.get(
                        "account", ""
                    ).split("/")[-1],
                    "property_name": prop.get(
                        "displayName", ""
                    ),
                    "property_id": prop.get(
                        "property", ""
                    ).split("/")[-1]
                })

        page_token = data.get("nextPageToken")

        if not page_token:
            break

    return properties


@frappe.whitelist()
def get_all_properties():
    check_analytics_permission()

    credentials = get_google_credentials()
    properties = _fetch_all_properties(credentials)

    return {
        "status": "success",
        "total_properties": len(properties),
        "properties": properties
    }


# ==================================================
# GOOGLE ANALYTICS REPORT
# ==================================================

def _ga_run_report(credentials, property_id, body):
    url = DATA_API.format(
        property_id=property_id
    )

    all_rows = []
    offset = 0

    while True:
        payload = dict(body)
        payload["limit"] = PAGE_LIMIT
        payload["offset"] = offset

        response = requests.post(
            url,
            headers=_google_headers(credentials),
            json=payload,
            timeout=60
        )

        response.raise_for_status()

        data = response.json()
        rows = data.get("rows", [])

        all_rows.extend(rows)
        offset += len(rows)

        total_rows = int(
            data.get("rowCount", 0)
        )

        if not rows or offset >= total_rows:
            break

        if offset >= MAX_REPORT_ROWS:
            raise ValueError(
                "Report exceeded maximum supported rows"
            )

    return all_rows


# ==================================================
# REPORT DEFINITIONS
# ==================================================

def _date_range(from_date, to_date):
    return [{
        "startDate": from_date,
        "endDate": to_date
    }]


def _traffic_report_body(from_date, to_date):
    return {
        "dateRanges": _date_range(
            from_date, to_date
        ),
        "dimensions": [
            {"name": "hostName"},
            {"name": "pagePath"},
            {"name": "pageTitle"}
        ],
        "metrics": [
            {"name": "screenPageViews"},
            {"name": "activeUsers"},
            {"name": "sessions"}
        ]
    }


def _click_report_body(from_date, to_date):
    return {
        "dateRanges": _date_range(
            from_date, to_date
        ),
        "dimensions": [
            {"name": "hostName"},
            {"name": "pagePath"}
        ],
        "metrics": [
            {"name": "eventCount"}
        ],
        "dimensionFilter": {
            "filter": {
                "fieldName": "eventName",
                "stringFilter": {
                    "matchType": "EXACT",
                    "value": "click"
                }
            }
        }
    }


def _property_totals_body(from_date, to_date):
    return {
        "dateRanges": _date_range(
            from_date, to_date
        ),
        "metrics": [
            {"name": "screenPageViews"},
            {"name": "activeUsers"},
            {"name": "sessions"},
            {"name": "eventCount"}
        ]
    }


def _advertising_cost_body(from_date, to_date):
    """
    GA4 advertising cost grouped by session campaign.

    This is GA4 advertiserAdCost, not guaranteed
    to equal Google Ads billing expenditure.
    """
    return {
        "dateRanges": _date_range(
            from_date, to_date
        ),
        "dimensions": [
            {"name": "sessionCampaignName"}
        ],
        "metrics": [
            {"name": "advertiserAdCost"},
            {"name": "advertiserAdClicks"}
        ]
    }


# ==================================================
# RESPONSE HELPERS
# ==================================================

def _dimension(row, index):
    values = row.get("dimensionValues", [])

    if index < len(values):
        return values[index].get("value", "")

    return ""


def _metric(row, index):
    values = row.get("metricValues", [])

    if index < len(values):
        return values[index].get("value", "0")

    return "0"


def _number(value):
    return int(float(value or 0))


def _get_click_lookup(rows):
    lookup = {}

    for row in rows:
        key = (
            _dimension(row, 0),
            _dimension(row, 1)
        )

        lookup[key] = (
            lookup.get(key, 0)
            + _number(_metric(row, 0))
        )

    return lookup


def _google_error(exc):
    """
    Return Google's actual error message instead
    of only '400 Client Error'.
    """
    if isinstance(exc, requests.HTTPError):
        response = exc.response

        if response is not None:
            try:
                data = response.json()
                return data.get(
                    "error", {}
                ).get("message", str(exc))
            except ValueError:
                pass

    return str(exc)


# ==================================================
# ALL WEBSITE ANALYTICS
# ==================================================

@frappe.whitelist()
def get_all_websites(
    from_date=None,
    to_date=None,
    include_ad_cost=1
):
    check_analytics_permission()

    from_date, to_date = _get_dates(
        from_date, to_date
    )

    include_ad_cost = str(
        include_ad_cost
    ).lower() in ("1", "true", "yes")

    credentials = get_google_credentials()

    properties = _fetch_all_properties(
        credentials
    )

    websites = []
    errors = []

    for prop in properties:
        property_id = prop["property_id"]

        try:
            # ----------------------------------
            # PAGE TRAFFIC
            # ----------------------------------

            traffic_rows = _ga_run_report(
                credentials,
                property_id,
                _traffic_report_body(
                    from_date, to_date
                )
            )

            # ----------------------------------
            # CLICK EVENTS
            # ----------------------------------

            click_rows = _ga_run_report(
                credentials,
                property_id,
                _click_report_body(
                    from_date, to_date
                )
            )

            click_lookup = _get_click_lookup(
                click_rows
            )

            # ----------------------------------
            # PROPERTY TOTALS
            # ----------------------------------

            total_rows = _ga_run_report(
                credentials,
                property_id,
                _property_totals_body(
                    from_date, to_date
                )
            )

            property_totals = {
                "page_views": 0,
                "active_users": 0,
                "sessions": 0,
                "all_events": 0,
                "click_events": sum(
                    click_lookup.values()
                )
            }

            if total_rows:
                total_row = total_rows[0]

                property_totals.update({
                    "page_views": _number(
                        _metric(total_row, 0)
                    ),
                    "active_users": _number(
                        _metric(total_row, 1)
                    ),
                    "sessions": _number(
                        _metric(total_row, 2)
                    ),
                    "all_events": _number(
                        _metric(total_row, 3)
                    )
                })

            # ----------------------------------
            # INDIVIDUAL WEBSITE PAGES
            # ----------------------------------

            pages = []

            for row in traffic_rows:
                host = _dimension(row, 0)
                path = _dimension(row, 1)
                title = _dimension(row, 2)

                pages.append({
                    "hostname": host,
                    "page_path": path,
                    "page_title": title,
                    "page_views": _number(
                        _metric(row, 0)
                    ),
                    "active_users": _number(
                        _metric(row, 1)
                    ),
                    "sessions": _number(
                        _metric(row, 2)
                    ),
                    "click_events": click_lookup.get(
                        (host, path), 0
                    )
                })

            # ----------------------------------
            # PROPERTY RESPONSE
            # ----------------------------------

            result = {
                "account_name": prop["account_name"],
                "account_id": prop["account_id"],
                "property_name": prop["property_name"],
                "property_id": property_id,
                "totals": property_totals,
                "total_pages": len(pages),
                "pages": pages
            }

            # ----------------------------------
            # ADVERTISING COST
            # ----------------------------------

            if include_ad_cost:
                try:
                    cost_rows = _ga_run_report(
                        credentials,
                        property_id,
                        _advertising_cost_body(
                            from_date, to_date
                        )
                    )

                    total_cost = 0.0
                    total_ad_clicks = 0
                    campaign_costs = []

                    for row in cost_rows:
                        campaign_name = _dimension(
                            row, 0
                        )

                        campaign_spend = float(
                            _metric(row, 0)
                        )

                        campaign_clicks = _number(
                            _metric(row, 1)
                        )

                        total_cost += campaign_spend
                        total_ad_clicks += campaign_clicks

                        campaign_costs.append({
                            "campaign_name": campaign_name,
                            "spend": round(campaign_spend, 2),
                            "ad_clicks": campaign_clicks
                        })

                    result[
                        "ga4_advertiser_ad_cost"
                    ] = round(total_cost, 2)

                    result[
                        "ga4_advertiser_ad_clicks"
                    ] = total_ad_clicks

                    result[
                        "campaign_costs"
                    ] = campaign_costs

                    result[
                        "ad_cost_source"
                    ] = "GA4 advertiserAdCost / advertiserAdClicks"

                except (
                    requests.RequestException,
                    ValueError
                ) as exc:
                    result[
                        "ga4_advertiser_ad_cost"
                    ] = None

                    result[
                        "ga4_advertiser_ad_clicks"
                    ] = None

                    result[
                        "campaign_costs"
                    ] = []

                    result[
                        "ad_cost_error"
                    ] = _google_error(exc)

            websites.append(result)

        except (
            requests.RequestException,
            ValueError
        ) as exc:
            errors.append({
                "property_id": property_id,
                "property_name": prop["property_name"],
                "error": _google_error(exc)
            })

    return {
        "status": (
            "success"
            if not errors
            else "partial_success"
        ),
        "from_date": from_date,
        "to_date": to_date,
        "total_properties": len(properties),
        "successful_properties": len(websites),
        "websites": websites,
        "errors": errors
    }
