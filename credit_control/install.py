import frappe


def after_install():
	create_custom_fields()
	hide_native_credit_limit_field()
	create_override_role()


def create_custom_fields():
	# Extends ERPNext's existing "Customer Credit Limit" child table (already
	# used by Customer's own credit_limit mechanism) instead of inventing a
	# parallel doctype for the same per-customer-per-company policy row.
	#
	# The actual limit is stored in a NEW field (custom_credit_limit), not in
	# ERPNext's native "credit_limit" field. ERPNext's own Sales Invoice
	# check (check_credit_limit, called in on_submit) reads that native field
	# and fires for any invoice not linked to a Sales Order/Delivery Note -
	# with no concept of grace_amount or the override role. Reusing it would
	# make ERPNext's own check silently double-block invoices that this
	# app's grace amount is meant to allow, and even block overrides after
	# they've been recorded. Leaving native credit_limit at 0 keeps that
	# check a permanent no-op (see disable_native_credit_limit).
	if frappe.db.exists("Custom Field", "Customer Credit Limit-custom_credit_limit"):
		return

	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields as _create

	_create(
		{
			"Customer Credit Limit": [
				{
					"fieldname": "custom_credit_limit",
					"label": "Credit Limit (Enforced)",
					"fieldtype": "Currency",
					"insert_after": "credit_limit",
					"description": "Credit limit enforced by the Credit Control app. ERPNext's own 'Credit Limit' field above is disabled by this app - use this field instead.",
				},
				{
					"fieldname": "grace_amount",
					"label": "Grace Amount",
					"fieldtype": "Currency",
					"insert_after": "custom_credit_limit",
					"description": "Extra amount allowed on top of the Credit Limit before Sales Invoice submission is blocked.",
				},
				{
					"fieldname": "max_overdue_days",
					"label": "Max Overdue Days",
					"fieldtype": "Int",
					"insert_after": "grace_amount",
					"description": "Block Sales Invoice submission if any invoice for this customer/company is overdue by more than this many days, regardless of amount.",
				},
			]
		},
		ignore_validate=True,
	)


def hide_native_credit_limit_field():
	# Hide ERPNext's native field instead of just leaving it unused, so
	# nobody sets it through the UI and accidentally reintroduces the
	# conflicting native check (see create_custom_fields for why).
	from frappe.custom.doctype.property_setter.property_setter import make_property_setter

	make_property_setter(
		"Customer Credit Limit", "credit_limit", "hidden", 1, "Check", validate_fields_for_doctype=False
	)


def create_override_role():
	if not frappe.db.exists("Role", "Sales Invoice Credit Override"):
		frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": "Sales Invoice Credit Override",
				"desk_access": 1,
			}
		).insert(ignore_permissions=True)
