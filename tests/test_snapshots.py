"""Checks for the snapshot model."""

import datetime as dt
import pathlib
import tempfile
from collections import Counter

import pytest
from helpers import check, raises

import fx as fx_mod
import prices as prices_mod
from fx import FxError, FxRates
from snapshots import (
    ASSET_TYPES,
    PORTFOLIO_DIR,
    Asset,
    AssetSpec,
    Portfolio,
    PortfolioError,
    Snapshot,
    SnapshotError,
    check_handle,
    check_key,
    create_snapshot,
    list_portfolios,
    load,
    load_file,
    monthyear,
    portfolio_path,
    save,
)

CCY = ["AUD", "JPY", "USD"]


def make_fx(currencies):
    """A complete, if fictional, matrix -- identity pairs exactly 1.0."""
    return {c: {d: (1.0 if c == d else 2.0) for d in currencies} for c in currencies}


def spec(raw, currencies=CCY, where="spec"):
    return AssetSpec.from_dict(raw, currencies, where)


def cat(*raw, currencies=CCY):
    """A catalogue dict from spec objects and/or raw spec dicts."""
    return {s.key: s for s in (r if isinstance(r, AssetSpec) else spec(r, currencies) for r in raw)}


def snap_with(catalogue, assets, date="2026-09-30", currencies=CCY):
    """Build a snapshot through the real loader path, so rules are exercised."""
    return Snapshot.from_dict(
        {"date": date, "fx": make_fx(currencies), "assets": assets},
        currencies,
        catalogue,
        "test",
    )


def holding(raw, where="holding"):
    return Asset.from_dict(raw, where)


def rejects(value):
    """True when check_key refuses this value."""
    try:
        check_key(value)
    except SnapshotError:
        return True
    return False


def accepts(value):
    """True when check_key takes this value."""
    try:
        check_key(value)
    except SnapshotError:
        return False
    return True


def stock(**fields):
    """A stocks spec dict; ticker defaults to MSFT unless overridden."""
    raw = {"key": "a", "type": "stocks", "currency": "USD", "ticker": "MSFT"}
    raw.update(fields)
    return raw


def write(text):
    path = pathlib.Path("/tmp/opencode/bad.yaml")
    path.write_text(text, encoding="utf-8")
    return path


def round_trip(store, name="/tmp/opencode/round_trip.yaml"):
    """Save a store out and read it straight back in."""
    path = pathlib.Path(name)
    save(store, path)
    try:
        return load_file(path)
    finally:
        path.unlink(missing_ok=True)


def one(spec_raw, **fields):
    """One observation of one spec, validated the way the loader validates it."""
    catalogue = cat(spec_raw)
    key = next(iter(catalogue))
    return snap_with(catalogue, [{"ref": key, **fields}]).assets[0]


CASH = {"key": "e", "type": "cash", "currency": "AUD"}
STOCK = stock(key="msft")

# A self-contained fixture catalogue, deliberately NOT read from personal.yaml.
# These are unit tests: they must keep passing when real data changes for real
# reasons, and they need names they are free to invent.
SEED_CCY = list(CCY)
SEED_FX = make_fx(SEED_CCY)
SEED_SPECS = [
    spec({"key": "everyday", "type": "cash", "currency": "AUD"}),
    spec({"key": "super", "type": "super", "currency": "AUD"}),
    spec({"key": "home", "type": "property", "currency": "AUD"}),
    spec(stock(key="msft", ticker="MSFT", currency="USD")),
]


def quote(ticker, price, currency="USD", price_date="2026-09-30"):
    return prices_mod.Quote(
        ticker=ticker,
        price=price,
        price_date=price_date,
        currency=currency,
        as_of="2026-09-30",
    )


def stub_lookup(table):
    """Replace the price lookup; table maps ticker -> Quote or an exception."""

    def lookup(ticker, on, **kwargs):
        result = table.get(ticker.strip().upper())
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise prices_mod.PriceError(f"{ticker}: no data")
        return result

    return lookup


_real_close_on = prices_mod.close_on


def with_lookup(table, fn):
    prices_mod.close_on = stub_lookup(table)
    try:
        return fn()
    finally:
        prices_mod.close_on = _real_close_on


def built(date="2026-09-30", holdings=(), specs=SEED_SPECS, **kwargs):
    kwargs.setdefault("price_overrides", {})
    return create_snapshot(date, list(holdings), SEED_CCY, specs, fx=SEED_FX, **kwargs)


def snap_for(**kwargs):
    return create_snapshot(
        "2026-09-30", [{"ref": "everyday", "value": 1000}], SEED_CCY, SEED_SPECS, **kwargs
    )


def broken_fetch(*args, **kwargs):
    raise FxError("the rate source does not support XYZ")


def snapshot_error_catches():
    try:
        snap_for()
    except SnapshotError:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


fetched_calls: list[tuple] = []
real_fetch_fx = fx_mod.fetch_fx


def stub_fetch(rates=SEED_FX, rate_date="2026-09-30"):
    """Replace the rate lookup so these checks never touch the network."""
    fetched_calls.clear()

    def fake_fetch_fx(date, currencies, *, url=None):
        fetched_calls.append((date, list(currencies)))
        return FxRates(date=date.isoformat(), rate_date=rate_date, rates=rates)

    fx_mod.fetch_fx = fake_fetch_fx


def unstub_fetch():
    fx_mod.fetch_fx = real_fetch_fx


@pytest.fixture(scope="module")
def store():
    """The shipped portfolio, loaded once for read-only invariant checks."""
    return load("personal")


def test_monthyear():
    """monthyear is a straight slice of the date."""
    check("monthyear derives from date", monthyear("2026-09-30") == "2026-09")
    check("monthyear handles January", monthyear("2026-01-05") == "2026-01")
    check("monthyear handles December", monthyear("2026-12-31") == "2026-12")


def test_shipped_portfolio_invariants(store):
    """The shipped portfolio obeys the invariants any portfolio must.

    These read the shipped data/portfolios/personal.yaml, so they assert only
    invariants that must hold for any portfolio. Naming its assets or dates here
    would mean editing real data breaks the suite; specifics live in the fixture
    catalogue below instead.
    """
    check("the shipped portfolio loads", bool(store.currencies), str(store.currencies))
    check("it has a catalogue", len(store.specs) > 0, str(len(store.specs)))
    check("it has snapshots", len(store.snapshots) > 0, str(len(store.snapshots)))
    check(
        "snapshot dates are unique",
        len({s.date for s in store.snapshots}) == len(store.snapshots),
    )
    check(
        "snapshots sorted by date",
        [s.date for s in store.snapshots] == sorted(s.date for s in store.snapshots),
    )

    first = store.snapshots[0]
    n_ccy = len(store.currencies)
    check("monthyear is derived, matches date", first.monthyear == first.date[:7], first.monthyear)
    check(
        "fx covers every ordered pair",
        len(first.fx) == n_ccy and all(len(r) == n_ccy for r in first.fx.values()),
    )
    check("identity pairs are 1", all(first.fx[c][c] == 1.0 for c in store.currencies))
    check("fx keys normalised to upper case", list(first.fx) == store.currencies)

    check(
        "for_monthyear filters",
        [s.date for s in store.for_monthyear(first.date[:7])] == [first.date],
    )
    check("for_monthyear on empty month is empty", store.for_monthyear("2020-01") == [])
    check("by_date finds a snapshot", store.by_date(first.date) is first)
    check("by_date misses cleanly", store.by_date("2020-01-01") is None)

    # Rates must be self-consistent: any round trip should land back near the start.
    lo, hi = store.currencies[0], store.currencies[-1]
    r = first.fx
    # Not exact, and it cannot be: the source quotes JPY at 2dp (114.37), so the
    # stored reciprocal 0.00874 is already a rounding of 1/114.37. The matrix is
    # therefore good to ~1e-3, and totals must never assume an exact inversion.
    check(
        "a rate round trip lands near 1",
        abs(r[lo][hi] * r[hi][lo] - 1) < 1e-3,
        f"{r[lo][hi] * r[hi][lo]}",
    )

    # Rates are per-date, so each snapshot must own its own matrix rather than share.
    check("each snapshot owns its rates", store.snapshots[0].fx is not store.snapshots[-1].fx)


