app_name = "itgenie"
app_title = "Itgenie"
app_publisher = "Arun Adithya T"
app_description = "IT Customizations"
app_email = "arun@automategenie.ai"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "itgenie",
# 		"logo": "/assets/itgenie/logo.png",
# 		"title": "Itgenie",
# 		"route": "/itgenie",
# 		"has_permission": "itgenie.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/itgenie/css/itgenie.css"
# app_include_js = "/assets/itgenie/js/itgenie.js"

# include js, css files in header of web template
# web_include_css = "/assets/itgenie/css/itgenie.css"
# web_include_js = "/assets/itgenie/js/itgenie.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "itgenie/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "itgenie/public/icons.svg"

# Home Pages
# ----------

fixtures = [
   {
       "dt": "Custom Field",
       "filters": {
           "module": ["in", ["Itgenie"]]
       }
   },
    {
       "dt": "Property Setter",
       "filters": {
           "module": ["in", ["Itgenie"]]
       }
   },
   {
     "dt":"DocType",
     "filters":{
       "module": ["in", ["Itgenie"]]
}  
}
]

#override_whitelisted_methods.update({
#    "/api/method/itgenie.api.slack_timesheet.slack_command": "itgenie.api.slack_timesheet.slack_command",
#    "/api/method/itgenie.api.slack_timesheet.slack_interactions": "itgenie.api.slack_timesheet.slack_interactions"
#})

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "itgenie.utils.jinja_methods",
# 	"filters": "itgenie.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "itgenie.install.before_install"
# after_install = "itgenie.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "itgenie.uninstall.before_uninstall"
# after_uninstall = "itgenie.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "itgenie.utils.before_app_install"
# after_app_install = "itgenie.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "itgenie.utils.before_app_uninstall"
# after_app_uninstall = "itgenie.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "itgenie.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"itgenie.tasks.all"
# 	],
# 	"daily": [
# 		"itgenie.tasks.daily"
# 	],
# 	"hourly": [
# 		"itgenie.tasks.hourly"
# 	],
# 	"weekly": [
# 		"itgenie.tasks.weekly"
# 	],
# 	"monthly": [
# 		"itgenie.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "itgenie.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "itgenie.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "itgenie.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["itgenie.utils.before_request"]
# after_request = ["itgenie.utils.after_request"]

# Job Events
# ----------
# before_job = ["itgenie.utils.before_job"]
# after_job = ["itgenie.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"itgenie.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }



doc_events = {
    "User": {
        "before_validate": "itgenie.password_policy.validate_user_password",
    },
}

override_whitelisted_methods = {
    "frappe.core.doctype.user.user.update_password":
        "itgenie.password_policy.update_password",
}


from itgenie.fcm.notification_hooks import (
    configure_notification_hooks as _configure_fcm_hooks,
)

_configure_fcm_hooks(globals())
