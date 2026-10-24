"""Export credit (e.g. Eksfin) loan: what the customer borrows and pays back.

The project total is split into a down payment (OECD export credits: at least 15 %) and the loan. During a grace
period (construction) only interest is paid. Then the loan is repaid over the repayment period, either as equal
instalments (annuity: the same payment every time) or as equal principal (OECD / Eksfin practice: the same
principal every time, interest on what is left, so payments fall). One-off fees (arrangement fee, Eksfin's risk
premium) are a share of the loan, paid up front or added to it.
"""


class LoanInputError(ValueError):
    pass


DEFAULTS = dict(project_total=1_000_000.0, down_pct=15.0, rate_pct=6.0, years=10.0, grace_months=0,
                per_year=12, method="annuity", fee_pct=0.0, premium_pct=0.0, finance_fees=False)
METHODS = {"annuity": "Equal payments (annuity)", "equal_principal": "Equal principal (OECD / Eksfin practice)"}
FREQUENCIES = {12: "Monthly", 4: "Quarterly", 2: "Semi-annual", 1: "Annual"}


def loan(**kw) -> dict:
    a = {**DEFAULTS, **{k: v for k, v in kw.items() if v is not None}}
    total, down_pct, rate, years = float(a["project_total"]), float(a["down_pct"]), float(a["rate_pct"]), float(a["years"])
    grace, m = int(a["grace_months"]), int(a["per_year"])
    if total <= 0:
        raise LoanInputError("Enter the project total.")
    if not 0 <= down_pct < 100:
        raise LoanInputError("The down payment must be 0 to 99 % of the project.")
    if not 0 <= rate <= 50:
        raise LoanInputError("The interest rate must be 0 to 50 % a year.")
    if not 0 < years <= 40:
        raise LoanInputError("The repayment period must be up to 40 years.")
    if not 0 <= grace <= 120:
        raise LoanInputError("The grace period must be 0 to 120 months.")
    if m not in FREQUENCIES:
        raise LoanInputError("Pay monthly, quarterly, semi-annually or annually.")
    if a["method"] not in METHODS:
        raise LoanInputError("Choose equal payments or equal principal.")
    down = total * down_pct / 100
    principal = total - down
    fees = principal * (float(a["fee_pct"]) + float(a["premium_pct"])) / 100
    borrowed = principal + (fees if a["finance_fees"] else 0.0)
    r = rate / 100 / m                          # interest per period
    n = max(int(round(years * m)), 1)           # repayment instalments
    grace_periods = int(round(grace * m / 12))  # interest-only periods

    rows, balance, k = [], borrowed, 0
    for _ in range(grace_periods):
        k += 1
        interest = balance * r
        rows.append({"n": k, "month": k * 12 / m, "interest": interest, "principal": 0.0, "payment": interest,
                     "balance": balance, "grace": True})
    annuity = borrowed * r / (1 - (1 + r) ** -n) if r > 0 else borrowed / n
    for i in range(n):
        k += 1
        interest = balance * r
        part = (annuity - interest) if a["method"] == "annuity" else borrowed / n
        if i == n - 1:  # the last one clears what is left (rounding)
            part = balance
        balance -= part
        rows.append({"n": k, "month": k * 12 / m, "interest": interest, "principal": part,
                     "payment": interest + part, "balance": max(balance, 0.0), "grace": False})

    years_out = {}
    for row in rows:
        y = int((row["month"] - 1e-9) // 12) + 1
        b = years_out.setdefault(y, {"year": y, "interest": 0.0, "principal": 0.0, "payment": 0.0, "balance": 0.0})
        for key in ("interest", "principal", "payment"):
            b[key] += row[key]
        b["balance"] = row["balance"]
    paid_interest = sum(x["interest"] for x in rows)
    regular = [x for x in rows if not x["grace"]]
    return {
        "inputs": a, "project_total": total, "down_payment": down, "principal": principal, "fees": fees,
        "borrowed": borrowed, "periods": n, "grace_periods": grace_periods,
        "first_payment": regular[0]["payment"] if regular else 0.0,
        "last_payment": regular[-1]["payment"] if regular else 0.0,
        "grace_payment": rows[0]["payment"] if grace_periods else None,
        "per_month": (regular[0]["payment"] * m / 12) if regular else 0.0,
        "interest_total": paid_interest, "repaid_total": borrowed + paid_interest,
        "cost_total": paid_interest + fees,     # what the credit costs on top of the loan
        "customer_total": down + borrowed + paid_interest + (0.0 if a["finance_fees"] else fees),
        "rows": rows, "years": list(years_out.values()), "months_total": rows[-1]["month"] if rows else 0,
    }
