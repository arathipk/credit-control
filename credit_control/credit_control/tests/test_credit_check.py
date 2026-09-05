# Copyright (c) 2026, Enfono Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, getdate, nowdate

from credit_control.credit_control.credit_check import (
	OVERRIDE_ROLE,
	get_customer_outstanding,
	get_worst_overdue_days,
)
from credit_control.credit_control.report.customer_credit_utilization.customer_credit_utilization import (
	execute as run_report,
)

# Dedicated companies for this suite - never assume a site's existing
# Company (or its currency/COA) is safe to reuse or mutate.
COMPANY_1 = "_CC Test Company 1"
COMPANY_2 = "_CC Test Company 2"


def _create_company(name, abbr):
	if frappe.db.exists("Company", name):
		return
	frappe.get_doc(
		{
			"doctype": "Company",
			"company_name": name,
			"abbr": abbr,
			"default_currency": "USD",
			"country": "United States",
			"chart_of_accounts": "Standard",
		}
	).insert()


def _ensure_fiscal_year():
	today = getdate(nowdate())
	if frappe.db.exists("Fiscal Year", {"year_start_date": ["<=", today], "year_end_date": [">=", today]}):
		return
	year = today.year
	frappe.get_doc(
		{
			"doctype": "Fiscal Year",
			"year": f"_CC Test FY {year}",
			"year_start_date": f"{year}-01-01",
			"year_end_date": f"{year}-12-31",
		}
	).insert()


def ensure_base_data():
	"""Bring up the minimum masters this suite needs. Customer Group /
	Territory / Item Group / UOM are global (not company-scoped) so whatever
	a site already has is reused as-is; only the two companies and the
	fiscal year are created by (and scoped to) this test suite."""
	_ensure_fiscal_year()
	_create_company(COMPANY_1, "CC1")
	_create_company(COMPANY_2, "CC2")


def get_income_account(company):
	return frappe.db.get_value("Account", {"company": company, "root_type": "Income", "is_group": 0})


def get_cost_center(company):
	return frappe.db.get_value("Cost Center", {"company": company, "is_group": 0})


def get_receivable_account(company):
	return frappe.get_cached_value("Company", company, "default_receivable_account")


def get_item_group():
	return frappe.db.get_value("Item Group", {"is_group": 0}) or frappe.db.get_value("Item Group", {})


def get_uom():
	return "Nos" if frappe.db.exists("UOM", "Nos") else frappe.db.get_value("UOM", {})


def get_customer_group():
	return frappe.db.get_value("Customer Group", {"is_group": 0}) or frappe.db.get_value("Customer Group", {})


def get_territory():
	return frappe.db.get_value("Territory", {"is_group": 0}) or frappe.db.get_value("Territory", {})


def make_item():
	item_code = frappe.generate_hash(length=8) + "-item"
	frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": item_code,
			"item_name": item_code,
			"item_group": get_item_group(),
			"stock_uom": get_uom(),
			"is_stock_item": 0,
		}
	).insert()
	return item_code


