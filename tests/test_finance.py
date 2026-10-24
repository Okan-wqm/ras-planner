"""Export credit loan: annuity and equal principal, grace period, fees, and the Library page."""
import pytest
from django.urls import reverse

from ras_engine.finance import LoanInputError, loan


def test_annuity_payment_matches_the_formula():
    r = loan(project_total=1_000_000, down_pct=15, rate_pct=6, years=10)
    assert r["principal"] == 850_000 and r["down_payment"] == 150_000
    i = 0.06 / 12
    assert r["first_payment"] == pytest.approx(850_000 * i / (1 - (1 + i) ** -120))   # 9,436.74
    assert r["first_payment"] == pytest.approx(r["last_payment"])
    assert sum(x["principal"] for x in r["rows"]) == pytest.approx(850_000)
    assert r["rows"][-1]["balance"] == pytest.approx(0, abs=1e-6)
    assert r["repaid_total"] == pytest.approx(r["first_payment"] * 120)


def test_equal_principal_with_grace_and_fees():
    r = loan(project_total=1_000_000, down_pct=15, rate_pct=6, years=10, method="equal_principal", per_year=2,
             grace_months=12, fee_pct=1, premium_pct=2, finance_fees=True)
    assert r["fees"] == pytest.approx(850_000 * 0.03) and r["borrowed"] == pytest.approx(850_000 * 1.03)
    assert r["grace_periods"] == 2 and r["grace_payment"] == pytest.approx(r["borrowed"] * 0.03)
    assert r["first_payment"] == pytest.approx(r["borrowed"] / 20 + r["borrowed"] * 0.03)
    assert r["last_payment"] < r["first_payment"]
    assert r["years"][0]["principal"] == 0 and len(r["years"]) == 11


def test_zero_interest_and_bad_inputs():
    assert loan(project_total=120_000, down_pct=0, rate_pct=0, years=1)["first_payment"] == pytest.approx(10_000)
    for kw in (dict(project_total=0), dict(down_pct=100), dict(rate_pct=60), dict(years=0), dict(per_year=5)):
        with pytest.raises(LoanInputError):
            loan(**kw)


@pytest.mark.django_db
def test_loan_page(client):
    client.get("/")
    html = client.get(reverse("loan_calculator"), {"project_total": "2000000", "grace_months": "12"}).content.decode()
    assert "Export credit (Eksfin)" in html and "Year by year" in html and "18,873" in html
    assert "Total interest" in html
    bad = client.get(reverse("loan_calculator"), {"project_total": "2000000", "down_pct": "120"}).content.decode()
    assert "down payment must be" in bad