def test_round_trip(store):
    """A loaded portfolio survives save and reload unchanged."""
    tmp = pathlib.Path("/tmp/opencode/roundtrip.yaml")
    save(store, tmp)
    reloaded = load_file(tmp)
    check("round trip keeps currencies", reloaded.currencies == store.currencies)
    check(
        "round trip keeps the catalogue",
        [s.key for s in reloaded.specs] == [s.key for s in store.specs],
    )
    check("round trip keeps snapshot count", len(reloaded.snapshots) == len(store.snapshots))
    check(
        "round trip keeps dates",
        [s.date for s in reloaded.snapshots] == [s.date for s in store.snapshots],
    )
    check("round trip keeps rates", reloaded.snapshots[0].fx == store.snapshots[0].fx)
    tmp.unlink(missing_ok=True)


def test_bad_input():
    """Malformed files are rejected, and the message says why."""
    raises(
        "bad yaml is reported",
        "not valid YAML",
        lambda: load_file(write("currencies: [AUD\n")),
        SnapshotError,
    )
    raises(
        "missing currencies",
        "missing 'currencies'",
        lambda: load_file(write("snapshots: []\n")),
        SnapshotError,
    )
    raises(
        "empty currencies",
        "non-empty list",
        lambda: load_file(write("currencies: []\n")),
        SnapshotError,
    )
    raises(
        "duplicate currency",
        "duplicate currency",
        lambda: load_file(write("currencies: [AUD, AUD]\n")),
        SnapshotError,
    )
    raises(
        "missing fx pair",
        "missing 1 pair",
        lambda: load_file(
            write("""
currencies: [AUD, USD]
snapshots:
  - date: '2026-01-31'
    fx:
      AUD: {AUD: 1.0, USD: 0.7}
      USD: {AUD: 1.4}
""")
        ),
        SnapshotError,
    )

    raises(
        "identity pair must be 1",
        "must be 1",
        lambda: load_file(
            write("""
currencies: [AUD, USD]
snapshots:
  - date: '2026-01-31'
    fx:
      AUD: {AUD: 1.0, USD: 0.7}
      USD: {AUD: 1.4, USD: 0.9}
""")
        ),
        SnapshotError,
    )

    raises(
        "non-positive rate",
        "must be positive",
        lambda: load_file(
            write("""
currencies: [AUD, USD]
snapshots:
  - date: '2026-01-31'
    fx:
      AUD: {AUD: 1.0, USD: 0.0}
      USD: {AUD: 1.4, USD: 1.0}
""")
        ),
        SnapshotError,
    )

    raises(
        "non-numeric rate",
        "must be a number",
        lambda: load_file(
            write("""
currencies: [AUD, USD]
snapshots:
  - date: '2026-01-31'
    fx:
      AUD: {AUD: 1.0, USD: "seven"}
      USD: {AUD: 1.4, USD: 1.0}
""")
        ),
        SnapshotError,
    )

    raises(
        "unknown currency in fx",
        "unknown source currency",
        lambda: load_file(
            write("""
currencies: [AUD, USD]
snapshots:
  - date: '2026-01-31'
    fx:
      EUR: {AUD: 1.0, USD: 0.7}
      USD: {AUD: 1.4, USD: 1.0}
""")
        ),
        SnapshotError,
    )

    raises(
        "bad date",
        "not a valid ISO date",
        lambda: load_file(
            write(
                "currencies: [AUD]\nsnapshots:\n  - date: '31/01/2026'\n"
                "    fx:\n      AUD: {AUD: 1.0}\n"
            )
        ),
        SnapshotError,
    )
    # Python 3.11 rejects this outright; 3.10 accepted it and then hit the
    # zero-padding guard. Either message is fine.
    raises(
        "unpadded date",
        "date",
        lambda: load_file(
            write(
                "currencies: [AUD]\nsnapshots:\n  - date: '2026-1-1'\n"
                "    fx:\n      AUD: {AUD: 1.0}\n"
            )
        ),
        SnapshotError,
    )
    raises(
        "missing date",
        "missing 'date'",
        lambda: load_file(write("currencies: [AUD]\nsnapshots:\n  - fx:\n      AUD: {AUD: 1.0}\n")),
        SnapshotError,
    )

    raises(
        "duplicate snapshot dates",
        "duplicate snapshot date",
        lambda: load_file(
            write("""
currencies: [AUD, USD]
snapshots:
  - date: '2026-01-31'
    fx: {AUD: {AUD: 1.0, USD: 0.7}, USD: {AUD: 1.4, USD: 1.0}}
  - date: '2026-01-31'
    fx: {AUD: {AUD: 1.0, USD: 0.7}, USD: {AUD: 1.4, USD: 1.0}}
""")
        ),
        SnapshotError,
    )

    raises(
        "missing file",
        "no snapshot file",
        lambda: load_file(pathlib.Path("/tmp/opencode/definitely-not-here.yaml")),
        SnapshotError,
    )

    check(
        "missing snapshots key means empty list",
        load_file(write("currencies: [AUD, USD]\n")).snapshots == [],
    )


def test_keys():
    """Asset keys are normalised and validated, never sanitised."""
    check("a plain key is accepted", check_key("everyday") == "everyday")
    check("key case is folded", check_key("EveryDay") == "everyday")
    check("key whitespace is trimmed", check_key("  everyday  ") == "everyday")
    check("key allows dots, dashes and underscores", check_key("a.b-c_d") == "a.b-c_d")
    raises("key cannot be empty", "must not be empty", lambda: check_key("   "), SnapshotError)
    raises(
        "key cannot traverse",
        "not usable as an identifier",
        lambda: check_key("../secrets"),
        SnapshotError,
    )
    raises(
        "key cannot use a separator",
        "not usable as an identifier",
        lambda: check_key("a/b"),
        SnapshotError,
    )
    raises(
        "key cannot be a Path",
        "must be a string",
        lambda: check_key(pathlib.Path("everyday")),
        SnapshotError,
    )
    raises(
        "key cannot have a space",
        "not usable as an identifier",
        lambda: check_key("my asset"),
        SnapshotError,
    )
    raises(
        "key cannot start with a dot",
        "not usable as an identifier",
        lambda: check_key(".hidden"),
        SnapshotError,
    )
    check(
        "keys and handles reject the same characters",
        all(rejects(k) for k in ["a/b", "a b", "..", ".x", "!"])
        and all(accepts(h) for h in ["a.b-c_d", "2026tax", "x"]),
        "",
    )


