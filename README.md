# Finance tracker

Personal net-worth history. This is a fresh start: currently it is just the
**portfolio data model**. There is no UI yet.

## Portfolios

You may hold several portfolios, and each is **its own YAML file** under
`data/portfolios/`:

```text
data/
  prices_cache.json          # derived price data, shared by all portfolios
  portfolios/
    personal.yaml            # loaded as load("personal")
    family.yaml              # loaded as load("family")
```

A portfolio file looks like this:

```yaml
name: Personal & Family      # optional label, not used for identity
currencies:                  # this portfolio's currency universe
  - AUD
  - JPY
  - USD

assets:                      # the catalogue: defined once, never repeated
  - key: everyday            # permanent identity, referred to by `ref`
    name: Everyday account
    type: cash
    currency: AUD
  - key: msft
    name: MSFT
    type: stocks
    ticker: MSFT             # what the price lookup uses
    currency: USD

snapshots:                   # one entry per point in time, in any order
  - date: '2026-09-30'       # YYYY-MM-DD
    fx:                      # units of <column> per 1 unit of <row>, for this date
      AUD: {AUD: 1.0, JPY: 109.39, USD: 0.69675}
      JPY: {AUD: 0.0091416, JPY: 1.0, USD: 0.00636941}
      USD: {AUD: 1.43523502, JPY: 157.00035881, USD: 1.0}
    assets:                  # what was held on this date
      - ref: everyday
        value: 4400.00
      - ref: msft
        units: 40
        price: 512.90
```

Key decisions:

- **The filename stem is the handle**, and it's what identifies the portfolio in code.
  `name:` is only a friendlier label, so two portfolios could share a label without
  conflict.
- **There is no default portfolio.** Every load names one explicitly, so it is always
  obvious which portfolio you are looking at and you can't edit the wrong one by
  forgetting an argument.
- **A handle is validated, never sanitised.** Case is folded to lower, but anything
  outside `a-z0-9._-` is rejected, so a handle can never escape the portfolios
  directory via `..` or a path separator.
- **Each portfolio keeps its own currency universe.** Currencies are not shared or
  global.
- **Prices are cached globally**, not per portfolio, since a close is the same fact
  wherever it is held.
