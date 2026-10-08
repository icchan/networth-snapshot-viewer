"""Portfolio storage.

A *portfolio* is one YAML file under :data:`PORTFOLIO_DIR`, holding the currency
universe for that portfolio and every snapshot ever taken of it:

* a ``name`` (optional, a friendlier label -- the filename stem is the handle)
* a ``currencies`` list
* zero or more snapshots

Each :class:`Snapshot` is one dated point in time. It carries:

* a ``date`` (``YYYY-MM-DD``)
* a ``monthyear``, always *derived* from that date, never read as truth
* an ``fx`` matrix holding the rate for **every ordered pair** of the portfolio's
  currencies, identity pairs included
* its ``assets``

Because the rates live inside the snapshot, converting a snapshot's numbers
always uses the rates that were true on its own date. Nothing is repriced by
later rate moves.

A handle is the filename stem, so each portfolio is opened explicitly by name --
there is no default portfolio to open by accident.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

#: Where the per-portfolio YAML files live.
PORTFOLIO_DIR = Path(__file__).resolve().parent / "data" / "portfolios"

#: A handle becomes a filename, so it is deliberately narrow: lowercase letters,
#: digits, dot, underscore and dash. Anything else is rejected rather than
#: sanitised, so a handle can never escape :data:`PORTFOLIO_DIR`.
#:
#: An asset key is held to the same rule. It only has to be an unambiguous label,
#: but there is no reason to let the two vocabularies drift apart.
HANDLE_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")
KEY_RE = HANDLE_RE

YAML_SUFFIX = ".yaml"

# live here rather than in fx.py so the snapshot model keeps no import of it
DEFAULT_FX_URL = "https://api.frankfurter.app"


class SnapshotError(ValueError):
    """The snapshot file doesn't match the expected shape."""


class PortfolioError(SnapshotError):
    """A portfolio handle or path is unusable.

    Subclasses :class:`SnapshotError`, so one ``except`` still catches everything
    the loader can raise.
    """


def monthyear(date: str) -> str:
    """``'2026-09-30'`` -> ``'2026-09'``."""
    return date[:7]


def check_date(value: object, where: str) -> str:
    """Return a validated ISO date string, or raise."""
    if not isinstance(value, str):
        raise SnapshotError(f"{where}: date must be a string, got {type(value).__name__}")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise SnapshotError(f"{where}: date {value!r} is not a valid ISO date ({exc})") from None
    if parsed.isoformat() != value:
        raise SnapshotError(f"{where}: date must be zero-padded YYYY-MM-DD, got {value!r}")
    return value