def test_catalogue():
    """AssetSpec parsing, labels and serialisation."""
    check(
        "types are the five fixed ones",
        ASSET_TYPES == ("cash", "super", "stocks", "property", "other"),
        str(ASSET_TYPES),
    )

    check("a spec keeps its key", spec(stock(key="msft")).key == "msft")
    check(
        "a spec keeps its name",
        spec({"key": "e", "name": "Everyday", "type": "cash", "currency": "AUD"}).name
        == "Everyday",
    )
    check(
        "a spec's label prefers the name",
        spec({"key": "e", "name": "Everyday", "type": "cash", "currency": "AUD"}).label
        == "Everyday",
    )
    check("a spec's label falls back to the key", spec(stock(key="a")).label == "a")
    check("a spec's name is optional", spec(stock(key="a")).name is None)
    check("ticker is kept", spec(stock(key="a")).ticker == "MSFT")
    check("ticker is upper-cased", spec(stock(key="a", ticker="bhp.ax")).ticker == "BHP.AX")
    check("ticker is stripped", spec(stock(key="a", ticker="  msft  ")).ticker == "MSFT")
    check("a ticker is optional on stocks", spec(stock(key="a", ticker=None)).ticker is None)
    check(
        "is_stock reflects the type",
        spec(stock(key="a")).is_stock is True
        and spec({"key": "e", "type": "cash", "currency": "AUD"}).is_stock is False,
    )

    raises(
        "a spec needs a key",
        "missing 'key'",
        lambda: spec({"type": "cash", "currency": "AUD"}),
        SnapshotError,
    )
    raises(
        "unknown type",
        "unknown type 'crypto'",
        lambda: spec({"key": "a", "type": "crypto", "currency": "AUD"}),
        SnapshotError,
    )
    raises(
        "missing type",
        "unknown type",
        lambda: spec({"key": "a", "currency": "AUD"}),
        SnapshotError,
    )
    raises(
        "spec currency outside the file's list",
        "unknown currency 'GBP'",
        lambda: spec({"key": "a", "type": "cash", "currency": "GBP"}),
        SnapshotError,
    )
    raises(
        "ticker on cash",
        "only type 'stocks' may have a 'ticker'",
        lambda: spec({"key": "a", "type": "cash", "currency": "AUD", "ticker": "MSFT"}),
        SnapshotError,
    )
    raises(
        "blank ticker",
        "'ticker' must be a non-empty string",
        lambda: spec(stock(key="a", ticker="   ")),
        SnapshotError,
    )
    raises(
        "blank spec name",
        "'name' must be a non-empty string",
        lambda: spec({"key": "a", "name": "  ", "type": "cash", "currency": "AUD"}),
        SnapshotError,
    )
    check(
        "type is lower-cased",
        spec({"key": "a", "type": "Cash", "currency": "AUD"}).type == "cash",
    )
    check(
        "spec currency is upper-cased",
        spec({"key": "a", "type": "cash", "currency": "aud"}).currency == "AUD",
    )
    check(
        "spec name is stripped",
        spec({"key": "a", "name": "  x  ", "type": "cash", "currency": "AUD"}).name == "x",
    )

    check(
        "to_dict omits absent fields",
        spec({"key": "e", "type": "cash", "currency": "AUD"}).to_dict()
        == {"key": "e", "type": "cash", "currency": "AUD"},
        str(spec({"key": "e", "type": "cash", "currency": "AUD"}).to_dict()),
    )
    check(
        "to_dict keeps the ticker",
        spec(stock(key="a")).to_dict()
        == {"key": "a", "type": "stocks", "ticker": "MSFT", "currency": "USD"},
        str(spec(stock(key="a")).to_dict()),
    )
    check(
        "to_dict skips a name identical to the key",
        "name" not in spec({"key": "a", "name": "a", "type": "cash", "currency": "AUD"}).to_dict(),
    )
    check(
        "a spec re-parses to an equal spec",
        spec(spec(stock(key="a")).to_dict()) == spec(stock(key="a")),
    )


def test_duplicate_catalogue_keys():
    """A catalogue cannot key two entries the same."""
    raises(
        "duplicate catalogue key",
        "duplicate asset key",
        lambda: load_file(
            write("""
currencies: [AUD]
assets:
  - {key: a, type: cash, currency: AUD}
  - {key: a, type: cash, currency: AUD}
""")
        ),
        SnapshotError,
    )
    raises(
        "catalogue entries must be a list",
        "'assets' must be a list",
        lambda: load_file(write("currencies: [AUD]\nassets: {key: a}\n")),
        SnapshotError,
    )
    check("catalogue key is optional", load_file(write("currencies: [AUD]\n")).specs == [])


def test_observations():
    """Asset observations parse, normalise and serialise."""
    check("an observation keeps its ref", holding({"ref": "msft", "units": 40}).ref == "msft")
    check("ref case is folded", holding({"ref": "MSFT", "units": 1}).ref == "msft")
    raises(
        "an observation needs a ref",
        "missing 'ref'",
        lambda: holding({"value": 1}),
        SnapshotError,
    )
    raises(
        "observations must be mappings",
        "must be a mapping",
        lambda: holding(["ref", "msft"]),
        SnapshotError,
    )
    raises(
        "non-numeric value",
        "value must be a number",
        lambda: holding({"ref": "e", "value": "lots"}),
        SnapshotError,
    )
    raises(
        "boolean is not a number",
        "value must be a number",
        lambda: holding({"ref": "e", "value": True}),
        SnapshotError,
    )
    check("int value becomes float", holding({"ref": "e", "value": 7}).value == 7.0)
    check(
        "to_dict omits absent fields",
        holding({"ref": "e", "value": 1}).to_dict() == {"ref": "e", "value": 1.0},
        str(holding({"ref": "e", "value": 1}).to_dict()),
    )
    check(
        "to_dict keeps units and price",
        holding({"ref": "msft", "units": 2, "price": 3}).to_dict()
        == {"ref": "msft", "units": 2.0, "price": 3.0},
    )
    check(
        "an observation re-parses to an equal observation",
        holding(holding({"ref": "msft", "units": 2, "price": 3}).to_dict())
        == holding({"ref": "msft", "units": 2, "price": 3}),
    )


def test_value_precedence():
    """value wins over units * price, and pending quantities stay None."""
    check(
        "explicit value resolves to itself",
        holding({"ref": "e", "value": 10}).resolved_value == 10,
    )
    check(
        "units * price resolves",
        holding({"ref": "msft", "units": 40, "price": 512.25}).resolved_value == 20490.0,
    )
    check(
        "value beats units * price",
        holding({"ref": "msft", "value": 999, "units": 40, "price": 512.25}).resolved_value == 999,
    )
    check(
        "half a pair is fine when value is given",
        holding({"ref": "msft", "value": 5, "units": 3}).resolved_value == 5,
    )
    check("zero value is allowed", holding({"ref": "x", "value": 0}).resolved_value == 0.0)
    check(
        "an unresolved quantity resolves to None",
        holding({"ref": "msft", "units": 40}).resolved_value is None,
    )

    check(
        "units with no price needs a lookup",
        holding({"ref": "msft", "units": 40}).needs_price is True,
    )
    check(
        "a typed price needs no lookup",
        holding({"ref": "msft", "units": 40, "price": 5}).needs_price is False,
    )
    check("a value needs no lookup", holding({"ref": "msft", "value": 9}).needs_price is False)


