"""Checks for the price lookup.

Everything here is offline: `requests.get` is replaced with a fake that returns
canned Yahoo-shaped payloads, so the suite is fast and deterministic.
"""

import datetime as dt
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, "/workspaces/hellocode")

import prices as prices_mod
from prices import PriceError, close_on

failures: list[str] = []
calls: list[str] = []


def check(label, condition, detail=""):
    print(("PASS  " if condition else "FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def raises(label, fragment, fn):
    try:
        fn()
    except PriceError as exc:
        check(label, fragment in str(exc), f"got: {exc}")
    except Exception as exc:  # noqa: BLE001
        check(label, False, f"wrong exception {type(exc).__name__}: {exc}")
    else:
        check(label, False, "no error raised")


def stamp(day: str) -> int:
    when = dt.datetime.combine(dt.date.fromisoformat(day), dt.time(), tzinfo=dt.timezone.utc)
    return int(when.timestamp())


def payload(rows, currency="USD", symbol="MSFT"):
    """A Yahoo chart payload. `rows` is a list of (date, close); close may be None."""
    return {
        "chart": {
            "error": None,
            "result": [
                {
                    "meta": {"currency": currency, "symbol": symbol},
                    "timestamp": [stamp(d) for d, _ in rows],
                    "indicators": {"quote": [{"close": [c for _, c in rows]}]},
                }
            ],
        }
    }


class FakeResponse:
    def __init__(self, data, status=200, not_json=False):
        self._data = data
        self.status_code = status
        self._not_json = not_json

    def json(self):
        if self._not_json:
            raise ValueError("not json")
        return self._data


def use(rows=(), **response_kwargs):
    """Serve `rows` for every lookup and record each call."""
    calls.clear()
    currency = response_kwargs.pop("currency", "USD")

    def fake_get(url, params=None, headers=None, timeout=None):
        ticker = url.rsplit("/", 1)[-1]
        calls.append(ticker)
        if callable(rows):
            rows_ = rows(ticker)
        else:
            rows_ = rows
        if rows_ == "no_result":
            return FakeResponse({"chart": {"error": None, "result": []}}, **response_kwargs)
        if rows_ == "error":
            return FakeResponse(
                {"chart": {"error": {"description": "No data found, symbol may be delisted"}}},
                **response_kwargs,
            )
        return FakeResponse(payload(rows_, currency=currency, symbol=ticker), **response_kwargs)

    prices_mod.requests.get = fake_get


def boom(*args, **kwargs):
    raise prices_mod.requests.RequestException("network down")


def use_boom():
    prices_mod.requests.get = boom


TODAY = dt.date.today()
SETTLED = (TODAY - dt.timedelta(days=10)).isoformat()   # safely in the past
RECENT = TODAY.isoformat()                              # never cached


def scratch():
    handle = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    handle.close()
    return pathlib.Path(handle.name)


REAL_GET = prices_mod.requests.get

try:
    # --- reading a close -----------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        cache = pathlib.Path(tmp) / "c.json"

        use([("2026-09-28", 509.22), ("2026-09-29", 508.96), ("2026-09-30", 512.9)])
        q = close_on("MSFT", "2026-09-30", cache_path=cache)
        check("exact date is used", q.price == 512.9 and q.price_date == "2026-09-30", str(q))
        check("not stale when the date matches", q.stale is False)

        use([("2026-09-28", 509.22), ("2026-09-29", 508.96), ("2026-09-30", 512.9)])
        q = close_on("MSFT", "2026-10-04", cache_path=cache)
        check("falls back to the last earlier close",
              q.price == 512.9 and q.price_date == "2026-09-30", str(q))
        check("the fallback is reported as stale", q.stale is True)

        check("ticker is upper-cased", close_on("msft", "2026-09-30", cache_path=cache).ticker == "MSFT")

        use([("2026-09-30", 512.9000244140625)])
        check("price is rounded to 4dp", close_on("MSFT", "2026-09-30", cache_path=cache).price == 512.9)

        use([("2026-09-30", 1.0)], currency="JPY")
        check("trading currency is read", close_on("6758.T", "2026-09-30", cache_path=cache).currency == "JPY")

    # --- only rows inside the window count ------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        cache = pathlib.Path(tmp) / "c.json"
        # Yahoo sometimes hands back a row past the requested range.
        use([("2026-09-25", 100.0), ("2026-09-30", 200.0), ("2026-10-05", 999.0)])
        q = close_on("MSFT", "2026-09-30", cache_path=cache)
        check("rows outside the window are ignored", q.price == 200.0, str(q))
        stored = json.loads(cache.read_text())["MSFT"]
        check("out-of-window rows are not cached", "2026-10-05" not in stored, str(sorted(stored)))

    # --- errors ---------------------------------------------------------------
    use("error")
    raises("unknown ticker", "No data found",
           lambda: close_on("NOPE", "2026-09-30", cache_path=scratch()))
    use("no_result")
    raises("empty result", "no price data",
           lambda: close_on("NOPE", "2026-09-30", cache_path=scratch()))
    use([(SETTLED, 1.0)], not_json=True)
    raises("non-JSON response", "not JSON",
           lambda: close_on("MSFT", "2026-09-30", cache_path=scratch()))
    use([(SETTLED, 1.0)], status=503)
    raises("HTTP error", "HTTP 503",
           lambda: close_on("MSFT", "2026-09-30", cache_path=scratch()))
    use_boom()
    raises("network failure", "could not reach",
           lambda: close_on("MSFT", "2026-09-30", cache_path=scratch()))
    prices_mod.requests.get = REAL_GET

    raises("blank ticker", "non-empty string", lambda: close_on("  ", "2026-09-30"))
    raises("empty ticker", "non-empty string", lambda: close_on("", "2026-09-30"))
    raises("bad date", "not a valid ISO date",
           lambda: close_on("MSFT", "31/09/2026", cache_path=scratch()))

    use([])
    raises("no close in the window", "no close on or before",
           lambda: close_on("MSFT", SETTLED, cache_path=scratch()))

    # The only close available is older than the lookback window allows.
    OLD = (TODAY - dt.timedelta(days=20)).isoformat()
    use([(OLD, 1.0)])
    raises("a too-old close is refused rather than reused", "no close on or before",
           lambda: close_on("MSFT", SETTLED, lookback_days=3, cache_path=scratch()))
    check("the same close is accepted once the window is wide enough",
          close_on("MSFT", SETTLED, lookback_days=30, cache_path=scratch()).price == 1.0)

    # --- caching --------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        cache = pathlib.Path(tmp) / "c.json"

        use([(SETTLED, 512.9)])
        close_on("MSFT", SETTLED, cache_path=cache)
        check("a settled date is fetched once", len(calls) == 1, str(calls))
        close_on("MSFT", SETTLED, cache_path=cache)
        check("a settled date is then served from cache", len(calls) == 1, str(calls))

        # A close from outside the lookback window must not satisfy the query.
        stale_only = scratch()
        stale_only.write_text(json.dumps({"MSFT": {
            "2020-01-02": 1.0, "currency": "USD"}}))
        use([(SETTLED, 512.9)])
        q = close_on("MSFT", SETTLED, cache_path=stale_only)
        check("a cache entry outside the window is refetched",
              q.price == 512.9 and len(calls) == 1, f"{q.price}, {len(calls)} calls")

        # Recent closes are never cached, so a recent query must always refetch.
        use([(RECENT, 517.53)])
        close_on("MSFT", RECENT, cache_path=cache)
        close_on("MSFT", RECENT, cache_path=cache)
        check("a recent date is never served from cache", len(calls) == 2, str(calls))
        stored = json.loads(cache.read_text())["MSFT"]
        check("recent closes are not cached", RECENT not in stored, str(sorted(stored)))

        # A gap in the cache is not an answer. The cache is a union of windows
        # fetched on demand, so it can have a hole; settled history used to be
        # served whenever *any* earlier day was cached, which silently returned a
        # close days out of date. Coverage has to be recorded to be trusted.
        with tempfile.TemporaryDirectory() as tmp:
            holed = pathlib.Path(tmp) / "c.json"
            # Every date must be settled, or the cache is bypassed by design.
            base = TODAY - dt.timedelta(days=30)
            ladder = [((base + dt.timedelta(days=i)).isoformat(), 100.0 + i)
                      for i in range(11)]
            use(ladder)

            base_iso = base.isoformat()
            check("a settled close is fetched first",
                  close_on("MSFT", base_iso, cache_path=holed).price == 100.0)
            ahead = ladder[10][0]
            q = close_on("MSFT", ahead, cache_path=holed)
            check("a gap past the cached window forces a fetch",
                  q.price == 110.0 and q.price_date == ahead,
                  f"{q.price} dated {q.price_date}, expected 110.0 on {ahead}")
            check("that cost a second call", len(calls) == 2, str(calls))

            seen = len(calls)
            again = close_on("MSFT", ahead, cache_path=holed)
            check("once covered, the window is cached again", len(calls) == seen,
                  str(calls))
            check("...and answers identically", again.price == q.price)

            stored = json.loads(holed.read_text())["MSFT"]
            check("coverage is recorded with the prices",
                  bool(stored.get("_ranges")), str(stored.get("_ranges")))

        # An entry written before coverage was tracked carries no promise, so it
        # must be refetched rather than believed -- otherwise every stale cache
        # on disk stays wrong forever.
        with tempfile.TemporaryDirectory() as tmp:
            legacy = pathlib.Path(tmp) / "c.json"
            legacy.write_text(json.dumps({"MSFT": {"currency": "USD", SETTLED: 999.0}}))
            use([(SETTLED, 512.9)])
            q = close_on("MSFT", SETTLED, cache_path=legacy)
            check("a cache with no coverage record is refetched",
                  q.price == 512.9, f"{q.price}, expected 512.9")

        cache.write_text("{not json")
        use([(SETTLED, 512.9)])
        check("a corrupt cache is ignored, not fatal",
              close_on("MSFT", SETTLED, cache_path=cache).price == 512.9)

finally:
    prices_mod.requests.get = REAL_GET

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")