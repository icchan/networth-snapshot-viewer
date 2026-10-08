# AGENTS.md

Personal net-worth tracker: a flat Python 3.11 project — data model + a Streamlit page.
No packages, no build step, no CI, no pre-commit. `README.md` is the design record and is
mostly accurate; where it and the code disagree, trust the code (e.g. the page's
asset-exclusion toggles are not documented there).

## Commands

```bash
pip install -r requirements-dev.txt   # runtime deps (PyYAML, requests, streamlit) + pytest + ruff

python -m pytest                       # the whole suite (68 tests, a few seconds)
python -m pytest tests/test_snapshots.py   # or one file — per-test selection with -k
ruff check .                           # lint; add --fix to autofix, --watch to re-run
ruff format .                          # format (line length 100, Python files only)
streamlit run app.py                   # dev server on :8501; PORTFOLIO_DIR env overrides data dir
```

- pytest and ruff are configured in `pyproject.toml` (which has no `[project]` table — this
  is not a package) and installed by the devcontainer from `requirements-dev.txt`. The VS
  Code config enables the pytest runner and the ruff extension (format-on-save).
- Run only the test file matching the module you touched when you want a quick loop;
  otherwise the whole run is fast enough to just `pytest` it. There are none of the old
  monolithic scripts left — suites are collections of `def test_*()` functions.
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
| `app.py` | Streamlit UI — the only entrypoint (via `streamlit run app.py`) |
| `tests/` | The pytest suites (`test_snapshots.py`, `test_prices.py`, `test_fx.py`, `test_market.py`, `test_app.py`) plus the shared `helpers.py` |
| `pyproject.toml` | pytest + ruff configuration only |
| `requirements-dev.txt` | Runtime deps plus pytest and ruff |
| `data/portfolios/*.yaml` | User data; filename stem is the portfolio handle |

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