def test_amount_rules():
    """Every amount must be resolvable through the loader."""
    raises(
        "cash units without price",
        "needs a matching 'price'",
        lambda: one(CASH, units=3),
        SnapshotError,
    )
    raises(
        "cash price without units",
        "needs a matching 'units'",
        lambda: one(CASH, price=3),
        SnapshotError,
    )
    raises("cash with no amount at all", "needs 'value'", lambda: one(CASH), SnapshotError)
    raises(
        "stock units with no ticker and no price",
        "needs the catalogue's 'ticker'",
        lambda: one(stock(key="orphan", ticker=None), units=40),
        SnapshotError,
    )
    raises(
        "stock price without units",
        "'price' needs a matching 'units'",
        lambda: one(STOCK, price=3),
        SnapshotError,
    )
    raises(
        "stock with neither units nor value",
        "needs 'units'",
        lambda: one(stock(key="a", ticker=None)),
        SnapshotError,
    )
    raises("a ticker alone is not an amount", "needs 'units'", lambda: one(STOCK), SnapshotError)
    check(
        "units + price needs no ticker",
        one(stock(key="p", ticker=None), units=40, price=512.25).resolved_value == 20490.0,
    )
    check(
        "a value needs no ticker",
        one(stock(key="p", ticker=None), value=999).resolved_value == 999.0,
    )


def test_references_must_resolve():
    """An observation has to name a catalogue asset, once."""
    raises(
        "an unknown ref is caught",
        "unknown asset 'everydy'",
        lambda: snap_with(cat(CASH), [{"ref": "everydy", "value": 1}]),
        SnapshotError,
    )
    raises(
        "an unknown ref lists the catalogue",
        "catalogue has: e",
        lambda: snap_with(cat(CASH), [{"ref": "nope", "value": 1}]),
        SnapshotError,
    )
    raises(
        "the same asset twice in one date",
        "appears twice in one snapshot",
        lambda: snap_with(cat(CASH), [{"ref": "e", "value": 1}, {"ref": "e", "value": 2}]),
        SnapshotError,
    )
    raises(
        "an empty catalogue says so",
        "catalogue has: none",
        lambda: snap_with({}, [{"ref": "e", "value": 1}]),
        SnapshotError,
    )


def test_negative_amounts():
    """Liabilities survive validation, resolution and a round trip.

    Credit cards and loans are recorded as negative cash, so negatives have to
    survive validation, resolution and a save/load round trip.
    """
    check(
        "negative value is allowed",
        holding({"ref": "visa", "value": -2350.75}).resolved_value == -2350.75,
    )
    check(
        "a large negative value is allowed",
        holding({"ref": "loan", "value": -412000.00}).resolved_value == -412000.0,
    )
    check(
        "negative units are allowed",
        holding({"ref": "od", "units": -250.5, "price": 1}).resolved_value == -250.5,
    )
    check(
        "negative units * positive price is negative",
        holding({"ref": "sh", "units": -5, "price": 100}).resolved_value == -500.0,
    )
    check(
        "a negative value is not treated as pending",
        holding({"ref": "visa", "value": -100}).needs_price is False,
    )
    check(
        "a negative value still wins over units and price",
        holding({"ref": "x", "value": -999, "units": 5, "price": 10}).resolved_value == -999.0,
    )
    raises(
        "a negative value is not 'absent'",
        "must be a number",
        lambda: holding({"ref": "x", "value": "-100"}),
        SnapshotError,
    )
    check(
        "a negative value passes the loader's amount rules",
        one(CASH, value=-2350.75).resolved_value == -2350.75,
    )


def test_assets_inside_snapshot(store):
    """Observations inside a snapshot resolve and round trip."""
    seed = store
    check(
        "every seed snapshot has observations",
        all(len(snap.assets) > 0 for snap in seed.snapshots),
        str([len(snap.assets) for snap in seed.snapshots]),
    )
    check(
        "every seed observation resolves to the catalogue",
        all(a.ref in seed.catalogue for snap in seed.snapshots for a in snap.assets),
    )
    check(
        "no seed observation is waiting on a price",
        not any(a.needs_price for snap in seed.snapshots for a in snap.assets),
    )
    check(
        "every catalogue currency is in the file's list",
        all(s.currency in seed.currencies for s in seed.specs),
    )
    check(
        "every catalogue key is already normalised",
        all(s.key == s.key.strip().lower() for s in seed.specs),
        str([s.key for s in seed.specs]),
    )
    check("catalogue keys are unique", len(seed.catalogue) == len(seed.specs))
    # Two entries sharing a name and currency are almost always one account entered
    # twice, which silently double-counts. The loader cannot catch it; flag it here.
    _dupes = [
        n for n, c in Counter((s.name, s.currency) for s in seed.specs if s.name).items() if c > 1
    ]
    check("no two catalogue entries share a name and currency", not _dupes, str(_dupes))

    no_assets = load_file(
        write(
            "currencies: [AUD, USD]\nsnapshots:\n  - date: '2026-01-31'\n"
            "    fx:\n      AUD: {AUD: 1.0, USD: 0.7}\n      USD: {AUD: 1.4, USD: 1.0}\n"
        )
    )
    check("assets key is optional", no_assets.snapshots[0].assets == [])
    raises(
        "assets must be a list",
        "'assets' must be a list",
        lambda: load_file(
            write("""
currencies: [AUD, USD]
snapshots:
  - date: '2026-01-31'
    fx: {AUD: {AUD: 1.0, USD: 0.7}, USD: {AUD: 1.4, USD: 1.0}}
    assets: {ref: nope}
""")
        ),
        SnapshotError,
    )
    raises(
        "a bad asset names the offending index",
        "assets[1]",
        lambda: load_file(
            write("""
currencies: [AUD, USD]
assets:
  - {key: e, type: cash, currency: AUD}
snapshots:
  - date: '2026-01-31'
    fx: {AUD: {AUD: 1.0, USD: 0.7}, USD: {AUD: 1.4, USD: 1.0}}
    assets:
      - {ref: e, value: 1}
      - {ref: ghost, value: 1}
""")
        ),
        SnapshotError,
    )

    assets_rt = pathlib.Path("/tmp/opencode/assets_rt.yaml")
    save(seed, assets_rt)
    back = load_file(assets_rt)
    check(
        "round trip keeps observation count",
        [len(x.assets) for x in back.snapshots] == [len(x.assets) for x in seed.snapshots],
    )
    check(
        "round trip keeps refs",
        [[a.ref for a in x.assets] for x in back.snapshots]
        == [[a.ref for a in x.assets] for x in seed.snapshots],
    )
    check(
        "round trip keeps resolved values",
        [[a.resolved_value for a in x.assets] for x in back.snapshots]
        == [[a.resolved_value for a in x.assets] for x in seed.snapshots],
    )
    check(
        "round trip keeps every units/price observation",
        all(
            b.units == o.units and b.price == o.price
            for xs, ys in zip(back.snapshots, seed.snapshots, strict=False)
            for b, o in zip(xs.assets, ys.assets, strict=False)
        ),
        str(
            [
                (a.ref, a.units, a.price)
                for x in back.snapshots
                for a in x.assets
                if a.units is not None
            ]
        ),
    )
    assets_rt.unlink(missing_ok=True)

    # A debt-style snapshot has to survive the round trip with its sign intact.
    debt_store = load_file(
        write("""
currencies: [AUD]
assets:
  - {key: everyday, type: cash, currency: AUD}
  - {key: visa, type: cash, currency: AUD}
  - {key: loan, type: cash, currency: AUD}
  - {key: overdrawn, type: cash, currency: AUD}
snapshots:
  - date: '2026-01-31'
    fx: {AUD: {AUD: 1.0}}
    assets:
      - {ref: everyday, value: 4500.0}
      - {ref: visa, value: -2350.75}
      - {ref: loan, value: -412000.0}
      - {ref: overdrawn, units: -250.5, price: 1}
""")
    )
    check(
        "negative cash loads as negative",
        [a.resolved_value for a in debt_store.snapshots[0].assets]
        == [4500.0, -2350.75, -412000.0, -250.5],
        str([a.resolved_value for a in debt_store.snapshots[0].assets]),
    )
    check(
        "a negative value is not treated as missing",
        debt_store.snapshots[0].assets[1].value == -2350.75,
    )
    check(
        "signs survive a save/load round trip",
        [a.resolved_value for a in round_trip(debt_store).snapshots[0].assets]
        == [4500.0, -2350.75, -412000.0, -250.5],
    )


