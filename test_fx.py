"""Checks for the exchange rate lookup.

Everything here is offline: `requests.get` is replaced with a fake serving
canned Frankfurter-shaped payloads, so the suite is fast and deterministic.
The live behaviours it stands in for were confirmed against the real API by hand.
"""

import datetime as dt
import sys

sys.path.insert(0, "/workspaces/hellocode")

import fx as fx_mod
from fx import FxError, fetch_fx

failures: list[str] = []
calls: list[tuple[str, dict]] = []


def check(label, condition, detail=""):
    print(("PASS  " if condition else "FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def raises(label, fragment, fn):
    try:
        fn()
    except FxError as exc:
        check(label, fragment in str(exc), f"got: {exc}")
    except Exception as exc:  # noqa: BLE001
        check(label, False, f"wrong exception {type(exc).__name__}: {exc}")
    else:
        check(label, False, "no error raised")


def message(fn) -> str:
    """The text of the FxError `fn` raises."""
    try:
        fn()
    except FxError as exc:
        return str(exc)
    return ""


def day(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


CCY = ["AUD", "JPY", "USD"]

# Real values the source returned for 2026-09-30, one row per source currency.
ROWS = {
    "AUD": {"JPY": 109.39, "USD": 0.69675},
    "JPY": {"AUD": 0.00914, "USD": 0.00637},
    "USD": {"AUD": 1.4352, "JPY": 157.0},
}


class FakeResponse:
    def __init__(self, data, status=200, not_json=False):
        self._data = data
        self.status_code = status
        self._not_json = not_json
        self.text = str(data)

    def json(self):
        if self._not_json:
            raise ValueError("not json")
        return self._data


def use(rows=None, *, used_date=None, status=200, supported=(),
        currencies_status=200, not_json=False):
    """Serve `rows` keyed by source currency, and record each rate request.

    A source absent from `rows` answers 404, which is what the real source does
    for an unknown currency or a date outside its series.
    """
    calls.clear()
    table = ROWS if rows is None else rows
    day_used = used_date or "2026-09-30"

    def fake_get(url, params=None, headers=None, timeout=None):
        params = params or {}
        if url.rstrip("/").endswith("/currencies"):
            if currencies_status != 200:
                return FakeResponse({"message": "not found"}, status=currencies_status)
            return FakeResponse(dict.fromkeys(supported, "Currency"))

        base = params.get("from")
        calls.append((base, dict(params)))
        if status != 200:
            return FakeResponse({"message": "not found"}, status=status)
        if not_json:
            return FakeResponse(None, not_json=True)
        if not isinstance(table, dict) or base not in table:
            return FakeResponse({"message": "not found"}, status=404)
        return FakeResponse(
            {"amount": 1.0, "base": base, "date": day_used, "rates": table[base]}
        )

    fx_mod.requests.get = fake_get
    # the supported list is cached per process, so clear it between tests
    fx_mod._SUPPORTED = dict.fromkeys(supported, "Currency") if supported else None


# --- the matrix ----------------------------------------------------------------
use()
result = fetch_fx(day("2026-09-30"), CCY)
check("matrix holds the published rates",
      result.rates["AUD"]["JPY"] == 109.39 and result.rates["JPY"]["AUD"] == 0.00914,
      str(result.rates["AUD"]))
check("every ordered pair is present",
      all(dst in result.rates[src] for src in CCY for dst in CCY))
check("identity pairs are exactly 1.0",
      all(result.rates[c][c] == 1.0 for c in CCY))
check("each currency is fetched as its own source",
      sorted(c for c, _ in calls) == CCY, str([c for c, _ in calls]))
check("one request covers all targets from that source",
      all(params.get("to") for _, params in calls))
check("a date with its own rates is not stale", not result.stale)
check("date and rate_date agree on a working day",
      result.date == result.rate_date == "2026-09-30")

# --- weekends and holidays ----------------------------------------------------
use(used_date="2026-10-02")
weekend = fetch_fx(day("2026-10-04"), ["AUD", "USD"])
check("a weekend request is marked stale", weekend.stale)
check("a weekend request reports the day actually used",
      weekend.date == "2026-10-04" and weekend.rate_date == "2026-10-02",
      f"{weekend.date} -> {weekend.rate_date}")

# --- single currency ----------------------------------------------------------
use()
one = fetch_fx(day("2026-09-30"), ["GBP"])
check("a one-currency portfolio yields the identity matrix",
      one.rates == {"GBP": {"GBP": 1.0}}, str(one.rates))
check("a one-currency portfolio makes no request", calls == [], str(calls))
check("a one-currency result is never stale", not one.stale)

# --- normalisation ------------------------------------------------------------
use()
normal = fetch_fx(day("2026-09-30"), ["aud", "Jpy", " usd "])
check("currency codes are normalised before lookup",
      sorted(c for c, _ in calls) == CCY, str([c for c, _ in calls]))
check("normalisation still yields a full matrix", normal.rates["USD"]["AUD"] == 1.4352)

# --- bad input ----------------------------------------------------------------
raises("a non-date is rejected", "must be a date object",
       lambda: fetch_fx("2026-09-30", CCY))
raises("an empty currency list is rejected", "non-empty list",
       lambda: fetch_fx(day("2026-09-30"), []))
raises("a duplicate currency is rejected", "duplicate",
       lambda: fetch_fx(day("2026-09-30"), ["AUD", "AUD"]))

# --- telling the failures apart ------------------------------------------------
use(status=404)
bad_date = message(lambda: fetch_fx(day("2030-01-01"), CCY))
check("every source failing reads as a bad date", "no rates for 2030-01-01" in bad_date,
      bad_date)
check("the bad-date message names the series start", "1999-01-04" in bad_date)

use(supported=("AUD", "JPY", "USD", "GBP"))
bad_ccy = message(lambda: fetch_fx(day("2026-09-30"), ["AUD", "XYZ"]))
check("an unsupported currency is named, not blamed on the date",
      "does not support XYZ" in bad_ccy, bad_ccy)
check("an unsupported currency is not misread as a date problem",
      "no rates for" not in bad_ccy, bad_ccy)
check("the unsupported-currency message says what is available",
      "ECB currencies only" in bad_ccy, bad_ccy)

use(supported=("AUD",))
check("two unsupported currencies are both named",
      "does not support XYZ, ZWL" in message(lambda: fetch_fx(day("2026-09-30"), ["XYZ", "ZWL"])))

# An unknown currency poisons the whole request, which is exactly why the
# supported list is consulted first.
use(supported=("AUD", "JPY", "USD"))
before = len(calls)
raises("the supported list is consulted before any rate request",
       "does not support XYZ",
       lambda: fetch_fx(day("2026-09-30"), ["AUD", "XYZ"]))
check("no rate requests are made when a currency is already known bad",
      len(calls) == before, str(calls))

# --- the supported list is a nicety, not a dependency --------------------------
use(currencies_status=500)
check("an unavailable supported list doesn't block the lookup",
      fetch_fx(day("2026-09-30"), CCY).rates["AUD"]["JPY"] == 109.39)

# --- malformed replies --------------------------------------------------------
use(not_json=True)
raises("a non-JSON reply is reported clearly", "isn't JSON",
       lambda: fetch_fx(day("2026-09-30"), CCY))

use(rows={"AUD": {"JPY": 109.39}, "JPY": {"AUD": 0.00914, "USD": 0.00637},
          "USD": {"AUD": 1.4352, "JPY": 157.0}})
raises("a source that omits a currency is caught", "omitted",
       lambda: fetch_fx(day("2026-09-30"), CCY))


def boom(*args, **kwargs):
    raise fx_mod.requests.RequestException("network down")


fx_mod.requests.get = boom
fx_mod._SUPPORTED = None
raises("a network failure is reported, not hidden", "could not reach",
       lambda: fetch_fx(day("2026-09-30"), CCY))

# --- the result is always safe to store ---------------------------------------
use()
from snapshots import check_fx  # noqa: E402

safe = fetch_fx(day("2026-09-30"), CCY).rates
try:
    check_fx(safe, CCY, "fetched")
    check("a fetched matrix passes the model's own validation", True)
except Exception as exc:  # noqa: BLE001
    check("a fetched matrix passes the model's own validation", False, str(exc))

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print("All checks passed.")