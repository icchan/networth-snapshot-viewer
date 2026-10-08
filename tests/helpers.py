"""Assertion helpers shared by the suites.

The suites started life as scripts that printed a PASS/FAIL line per check and
named each one. pytest already reports and names failures, but not individual
checks, so these keep the labelling: a failed assertion still says *which*
check broke. Everything else about them is a plain pytest assertion.
"""

from collections.abc import Callable


def check(label: str, condition: object, detail: str = "") -> None:
    """Assert ``condition``, naming the check in the failure message."""
    if condition:
        return
    raise AssertionError(f"{label}: {detail}" if detail else label)


def raises(
    label: str,
    fragment: str,
    fn: Callable[[], object],
    error: type[BaseException],
) -> None:
    """Assert that ``fn`` raises ``error`` naming ``fragment``.

    Any other exception, or none at all, is a failure of the named check rather
    than a mystery traceback -- which is the whole reason this is not a bare
    ``pytest.raises``.
    """
    try:
        fn()
    except error as exc:
        check(label, fragment in str(exc), f"got: {exc}")
    except Exception as exc:  # noqa: BLE001 -- the wrong error is a failed check too
        check(label, False, f"wrong exception {type(exc).__name__}: {exc}")
    else:
        check(label, False, "no error raised")


__all__ = ["check", "raises"]