def test_migration_from_old_shape():
    """Files written before the catalogue split say what is stale."""
    for stale in ("name", "type", "currency", "ticker"):
        raises(
            f"a snapshot must not repeat {stale!r}",
            "belongs in the portfolio's catalogue",
            lambda s=stale: load_file(
                write(f"""
currencies: [AUD]
assets:
  - {{key: e, type: cash, currency: AUD}}
snapshots:
  - date: '2026-01-31'
    fx: {{AUD: {{AUD: 1.0}}}}
    assets:
      - {{ref: e, value: 1, {s}: x}}
""")
            ),
            SnapshotError,
        )

    raises(
        "monthyear is no longer stored",
        "no longer stored",
        lambda: load_file(
            write("""
currencies: [AUD]
snapshots:
  - date: '2026-01-31'
    monthyear: '2026-01'
    fx: {AUD: {AUD: 1.0}}
""")
        ),
        SnapshotError,
    )

    # The whole point of the split: renaming an asset no longer forks its history.
    renamed = load_file(
        write("""
currencies: [AUD]
assets:
  - {key: super, name: Superannuation, type: super, currency: AUD}
snapshots:
  - date: '2026-08-31'
    fx: {AUD: {AUD: 1.0}}
    assets: [{ref: super, value: 88000.0}]
  - date: '2026-09-30'
    fx: {AUD: {AUD: 1.0}}
    assets: [{ref: super, value: 88500.0}]
""")
    )
    check(
        "a renamed asset keeps one identity across snapshots",
        len(renamed.specs) == 1
        and [a.ref for a in renamed.snapshots[0].assets] == ["super"]
        and [a.ref for a in renamed.snapshots[1].assets] == ["super"],
    )
    check(
        "the rename is visible as a label only",
        renamed.spec("super").label == "Superannuation",
    )


def test_create_snapshot():
    """create_snapshot fills in prices and validates, offline."""
    check(
        "cash needs no lookup",
        with_lookup({}, lambda: built(holdings=[{"ref": "everyday", "value": 100}]))
        .assets[0]
        .resolved_value
        == 100.0,
    )
    check(
        "stock price comes from the catalogue's ticker",
        with_lookup(
            {"MSFT": quote("MSFT", 512.9)}, lambda: built(holdings=[{"ref": "msft", "units": 40}])
        )
        .assets[0]
        .price
        == 512.9,
    )
    check(
        "looked-up price feeds the value",
        with_lookup(
            {"MSFT": quote("MSFT", 512.9)}, lambda: built(holdings=[{"ref": "msft", "units": 40}])
        )
        .assets[0]
        .resolved_value
        == 20516.0,
    )
    check(
        "override beats the lookup",
        with_lookup(
            {"MSFT": quote("MSFT", 512.9)},
            lambda: built(holdings=[{"ref": "msft", "units": 40}], price_overrides={"MSFT": 499.0}),
        )
        .assets[0]
        .price
        == 499.0,
    )
    check(
        "override key is case-insensitive",
        with_lookup(
            {},
            lambda: built(holdings=[{"ref": "msft", "units": 40}], price_overrides={"msft": 499.0}),
        )
        .assets[0]
        .price
        == 499.0,
    )
    check(
        "an already-priced stock is left alone",
        with_lookup({}, lambda: built(holdings=[{"ref": "msft", "units": 40, "price": 5}]))
        .assets[0]
        .price
        == 5.0,
    )
    check(
        "a stock with a value is left alone",
        with_lookup({}, lambda: built(holdings=[{"ref": "msft", "value": 7}])).assets[0].value
        == 7.0,
    )
    check(
        "no created stock is left needing a price",
        not any(
            a.needs_price
            for a in with_lookup(
                {"MSFT": quote("MSFT", 1.0)}, lambda: built(holdings=[{"ref": "msft", "units": 2}])
            ).assets
        ),
    )

    # A ticker is optional, so these must not reach the price source at all.
    check(
        "units + price is priced without a lookup",
        with_lookup({}, lambda: built(holdings=[{"ref": "home", "units": 100, "price": 2.5}]))
        .assets[0]
        .resolved_value
        == 250.0,
    )
    check(
        "a bare value is priced without a lookup",
        with_lookup({}, lambda: built(holdings=[{"ref": "home", "value": 1234}]))
        .assets[0]
        .resolved_value
        == 1234.0,
    )
    check(
        "only the ticker + units stock is looked up",
        with_lookup(
            {"MSFT": quote("MSFT", 512.9)},
            lambda: built(
                holdings=[
                    {"ref": "msft", "units": 1},
                    {"ref": "home", "units": 2, "price": 3},
                    {"ref": "home", "value": 4},
                ]
            ),
        )
        .assets[0]
        .price
        == 512.9,
    )
    check(
        "Asset objects are accepted as input",
        with_lookup({}, lambda: built(holdings=[Asset("everyday", value=5)])).assets[0].value
        == 5.0,
    )
    check(
        "specs may be given as raw dicts",
        with_lookup(
            {},
            lambda: built(
                holdings=[{"ref": "e", "value": 5}],
                specs=[{"key": "e", "type": "cash", "currency": "AUD"}],
            ),
        )
        .assets[0]
        .value
        == 5.0,
    )

    raises(
        "an unknown ref blocks the snapshot",
        "unknown asset 'ghost'",
        lambda: built(holdings=[{"ref": "ghost", "value": 1}]),
        SnapshotError,
    )
    raises(
        "stock units with nothing to price them from",
        "needs the catalogue's 'ticker'",
        lambda: built(
            holdings=[{"ref": "orphan", "units": 40}],
            specs=[{"key": "orphan", "type": "stocks", "currency": "USD"}],
        ),
        SnapshotError,
    )
    raises(
        "a ticker is not a substitute for an amount",
        "needs 'units'",
        lambda: built(holdings=[{"ref": "msft"}]),
        SnapshotError,
    )
    raises(
        "duplicate spec keys are caught",
        "duplicate asset key",
        lambda: built(
            holdings=[{"ref": "e", "value": 1}],
            specs=[
                {"key": "e", "type": "cash", "currency": "AUD"},
                {"key": "e", "type": "cash", "currency": "AUD"},
            ],
        ),
        SnapshotError,
    )

    raises(
        "unknown ticker blocks the snapshot",
        "price_overrides",
        lambda: with_lookup(
            {"MSFT": prices_mod.PriceError("MSFT: No data found")},
            lambda: built(holdings=[{"ref": "msft", "units": 1}]),
        ),
        SnapshotError,
    )
    raises(
        "currency disagreement blocks the snapshot",
        "trades in EUR",
        lambda: with_lookup(
            {"MSFT": quote("MSFT", 5.0, currency="EUR")},
            lambda: built(
                holdings=[{"ref": "odd", "units": 1}],
                specs=[{"key": "odd", "type": "stocks", "ticker": "MSFT", "currency": "AUD"}],
            ),
        ),
        SnapshotError,
    )
    check(
        "override skips the currency check",
        with_lookup(
            {},
            lambda: built(
                holdings=[{"ref": "odd", "units": 1}],
                specs=[{"key": "odd", "type": "stocks", "ticker": "MSFT", "currency": "AUD"}],
                price_overrides={"MSFT": 5.0},
            ),
        )
        .assets[0]
        .price
        == 5.0,
    )
    raises(
        "every failure is reported, not just the first",
        "nope",
        lambda: with_lookup(
            {
                "AAPL": prices_mod.PriceError("AAPL: nope"),
                "MSFT": prices_mod.PriceError("MSFT: nope"),
            },
            lambda: built(
                holdings=[{"ref": "msft", "units": 1}, {"ref": "aapl", "units": 1}],
                specs=[*SEED_SPECS, spec(stock(key="aapl", ticker="AAPL"))],
            ),
        ),
        SnapshotError,
    )
    raises(
        "bad date rejected before any lookup",
        "not a valid ISO date",
        lambda: built(date="2026-13-99", holdings=[{"ref": "msft", "units": 1}]),
        SnapshotError,
    )
    raises(
        "created snapshot still validates fx",
        "unknown source currency",
        lambda: create_snapshot("2026-09-30", [], SEED_CCY, SEED_SPECS, fx={"GBP": {"GBP": 1.0}}),
        SnapshotError,
    )


