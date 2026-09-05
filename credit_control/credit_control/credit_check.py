# Copyright (c) 2026, Enfono Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import cint, date_diff, flt, getdate, now_datetime

OVERRIDE_ROLE = "Sales Invoice Credit Override"


def get_credit_policy(customer, company):
	"""Read custom_credit_limit / grace_amount / max_overdue_days from the
	same row on ERPNext's own "Customer Credit Limit" child table (on
	Customer), which this app extends with Custom Fields instead of a
	parallel doctype.

	custom_credit_limit (not ERPNext's native credit_limit field) is the
	limit this app enforces - see disable_native_credit_limit for why the
	native field is deliberately kept out of use.

	Deliberately scoped to parenttype="Customer" only (no fallback to
	Customer Group / Company defaults, unlike erpnext.get_credit_limit) -
	grace_amount/max_overdue_days only make sense paired with the specific
	credit_limit row they were configured against.
	"""
	return frappe.db.get_value(
		"Customer Credit Limit",
		{"parent": customer, "parenttype": "Customer", "company": company},
		["custom_credit_limit as credit_limit", "grace_amount", "max_overdue_days"],
		as_dict=True,
	)


def disable_native_credit_limit(doc, method=None):
	"""Hooked on Customer validate.

	ERPNext's own Sales Invoice.check_credit_limit() reads the native
	`credit_limit` field on this same "Customer Credit Limit" child table,
	and fires unconditionally for any invoice not linked to a Sales
	Order/Delivery Note (i.e. essentially always, for direct invoices) -
	with no concept of grace_amount or the override role. If that native
	field were ever set (e.g. by mistake through the UI, or a data import),
	ERPNext would silently double-block invoices this app's grace amount is
	meant to allow, and even block submission after an override has already
	been logged. Force it to 0 so ERPNext's own gate
	(`if not credit_limit: return`) stays a permanent no-op; this app's
	actual limit lives only in custom_credit_limit.
	"""
	for row in doc.get("credit_limits") or []:
		if row.credit_limit:
			row.credit_limit = 0


def get_customer_outstanding(customer, company, exclude_invoice=None):
	"""Sum of outstanding_amount (converted to Company currency) across
	submitted Sales Invoices only.

	- Drafts (docstatus=0) are excluded - they do not count toward exposure.
	- Cancelled invoices (docstatus=2) are excluded, and an amended invoice
	  gets a new name, so amend/cancel never double-counts.
	- Credit notes (is_return=1) already carry negative outstanding_amount
	  in ERPNext, so they net out of the sum automatically - no special-casing
	  needed here.
	- Sales Invoice has no base_outstanding_amount field - outstanding_amount
	  is in the invoice's own currency, so it is multiplied by conversion_rate
	  (the same way every other base_* field on the doctype is derived) to
	  get a Company-currency figure. This is what makes a multi-currency
	  invoice comparable against a Company-currency credit limit regardless
	  of the invoice's own currency.
	"""
	filters = {"customer": customer, "company": company, "docstatus": 1}
	condition = ""
	values = [customer, company]
	if exclude_invoice:
		condition = "and name != %s"
		values.append(exclude_invoice)

	result = frappe.db.sql(
		f"""
		select sum(outstanding_amount * conversion_rate)
		from `tabSales Invoice`
		where customer=%s and company=%s and docstatus=1 {condition}
		""",
		values,
	)
	return flt(result[0][0]) if result else 0.0


def get_worst_overdue_days(customer, company, exclude_invoice=None):
	"""Largest number of days any of the customer's submitted, still-outstanding
	invoices (in this company) is past its due date. 0 if none are overdue."""
	condition = ""
	values = [customer, company]
	if exclude_invoice:
		condition = "and name != %s"
		values.append(exclude_invoice)

	rows = frappe.db.sql(
		f"""
		select due_date
		from `tabSales Invoice`
		where customer=%s and company=%s and docstatus=1
		and outstanding_amount > 0 and due_date is not null {condition}
		""",
		values,
		as_dict=True,
	)

	today = getdate()
	worst = 0
	for row in rows:
		days = date_diff(today, row.due_date)
		if days > worst:
			worst = days
	return worst