def check_currencies(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        raise SnapshotError("currencies must be a non-empty list")
    codes: list[str] = []
    for code in value:
        if not isinstance(code, str) or not code.strip():
            raise SnapshotError(f"currency codes must be non-empty strings, got {code!r}")
        code = code.strip().upper()
        if code in codes:
            raise SnapshotError(f"duplicate currency {code!r} in currencies")
        codes.append(code)
    return codes


def check_fx(value: object, currencies: list[str], where: str) -> dict[str, dict[str, float]]:
    """Validate the rate matrix: every ordered pair present, all rates positive."""
    if not isinstance(value, dict):
        raise SnapshotError(f"{where}: fx must be a mapping of currency -> currency -> rate")

    known = set(currencies)
    seen: set[tuple[str, str]] = set()

    for source, row in value.items():
        if not isinstance(source, str) or source.upper() not in known:
            raise SnapshotError(f"{where}: fx has unknown source currency {source!r}")
        source = source.upper()
        if not isinstance(row, dict):
            raise SnapshotError(f"{where}: fx[{source}] must map each currency to a rate")
        for target, rate in row.items():
            if not isinstance(target, str) or target.upper() not in known:
                raise SnapshotError(f"{where}: fx has unknown target currency {target!r}")
            target = target.upper()
            if isinstance(rate, bool) or not isinstance(rate, (int, float)):
                raise SnapshotError(
                    f"{where}: fx[{source}][{target}] must be a number, got {rate!r}"
                )
            if rate <= 0:
                raise SnapshotError(f"{where}: fx[{source}][{target}] must be positive, got {rate}")
            seen.add((source, target))

    missing = sorted(f"{s}->{t}" for s in currencies for t in currencies if (s, t) not in seen)
    if missing:
        raise SnapshotError(f"{where}: fx is missing {len(missing)} pair(s): {', '.join(missing)}")

    for code in currencies:
        if value[code][code] != 1 and float(value[code][code]) != 1.0:
            raise SnapshotError(f"{where}: fx[{code}][{code}] must be 1, got {value[code][code]!r}")

    return {
        source.upper(): {target.upper(): float(rate) for target, rate in row.items()}
        for source, row in value.items()
    }


#: The asset types the app understands. Fixed in code so a type means the same
#: thing everywhere.
TYPE_CASH = "cash"
TYPE_SUPER = "super"
TYPE_STOCKS = "stocks"
TYPE_PROPERTY = "property"
TYPE_OTHER = "other"

ASSET_TYPES = (TYPE_CASH, TYPE_SUPER, TYPE_STOCKS, TYPE_PROPERTY, TYPE_OTHER)


def check_number(value: object, where: str, field_name: str) -> float | None:
    """Accept a number, or ``None`` for absent. Rejects bools and junk."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SnapshotError(f"{where}: {field_name} must be a number, got {value!r}")
    return float(value)


def _check_slug(
    value: object,
    pattern: re.Pattern[str],
    label: str,
    error: type[SnapshotError],
    why: str,
) -> str:
    """Fold case and reject anything outside ``pattern``.

    Shared by asset keys and portfolio handles so the two vocabularies can never
    drift apart. Values are rejected rather than cleaned up, because a handle
    becomes a filename and a key becomes a lookup identity -- quietly rewriting
    either would point somewhere the author didn't ask for.
    """
    if not isinstance(value, str):
        raise error(f"{label} must be a string, got {type(value).__name__}")
    normalised = value.strip().lower()
    if not normalised:
        raise error(f"{label} must not be empty")
    if not pattern.fullmatch(normalised):
        raise error(
            f"{label} {value!r} is not usable {why}: use lowercase letters, digits, "
            "'.', '_' or '-', starting with a letter or digit"
        )
    return normalised


def check_key(key: object) -> str:
    """Normalise and validate an asset key.

    A key is how a snapshot points at its catalogue entry, so it has to stay
    stable for the life of the asset. Same rules as a handle, on purpose.
    """
    return _check_slug(key, KEY_RE, "asset key", SnapshotError, "as an identifier")


class _FlowMap(dict):
    """A mapping written in YAML flow style, for dense numeric tables."""


class _Dumper(yaml.SafeDumper):
    """Dumps :class:`_FlowMap` inline, everything else as usual."""


def _flow_representer(dumper, data):
    return dumper.represent_mapping("tag:yaml.org,2002:map", data, flow_style=True)


_Dumper.add_representer(_FlowMap, _flow_representer)


@dataclass(frozen=True)
class AssetSpec:
    """One asset as it is *defined*: the facts that don't change over time.

    This is a catalogue entry, written once per portfolio. A snapshot never
    repeats these fields -- it references the entry by :attr:`key` and records
    only what was true on its own date.

    That split is what stops a rename in one snapshot from silently forking an
    asset into two, which is exactly what happened while every snapshot carried
    its own copy of ``name``/``type``/``currency``/``ticker``.
    """

    key: str
    type: str
    currency: str
    name: str | None = None
    ticker: str | None = None

    @property
    def label(self) -> str:
        """The display name, falling back to the key."""
        return self.name or self.key

    @property
    def is_stock(self) -> bool:
        return self.type == TYPE_STOCKS

    @classmethod
    def from_dict(cls, raw: object, currencies: list[str], where: str) -> AssetSpec:
        if not isinstance(raw, dict):
            raise SnapshotError(f"{where}: each asset definition must be a mapping")
        if "key" not in raw:
            raise SnapshotError(f"{where}: asset definition is missing 'key'")
        key = check_key(raw["key"])

        raw_name = raw.get("name")
        if raw_name is not None and (not isinstance(raw_name, str) or not raw_name.strip()):
            raise SnapshotError(f"{where} ({key}): 'name' must be a non-empty string when given")
        name = raw_name.strip() if isinstance(raw_name, str) else None

        asset_type = raw.get("type")
        if not isinstance(asset_type, str) or asset_type.strip().lower() not in ASSET_TYPES:
            raise SnapshotError(
                f"{where} ({key}): unknown type {asset_type!r} "
                f"(expected one of {', '.join(ASSET_TYPES)})"
            )
        asset_type = asset_type.strip().lower()

        currency = raw.get("currency")
        if not isinstance(currency, str) or currency.strip().upper() not in currencies:
            raise SnapshotError(
                f"{where} ({key}): unknown currency {currency!r} "
                f"(expected one of {', '.join(currencies)})"
            )
        currency = currency.strip().upper()

        raw_ticker = raw.get("ticker")
        if raw_ticker is not None and (not isinstance(raw_ticker, str) or not raw_ticker.strip()):
            raise SnapshotError(f"{where} ({key}): 'ticker' must be a non-empty string")
        ticker = raw_ticker.strip().upper() if isinstance(raw_ticker, str) else None

        if asset_type != TYPE_STOCKS and ticker is not None:
            raise SnapshotError(
                f"{where} ({key}): only type 'stocks' may have a 'ticker', not {asset_type!r}"
            )

        return cls(key=key, type=asset_type, currency=currency, name=name, ticker=ticker)

    def to_dict(self) -> dict:
        out: dict = {"key": self.key}
        # a name identical to the key says nothing the key doesn't
        if self.name is not None and self.name != self.key:
            out["name"] = self.name
        out["type"] = self.type
        if self.ticker is not None:
            out["ticker"] = self.ticker
        out["currency"] = self.currency
        return out


@dataclass(frozen=True)
class Asset:
    """One asset *as held on a date*: the numbers, and nothing else.

    Identity comes from :attr:`ref`, which points at an :class:`AssetSpec` in the
    portfolio's catalogue. Only the observation lives here, because only the
    observation changes between snapshots:

    ==========================  ==================================
    ``ref``                     which catalogue entry this is
    ``value``                   a flat amount on that date
    ``units`` + ``price``       a quantity and the price it had that day
    ==========================  ==================================

    ``units`` with no ``price`` is *unresolved* -- legal to load, because
    :func:`create_snapshot` fills the price in from the spec's ticker.
    """

    ref: str
    value: float | None = None
    units: float | None = None
    price: float | None = None

    @property
    def resolved_value(self) -> float | None:
        """``value`` if given, else ``units * price``, else ``None``."""
        if self.value is not None:
            return self.value
        if self.units is not None and self.price is not None:
            return self.units * self.price
        return None

    @property
    def needs_price(self) -> bool:
        """A quantity whose price still has to come from the spec's ticker."""
        return self.units is not None and self.price is None and self.value is None

    @classmethod
    def from_dict(cls, raw: object, where: str) -> Asset:
        if not isinstance(raw, dict):
            raise SnapshotError(f"{where}: each asset must be a mapping")
        if "ref" not in raw:
            raise SnapshotError(f"{where}: asset is missing 'ref'")
        ref = check_key(raw["ref"])

        # A definition field appearing here means the file predates the split, or
        # was half-edited. Worth naming rather than quietly ignoring, since the
        # whole point of the change was to stop that data being duplicated.
        for stale in ("name", "type", "currency", "ticker"):
            if stale in raw:
                raise SnapshotError(
                    f"{where} ({ref}): '{stale}' belongs in the portfolio's catalogue, "
                    f"not in a snapshot -- snapshots record only ref, value, units "
                    f"and price"
                )

        return cls(
            ref=ref,
            value=check_number(raw.get("value"), where, "value"),
            units=check_number(raw.get("units"), where, "units"),
            price=check_number(raw.get("price"), where, "price"),
        )

    def to_dict(self) -> dict:
        out: dict = {"ref": self.ref}
        if self.value is not None:
            out["value"] = self.value
        if self.units is not None:
            out["units"] = self.units
        if self.price is not None:
            out["price"] = self.price
        return out


def _check_amount(
    spec: AssetSpec,
    value: float | None,
    units: float | None,
    price: float | None,
    where: str,
) -> None:
    """Validate that this observation can be, or already is, resolved.

    ``value`` short-circuits, so an incomplete units/price pair is only a problem
    when there's nothing else to fall back on.

    A stock's ``units`` are acceptable on their own, because the price can still
    come from its ticker. Without a ticker there is nothing to price them from, so
    a ticker or a price is then required.
    """
    if value is not None:
        return  # explicit value wins; anything else is redundant

    if units is None:
        if price is None:
            if spec.type == TYPE_STOCKS:
                raise SnapshotError(
                    f"{where} ({spec.key}): needs 'units' (with a 'ticker' or 'price'), "
                    f"or a 'value'"
                )
            raise SnapshotError(f"{where} ({spec.key}): needs 'value', or 'units' and 'price'")
        raise SnapshotError(
            f"{where} ({spec.key}): 'price' needs a matching 'units', or give 'value'"
        )

    if price is not None:
        return  # units * price

    # units with no price
    if spec.type == TYPE_STOCKS and spec.ticker is not None:
        return  # the close gets looked up from the ticker
    if spec.type == TYPE_STOCKS:
        raise SnapshotError(
            f"{where} ({spec.key}): 'units' needs the catalogue's 'ticker' to price "
            f"it, or give 'price' or 'value'"
        )
    raise SnapshotError(f"{where} ({spec.key}): 'units' needs a matching 'price', or give 'value'")


def _catalogue(
    specs: list[AssetSpec] | list[dict],
    currencies: list[str],
    where: str = "catalogue",
) -> dict[str, AssetSpec]:
    """Key a list of spec objects or raw dicts by asset key."""
    out: dict[str, AssetSpec] = {}
    for index, entry in enumerate(specs):
        spec = (
            entry
            if isinstance(entry, AssetSpec)
            else AssetSpec.from_dict(entry, currencies, f"{where}[{index}]")
        )
        if spec.key in out:
            raise SnapshotError(f"{where}: duplicate asset key {spec.key!r}")
        out[spec.key] = spec
    return out


@dataclass(frozen=True)
class Snapshot:
    """One dated point in time, with the rates that applied on that date."""

    date: str
    fx: dict[str, dict[str, float]] = field(repr=False)
    assets: list[Asset] = field(default_factory=list, repr=False)

    @property
    def monthyear(self) -> str:
        """Derived from :attr:`date`; never stored as truth."""
        return monthyear(self.date)

    def convert(self, amount: float, source: str, target: str) -> float:
        """Restate ``amount`` from ``source`` into ``target`` at this date's rates.

        The matrix reads "units of column per 1 unit of row", so ``fx[AUD][JPY]``
        is how many yen one dollar buys. Converting therefore multiplies by the
        rate *out of* the source currency -- getting this backwards is silent and
        wrong by the square of the rate, so it lives here with a test rather than
        being re-derived by each caller.
        """
        source, target = source.strip().upper(), target.strip().upper()
        for code in (source, target):
            if code not in self.fx:
                raise SnapshotError(
                    f"{self.date}: no rates for {code!r} (this snapshot covers: "
                    f"{', '.join(sorted(self.fx))})"
                )
        return amount * self.fx[source][target]

    @property
    def unresolved(self) -> list[str]:
        """Refs held on this date whose amount cannot be valued yet.

        That is ``units`` with no ``price``. Legal to load -- the price may not
        have been fetched -- but it means any total would be short by that
        holding, so callers that add things up have to check this first.
        """
        return [a.ref for a in self.assets if a.resolved_value is None]

    @classmethod
    def from_dict(
        cls,
        raw: object,
        currencies: list[str],
        catalogue: dict[str, AssetSpec],
        where: str,
    ) -> Snapshot:
        if not isinstance(raw, dict):
            raise SnapshotError(f"{where}: each snapshot must be a mapping")
        if "date" not in raw:
            raise SnapshotError(f"{where}: snapshot is missing 'date'")
        date = check_date(raw["date"], where)

        if "monthyear" in raw:
            raise SnapshotError(
                f"{where}: 'monthyear' is no longer stored -- it is derived from "
                f"'date', so remove it"
            )

        if "fx" not in raw:
            raise SnapshotError(f"{where}: snapshot is missing 'fx'")

        entries = raw.get("assets", [])
        if entries is None:
            entries = []
        if not isinstance(entries, list):
            raise SnapshotError(f"{where}: 'assets' must be a list")

        assets: list[Asset] = []
        seen: set[str] = set()
        for index, entry in enumerate(entries):
            spot = f"{where}: assets[{index}]"
            asset = Asset.from_dict(entry, spot)
            if asset.ref in seen:
                raise SnapshotError(
                    f"{spot}: {asset.ref!r} appears twice in one snapshot -- a date "
                    f"can hold an asset once"
                )
            seen.add(asset.ref)
            spec = catalogue.get(asset.ref)
            if spec is None:
                known = ", ".join(sorted(catalogue)) or "none"
                raise SnapshotError(f"{spot}: unknown asset {asset.ref!r} (catalogue has: {known})")
            _check_amount(spec, asset.value, asset.units, asset.price, spot)
            assets.append(asset)

        return cls(
            date=date,
            fx=check_fx(raw["fx"], currencies, where),
            assets=assets,
        )

    def to_dict(self) -> dict:
        out = {
            "date": self.date,
            # flow style: an fx row is a dense table, and one line per row
            # beats the ~n lines block style costs per currency
            "fx": {
                source: _FlowMap(sorted(row.items())) for source, row in sorted(self.fx.items())
            },
        }
        if self.assets:
            out["assets"] = [a.to_dict() for a in self.assets]
        return out


def check_handle(handle: object) -> str:
    """Normalise and validate a portfolio handle.

    Case is folded to lower, since the handle is a filename. Everything else is
    rejected rather than cleaned up, so a handle can never contain a path
    separator or climb out of :data:`PORTFOLIO_DIR`.
    """
    return _check_slug(handle, HANDLE_RE, "portfolio handle", PortfolioError, "as a filename")


def portfolio_path(handle: str, directory: Path | str = PORTFOLIO_DIR) -> Path:
    """The file a handle refers to, verified to stay inside ``directory``."""
    safe = check_handle(handle)
    base = Path(directory).resolve()
    target = (base / f"{safe}{YAML_SUFFIX}").resolve()
    if target.parent != base:
        # Unreachable given HANDLE_RE, but cheap and documents the guarantee.
        raise PortfolioError(f"portfolio handle {handle!r} escapes {base}")
    return target


@dataclass
class Portfolio:
    """One portfolio file: a currency universe, an asset catalogue, and snapshots.

    ``handle`` is the filename stem, so ``load``/``save`` can round-trip a
    portfolio without the caller passing a path. ``name`` is an optional display
    label and is not used for identity.

    ``directory`` records where the portfolio was read from, so ``save`` writes
    back to that same place instead of the default one.

    ``specs`` is the catalogue: every asset this portfolio knows about, defined
    once. Snapshots reference those definitions by key and record only the
    numbers that were true on their own date.
    """

    currencies: list[str]
    specs: list[AssetSpec] = field(default_factory=list)
    snapshots: list[Snapshot] = field(default_factory=list)
    name: str | None = None
    handle: str | None = None
    directory: Path | None = None

    @property
    def catalogue(self) -> dict[str, AssetSpec]:
        """The specs, keyed by asset key."""
        return {spec.key: spec for spec in self.specs}

    def spec(self, key: str) -> AssetSpec:
        """The catalogue entry for ``key``, or a clear error."""
        found = self.catalogue.get(check_key(key))
        if found is None:
            known = ", ".join(sorted(s.key for s in self.specs)) or "none"
            where = self.label or "this portfolio"
            raise PortfolioError(f"no asset {key!r} in {where} (have: {known})")
        return found

    def for_monthyear(self, target: str) -> list[Snapshot]:
        return [s for s in self.snapshots if s.monthyear == target]

    def by_date(self, date: str) -> Snapshot | None:
        return next((s for s in self.snapshots if s.date == date), None)

    def value_in(self, snapshot: Snapshot, ref: str, target: str) -> float | None:
        """One holding restated into ``target``.

        ``None`` means the holding cannot be shown in that column: either it was
        not held on that date, or it is held but has no amount yet. Callers that
        need to tell those apart should test :attr:`Snapshot.unresolved`.
        """
        asset = next((a for a in snapshot.assets if a.ref == check_key(ref)), None)
        if asset is None:
            return None
        amount = asset.resolved_value
        if amount is None:
            return None
        return snapshot.convert(amount, self.spec(ref).currency, target)

    def total_in(
        self,
        snapshot: Snapshot,
        target: str,
        *,
        only: Iterable[str] | None = None,
    ) -> float:
        """Everything held on ``snapshot``'s date, restated into ``target``.

        Raises rather than summing around a holding it cannot value: a net worth
        that is quietly short by one unpriced position is worse than no number.
        Assets absent from the date contribute nothing, and negative amounts
        (liabilities) subtract, as they should.

        ``only`` restricts the sum to those asset keys, which is how the page
        shows what a portfolio looks like with one holding ticked out of the
        totals. An asset left out holds the total hostage no longer: it is not
        part of the number, so it cannot be missing from it either.
        """
        if only is None:
            wanted = [spec.key for spec in self.specs]
        else:
            keep = set(only)
            wanted = [spec.key for spec in self.specs if spec.key in keep]
        blocked = [ref for ref in snapshot.unresolved if ref in wanted]
        if blocked:
            raise SnapshotError(
                f"{snapshot.date}: cannot total in {target}, these have no amount "
                f"yet: {', '.join(blocked)}"
            )
        total = 0.0
        for key in wanted:
            value = self.value_in(snapshot, key, target)
            total += value if value is not None else 0.0
        return total

    @property
    def label(self) -> str:
        """The display name, falling back to the handle."""
        return self.name or self.handle or ""

    def add_snapshot(
        self,
        date: str,
        holdings: list[dict] | list[Asset],
        **kwargs,
    ) -> Snapshot:
        """Build a snapshot against this portfolio's catalogue and append it.

        The ergonomic counterpart to :func:`create_snapshot`: it reuses this
        portfolio's currencies and specs, so a call site only has to say which
        assets were held and what they were worth.
        """
        date = check_date(date, "add_snapshot")
        if self.by_date(date) is not None:
            raise PortfolioError(f"{self.label}: already has a snapshot for {date}")
        snapshot = create_snapshot(date, holdings, self.currencies, self.specs, **kwargs)
        self.snapshots.append(snapshot)
        return snapshot

    def to_dict(self) -> dict:
        out: dict = {}
        if self.name is not None:
            out["name"] = self.name
        out["currencies"] = list(self.currencies)
        if self.specs:
            out["assets"] = [s.to_dict() for s in self.specs]
        out["snapshots"] = [s.to_dict() for s in sorted(self.snapshots, key=lambda s: s.date)]
        return out


def list_portfolios(directory: Path | str = PORTFOLIO_DIR) -> list[str]:
    """Every portfolio handle in ``directory``, sorted.

    A missing directory is an empty list, so a fresh checkout has no portfolios
    rather than an error.
    """
    base = Path(directory)
    if not base.is_dir():
        return []
    return sorted(
        path.stem
        for path in base.glob(f"*{YAML_SUFFIX}")
        if path.is_file() and HANDLE_RE.fullmatch(path.stem)
    )


def load_file(path: Path | str) -> Portfolio:
    """Read and validate a portfolio from an explicit path."""
    path = Path(path)
    if not path.exists():
        raise SnapshotError(f"no snapshot file at {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SnapshotError(f"{path} is not valid YAML: {exc}") from None

    if not isinstance(raw, dict):
        raise SnapshotError(f"{path}: top level must be a mapping")

    raw_name = raw.get("name")
    if raw_name is not None and (not isinstance(raw_name, str) or not raw_name.strip()):
        raise SnapshotError(f"{path}: 'name' must be a non-empty string when given")
    name = raw_name.strip() if isinstance(raw_name, str) else None

    if "currencies" not in raw:
        raise SnapshotError(f"{path}: missing 'currencies'")
    currencies = check_currencies(raw["currencies"])

    raw_specs = raw.get("assets", [])
    if raw_specs is None:
        raw_specs = []
    if not isinstance(raw_specs, list):
        raise SnapshotError(f"{path}: 'assets' must be a list")
    catalogue = _catalogue(raw_specs, currencies, f"{path}: assets")

    entries = raw.get("snapshots", [])
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise SnapshotError(f"{path}: 'snapshots' must be a list")

    snapshots = [
        Snapshot.from_dict(entry, currencies, catalogue, f"{path}: snapshots[{index}]")
        for index, entry in enumerate(entries)
    ]

    dates = [s.date for s in snapshots]
    duplicates = sorted({d for d in dates if dates.count(d) > 1})
    if duplicates:
        raise SnapshotError(f"{path}: duplicate snapshot date(s): {', '.join(duplicates)}")

    snapshots.sort(key=lambda s: s.date)
    return Portfolio(
        currencies=currencies,
        specs=list(catalogue.values()),
        snapshots=snapshots,
        name=name,
        handle=check_handle(path.stem),
        directory=path.parent,
    )


def load(handle: str, directory: Path | str = PORTFOLIO_DIR) -> Portfolio:
    """Read and validate the portfolio with this handle.

    Raises :class:`PortfolioError` when the handle is unusable or no such
    portfolio exists, so naming a portfolio always either works or says why.
    """
    path = portfolio_path(handle, directory)
    if not path.exists():
        available = ", ".join(list_portfolios(directory)) or "none"
        raise PortfolioError(f"no portfolio named {handle!r} in {directory} (have: {available})")
    return load_file(path)


def save(
    portfolio: Portfolio,
    path: Path | str | None = None,
    directory: Path | str = PORTFOLIO_DIR,
) -> Path:
    """Write a portfolio out, oldest snapshot first. Comments are not preserved.

    With no ``path``, a portfolio that remembers where it came from is written
    back to exactly that file -- falling back to ``directory`` for a portfolio
    that was built in memory. Returns the path written.
    """
    if path is None:
        if portfolio.handle is None:
            raise PortfolioError("this portfolio has no handle, so pass an explicit path to save()")
        target = portfolio.directory if portfolio.directory is not None else directory
        path = portfolio_path(portfolio.handle, target)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(
            portfolio.to_dict(),
            Dumper=_Dumper,
            sort_keys=False,
            default_flow_style=False,
            width=100,
        ),
        encoding="utf-8",
    )
    return path


def create_snapshot(
    date: str,
    holdings: list[dict] | list[Asset],
    currencies: list[str],
    specs: list[AssetSpec] | list[dict],
    fx: dict[str, dict[str, float]] | None = None,
    price_overrides: dict[str, float] | None = None,
    price_url: str | None = None,
    cache_path: Path | str | None = None,
    fx_url: str | None = None,
) -> Snapshot:
    """Build a snapshot for `date`, filling in stock prices and exchange rates.

    ``holdings`` says what was held, and nothing else: a ``ref`` naming a
    catalogue entry, plus ``value``, or ``units`` with or without ``price``. The
    type, currency and ticker all come from ``specs``, so they are never repeated
    per snapshot.

    For a stock you supply only ``ref`` and ``units``; the close on or before
    `date` is looked up from the spec's ticker and written into the snapshot, so
    the stored record never has to be fetched again. ``value`` is left out of the
    file because it is just ``units * price``.

    Leave ``fx`` out and the exchange rates for `date` are fetched too, giving
    you the whole snapshot from the date and the holdings. Pass ``fx`` yourself
    and no rate lookup happens at all.

    Nothing here is ever refreshed later. Prices and rates are recorded when the
    snapshot is built and left alone afterwards, so a snapshot's totals keep
    meaning what they meant the day it was written.

    ``price_overrides`` maps ticker to a hand-entered price. That price wins over
    the lookup, which is how you cover an instrument the price source doesn't
    carry — an override also skips the currency cross-check below.

    Raises :class:`SnapshotError` if any price can't be resolved, or if a
    ticker's trading currency disagrees with the asset's currency. A failed rate
    lookup raises :class:`fx.FxError`.
    """
    # imported here so the snapshot model itself still works without requests
    from prices import PriceError, close_on

    date = check_date(date, "create_snapshot")
    codes = check_currencies(currencies)
    catalogue = _catalogue(specs, codes, "create_snapshot: specs")

    if fx is None:
        from fx import fetch_fx

        fetched = fetch_fx(dt.date.fromisoformat(date), codes, url=fx_url or DEFAULT_FX_URL)
        fx = fetched.rates
        if fetched.stale:
            print(
                f"note: no rates for {date} itself, using {fetched.rate_date} "
                f"(weekends and holidays have no rates)"
            )
    fx = check_fx(fx, codes, "create_snapshot")

    overrides = {t.strip().upper(): float(p) for t, p in (price_overrides or {}).items()}

    prepared: list[Asset] = []
    problems: list[str] = []

    for index, entry in enumerate(holdings):
        where = f"create_snapshot: holdings[{index}]"
        raw = entry.to_dict() if isinstance(entry, Asset) else entry
        try:
            asset = Asset.from_dict(raw, where)
            spec = catalogue.get(asset.ref)
            if spec is None:
                known = ", ".join(sorted(catalogue)) or "none"
                raise SnapshotError(
                    f"{where}: unknown asset {asset.ref!r} (catalogue has: {known})"
                )
            _check_amount(spec, asset.value, asset.units, asset.price, where)
        except SnapshotError as exc:
            problems.append(str(exc))
            continue

        if not asset.needs_price:
            prepared.append(asset)
            continue

        ticker = spec.ticker
        if ticker in overrides:
            price = overrides[ticker]
        else:
            kwargs = {"url": price_url} if price_url else {}
            if cache_path is not None:
                kwargs["cache_path"] = cache_path
            try:
                quote = close_on(ticker, date, **kwargs)
            except PriceError as exc:
                problems.append(str(exc))
                continue
            if quote.currency and quote.currency != spec.currency:
                problems.append(
                    f"{asset.ref}: {ticker} trades in {quote.currency}, but the asset is "
                    f"recorded as {spec.currency}"
                )
                continue
            price = quote.price
        prepared.append(replace(asset, price=price))

    if problems:
        raise SnapshotError(
            "could not price every stock: "
            + "; ".join(problems)
            + ". Pass price_overrides={ticker: price} to enter a price by hand."
        )

    return Snapshot(
        date=date,
        fx=fx,
        assets=prepared,
    )
