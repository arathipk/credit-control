## Credit Control

Blocks Sales Invoice submission when a customer has gone past an agreed
credit position (credit limit + grace amount, or a maximum overdue-days
rule), with a recorded, non-silent override path for a nominated role.

### Approach: extend, but supersede the check

ERPNext already ships a Credit Limit feature: a `credit_limit` field per
customer/company on the **Customer Credit Limit** child table (on
`Customer`), enforced by `Sales Invoice.check_credit_limit()` in `on_submit`.

This app **extends that same child table** with three Custom Fields
(`custom_credit_limit`, `grace_amount`, `max_overdue_days`) instead of
inventing a parallel doctype - the policy is still configured in the one
place ERPNext users already expect it (on the Customer, per company).

It does **not**, however, reuse ERPNext's own check function. That function
has no concept of a grace amount, a max-overdue-days rule, or a recorded
override - and critically, it reads the native `credit_limit` field and
fires unconditionally for any invoice not linked to a Sales Order/Delivery
Note (i.e. essentially always, for a direct invoice). If this app's policy
value were stored in that same native field, ERPNext's own check would run
*in addition to* this app's, and would incorrectly block anything over the
raw limit even when this app's grace amount was meant to allow it - and
even block a submission after this app's own override had already been
logged. So this app's validation is a **fresh implementation**
(`before_submit` hook), and the native `credit_limit` field is deliberately
forced to `0` (and hidden) so ERPNext's own gate is a permanent no-op. See
the comments on `disable_native_credit_limit` and `create_custom_fields` in
the code for the full reasoning.

### Edge case decisions

- **Amended invoices**: only submitted invoices (`docstatus=1`) count
  toward outstanding/overdue. A cancelled invoice (`docstatus=2`) is
  excluded, and its amendment gets a new `name`, so cancel -> amend never
  double-counts.
- **Return invoices**: an invoice with `is_return=1` is never itself
  subject to the credit check (it reduces exposure, it isn't new exposure),
  and its already-negative `outstanding_amount` nets out of the customer's
  outstanding total automatically - no special-casing needed in the sum.
- **Multi-currency**: everything is compared in the **Company's currency**,
  because the credit limit is configured per company, not per currency.
  Sales Invoice has no `base_outstanding_amount` field, so
  `outstanding_amount * conversion_rate` is used instead (the same
  derivation ERPNext uses for every other `base_*` field on the doctype).
- **Zero or blank credit limit**: treated as **unlimited**, not zero. If no
  policy row exists for a customer/company, or the row has neither a
  credit limit nor a max-overdue-days value set, the check is skipped
  entirely - a customer is only ever restricted by a deliberately
  configured policy, never blocked by omission.
- **Multi-company**: the policy (and the outstanding/overdue calculations)
  are always scoped by `(customer, company)` together - the same customer
  under two companies has two entirely independent positions, computed by
  entirely separate queries.
- **Draft invoices**: excluded from exposure. Both the submit-time block and
  the report call the same `get_customer_outstanding()` /
  `get_worst_overdue_days()` helpers, which filter to `docstatus=1` only -
  so this is consistent between the two by construction, not by convention.

### A note on testing/demoing the block vs. the override

`frappe.get_roles()` special-cases the **Administrator** user to implicitly
hold *every* role in the system, including this app's override role -
regardless of what's actually assigned to it. Logged in as Administrator,
every over-limit invoice will appear to succeed via the override path, and
the block can never be observed. Use an ordinary user (with real,
explicitly assigned roles) to demonstrate/test the block, and only add the
`Sales Invoice Credit Override` role to a user when demonstrating the
override itself. The test suite creates two such dedicated users for
exactly this reason.

### Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO
bench --site <site> install-app credit_control
```

`after_install` creates the three Custom Fields above, hides ERPNext's
native `credit_limit` field on the same child table, and creates the
`Sales Invoice Credit Override` role.

### Tests

```bash
bench --site <site> set-config allow_tests true   # first time only
bench --site <site> run-tests --app credit_control
```

### License

mit
