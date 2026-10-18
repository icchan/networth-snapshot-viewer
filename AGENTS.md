# AGENTS.md

Personal net-worth tracker: a flat Python 3.11 project — data model + a Streamlit page.
No packages, no build step, no CI, no pre-commit. [README.md](README.md) is the design
record; trust code and tests where it is stale (its opening still says there is no UI).
See [data format](README.md#portfolios), [validation](README.md#rules-enforced-on-load),
[API usage](README.md#usage), and [page behavior](README.md#the-page) for details.

## Commands

```bash
uv sync --locked                       # runtime deps + pytest + ruff in .venv

uv run python -m pytest                 # the whole suite (a few seconds)
uv run python -m pytest tests/test_snapshots.py   # or one file — per-test selection with -k
uv run ruff check .                     # lint; add --fix to autofix, --watch to re-run
uv run ruff format .                    # format (line length 100, Python files only)
uv run streamlit run app.py             # dev server on :8501; PORTFOLIO_DIR env overrides data dir
```

- Dependencies, pytest, and ruff are configured in [pyproject.toml](pyproject.toml).
  This remains a non-packaged application (`tool.uv.package = false`). Commit
  [uv.lock](uv.lock) with dependency changes; use `uv add` or `uv add --dev` rather
  than maintaining requirements files. See [development](README.md#development).
- The devcontainer installs uv and syncs `.venv` from the lockfile. VS Code uses
  `.venv/bin/python`, enables the pytest runner, and uses ruff for format-on-save.
- Run only the test file matching the module you touched when you want a quick loop;
  otherwise the whole run is fast enough to run `uv run python -m pytest`.
- The suites share labelled assertion helpers in `tests/helpers.py`: `check("label", cond)`
  is a plain assert whose message names the check, and `raises()` names the exception type
  explicitly. Keep using them so a failure says which check broke.
- All suites are intentionally **offline and deterministic**. Network calls are faked at
  the seam each suite owns: `requests.get` swapped (`test_prices`, `test_fx`), fetchers
  injected (`test_market`), the one page fetch replaced per test (`test_app`). Keep new
  tests offline and self-contained — no test may rely on another having run, and no test
  may leave a monkeypatch or a modified module global behind (use the `monkeypatch`
  fixture or an autouse restore fixture).

## Layout

| File | Role |
| --- | --- |
| `snapshots.py` | Core model: `Portfolio`, `Snapshot`, `Asset`, `AssetSpec`, YAML load/save, all validation, `ASSET_TYPES` |
| `fx.py` | ECB rates via Frankfurter; builds and validates the rate matrix |
| `prices.py` | Yahoo Finance closes + the shared cache at `data/prices_cache.json` |
| `market.py` | The live `Today` column: re-marks latest holdings at today's prices/rates |
| `labels.py` | Row labels: type emoji + currency flag (`CURRENCY_COUNTRIES`) |
| `portfolio_view.py` | Streamlit-free view-data builders: pie frames, asset/workings rows, and snapshot/currency totals |
| `app.py` | Streamlit entrypoint: page flow, rendering, snapshot-entry form, widget callbacks, and session state |
| `tests/` | The pytest suites (`test_snapshots.py`, `test_prices.py`, `test_fx.py`, `test_market.py`, `test_app.py`, `test_portfolio_view.py`) plus the shared `helpers.py` |
| `pyproject.toml` | uv dependency declarations, dev group, pytest + ruff configuration |
| `uv.lock` | Locked runtime and development dependencies; commit with dependency changes |
| `data/portfolios/*.yaml` | User data; filename stem is the portfolio handle |

- Keep view-data calculations in [portfolio_view.py](portfolio_view.py), independent of
  Streamlit; keep layout, section render functions, callbacks, and session state in
  [app.py](app.py). Test pure builders in [tests/test_portfolio_view.py](tests/test_portfolio_view.py)
  and page interactions in [tests/test_app.py](tests/test_app.py).
- Snapshot entry must fetch missing historical prices/rates and save only on submission,
  not ordinary Streamlit reruns. Creating a snapshot uses `Portfolio.add_snapshot()`;
  editing an existing snapshot preserves its frozen FX matrix. Follow the offline
  create/edit coverage in [tests/test_app.py](tests/test_app.py).
- Errors: `SnapshotError(ValueError)` is the base; `PortfolioError` and `FxError`
  subclass it. `PriceError(RuntimeError)` is separate — a bare `except SnapshotError`
  does not catch price failures.
- Each `load()` names a portfolio explicitly (no default); handles and asset keys are
  validated against `[a-z0-9._-]`, case-folded, never sanitised.

## Data rules (breaking these corrupts or loses user data)

- **`save()` rewrites the YAML wholesale — comments are stripped.** Portfolios in
  `data/portfolios/` are committed personal data; never hand-edit programmatically or
  write test output there. Tests copy into a `tempfile`/`tmp_path` first, and
  `test_snapshots.py` asserts the real `personal.yaml` stays byte-identical — preserve
  that invariant.
- **FX rates are frozen at snapshot creation.** There is deliberately no refresh path;
  `fx` is required on every snapshot.
- `data/prices_cache.json` is derived, gitignored, shared across portfolios; the last
  two days are never cached (settled history only).
- Amounts may be negative (liabilities) and `value` always wins over `units`/`price`.
- The `Today` column is a view, never written to disk.