- **An asset is defined once, in the catalogue**, and every snapshot refers to it by
  `key`. Only the numbers that change between dates are recorded per snapshot. See
  [Assets](#assets).

```python
from snapshots import list_portfolios, load, save

list_portfolios()                  # ['personal', 'family'] -- sorted
store = load("personal")           # PortfolioError if it doesn't exist
store.label                        # 'Personal & Family', or the handle
store.handle                       # 'personal'

store.snapshots.append(new_snapshot)
save(store)                        # writes back to the file it came from
```

A loaded portfolio remembers the directory it came from, so `save(store)` writes
back to exactly that file — it can't quietly write to the default directory
instead. A portfolio built in memory has no such memory, so pass `directory=` or an
explicit `path`:

```python
save(Portfolio(currencies=["GBP"], name="Work", handle="work"), directory=root)
```

`load` requires a handle that exists, and says what it does have:

```
PortfolioError: no portfolio named 'nope' in .../data/portfolios (have: personal)
```

Use `load_file(path)` to read an arbitrary path — handy for checking a file before
moving it into place. `PortfolioError` subclasses `SnapshotError`, so a single
`except` still catches everything the loader raises.

## A snapshot

Each snapshot is one dated point in time:

- **A snapshot stores its own rates.** Converting a snapshot's numbers always uses the
  rates that were true on its own date, so history is never retroactively repriced by
  later rate moves.
- **`monthyear` is not stored.** It is derived from `date` on demand
  (`snapshot.monthyear`, `monthyear("2026-09-30")`). Storing it only gave it a second
  way to disagree with the date, and a file that still contains it is rejected with a
  message saying so.
- **An asset absent from a snapshot was not held on that date.** Nothing needs a
  "closed" flag, and adding a new asset needs no backfill of old snapshots.
- **`fx` covers every ordered pair** of the portfolio's currency list, identity pairs
  included, and identity pairs must be exactly `1.0`.
- **A date identifies a snapshot.** Two snapshots cannot share a date.

## Assets

An asset is split in two, because those two halves change at completely different rates.

**The definition** lives once in the portfolio's `assets:` catalogue:

| Field | Required | Notes |
| --- | --- | --- |
| `key` | yes | The permanent identity a snapshot refers to. Same rules as a handle: `[a-z0-9._-]`, case folded, never sanitised. |
| `name` | no | A display label. Defaults to the key, and is not written when it matches. |
| `type` | yes | One of the five fixed types below. |
| `ticker` | no, `stocks` only | Exchange symbol, upper-cased. Rejected on every other type. |
| `currency` | yes | Must appear in the file's `currencies` list. |

**The observation** is what a snapshot records, and it is only the numbers:

| Field | Required | Notes |
| --- | --- | --- |
| `ref` | yes | A catalogue `key`. An unknown key is an error, never a silent new asset. |
| `value` | one of these | The amount, in the catalogue entry's currency. |
| `units` + `price` | one of these | For holdings you track by quantity. |

```yaml
assets:                              # the catalogue, once
  - key: everyday
    name: Everyday account
    type: cash
    currency: AUD
  - key: super
    name: Super
    type: super
    currency: AUD
  - key: home
    name: Home
    type: property
    currency: AUD
  - key: msft
    name: MSFT
    type: stocks
    ticker: MSFT
    currency: USD
  - key: sony
    name: Sony
    type: stocks
    ticker: 6758.T
    currency: JPY

snapshots:
  - date: '2026-08-31'
    fx: {AUD: {AUD: 1.0, JPY: 114.4, USD: 0.7162}, ...}
    assets:
      - ref: everyday
        value: 4200.50
      - ref: super
        value: 88000.00
      - ref: home
        value: 950000.00
      - ref: msft
        units: 40
        price: 507.29
      - ref: sony
        units: 100
        price: 4016.00
```

### Why the split

With the definition repeated in every snapshot, **71% of every asset block was
copy-paste**, and it grew linearly with your history: 17 lines per snapshot for the
five assets above, all of it identical. Worse, `name` was the only identity, so
renaming an asset in one snapshot silently forked it into two with no error at all.

Now `key` is the identity. Renaming an asset means changing `name` in the catalogue, and
no history moves; a snapshot that refers to a key which isn't in the catalogue is
rejected, so a typo can't invent an asset. Adding a snapshot costs 12 lines instead of
24, and the duplicated part never grows again.

At two snapshots the two shapes are about the same size — the catalogue has to be paid
for once. The difference shows up as history accumulates: about 50% fewer lines at 120
snapshots, and no growth in duplication.

### Types

Fixed in code as `ASSET_TYPES` in [`snapshots.py`](snapshots.py), so a type means the same thing
everywhere:

`cash`, `super`, `stocks`, `property`, `other`

### Amount precedence

An observation's amount is either an explicit `value` or a `units` × `price` pair, and at
least one must resolve. **`value` wins**: whenever it is present, `units`/`price` are
redundant and ignored, so this is legal and resolves to `999`:

```yaml
- ref: msft
  value: 999
  units: 40
  price: 512.25
```

Read it back with `Asset.resolved_value`:

```python
Asset("msft", units=40, price=512.25).resolved_value   # 20490.0
Asset("msft", value=999, units=40, price=512.25).resolved_value   # 999
```

A half-specified pair is only an error when there is no `value` to fall back on, and the
error names the key:

| Observation | Result |
| --- | --- |
| `value` only | resolves to `value` |
| `units` + `price` | resolves to their product |
| `value` + a half pair | resolves to `value`, half pair ignored |
| `units` only, non-`stocks` | error: `'units' needs a matching 'price', or give 'value'` |
| `price` only, non-`stocks` | error: `'price' needs a matching 'units', or give 'value'` |
| neither, non-`stocks` | error: `needs 'value', or 'units' and 'price'` |
| `units` only, `stocks` with a `ticker` | **allowed** — `needs_price` is `True`, see below |
| `units` only, `stocks` without a `ticker` | error: nothing to price the units from |

The rules come from the catalogue entry, so `units` only needing a price on non-`stocks`
is decided by the asset's `type`, not by anything in the snapshot.

`units`/`price` are not restricted to `stocks` — the shape is uniform, so a future
`property` measured by area needs no code change.

`assets:` may be omitted from a snapshot entirely, for recording rates before adding
holdings.

### Negative amounts

Amounts may be negative, and this is deliberate: liabilities are recorded as
negative cash. Zero is valid too.

```yaml
- ref: everyday
  value: 4500.00
- ref: visa
  value: -2350.75      # credit card debt
- ref: loan
  value: -412000.00    # loan balance
- ref: overdrawn
  units: -250.50       # negative units work the same way
  price: 1
```

A negative amount is a real value, never treated as "absent": it wins over
`units`/`price` just like a positive `value` does, and it is not a pending price
lookup. Signs survive a `save`/`load` round trip.

Nothing in the model interprets the sign, so a snapshot's debts net off against its
assets once totals exist. If you would rather see liabilities broken out on their own,
that belongs in the totals logic, not in the stored records.

## Exchange rates

Rates come from [Frankfurter](https://frankfurter.dev), a free public API over European
Central Bank reference rates — no API key, in [`fx.py`](fx.py).

```python
from fx import fetch_fx

rates = fetch_fx(date(2026, 9, 30), ["AUD", "JPY", "USD"])
rates.rates["AUD"]["JPY"]   # 109.39
rates.stale                 # False
```

You rarely need to call that directly. Leave `fx` out of `create_snapshot` and the rates
for that date are fetched for you, so a whole snapshot comes from a date and a list of
assets:

```python
snapshot = create_snapshot("2026-09-30", [...assets...], currencies=["AUD", "JPY", "USD"])
```

Rules, chosen deliberately:

- **Each currency is fetched as its own source**, so the matrix holds the rate the source
  published rather than an inverted guess. Rates are as exact as the source publishes.
- **The ECB publishes on working days only.** A Sunday or holiday returns that Friday's
  rates, and the snapshot still gets built — with a note saying which day was actually
  used. `FxRates.stale` reports it. Same philosophy as the price lookup.
- **History starts 1999-01-04.** A date before that, or in the future, is an error rather
  than a wrong number.
- **Only ECB currencies.** The source's currency list is consulted first, so `XYZ` is
  named as unsupported instead of surfacing as a confusing failure.
- **The fetched matrix is checked against the same rules a hand-written one must pass**,
  so anything `fetch_fx` returns is always safe to store.

### Rates are frozen once written

There is no refresh. A snapshot's rates are recorded when it is built and never
re-fetched, so a correct rate today can never silently change what a past total meant.
If a stored matrix turns out to be wrong, editing that snapshot is a deliberate act:

```python
store = load("personal")
store.snapshots[0].fx = fetch_fx(date(2026, 8, 31), store.currencies).rates
save(store)
```

Because `fx` is required, a snapshot with a broken matrix cannot exist on disk — so there
is no "fill in the rates later" path, by design. Rates must be right when the snapshot is
made.

## Stocks and prices

A `stocks` asset **may** carry a `ticker`; it is not required. No other type may carry
one. That makes three ways to record a stock, all equally valid:

| You enter | Price comes from |
| --- | --- |
| `ticker` + `units` | looked up, value computed |
| `units` + `price` | typed by hand, no lookup |
| `value` | a flat amount, no lookup |

```yaml
- name: MSFT
  type: stocks
  ticker: MSFT
  currency: USD
  units: 40          # price filled in by create_snapshot

- name: Private shares
  type: stocks
  currency: USD
  units: 100
  price: 2.50        # typed by hand

- name: Legacy holding
  type: stocks
  currency: AUD
  value: 1234.00     # flat amount
```

`value` is deliberately never stored alongside `units`, because it is just
`units × price` and writing it would be redundant state that can drift.

Creating a snapshot prices only the stocks that need it, then you save the result. Each
holding is a `ref` into the catalogue plus its amount:

```python
from snapshots import create_snapshot

snapshot = create_snapshot("2026-10-02", [
    {"ref": "msft", "units": 40},                          # price looked up
    {"ref": "private", "units": 100, "price": 2.5},        # price given
    {"ref": "cash", "value": 4400},                        # flat amount
], currencies=currencies, specs=store.specs)   # rates fetched for 2026-10-02 too
# only the msft holding reaches the price source
# snapshot.assets[0].price == 517.53, resolved_value == 20701.2
```

`Portfolio.add_snapshot` is the same thing with the portfolio's own currencies and
catalogue filled in, which is usually what you want:

```python
store.add_snapshot("2026-10-31", [
    {"ref": "msft", "units": 42},
    {"ref": "cash", "value": 4500},
])
save(store)
```

Prices come from Yahoo Finance's public chart endpoint in [`prices.py`](prices.py) — no API
key. Rules, chosen deliberately:

- **A non-trading date uses the last earlier close.** A snapshot dated a Sunday or a public
  holiday takes the most recent close on or before it, the same rule the `fx` matrix
  already uses. `Quote.stale` reports when that happened.
- **The search never reaches back more than 14 days.** A long market closure is an *error*
  rather than a quietly ancient price.
- **The ticker's trading currency must match the catalogue entry's currency.** `MSFT`
  recorded under an AUD entry is rejected, since that is a data-entry mistake worth
  catching. This check only applies when a lookup actually happens, so typed prices and
  bare `value`s are unaffected.
- **A hand-entered price wins over the lookup.** Pass `price_overrides={"MSFT": 499.0}` for
  an instrument the source doesn't carry. An override also skips the currency check, so it
  doubles as "I know better".
- **Past closes are cached forever; recent ones never are.** The last two days are still
  settling, so they are always re-fetched, and the cache is only allowed to answer queries
  that lie entirely in settled history. This is what stops a cached entry from quietly
  returning a price that is a day or two out of date.
- Prices are rounded to 4 decimal places on the way in, so `512.9000244140625` is stored as
  `512.9`.

A stock observed as `units` with no `price` loads fine and reports `needs_price`; that is
the "not fetched yet" state. The ticker it would be looked up from is on the catalogue
entry, not in the snapshot. `create_snapshot` refuses to return a snapshot where
any stock is still waiting on a price.

## Rules enforced on load

Loading fails loudly rather than guessing, with the offending path in the message:

| Problem | Error |
| --- | --- |
| Unknown or unusable portfolio handle | `no portfolio named 'x' (have: ...)` / `not usable as a filename` |
| Blank or non-string `name:` | `'name' must be a non-empty string when given` |
| Malformed YAML | `not valid YAML` |
| No `currencies` key, or empty list | `missing 'currencies'` / `non-empty list` |
| Repeated currency | `duplicate currency` |
| Unparseable or unpadded date | `not a valid ISO date` / `zero-padded` |
| `monthyear` still present | `'monthyear' is no longer stored` |
| `assets:` at the top level not a list | `'assets' must be a list` |
| Two catalogue entries with one `key` | `duplicate asset key 'a'` |
| Catalogue entry missing `key` | `asset definition is missing 'key'` |
| Catalogue entry `type` not one of the five | `unknown type 'crypto'` |
| Catalogue `currency` not in the file | `unknown currency 'GBP'` |
| `key` or `ref` that isn't usable as an identifier | `not usable as an identifier` |
| Snapshot `ref` not in the catalogue | `unknown asset 'everydy' (catalogue has: ...)` |
| The same `ref` twice in one date | `'super' appears twice in one snapshot` |
| A definition field repeated inside a snapshot | `'name' belongs in the portfolio's catalogue` |
| Unknown currency inside `fx` | `unknown source currency` / `unknown target currency` |
| Missing ordered pair | `fx is missing N pair(s): AUD->JPY, ...` |
| Unsupported currency at lookup | `the rate source does not support XYZ` |
| Identity pair that isn't `1.0` | `must be 1` |
| Non-numeric or non-positive rate | `must be a number` / `must be positive` |
| Two snapshots on one date | `duplicate snapshot date(s)` |
| Observation missing `ref` | `asset is missing 'ref'` |
| `ticker` on any non-`stocks` type | `only type 'stocks' may have a 'ticker'` |
| Blank `ticker` | `'ticker' must be a non-empty string` |
| Non-numeric or boolean amount | `value must be a number` |
| Amount written as a string (`"-100"`) | `value must be a number` |
| `stocks` with a `price` but no `units` | `'price' needs a matching 'units'` |
| `stocks` with `units` but no `ticker` or `price` | `'units' needs the catalogue's 'ticker' to price it` |
| `stocks` with no amount at all | `needs 'units' (with a 'ticker' or 'price'), or a 'value'` |
| Unresolvable non-`stocks` amount | `needs 'value', or 'units' and 'price'` |
| `assets` that isn't a list | `'assets' must be a list` |
| `save()` with no path and no handle | `pass an explicit path to save()` |

Asset errors carry their position, e.g. `snapshots[0]: assets[1]: unknown asset 'ghost'`.

A missing `snapshots` key is treated as an empty list, and a missing `name` is treated as
absent rather than an error.

## Usage

```python
from snapshots import create_snapshot, list_portfolios, load, save

store = load("personal")            # by handle; there is no default
store.currencies                    # ['AUD', 'JPY', 'USD']
store.by_date("2026-10-01")         # the snapshot, or None
store.for_monthyear("2026-10")      # every snapshot in that month

store.specs                         # list[AssetSpec] -- the catalogue
store.catalogue                     # {key: AssetSpec}
store.spec("msft-ian").ticker       # 'MSFT'

snapshot = store.snapshots[0]
snapshot.monthyear                  # '2026-09' (derived)
snapshot.fx["AUD"]["JPY"]           # 114.37
snapshot.assets                     # list[Asset] -- observations only

asset = snapshot.assets[0]          # ref='ubank-tomomi'
asset.ref, asset.resolved_value     # 'ubank-tomomi', 1000.0
store.spec(asset.ref).name          # 'UBank Tomomi' -- the definition

save(store)                         # writes back to personal.yaml
```

Adding a snapshot fetches rates, prices any stock needing it, and appends:

```python
store.add_snapshot("2026-11-01", [{"ref": "msft-ian", "units": 42}])
save(store)
```

A second portfolio is just another file:

```python
from snapshots import AssetSpec, Portfolio

save(Portfolio(
    currencies=["GBP"],
    specs=[AssetSpec(key="pension", name="Pension", type="super", currency="GBP")],
    name="Work", handle="work",
))
```

`save` rewrites the file, so **comments are not preserved** — the first save of a
hand-commented file will strip them. Keep the files in `data/portfolios/` in version
control (they are small and diff cleanly); if you comment them heavily, expect to
lose that on the first programmatic write. [`data/prices_cache.json`](data/prices_cache.json)
is derived data and is git-ignored.

## Totalling across currencies

Amounts are recorded in each asset's own currency, so anything that adds them up has to
convert. That lives in the model rather than in the page, because a conversion done in the
wrong direction still renders as a plausible-looking number.

| Call | Does |
| --- | --- |
| `snapshot.convert(amount, source, target)` | restate one amount at that date's rates |
| `snapshot.unresolved` | refs held that date with no amount yet |
| `portfolio.value_in(snapshot, ref, target)` | one holding converted; `None` if not held |
| `portfolio.total_in(snapshot, target)` | everything that date, converted |

The matrix reads *units of column per 1 unit of row*, so `fx[AUD][JPY]` is how many yen a
dollar buys. Converting multiplies by the rate **out of** the source currency.

`total_in` raises rather than summing around a holding it cannot value. A net worth that
is quietly short by one unpriced position is worse than no number at all, and an asset
absent from a date contributes nothing — which is different from being present and worth
zero.

Amounts are not reversible: ECB quotes JPY at 2 decimal places, so a round trip is only
good to about 0.1% (see [Exchange rates](#exchange-rates)). Two currencies converted from
the same date will not agree to the cent.

## The page

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run from
the repository root:

```bash
uv sync --locked --no-dev
uv run --no-dev streamlit run app.py
```

Assets run down and snapshot dates run across, newest first. A change row sits above
the total in the currency picked in the sidebar: each snapshot compares with the
preceding saved snapshot, and `Today` compares with the most recent snapshot.
Increases are green, decreases red, and zero is neutral. The oldest comparison and
comparisons with unavailable totals stay blank. Changes use unrounded totals and
respect asset exclusions. Underneath, the same totals are expressed in every currency
the portfolio declares.

Deliberate choices:

- **The first column is `Today`, and it is not a snapshot.** It holds the most recent
  snapshot's holdings — the same quantities — re-marked at today's closes and converted
  at today's rates. It is never written to the file.
- **Each row is marked `<type emoji> <flag> <name>`.** The mapping lives in
  [`labels.py`](labels.py): cash · super · stocks · property · other. The flag is built
  from the currency's country code, which is an approximation: the catalogue has no
  country field, USD is also Canada, and EUR spans a dozen countries. A currency with no
  defensible country (XAU, say) is left unflagged rather than confidently wrong — so the
  row keeps its type emoji and gains no stray space. Add currencies to `CURRENCY_COUNTRIES`
  in `labels.py`.
- **A blank cell is not a zero.** It means the asset was not held that date, or is held
  with no amount yet. Those are different facts and the table does not blur them.
- **Every other column converts at its own date's rates**, not today's, so the columns are
  comparable over time rather than being restated into the present.
- **The last column opens the arithmetic.** Each asset row ends with a
  `:material/info:` button that opens a panel below the table: units, price and rate,
  the conversion, and the whole unit the cell shows — one line per date, in the
  table's order. It is a button rather than a hover tooltip because there is nothing
  to hover: the grid is canvas-rendered with no DOM element per cell, and the
  `cell.tooltip` field Streamlit 1.65 reads is never populated by anything. The
  callback only records which row was clicked (and re-clicking closes the panel);
  the panel re-runs the model's own `value_in`, so its numbers cannot drift from
  the table above it.

Every amount is shown as a whole unit, thousands separators and all, so the table stays
readable at a glance. That is a display choice only: the file keeps full precision and
nothing is rounded on disk. Totals are added up before rounding, so a total can differ
from the sum of the cells above it by a unit or two.

Whole units cost nothing in accuracy here, because the FX matrix is only good to about
0.1% — that is roughly ±300,000 JPY on the JPY total, far more than the rounding.

### The `Today` column

It is a *view*, not a record: the model in [`market.py`](market.py) rebuilds it on load and
nothing is saved. Only holdings that are genuinely quantities — those with `units` and a
`ticker` — can be marked to market. Cash, loans, cards and anything recorded as a flat
`value` pass through at face value, because there is nothing to reprice. The table says
how many of each, rather than implying the whole column is live.

Two ways it can be less than complete, both shown rather than hidden:

- **No close for a ticker.** That cell and the whole total go blank. Substituting the last
  snapshot's price would mix today's rates with an older close and produce a number that
  looks computed but isn't comparable to anything.
- **No rates for the day.** The column is dropped entirely and the page says why. The ECB
  publishes on working days only, so a weekend ask legitimately lands on Friday's rates;
  the caption names the day actually used.

Closes are cached in the page for 15 minutes, so moving the currency selector does not
refetch every holding, and each close is retried once so a single timeout does not blank
the column.

Set `PORTFOLIO_DIR` to read a different folder.

## Development

```bash
uv sync --locked                       # runtime deps + pytest + ruff
uv run python -m pytest                # all suites, from tests/
uv run ruff check .                    # lint (add --fix to autofix)
uv run ruff format .                   # format (line length 100; Python files only)
```

Dependencies live in [pyproject.toml](pyproject.toml); [uv.lock](uv.lock) pins the
resolved versions and belongs in version control. `uv sync` creates the local
`.venv`, and `uv run` uses it without requiring activation. This is a flat application,
not an installable package (`tool.uv.package = false`).

Use `uv add <dependency>` for runtime dependencies or `uv add --dev <dependency>`
for development tools. These update both dependency declarations and the lockfile;
commit both files. After manually editing dependencies, run `uv lock` and
`uv sync --locked`. Use `uv lock --upgrade` only when intentionally upgrading the
locked dependencies.

The devcontainer installs uv and runs `uv sync --locked` automatically. VS Code
uses `.venv/bin/python` for running and testing Python. Rebuild the container after
changing its configuration; if an existing workspace still selects the old interpreter,
use **Python: Select Interpreter** to choose `.venv/bin/python`.

The suites live in `tests/` and are offline and deterministic: the lookup suites replace
`requests.get` with a fake serving canned payloads, `test_market.py` injects its fetchers,
and `test_app.py` replaces the one network call the page makes before running it through
Streamlit's `AppTest` against the real portfolio. It cross-checks every rendered total
against `Portfolio.total_in`, so the page cannot quietly start computing its own
arithmetic. Test, lint and format configuration live in `pyproject.toml`, and each check
is a named assertion via `tests/helpers.py`, so a failure says which check broke.

The handful of live behaviours the fakes stand in for — weekend roll-back, the 1999 series
start, the ECB currency list, a Yahoo holiday — were confirmed against the real endpoints
by hand.