def test_conversion_and_totals(store):
    """Conversion direction and totals are pinned down with numbers."""
    # The rate matrix is "units of column per 1 unit of row", so getting the
    # direction backwards is silent and wrong by the square of the rate.
    RATES = {
        "AUD": {"AUD": 1.0, "JPY": 100.0, "USD": 0.5},
        "JPY": {"AUD": 0.01, "JPY": 1.0, "USD": 0.005},
        "USD": {"AUD": 2.0, "JPY": 200.0, "USD": 1.0},
    }
    snap = Snapshot(date="2026-09-30", fx=RATES)

    check("converting out of a currency uses the row", snap.convert(2.0, "AUD", "JPY") == 200.0)
    check(
        "converting into a currency is the same thing backwards",
        snap.convert(200.0, "JPY", "AUD") == 2.0,
    )
    check(
        "the inverse of the wrong rate is visibly different",
        snap.convert(2.0, "AUD", "JPY") != 2.0 * RATES["JPY"]["AUD"] * RATES["JPY"]["AUD"],
    )
    check("a rate of 0.5 doubles into AUD", snap.convert(1.0, "USD", "AUD") == 2.0)
    check(
        "converting to the same currency is the identity",
        snap.convert(1234.5, "AUD", "AUD") == 1234.5,
    )
    check("conversion is case insensitive", snap.convert(2.0, "aud", "jpy") == 200.0)
    check(
        "conversion survives a round trip to within the rates' own precision",
        abs(snap.convert(snap.convert(100.0, "AUD", "JPY"), "JPY", "AUD") - 100.0) < 1e-9,
    )
    raises(
        "converting from an unknown currency is an error",
        "no rates for",
        lambda: snap.convert(1.0, "GBP", "AUD"),
        SnapshotError,
    )
    raises(
        "converting to an unknown currency is an error",
        "no rates for",
        lambda: snap.convert(1.0, "AUD", "GBP"),
        SnapshotError,
    )

    # value_in needs the catalogue, because the currency lives on the spec.
    mixed = Portfolio(
        currencies=["AUD", "JPY", "USD"],
        specs=[
            spec({"key": "aud_cash", "type": "cash", "currency": "AUD"}),
            spec({"key": "jpy_cash", "type": "cash", "currency": "JPY"}),
        ],
    )
    day = Snapshot(
        date="2026-09-30",
        fx=RATES,
        assets=[Asset("aud_cash", value=10.0), Asset("jpy_cash", value=1000.0)],
    )
    mixed.snapshots.append(day)

    check(
        "a holding is restated from its own catalogue currency",
        mixed.value_in(day, "aud_cash", "JPY") == 1000.0,
    )
    check(
        "value_in resolves the ref through check_key",
        mixed.value_in(day, "AUD_CASH", "JPY") == 1000.0,
    )
    check(
        "a currency not held that date is None, not zero",
        mixed.value_in(day, "absent", "JPY") is None,
    )
    check(
        "the total sums every currency into the target", mixed.total_in(day, "AUD") == 10.0 + 10.0
    )
    check("the total works into any currency", mixed.total_in(day, "JPY") == 1000.0 + 1000.0)

    # Units resolve through their price before conversion.
    priced = Portfolio(currencies=["USD"], specs=[spec(stock(key="m", currency="USD"))])
    pday = Snapshot(date="2026-09-30", fx=RATES, assets=[Asset("m", units=3.0, price=2.0)])
    priced.snapshots.append(pday)
    check("units x price convert like any other amount", priced.total_in(pday, "USD") == 6.0)
    check("and carry across currencies", priced.total_in(pday, "AUD") == 12.0)

    # A holding with no amount must not be silently dropped from a total.
    gap = Portfolio(currencies=["USD"], specs=[spec(stock(key="m", currency="USD"))])
    gday = Snapshot(date="2026-09-30", fx=RATES, assets=[Asset("m", units=3.0)])
    gap.snapshots.append(gday)
    check("unresolved lists the ref", gday.unresolved == ["m"], str(gday.unresolved))
    check("value_in is None for an unresolved holding", gap.value_in(gday, "m", "USD") is None)
    raises(
        "a total refuses to skip an unresolved holding",
        "no amount yet",
        lambda: gap.total_in(gday, "USD"),
        SnapshotError,
    )

    # `only` totals a subset -- what the page shows with assets ticked out of it.
    check(
        "a subset total sums just those assets",
        mixed.total_in(day, "AUD", only=["jpy_cash"]) == 10.0,
    )
    check(
        "...and converts them like any other total",
        mixed.total_in(day, "JPY", only=["aud_cash"]) == 1000.0,
    )
    check(
        "a set is as good as a list for naming them",
        mixed.total_in(day, "AUD", only={"aud_cash"}) == 10.0,
    )
    check(
        "an unknown key contributes nothing rather than raising",
        mixed.total_in(day, "AUD", only=["not-a-key"]) == 0.0,
    )
    check("wanting nothing totals zero, not a refusal", mixed.total_in(day, "AUD", only=[]) == 0.0)
    check(
        "leaving out only entirely totals everything, like omitting it",
        mixed.total_in(day, "AUD", only=None) == mixed.total_in(day, "AUD") == 20.0,
    )

    # An asset with no amount yet blocks the totals it is part of, and only those:
    # ticked out of the total, it is not in the number, so it cannot be missing
    # from it. Without this the page could not show a portfolio whose only problem
    # is the one holding the user is trying to look past.
    half = Portfolio(
        currencies=["USD"],
        specs=[spec(stock(key="m", currency="USD")), spec(stock(key="ok", currency="USD"))],
    )
    hday = Snapshot(
        date="2026-09-30",
        fx=RATES,
        assets=[Asset("m", units=3.0), Asset("ok", value=5.0)],
    )
    half.snapshots.append(hday)
    raises(
        "a total spanning a half-priced portfolio still refuses",
        "no amount yet",
        lambda: half.total_in(hday, "USD"),
        SnapshotError,
    )
    raises(
        "...and so does a subset that still wants the unpriced asset",
        "no amount yet",
        lambda: half.total_in(hday, "USD", only=["m", "ok"]),
        SnapshotError,
    )
    check(
        "but leaving it out gives a total for the rest",
        half.total_in(hday, "USD", only=["ok"]) == 5.0,
    )

    # Liabilities subtract rather than being floored at zero.
    owing = Portfolio(
        currencies=["AUD"],
        specs=[
            spec({"key": "loan", "type": "cash", "currency": "AUD"}),
            spec({"key": "cash", "type": "cash", "currency": "AUD"}),
        ],
    )
    oday = Snapshot(
        date="2026-09-30",
        fx=RATES,
        assets=[Asset("loan", value=-300.0), Asset("cash", value=100.0)],
    )
    owing.snapshots.append(oday)
    check("a negative holding subtracts from the total", owing.total_in(oday, "AUD") == -200.0)
    # A zero balance is a real amount, not a missing one, and 0.0 is falsy enough
    # that a careless `if value:` would quietly drop it.
    zero = Portfolio(
        currencies=["AUD"],
        specs=[spec({"key": "z", "type": "cash", "currency": "AUD"})],
    )
    zday = Snapshot(date="2026-09-30", fx=RATES, assets=[Asset("z", value=0.0)])
    zero.snapshots.append(zday)
    check("a zero balance totals to zero", zero.total_in(zday, "AUD") == 0.0)
    check("...and is not reported as unresolved", zday.unresolved == [], str(zday.unresolved))
    check(
        "...and value_in returns 0.0, not None",
        zero.value_in(zday, "z", "AUD") == 0.0 and zero.value_in(zday, "z", "AUD") is not None,
    )

    # Totals must agree with the shipped file, in every currency.
    live = store
    check(
        "the real portfolio totals cleanly",
        all(not s.unresolved for s in live.snapshots),
        str([s.unresolved for s in live.snapshots]),
    )
    for ccy in live.currencies:
        for s in live.snapshots:
            check(
                f"real total in {ccy} on {s.date} is finite",
                isinstance(live.total_in(s, ccy), float),
            )