def has_override_role():
	return OVERRIDE_ROLE in frappe.get_roles(frappe.session.user)


def create_override_log(doc, outstanding, credit_limit, grace_amount, effective_limit, worst_overdue, reason):
	frappe.get_doc(
		{
			"doctype": "Credit Limit Override Log",
			"sales_invoice": doc.name,
			"customer": doc.customer,
			"company": doc.company,
			"overridden_by": frappe.session.user,
			"override_datetime": now_datetime(),
			"invoice_grand_total": doc.base_grand_total,
			"outstanding_amount": outstanding,
			"credit_limit": credit_limit,
			"grace_amount": grace_amount,
			"effective_limit": effective_limit,
			"worst_overdue_days": worst_overdue,
			"reason": reason,
		}
	).insert(ignore_permissions=True)


def validate_credit_limit(doc, method=None):
	# A credit note reduces exposure, not a new charge against the customer -
	# it must never itself be blocked by the credit check.
	if doc.is_return:
		return

	if not doc.customer or not doc.company:
		return

	policy = get_credit_policy(doc.customer, doc.company)
	if not policy:
		# No policy row configured for this customer/company -> unlimited,
		# not zero. An unconfigured customer should not be silently blocked;
		# the policy has to be deliberately set to restrict anyone.
		return

	credit_limit = flt(policy.credit_limit)
	grace_amount = flt(policy.grace_amount)
	max_overdue_days = cint(policy.max_overdue_days)

	if not credit_limit and not max_overdue_days:
		# Row exists but nothing meaningful configured on it - same as no
		# policy at all (blank/zero credit limit = unlimited, documented).
		return

	outstanding = get_customer_outstanding(doc.customer, doc.company, exclude_invoice=doc.name)
	new_exposure = outstanding + flt(doc.base_grand_total)
	effective_limit = credit_limit + grace_amount

	worst_overdue = get_worst_overdue_days(doc.customer, doc.company, exclude_invoice=doc.name)

	breach_amount = bool(credit_limit) and new_exposure > effective_limit
	breach_overdue = bool(max_overdue_days) and worst_overdue > max_overdue_days

	if not breach_amount and not breach_overdue:
		return

	reasons = []
	if breach_amount:
		reasons.append(
			_("outstanding {0} + this invoice {1} = {2} exceeds the effective limit of {3} (credit limit {4} + grace {5})").format(
				frappe.format_value(outstanding, {"fieldtype": "Currency"}),
				frappe.format_value(doc.base_grand_total, {"fieldtype": "Currency"}),
				frappe.format_value(new_exposure, {"fieldtype": "Currency"}),
				frappe.format_value(effective_limit, {"fieldtype": "Currency"}),
				frappe.format_value(credit_limit, {"fieldtype": "Currency"}),
				frappe.format_value(grace_amount, {"fieldtype": "Currency"}),
			)
		)
	if breach_overdue:
		reasons.append(
			_("an existing invoice is {0} days overdue, exceeding the maximum allowed {1} days").format(
				worst_overdue, max_overdue_days
			)
		)
	reason_text = "; ".join(reasons)

	if has_override_role():
		create_override_log(doc, outstanding, credit_limit, grace_amount, effective_limit, worst_overdue, reason_text)
		frappe.msgprint(
			_("Credit limit breach overridden and recorded against {0}: {1}").format(doc.name, reason_text),
			title=_("Credit Limit Override Recorded"),
			indicator="orange",
		)
		return

	frappe.throw(
		_("Cannot submit Sales Invoice for {0}: {1}.").format(doc.customer, reason_text),
		title=_("Credit Limit Exceeded"),
	)