class TestCreditCheck(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_base_data()
		cls.company = COMPANY_1
		cls.company_2 = COMPANY_2
		cls.item = make_item()
		# frappe.get_roles() special-cases "Administrator" to implicitly hold
		# EVERY role in the system (see frappe/permissions.py get_roles) -
		# including this app's override role - regardless of what's actually
		# assigned. Since `bench run-tests` runs as Administrator by default,
		# testing the block/override distinction requires two ordinary users
		# with real, explicit role assignments instead.
		cls.regular_user = cls._make_test_user(["System Manager", "Accounts Manager"])
		cls.override_user = cls._make_test_user(["System Manager", "Accounts Manager", OVERRIDE_ROLE])

	@classmethod
	def _make_test_user(cls, roles):
		email = frappe.generate_hash(length=8) + "@example.com"
		user = frappe.new_doc("User")
		user.email = email
		user.first_name = "CC Test User"
		user.send_welcome_email = 0
		for role in roles:
			user.append("roles", {"role": role})
		user.insert(ignore_permissions=True)
		return user.name

	def make_customer(self):
		name = "_CC Test Customer " + frappe.generate_hash(length=8)
		frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": name,
				"customer_group": get_customer_group(),
				"territory": get_territory(),
			}
		).insert()
		return name

	def set_policy(self, customer, company, credit_limit=0, grace_amount=0, max_overdue_days=0):
		customer_doc = frappe.get_doc("Customer", customer)
		row = None
		for r in customer_doc.get("credit_limits") or []:
			if r.company == company:
				row = r
				break
		if not row:
			row = customer_doc.append("credit_limits", {"company": company})
		row.custom_credit_limit = credit_limit
		row.grace_amount = grace_amount
		row.max_overdue_days = max_overdue_days
		customer_doc.save()

	def create_invoice(
		self,
		customer,
		company,
		grand_total,
		posting_date=None,
		due_date=None,
		is_return=False,
		return_against=None,
		do_not_submit=False,
		as_user=None,
	):
		posting_date = posting_date or nowdate()
		as_user = as_user or self.regular_user
		with self.set_user(as_user):
			si = frappe.new_doc("Sales Invoice")
			si.customer = customer
			si.company = company
			si.posting_date = posting_date
			si.set_posting_time = 1
			si.due_date = due_date or posting_date
			si.debit_to = get_receivable_account(company)
			# Company currency is USD but the site's global default currency
			# is INR - pin everything to USD so no Currency Exchange record
			# is needed to resolve a price list conversion rate.
			si.currency = "USD"
			si.conversion_rate = 1
			si.price_list_currency = "USD"
			si.plc_conversion_rate = 1
			si.ignore_pricing_rule = 1
			si.is_return = 1 if is_return else 0
			si.return_against = return_against
			qty = -1 if is_return else 1
			si.append(
				"items",
				{
					"item_code": self.item,
					"item_name": self.item,
					"description": self.item,
					"qty": qty,
					"rate": grand_total,
					"uom": get_uom(),
					"income_account": get_income_account(company),
					"cost_center": get_cost_center(company),
				},
			)
			si.insert()
			if not do_not_submit:
				si.submit()
		return si

	# --- Core requirement: block on (outstanding + this invoice) > (limit + grace) ---

	def test_submit_within_limit_is_allowed(self):
		customer = self.make_customer()
		self.set_policy(customer, self.company, credit_limit=100)
		si = self.create_invoice(customer, self.company, 80)
		self.assertEqual(si.docstatus, 1)

	def test_submit_over_limit_is_blocked(self):
		customer = self.make_customer()
		self.set_policy(customer, self.company, credit_limit=100)
		with self.assertRaises(frappe.ValidationError):
			self.create_invoice(customer, self.company, 150)

	def test_grace_amount_extends_the_limit(self):
		customer = self.make_customer()
		self.set_policy(customer, self.company, credit_limit=100, grace_amount=50)
		# Over the raw limit (100) but within limit + grace (150) -> allowed.
		si = self.create_invoice(customer, self.company, 140)
		self.assertEqual(si.docstatus, 1)
		# Over limit + grace -> blocked.
		with self.assertRaises(frappe.ValidationError):
			self.create_invoice(customer, self.company, 20)

	def test_block_message_contains_actual_numbers(self):
		customer = self.make_customer()
		self.set_policy(customer, self.company, credit_limit=100, grace_amount=10)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.create_invoice(customer, self.company, 200)
		message = str(ctx.exception)
		for number in ("100", "10", "200"):
			self.assertIn(number, message)

	# --- Overdue-days block, independent of amount ---

	def test_overdue_days_blocks_regardless_of_amount(self):
		customer = self.make_customer()
		self.set_policy(customer, self.company, credit_limit=10_000, max_overdue_days=30)
		# An old, small, unpaid invoice overdue by 100 days.
		self.create_invoice(
			customer, self.company, 10, posting_date=add_days(nowdate(), -120), due_date=add_days(nowdate(), -100)
		)
		# A brand new invoice, tiny amount, nowhere near the credit limit.
		with self.assertRaises(frappe.ValidationError):
			self.create_invoice(customer, self.company, 1)

	# --- Override role: recorded, not silent ---

	def test_override_role_allows_submit_and_is_recorded(self):
		customer = self.make_customer()
		self.set_policy(customer, self.company, credit_limit=100)
		si = self.create_invoice(customer, self.company, 500, as_user=self.override_user)
		self.assertEqual(si.docstatus, 1)
		log_name = frappe.db.exists("Credit Limit Override Log", {"sales_invoice": si.name})
		self.assertTrue(log_name)
		log = frappe.get_doc("Credit Limit Override Log", log_name)
		self.assertEqual(log.customer, customer)
		self.assertEqual(log.company, self.company)
		self.assertEqual(log.overridden_by, self.override_user)
		self.assertIsNotNone(log.override_datetime)

	def test_without_override_role_breach_is_not_recorded(self):
		customer = self.make_customer()
		self.set_policy(customer, self.company, credit_limit=100)
		with self.assertRaises(frappe.ValidationError):
			self.create_invoice(customer, self.company, 500)
		self.assertFalse(frappe.db.exists("Credit Limit Override Log", {"customer": customer}))

	# --- Edge case: amended invoices must not double-count ---

	def test_amended_invoice_not_double_counted(self):
		customer = self.make_customer()
		self.set_policy(customer, self.company, credit_limit=1000)
		si = self.create_invoice(customer, self.company, 300)
		with self.set_user(self.regular_user):
			si.cancel()
			amended = frappe.copy_doc(si)
			amended.docstatus = 0
			amended.amended_from = si.name
			amended.set_posting_time = 1
			amended.debit_to = get_receivable_account(self.company)
			amended.insert()
			amended.submit()

		outstanding = get_customer_outstanding(customer, self.company)
		self.assertEqual(outstanding, 300)

	# --- Edge case: returns reduce exposure, and are never themselves blocked ---

	def test_return_invoice_reduces_exposure_and_is_not_blocked(self):
		customer = self.make_customer()
		self.set_policy(customer, self.company, credit_limit=100)
		si = self.create_invoice(customer, self.company, 90)
		before = get_customer_outstanding(customer, self.company)
		self.assertEqual(before, 90)

		# A return larger in magnitude than the remaining headroom would
		# breach the limit if it were treated as new exposure - it must not
		# be blocked, and must reduce (not add to) outstanding.
		credit_note = self.create_invoice(customer, self.company, 90, is_return=True, return_against=si.name)
		self.assertEqual(credit_note.docstatus, 1)

		after = get_customer_outstanding(customer, self.company)
		self.assertEqual(after, 0)

	# --- Edge case: blank/zero credit limit means unlimited ---

	def test_blank_credit_limit_is_unlimited(self):
		customer = self.make_customer()
		# No policy row at all for this company.
		si = self.create_invoice(customer, self.company, 1_000_000)
		self.assertEqual(si.docstatus, 1)

	# --- Edge case: multi-company positions are independent ---

	def test_multi_company_positions_are_independent(self):
		customer = self.make_customer()
		self.set_policy(customer, self.company, credit_limit=100)
		self.set_policy(customer, self.company_2, credit_limit=100)

		# Use up the limit under company 1 only.
		self.create_invoice(customer, self.company, 90)
		with self.assertRaises(frappe.ValidationError):
			self.create_invoice(customer, self.company, 50)

		# Company 2's position for the same customer is untouched.
		si = self.create_invoice(customer, self.company_2, 90)
		self.assertEqual(si.docstatus, 1)

	# --- Edge case: draft invoices do not count toward exposure ---

	def test_draft_invoices_do_not_count_toward_exposure(self):
		customer = self.make_customer()
		self.set_policy(customer, self.company, credit_limit=100)
		self.create_invoice(customer, self.company, 90, do_not_submit=True)
		# The draft above is not counted, so a second invoice within the
		# limit on its own submits cleanly.
		si = self.create_invoice(customer, self.company, 90)
		self.assertEqual(si.docstatus, 1)

	# --- Report ---

	def test_report_lists_customers_at_or_above_80_percent(self):
		customer = self.make_customer()
		self.set_policy(customer, self.company, credit_limit=100)
		self.create_invoice(customer, self.company, 85)

		columns, data = run_report({"company": self.company, "min_utilization_percent": 80})
		row = next((d for d in data if d["customer"] == customer), None)
		self.assertIsNotNone(row)
		self.assertEqual(row["credit_limit"], 100)
		self.assertEqual(row["outstanding"], 85)
		self.assertEqual(row["utilisation_percent"], 85.0)

		fieldnames = {c["fieldname"] for c in columns}
		self.assertEqual(
			fieldnames,
			{"customer", "company", "credit_limit", "grace_amount", "outstanding", "utilisation_percent", "worst_overdue_days"},
		)

	def test_report_excludes_customers_below_threshold(self):
		customer = self.make_customer()
		self.set_policy(customer, self.company, credit_limit=1000)
		self.create_invoice(customer, self.company, 10)

		_columns, data = run_report({"company": self.company, "min_utilization_percent": 80})
		self.assertIsNone(next((d for d in data if d["customer"] == customer), None))

	def test_worst_overdue_days_is_zero_when_nothing_overdue(self):
		customer = self.make_customer()
		self.create_invoice(customer, self.company, 10, due_date=add_days(nowdate(), 30))
		self.assertEqual(get_worst_overdue_days(customer, self.company), 0)
