# Copyright (c) 2026, Enfono Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt

from credit_control.credit_control.credit_check import get_customer_outstanding, get_worst_overdue_days


def execute(filters=None):
	filters = filters or {}
	columns = get_columns()
	data = get_data(filters)
	return columns, data


def get_columns():
	return [
		{"label": _("Customer"), "fieldname": "customer", "fieldtype": "Link", "options": "Customer", "width": 180},
		{"label": _("Company"), "fieldname": "company", "fieldtype": "Link", "options": "Company", "width": 150},
		{"label": _("Credit Limit"), "fieldname": "credit_limit", "fieldtype": "Currency", "width": 120},
		{"label": _("Grace"), "fieldname": "grace_amount", "fieldtype": "Currency", "width": 100},
		{"label": _("Outstanding"), "fieldname": "outstanding", "fieldtype": "Currency", "width": 120},
		{"label": _("Utilisation %"), "fieldname": "utilisation_percent", "fieldtype": "Percent", "width": 120},
		{"label": _("Worst Overdue Days"), "fieldname": "worst_overdue_days", "fieldtype": "Int", "width": 140},
	]


def get_data(filters):
	min_utilization = flt(filters.get("min_utilization_percent") or 80)

	row_filters = {"parenttype": "Customer"}
	if filters.get("company"):
		row_filters["company"] = filters["company"]

	policy_rows = frappe.get_all(
		"Customer Credit Limit",
		filters=row_filters,
		fields=["parent as customer", "company", "custom_credit_limit as credit_limit", "grace_amount"],
	)

	data = []
	for row in policy_rows:
		credit_limit = flt(row.credit_limit)
		grace_amount = flt(row.grace_amount)
		effective_limit = credit_limit + grace_amount
		if not effective_limit:
			# Blank/zero credit limit = unlimited for this customer/company -
			# there is no meaningful "utilisation" to report against.
			continue

		outstanding = get_customer_outstanding(row.customer, row.company)
		utilisation_percent = (outstanding / effective_limit) * 100 if effective_limit else 0

		if utilisation_percent < min_utilization:
			continue

		worst_overdue_days = get_worst_overdue_days(row.customer, row.company)

		data.append(
			{
				"customer": row.customer,
				"company": row.company,
				"credit_limit": credit_limit,
				"grace_amount": grace_amount,
				"outstanding": outstanding,
				"utilisation_percent": utilisation_percent,
				"worst_overdue_days": worst_overdue_days,
			}
		)

	data.sort(key=lambda d: d["utilisation_percent"], reverse=True)
	return data