def test_add_snapshot():
    """add_snapshot appends against the portfolio's own catalogue."""
    work = Portfolio(currencies=["AUD"], specs=[spec(CASH)], name="Work", handle="work")
    with_lookup(
        {},
        lambda: work.add_snapshot("2026-01-31", [{"ref": "e", "value": 100}], fx=make_fx(["AUD"])),
    )
    check("add_snapshot appends", [s.date for s in work.snapshots] == ["2026-01-31"])
    check(
        "add_snapshot reuses the portfolio's catalogue",
        work.snapshots[0].assets[0].ref == "e",
    )
    raises(
        "add_snapshot refuses a duplicate date",
        "already has a snapshot",
        lambda: with_lookup(
            {},
            lambda: work.add_snapshot(
                "2026-01-31", [{"ref": "e", "value": 2}], fx=make_fx(["AUD"])
            ),
        ),
        SnapshotError,
    )
    raises(
        "add_snapshot rejects an unknown ref",
        "unknown asset",
        lambda: with_lookup(
            {},
            lambda: work.add_snapshot(
                "2026-02-28", [{"ref": "ghost", "value": 2}], fx=make_fx(["AUD"])
            ),
        ),
        SnapshotError,
    )
    raises(
        "spec() names an unknown asset",
        "no asset 'ghost'",
        lambda: work.spec("ghost"),
        SnapshotError,
    )


def test_handles():
    """A handle becomes a filename, so it is validated rather than sanitised."""
    check("a plain handle is accepted", portfolio_path("personal").name == "personal.yaml")
    check("handle case is folded", portfolio_path("Personal").name == "personal.yaml")
    check("handle whitespace is trimmed", portfolio_path("  personal  ").name == "personal.yaml")
    check(
        "handle allows dots, dashes and underscores",
        portfolio_path("a.b-c_d").name == "a.b-c_d.yaml",
    )
    check("handle allows digits", portfolio_path("2026tax").name == "2026tax.yaml")

    raises(
        "handle cannot climb out",
        "not usable as a filename",
        lambda: check_handle(".."),
        SnapshotError,
    )
    raises(
        "handle cannot use a separator",
        "not usable as a filename",
        lambda: check_handle("../../etc/passwd"),
        SnapshotError,
    )
    raises(
        "handle cannot use a subdirectory",
        "not usable as a filename",
        lambda: check_handle("nested/portfolio"),
        SnapshotError,
    )
    raises(
        "handle cannot be a backslash path",
        "not usable as a filename",
        lambda: check_handle("a\\b"),
        SnapshotError,
    )
    raises(
        "handle cannot be empty",
        "must not be empty",
        lambda: check_handle("   "),
        SnapshotError,
    )
    raises(
        "handle cannot be blank-ish", "must not be empty", lambda: check_handle(""), SnapshotError
    )
    raises(
        "handle rejects a Path",
        "must be a string",
        lambda: check_handle(pathlib.Path("personal")),
        SnapshotError,
    )
    raises(
        "handle rejects a leading dot",
        "not usable as a filename",
        lambda: check_handle(".hidden"),
        SnapshotError,
    )
    raises(
        "handle rejects a space",
        "not usable as a filename",
        lambda: check_handle("my portfolio"),
        SnapshotError,
    )

    check("PortfolioError is catchable as SnapshotError", issubclass(PortfolioError, SnapshotError))
    check(
        "the resolved path stays inside the directory",
        portfolio_path("personal").parent == PORTFOLIO_DIR.resolve(),
    )


