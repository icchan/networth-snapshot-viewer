"""How a holding is shown: its type emoji, and the flag for its currency.

Kept here rather than inside ``app.py`` so the tests can import the very mapping
the page uses, instead of each restating it and drifting. The emoji themselves
are a presentation choice and are expected to change; what is worth pinning is
the *rule* -- one type emoji, then a flag, then the name, with a flag only where
a currency actually names a country.
"""

from __future__ import annotations

# One emoji per asset type, so a row is recognisable before it is read. Literal
# characters rather than \N{...} names, which this Python's Unicode database
# does not carry for emoji.
TYPE_EMOJI = {
    "cash": "💰",
    "super": "🥚",       # superannuation: a nest egg
    "stocks": "📈",
    "property": "🏠",
    "other": "🗂️",
}

#: Shown when a type is not in :data:`TYPE_EMOJI`, so an unknown type is still a
#: marked row rather than a bare name.
TYPE_FALLBACK = "•"

# Currency is the only country signal the catalogue carries -- there is no
# country field -- so this is an approximation: USD also means Canada and EUR
# spans a dozen countries. Only the currencies whose country is defensible are
# listed, and anything else goes unflagged rather than confidently wrong.
CURRENCY_COUNTRIES = {
    "AUD": "AU", "CAD": "CA", "CHF": "CH", "CNY": "CN", "EUR": "EU",
    "GBP": "GB", "HKD": "HK", "IDR": "ID", "INR": "IN", "JPY": "JP",
    "KRW": "KR", "MXN": "MX", "MYR": "MY", "NOK": "NO", "NZD": "NZ",
    "PHP": "PH", "SEK": "SE", "SGD": "SG", "THB": "TH", "TRY": "TR",
    "TWD": "TW", "USD": "US", "VND": "VN", "ZAR": "ZA",
}


def flag(currency: str) -> str:
    """A flag emoji for a currency, or an empty string if there isn't an honest one."""
    code = CURRENCY_COUNTRIES.get(currency.strip().upper())
    if not code:
        return ""
    # Regional indicator symbols: 'A' + 0x1F1E6 is the flag for A.
    return "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in code)


def row_label(spec) -> str:
    """``<type emoji> <flag> <label>``, degrading gracefully when either is missing."""
    marks = " ".join(
        m for m in (TYPE_EMOJI.get(spec.type, TYPE_FALLBACK), flag(spec.currency)) if m
    )
    return f"{marks} {spec.label}" if marks else spec.label