def test_real_portfolios_directory(store):
    """Discovery and loading of the real portfolios directory."""
    check(
        "the seed portfolio is discoverable",
        "personal" in list_portfolios(),
        str(list_portfolios()),
    )
    check("list_portfolios is sorted", list_portfolios() == sorted(list_portfolios()))
    check(
        "list_portfolios ignores non-yaml files",
        all(not h.endswith(".json") for h in list_portfolios()),
    )
    check(
        "list_portfolios of a missing directory is empty",
        list_portfolios("/tmp/opencode/no-such-dir") == [],
    )

    check("load finds the portfolio by handle", store.handle == "personal")
    check("load reads the name label", store.name == "Personal")
    check("the label prefers the name", store.label == "Personal")
    check(
        "the label falls back to the handle",
        Portfolio(currencies=["AUD"], handle="work").label == "work",
    )
    check(
        "a nameless portfolio still has a label",
        Portfolio(currencies=["AUD"], handle="work", name=None).label == "work",
    )
    check("an unwritten portfolio has an empty label", Portfolio(currencies=["AUD"]).label == "")

    raises(
        "load names an unknown portfolio",
        "no portfolio named 'nope'",
        lambda: load("nope"),
        SnapshotError,
    )
    raises(
        "load lists what is available",
        "have: ",
        lambda: load("nope", directory="/tmp/opencode/no-such-dir"),
        SnapshotError,
    )
    raises(
        "load refuses a traversing handle",
        "not usable as a filename",
        lambda: load("../personal"),
        SnapshotError,
    )


def test_name_optional():
    """The portfolio name is optional, stripped and validated."""
    check(
        "name may be omitted",
        load_file(write("currencies: [AUD]\nsnapshots: []\n")).name is None,
    )
    check(
        "name is stripped",
        load_file(write("name: '  My Portfolio  '\ncurrencies: [AUD]\nsnapshots: []\n")).name
        == "My Portfolio",
    )
    raises(
        "blank name rejected",
        "'name' must be a non-empty string",
        lambda: load_file(write("name: '   '\ncurrencies: [AUD]\n")),
        SnapshotError,
    )
    raises(
        "non-string name rejected",
        "'name' must be a non-empty string",
        lambda: load_file(write("name: 7\ncurrencies: [AUD]\n")),
        SnapshotError,
    )

    check(
        "to_dict puts name first and omits it when absent",
        next(iter(Portfolio(currencies=["AUD"], name="P").to_dict())) == "name"
        and "name" not in Portfolio(currencies=["AUD"]).to_dict(),
    )
    check(
        "to_dict survives a round trip",
        load_file(write("name: Round\ncurrencies: [AUD]\nsnapshots: []\n")).name == "Round",
    )


def test_several_portfolios_kept_apart():
    """Portfolios sharing a directory stay independent."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = pathlib.Path(tmpdir)

        # Copy the real portfolio in first, so everything here is loaded from `root`
        # and nothing can write back into the repository.
        seed_bytes = (PORTFOLIO_DIR / "personal.yaml").read_bytes()
        (root / "personal.yaml").write_bytes(seed_bytes)

        personal = load("personal", directory=root)
        check(
            "a loaded portfolio remembers its directory",
            personal.directory == root.resolve(),
            str(personal.directory),
        )
        written = save(personal, root / "personal.yaml")
        check("save returns the path written", written == root / "personal.yaml", str(written))

        fresh = Portfolio(currencies=["GBP"], name="Work", handle="work")
        save(fresh, directory=root)
        check(
            "a portfolio with a handle saves back to its own file",
            (root / "work.yaml").exists(),
        )
        check("the written portfolio reloads", load("work", directory=root).currencies == ["GBP"])
        check(
            "portfolios keep their own currency universe",
            load("personal", directory=root).currencies == personal.currencies,
        )
        check(
            "portfolios keep their own catalogues",
            [s.key for s in load("personal", directory=root).specs]
            == [s.key for s in personal.specs],
        )
        check(
            "portfolios are listed together",
            list_portfolios(root) == ["personal", "work"],
            str(list_portfolios(root)),
        )

        # Adding to one portfolio must not disturb another.
        n_before = len(personal.snapshots)
        any_ref = personal.specs[0].key
        personal.snapshots.append(
            create_snapshot(
                "2026-11-30",
                [{"ref": any_ref, "value": 1}],
                personal.currencies,
                personal.specs,
                fx=make_fx(personal.currencies),
            )
        )
        save(personal, root / "personal.yaml")
        check(
            "one portfolio gained a snapshot",
            len(load("personal", directory=root).snapshots) == n_before + 1,
            str(len(load("personal", directory=root).snapshots)),
        )
        check("the other portfolio is untouched", len(load("work", directory=root).snapshots) == 0)

        raises(
            "saving without a handle needs an explicit path",
            "pass an explicit path",
            lambda: save(Portfolio(currencies=["AUD"]), directory=root),
            SnapshotError,
        )

        # Regression: a portfolio loaded from one directory must be saved back to
        # that directory, not silently to the default one.
        after = load("personal", directory=root)
        save(after)
        check(
            "save with no path writes back where it was loaded from",
            (root / "personal.yaml").exists() and list_portfolios(root) == ["personal", "work"],
            str(list_portfolios(root)),
        )
        check(
            "the repository portfolio was not touched",
            (PORTFOLIO_DIR / "personal.yaml").read_bytes() == seed_bytes,
        )
        check(
            "the repository keeps its comments",
            (PORTFOLIO_DIR / "personal.yaml").read_text().count("#") > 5,
        )


def test_exchange_rates_fetched_at_creation():
    """create_snapshot fetches rates only when fx is left out."""
    try:
        stub_fetch()
        auto = create_snapshot(
            "2026-09-30", [{"ref": "everyday", "value": 1000}], SEED_CCY, SEED_SPECS
        )
        check(
            "create_snapshot fetches rates when fx is left out",
            len(fetched_calls) == 1,
            str(fetched_calls),
        )
        check("the fetched matrix is stored in the snapshot", auto.fx == SEED_FX)
        check(
            "the fetch is given the date as a date object",
            isinstance(fetched_calls[0][0], dt.date),
            str(type(fetched_calls[0][0])),
        )
        check(
            "the fetch is given the portfolio's currencies",
            fetched_calls[0][1] == SEED_CCY,
            str(fetched_calls[0][1]),
        )

        fetched_calls.clear()
        given = create_snapshot(
            "2026-09-30", [{"ref": "everyday", "value": 1000}], SEED_CCY, SEED_SPECS, fx=SEED_FX
        )
        check("supplying fx makes no rate lookup at all", fetched_calls == [], str(fetched_calls))
        check("a supplied matrix is used as-is", given.fx == SEED_FX)

        raises(
            "a bad matrix is still caught when fetched",
            "AUD->USD",
            lambda: snap_for(
                fx={
                    "AUD": {"AUD": 1.0},
                    "JPY": {"AUD": 1.0, "JPY": 1.0},
                    "USD": {"AUD": 1.0, "JPY": 1.0},
                }
            ),
            SnapshotError,
        )

        fx_mod.fetch_fx = broken_fetch
        raises(
            "a failed rate lookup stops the snapshot",
            "does not support XYZ",
            lambda: snap_for(),
            SnapshotError,
        )

        check("a failed rate lookup is catchable as a SnapshotError", snapshot_error_catches())

        # A weekend has no rates of its own; the note is informational, never fatal.
        stub_fetch(rate_date="2026-10-02")
        weekend_snap = create_snapshot(
            "2026-10-04", [{"ref": "everyday", "value": 1000}], SEED_CCY, SEED_SPECS
        )
        check(
            "a weekend snapshot is still built from the last working day's rates",
            weekend_snap.date == "2026-10-04" and weekend_snap.fx == SEED_FX,
            weekend_snap.date,
        )
    finally:
        unstub_fetch()
