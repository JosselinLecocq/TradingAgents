"""
title: Autocall Structuring Data
author: TradingAgents
version: 0.1.0
required_open_webui_version: 0.7.0
requirements: yfinance>=1.4.1
description: Basket analytics for autocall structured products: per-underlying volatility, pairwise correlation, distance to barrier measured in standard deviations, and a historical replay of the payoff over every past window. Companion to the autocall-analysis skill.
licence: MIT
"""

# Design notes for maintainers
# ---------------------------
# * yfinance only, deliberately. Autocall baskets mix indices (^STOXX50E, ^FCHI)
#   with individual shares, often European (MC.PA, BNP.PA). Alpha Vantage serves
#   neither indices nor Yahoo-suffixed European lines; yfinance serves both, and
#   needs no key.
# * The basket mode defaults to `average`, the shape that dominates French
#   retail distribution. A garbled value is still refused — that is a mistake,
#   not an omission — and whenever the default was applied rather than chosen,
#   the report says so, so the assumption stays visible to whoever signs the
#   recommendation.
# * The centrepiece is `simulate_autocall_history`. Everything a term sheet
#   argues about — "the barrier has never been breached", "you get called
#   quickly" — is a testable claim about the past, and replaying the payoff over
#   every historical window answers it without a pricing model, without implied
#   volatility, and without anyone's assumptions but the observed data.
# * Comma-separated strings rather than JSON arrays for the list arguments:
#   models emit them reliably, arrays arrive as strings often enough to break a
#   run, and the parsing cost here is three lines.
# * This file is standalone by necessity — Open WebUI tools cannot import one
#   another — so the small formatting and retry helpers are duplicated from
#   tradingagents_data.py rather than shared.

import asyncio
import contextlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import requests
from pydantic import BaseModel, Field

try:
    import numpy as np
    import pandas as pd
    import yfinance as yf

    _IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - surfaced to the model at call time
    _IMPORT_ERROR = f"yfinance/pandas unavailable: {exc}"


_TD_URL = "https://api.twelvedata.com/time_series"

# Yahoo venue suffix -> ISO market identifier code, for the Twelve Data fallback.
# Verified against their reference endpoint: LVMH is symbol "MC" on mic XPAR,
# where Yahoo calls the same line MC.PA.
_MIC_BY_SUFFIX = {
    "PA": "XPAR", "AS": "XAMS", "BR": "XBRU", "LS": "XLIS", "DE": "XETR",
    "F": "XFRA", "MI": "XMIL", "MC": "XMAD", "SW": "XSWX", "L": "XLON",
    "ST": "XSTO", "HE": "XHEL", "OL": "XOSL", "CO": "XCSE", "VI": "XWBO",
    "IR": "XDUB", "TO": "XTSE", "AX": "XASX",
}

# Expected quote currency per venue. A resolved instrument that does not match
# is almost always the wrong listing of a similarly-named company.
_CURRENCY_BY_SUFFIX = {
    "PA": "EUR", "AS": "EUR", "BR": "EUR", "LS": "EUR", "DE": "EUR", "F": "EUR",
    "MI": "EUR", "MC": "EUR", "VI": "EUR", "IR": "EUR", "HE": "EUR",
    "SW": "CHF", "L": "GBP", "ST": "SEK", "OL": "NOK", "CO": "DKK",
}

# Yahoo exchange suffixes. Needed as an explicit set because single letters are
# ambiguous: `.L` is London while `.B` is a share class, and both are one
# character. Anything listed here is a venue, never a class.
_YAHOO_VENUE_SUFFIXES = frozenset({
    "PA", "DE", "F", "AS", "BR", "LS", "VI", "IR", "HE", "ST", "OL", "CO",
    "L", "IL", "SW", "AT", "WA", "PR", "BD", "MC", "MI",
    "TO", "V", "NE", "CN", "MX", "SA", "BA", "SN",
    "T", "HK", "SS", "SZ", "AX", "NZ", "NS", "BO", "KS", "KQ", "TW", "TWO",
    "JK", "SI", "BK", "KL", "TA", "SR", "QA", "JO", "CA", "IS",
})

_YF_RETRIES = 2
_YF_BACKOFF_SECONDS = 1.5
_TRADING_DAYS = 252

# Tickers a user is likely to type for the usual autocall underlyings.
_INDEX_ALIASES = {
    "EUROSTOXX50": "^STOXX50E", "EUROSTOXX": "^STOXX50E", "SX5E": "^STOXX50E",
    "ESTOXX50": "^STOXX50E", "STOXX50": "^STOXX50E",
    "CAC": "^FCHI", "CAC40": "^FCHI", "PX1": "^FCHI",
    "DAX": "^GDAXI", "FTSE": "^FTSE", "FTSE100": "^FTSE",
    "SP500": "^GSPC", "SPX": "^GSPC", "S&P500": "^GSPC",
    "NASDAQ": "^IXIC", "NDX": "^NDX", "NIKKEI": "^N225", "SMI": "^SSMI",
    "IBEX": "^IBEX", "MIB": "FTSEMIB.MI", "AEX": "^AEX",
}


def _normalize(symbol: str) -> str:
    """Map a spoken index name to the ticker the data vendor serves."""
    cleaned = (symbol or "").strip().upper().replace("$", "")
    alias = _INDEX_ALIASES.get(cleaned.replace(" ", ""))
    if alias:
        return alias
    # Share classes take a dash on Yahoo, not a dot: BRK.B is BRK-B. Only a
    # single letter that is not a known exchange code qualifies, otherwise this
    # would mangle London's TSCO.L into TSCO-L.
    base, _, suffix = cleaned.rpartition(".")
    if base and len(suffix) == 1 and suffix not in _YAHOO_VENUE_SUFFIXES:
        return f"{base}-{suffix}"
    return cleaned


def _split(raw: str) -> list[str]:
    """Parse a comma-separated argument into a clean list."""
    return [part.strip() for part in (raw or "").replace(";", ",").split(",") if part.strip()]


def _num(value: Any) -> float | None:
    """Coerce one provider value to a float, tolerating the usual null spellings."""
    if value in (None, "", "-", "None", "none"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _numbers(raw: str) -> list[float]:
    """Parse a comma-separated list of numbers, tolerating % signs and commas."""
    out = []
    for part in _split(raw):
        try:
            out.append(float(part.replace("%", "").replace(",", ".")))
        except ValueError:
            continue
    return out


def _resolve_as_of(as_of_date: str) -> datetime:
    """Parse the analysis date, clamped to today; invalid input falls back."""
    now = datetime.now()
    if as_of_date:
        try:
            return min(datetime.strptime(as_of_date.strip()[:10], "%Y-%m-%d"), now)
        except ValueError:
            pass
    return now


def _fmt(value: Any, digits: int = 2) -> str:
    """Format a number, or 'n/a' when it is missing or not a number."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return "n/a" if number != number else f"{number:,.{digits}f}"


def _pct(value: Any, digits: int = 1) -> str:
    """Format a 0-1 ratio as a percentage."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return "n/a" if number != number else f"{number * 100:,.{digits}f}%"


_BASKET_MODES = {
    "worst_of": "worst-of (le sous-jacent le plus faible decide seul)",
    "best_of": "best-of (le sous-jacent le plus fort decide seul)",
    "average": "panier en moyenne (performances agregees)",
}


_MODE_UNKNOWN = (
    "Le mode de panier fourni n'est pas reconnu. Valeurs acceptees : 'average' "
    "(panier en moyenne, la forme la plus repandue et la valeur par defaut), "
    "'worst_of' (le sous-jacent le plus faible decide seul), 'best_of', ou 'mono' "
    "pour un produit a sous-jacent unique. Une valeur illisible n'est pas traitee "
    "comme une omission : elle signale une erreur de saisie, pas un defaut."
)


def _check_pct(label: str, value: Any, low: float, high: float) -> str:
    """Reject a percentage outside a plausible band, naming the likely mistake.

    A barrier entered as 0.6 instead of 60 becomes a barrier at 0.6% of the
    initial level: capital is only lost after a 99.4% fall, so every product
    replays as flawless. Nothing downstream would look wrong — which is exactly
    why the check belongs at the door.
    """
    number = _num(value)
    if number is None:
        return f"Le parametre {label} est manquant ou illisible."
    if low <= number <= high:
        return ""
    hint = (
        " Une valeur inferieure a 1 suggere un ratio saisi a la place d'un "
        "pourcentage : une barriere a 60% s'ecrit 60, pas 0,6."
        if number < 1
        else ""
    )
    return (
        f"Valeur implausible pour {label} : {number}. Attendu entre {low} et {high}, "
        f"en pourcentage.{hint}"
    )


def _check_weights(weights: list, count: int) -> str:
    """Reject weights that do not describe the basket actually resolved."""
    if not weights:
        return ""
    if len(weights) != count:
        return (
            f"{len(weights)} poids fournis pour {count} sous-jacents. Fournir un poids "
            "par sous-jacent, dans le meme ordre, ou aucun pour une equiponderation. "
            "Un nombre incoherent serait sinon ignore au profit de poids egaux, sans "
            "que le resultat le signale."
        )
    if any(w <= 0 for w in weights):
        return "Les poids doivent etre strictement positifs."
    return ""


def _mono_conflict(raw_mode: str, count: int) -> str:
    """Reject 'mono' declared over a basket, instead of quietly reading it as worst-of.

    Someone who wrote "mono" while handing over several underlyings has misread
    the term sheet. Resolving it silently would answer a question they did not
    ask, on the aggregation that governs the whole payoff.
    """
    if count > 1 and (raw_mode or "").strip().lower() in {"mono", "mono_sous_jacent", "unique"}:
        return (
            f"Contradiction : le mode 'mono' a ete indique alors que {count} sous-jacents "
            "sont fournis. Un produit mono-sous-jacent n'en porte qu'un. Verifier la "
            "documentation et preciser comment les sous-jacents se combinent : "
            "'average', 'worst_of' ou 'best_of'."
        )
    return ""


def _resolve_mode(mode: str) -> str:
    """Normalise the basket mode. Returns '' when nothing usable was given."""
    cleaned = (mode or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not cleaned:
        # Omission falls back to the commonest shape; a garbled value does not.
        return "average"
    aliases = {
        "worstof": "worst_of", "worst": "worst_of", "pire": "worst_of", "mono": "worst_of",
        "bestof": "best_of", "best": "best_of", "meilleur": "best_of",
        "moyenne": "average", "basket": "average", "panier": "average",
        "equipondere": "average", "rainbow": "average",
    }
    cleaned = aliases.get(cleaned, cleaned)
    return cleaned if cleaned in _BASKET_MODES else ""


# Index tickers that carry no caret. Built from the alias table so a new entry
# there cannot silently be mistaken for an individual share.
_KNOWN_INDEX_TICKERS = frozenset(_INDEX_ALIASES.values())


def _venue_is_single_stock(symbol: str) -> bool:
    """Whether a ticker denotes an individual share rather than an index."""
    return not symbol.startswith("^") and symbol.upper() not in _KNOWN_INDEX_TICKERS


def _discontinuities(serie) -> list:
    """Single-day moves large enough to be a corporate action, not a market move.

    Vendor-independent on purpose. Both providers claim to adjust for splits, but
    a series that slipped through unadjusted would put a 90% cliff in the middle
    of the replay and report it as a capital loss — a catastrophic reading of an
    event where nobody lost anything. Cheaper to detect than to trust.
    """
    if serie is None or len(serie) < 3:
        return []
    ratios = (serie / serie.shift(1)).dropna()
    breaks = ratios[(ratios > 1.6) | (ratios < 0.625)]
    return [(idx, float(value)) for idx, value in breaks.items()]


def _mode_label(mode: str, count: int, defaulted: bool = False) -> str:
    """Describe the aggregation, without claiming a basket effect that cannot exist."""
    if count <= 1:
        return "mono-sous-jacent (un seul actif determine le remboursement)"
    suffix = (
        " — *valeur par defaut, non precisee lors de l'appel : a confirmer sur la "
        "documentation du produit*"
        if defaulted
        else ""
    )
    return _BASKET_MODES[mode] + suffix


def _basket_perf(ratios, mode: str, weights=None) -> float:
    """Aggregate per-underlying performance ratios into the basket's performance.

    A single underlying returns the same answer under every mode, which is why a
    mono-underlying product needs no special case.
    """
    if mode == "best_of":
        return float(np.max(ratios))
    if mode == "average":
        if weights is not None and len(weights) == len(ratios):
            total = float(np.sum(weights))
            if total > 0:
                return float(np.dot(ratios, weights) / total)
        return float(np.mean(ratios))
    return float(np.min(ratios))


def _unresolved_message(tickers, as_of: datetime, what: str) -> str:
    """Explain an unresolved underlying, naming the cause that matters in practice.

    "Verify the symbols" is the wrong advice most of the time: the usual reason a
    French retail autocall cannot be replayed is that its underlying is a
    proprietary decrement or custom strategy index, which no public data source
    carries. Saying so points the user at the real question instead of sending
    them hunting for a typo.
    """
    return (
        f"DONNEES_INDISPONIBLES : aucune serie exploitable pour "
        f"{', '.join(tickers)} au {as_of:%Y-%m-%d}. Trois causes possibles, par "
        "ordre de frequence :\n"
        "1. **Indice proprietaire** — decrement, ESG, equipondere, « strategy » "
        "ou tout indice sur mesure construit pour l'emission. Ces indices ne "
        "figurent dans aucune source publique. Le rejeu est alors impossible tel "
        "quel : demander l'indice parent et le montant du decrement, ou se "
        "reporter aux simulations de la documentation en les traitant comme une "
        "source interessee.\n"
        "2. **Symbole mal orthographie** ou place non couverte.\n"
        "3. **Fournisseur temporairement indisponible.**\n"
        f"Ne pas estimer de {what}."
    )


def _is_rate_limited(exc: BaseException) -> bool:
    """Whether a vendor exception is a throttle rather than a real failure."""
    if "RateLimit" in type(exc).__name__:
        return True
    text = str(exc).lower()
    return "too many requests" in text or "rate limited" in text or "429" in text


class Tools:
    """Basket analytics for autocall structured products."""

    class Valves(BaseModel):
        HISTORY_YEARS: int = Field(
            default=20,
            description=(
                "Years of history loaded for correlation and for the payoff replay. "
                "Twenty covers 2008, which matters for a tool whose whole purpose is "
                "measuring how often a barrier broke: a six-year product tested on "
                "fifteen years yields barely 1.5 independent trials, on twenty it gets "
                "2.3. The cost is only the first download, since the series is cached."
            ),
        )
        WINDOW_STEP_DAYS: int = Field(
            default=5,
            description=(
                "Spacing between the start dates of the replayed windows, in trading "
                "days. 5 means one window per week: dense enough to be stable, coarse "
                "enough to stay fast."
            ),
        )
        TWELVEDATA_API_KEY: str = Field(
            default="",
            description=(
                "Twelve Data API key. When set, individual shares are served by Twelve "
                "Data first and Yahoo becomes their fallback, which spreads the load "
                "across two providers and keeps Yahoo's per-IP throttle at bay. Indices "
                "stay on Yahoo either way: Twelve Data does not expose index time series "
                "on the tested plans."
            ),
            json_schema_extra={"input": {"type": "password"}},
        )
        CACHE_DIR: str = Field(
            default="",
            description=(
                "Directory for the on-disk price cache. Empty uses a temporary folder, "
                "which survives between requests but not a container restart. Point it "
                "at a mounted volume to make the history permanent — the main defence "
                "against a provider throttling you mid-analysis."
            ),
        )
        MAX_OUTPUT_CHARS: int = Field(
            default=6000,
            description="Hard cap on the characters returned by any single call.",
        )
        REQUEST_TIMEOUT: int = Field(
            default=45,
            description="Seconds before a data provider call is abandoned.",
        )

    def __init__(self):
        """Initialize the Tool."""
        self.valves = self.Valves()
        self._price_cache: dict[tuple, tuple[float, Any]] = {}
        # Set when Twelve Data refuses for a reason worth telling the reader
        # about — running out of credits degrades silently otherwise.
        self._td_note = ""
        # What each vendor actually returned, so a ticker that resolved to the
        # wrong listing of a similar name cannot pass unnoticed.
        self._resolved: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _cap(self, text: str) -> str:
        """Truncate an output that would blow past the configured budget."""
        limit = max(500, int(self.valves.MAX_OUTPUT_CHARS))
        if len(text) <= limit:
            return text
        return text[:limit] + "\n\n[... sortie tronquee par MAX_OUTPUT_CHARS ...]"

    async def _emit(
        self, emitter: Callable[[dict], Any] | None, description: str, done: bool = False
    ) -> None:
        """Push a status line into the chat (the one Native-mode-safe event)."""
        if emitter is None:
            return
        with contextlib.suppress(Exception):
            await emitter(
                {"type": "status", "data": {"description": description, "done": done}}
            )

    async def _run(self, func: Callable[..., Any], *args: Any) -> Any:
        """Run a blocking call in a worker thread under REQUEST_TIMEOUT."""
        return await asyncio.wait_for(
            asyncio.to_thread(func, *args), timeout=max(10, int(self.valves.REQUEST_TIMEOUT))
        )

    @staticmethod
    def _retry(func: Callable[[], Any]) -> Any:
        """Run a yfinance call, backing off on Yahoo's per-IP throttle."""
        delay = _YF_BACKOFF_SECONDS
        for attempt in range(_YF_RETRIES + 1):
            try:
                return func()
            except Exception as exc:
                if attempt >= _YF_RETRIES or not _is_rate_limited(exc):
                    raise
                time.sleep(delay)
                delay *= 2
        return None

    def _cache_path(self, symbol: str) -> str:
        """File backing one symbol's history, or '' when caching is impossible."""
        base = (self.valves.CACHE_DIR or "").strip() or os.path.join(
            tempfile.gettempdir(), "tradingagents-autocall"
        )
        try:
            os.makedirs(base, exist_ok=True)
        except OSError:
            return ""
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in symbol)
        return os.path.join(base, f"{safe}.csv")

    def _read_cache(self, symbol: str):
        """Cached series for one symbol, or None."""
        path = self._cache_path(symbol)
        if not path or not os.path.exists(path):
            return None
        try:
            frame = pd.read_csv(path, index_col=0, parse_dates=True)
        except Exception:
            return None
        # Older cache files predate the Dividends column; their absence must not
        # invalidate an otherwise usable history.
        return None if frame.empty or "Close" not in frame.columns else frame

    def _write_cache(self, symbol: str, frame) -> None:
        """Merge a freshly downloaded series into the symbol's cache file."""
        path = self._cache_path(symbol)
        if not path or frame is None or frame.empty:
            return
        existing = self._read_cache(symbol)
        if existing is not None:
            # combine_first rather than concat: the new download wins on every
            # value it carries, but columns it does not carry survive. Twelve
            # Data returns no dividends, and a plain concat let a refresh wipe
            # the payout history Yahoo had stored — silently disabling the
            # dividend-drag signal on exactly the shares it exists for.
            frame = frame.combine_first(existing).sort_index()
        with contextlib.suppress(OSError):
            frame.to_csv(path)

    def _td_daily(self, symbol: str, as_of: datetime, years: int):
        """Daily series from Twelve Data. Individual shares only.

        Their index time series are not exposed on the plans tested, so an index
        symbol is refused here rather than producing a confusing 404 upstream.
        """
        key = (self.valves.TWELVEDATA_API_KEY or "").strip()
        if not key or not _venue_is_single_stock(symbol):
            return None
        base, _, suffix = symbol.rpartition(".")
        # Only query Twelve Data when the venue can be named. Stripping a suffix
        # this table does not know sends a bare ticker to a provider that will
        # answer with whatever listing it ranks first — 7203.T became "7203",
        # 0700.HK became "0700". Yahoo understands those suffixes natively, so
        # the safe move is to decline and let it serve them.
        if base and suffix.upper() not in _MIC_BY_SUFFIX:
            return None
        params = {
            "symbol": base or symbol,
            "interval": "1day",
            "outputsize": "5000",
            # Stated rather than inherited. Their default is already "splits",
            # which is the right convention here — a barrier observes the price,
            # dividends detached — but a provider default that changes silently
            # is precisely how the Alpha Vantage adjusted endpoint caught me out.
            "adjust": "splits",
            "apikey": key,
            "start_date": (as_of - timedelta(days=int(years * 365.25))).strftime("%Y-%m-%d"),
            "end_date": as_of.strftime("%Y-%m-%d"),
        }
        mic = _MIC_BY_SUFFIX.get(suffix.upper()) if base else None
        if mic:
            params["mic_code"] = mic
        response = requests.get(
            _TD_URL, params=params, timeout=max(10, int(self.valves.REQUEST_TIMEOUT))
        )
        response.raise_for_status()
        payload = json.loads(response.text)
        if isinstance(payload, dict) and payload.get("status") == "error":
            message = str(payload.get("message", ""))
            if payload.get("code") in (429, 432) or "credit" in message.lower():
                self._td_note = (
                    "Twelve Data a refuse la requete (credits epuises ou cadence "
                    "depassee) : le relais a ete pris par Yahoo."
                )
            return None
        values = payload.get("values") if isinstance(payload, dict) else None
        if not values:
            return None
        self._resolved[symbol] = payload.get("meta") or {}
        rows = {}
        for row in values:
            close = _num(row.get("close"))
            if close is None:
                continue
            rows[row["datetime"]] = {
                "Open": _num(row.get("open")),
                "High": _num(row.get("high")),
                "Low": _num(row.get("low")),
                "Close": close,
                "Volume": _num(row.get("volume")) or 0.0,
            }
        if not rows:
            return None
        frame = pd.DataFrame.from_dict(rows, orient="index")
        frame.index = pd.to_datetime(frame.index)
        return frame.sort_index()

    def _fetch_one(self, symbol: str, as_of: datetime, years: int):  # noqa: C901
        """One symbol's history: cache first, then Yahoo, then Twelve Data.

        The cache is consulted before any network call and is the reason a second
        analysis of the same basket costs nothing. It is only trusted when it
        already reaches the analysis date — a series stopping short would quietly
        move every barrier observation.
        """
        cached = self._read_cache(symbol)
        needed_start = as_of - timedelta(days=int(years * 365.25))
        if cached is not None:
            covered = cached.index.max() >= pd.Timestamp(as_of) - pd.Timedelta(days=5)
            deep_enough = cached.index.min() <= pd.Timestamp(needed_start) + pd.Timedelta(days=10)
            if covered and deep_enough:
                return cached, "cache"

        start = needed_start.strftime("%Y-%m-%d")
        end = (as_of + timedelta(days=1)).strftime("%Y-%m-%d")

        def from_yahoo():
            # auto_adjust=False on purpose: the Close stays adjusted for splits
            # but NOT for dividends, which is exactly what an autocall barrier
            # observes. A total-return series would drift above the real price
            # and understate every barrier breach.
            return self._retry(
                lambda: yf.Ticker(symbol).history(start=start, end=end, auto_adjust=False)
            )

        # Shares go to Twelve Data first when a key is set: it spreads the load
        # over two providers, and every share not asked of Yahoo is one fewer
        # request counting towards its per-IP throttle. Indices have no such
        # choice — Twelve Data does not serve them.
        def from_td():
            return self._td_daily(symbol, as_of, years)

        fetchers = [("Yahoo Finance", from_yahoo), ("Twelve Data", from_td)] if not (
            (self.valves.TWELVEDATA_API_KEY or "").strip() and _venue_is_single_stock(symbol)
        ) else [("Twelve Data", from_td), ("Yahoo Finance", from_yahoo)]

        frame = None
        source = ""
        for name, fetch in fetchers:
            try:
                candidate = fetch()
            except Exception:
                continue
            if candidate is not None and not candidate.empty:
                frame, source = candidate, name
                break
        if frame is None or frame.empty:
            # Nothing live: a cache that does not quite reach today still beats
            # refusing the analysis, provided the caller is told.
            return (cached, "cache (perime)") if cached is not None else (None, "")
        self._write_cache(symbol, frame)
        return frame, source

    def _closes(self, symbols: tuple, as_of: datetime, years: int):
        """Aligned daily closes for the basket, bounded at the analysis date.

        Alignment matters more than it looks: the underlyings of a worst-of
        often trade on different calendars, and comparing a Paris close to a
        New York close from the previous session quietly distorts both the
        correlation and every barrier observation.

        Always downloaded at the longest window any tool needs, then sliced. The
        cache is keyed on the basket and the date only, so a three-year request
        reuses the fifteen-year series instead of fetching the whole basket a
        second time — which halved the number of calls Yahoo sees per analysis,
        and with it the odds of being throttled mid-run.
        """
        full_years = max(int(years), int(self.valves.HISTORY_YEARS))
        cache_key = (symbols, as_of.strftime("%Y-%m-%d"))
        cached = self._price_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 900:
            return self._slice(cached[1], as_of, years)

        # `full_years` drives the download, `years` stays the caller's request:
        # conflating them made the first call return the whole history instead
        # of the window that was asked for.
        # Reset per call: a note kept from an earlier failure would keep warning
        # about exhausted credits long after the provider recovered.
        self._td_note = ""
        self._resolved = {}
        series = {}
        sources = {}
        payouts = {}
        for symbol in symbols:
            frame, source = self._fetch_one(symbol, as_of, full_years)
            if frame is None or frame.empty:
                continue
            sources[symbol] = source
            closes = frame["Close"].copy()
            closes.index = pd.to_datetime(closes.index).tz_localize(None).normalize()
            # A zero or negative close is never a market price; left in, it turns
            # every ratio against it into an infinity and poisons the replay in
            # silence.
            closes = closes[closes > 0]
            if closes.empty:
                continue
            series[symbol] = closes[~closes.index.duplicated(keep="last")]
            # Yahoo returns dividends in the same response as the prices; they
            # were being thrown away. They cost nothing extra and carry the one
            # fundamental that mechanically moves an autocall barrier.
            if "Dividends" in frame.columns:
                paid = frame["Dividends"].copy()
                paid.index = pd.to_datetime(paid.index).tz_localize(None).normalize()
                paid = paid[paid > 0]
                if not paid.empty:
                    payouts[symbol] = paid[~paid.index.duplicated(keep="last")]

        if not series:
            return None
        spans = {k: (v.index[0], v.index[-1], len(v)) for k, v in series.items()}
        # Inner join: only sessions every underlying actually traded.
        frame = pd.DataFrame(series).dropna(how="any")
        if frame.empty:
            return None
        # Kept so the caller can name which underlying truncates the common
        # history: one recently listed share silently shortens the whole replay.
        frame.attrs["spans"] = spans
        frame.attrs["sources"] = sources
        frame.attrs["payouts"] = payouts
        frame.attrs["resolved"] = dict(self._resolved)
        if len(self._price_cache) >= 16:
            self._price_cache.pop(next(iter(self._price_cache)))
        self._price_cache[cache_key] = (time.monotonic(), frame)
        return self._slice(frame, as_of, years)

    @staticmethod
    def _slice(frame, as_of: datetime, years: int):
        """Narrow a cached basket to the window a caller asked for."""
        if frame is None:
            return None
        start = as_of - timedelta(days=int(years * 365.25))
        # The upper bound is re-applied here rather than trusted to the vendor's
        # `end` semantics. Look-ahead is the one guarantee of this tool that must
        # not depend on a third party keeping its exclusive bound exclusive.
        out = frame[(frame.index >= start) & (frame.index <= as_of)]
        if out.empty:
            return None
        # .attrs does not reliably survive slicing across pandas versions.
        out.attrs["spans"] = frame.attrs.get("spans", {})
        out.attrs["sources"] = frame.attrs.get("sources", {})
        out.attrs["payouts"] = frame.attrs.get("payouts", {})
        out.attrs["resolved"] = frame.attrs.get("resolved", {})
        return out

    @staticmethod
    def _annualized_vol(closes) -> float:
        """Annualised volatility of daily log returns."""
        returns = np.log(closes / closes.shift(1)).dropna()
        return float(returns.std() * np.sqrt(_TRADING_DAYS)) if len(returns) > 5 else float("nan")

    def _replay(
        self,
        closes,
        autocall: float,
        barrier: float,
        coupon: float,
        per_year: int,
        horizon_years: float,
        step: int,
        mode: str = "worst_of",
        weights=None,
    ) -> dict:
        """Replay the payoff over every historical start date.

        No pricing model and no implied volatility: each past window is run
        through the product's own rules and the outcomes are counted. It answers
        "how often would this have lost capital" with observed data rather than
        with an assumption about the distribution of returns.
        """
        total_obs = max(1, int(round(horizon_years * per_year)))
        period_days = max(1, int(round(_TRADING_DAYS / per_year)))
        horizon_days = total_obs * period_days
        values = closes.to_numpy()
        n = len(values)
        if n <= horizon_days + 1:
            return {"windows": 0, "needed_years": horizon_days / _TRADING_DAYS}
        # Entry state: how stretched the basket was at the start of a window,
        # measured against its own three-year trailing average. Averaging every
        # historical window together hides the thing that most separates two
        # otherwise identical products — whether they are struck at a high or a low.
        lookback = 3 * _TRADING_DAYS
        entries: list[tuple[float, bool]] = []

        called_at: dict[int, int] = {}
        losses: list[float] = []
        survived = 0
        lives: list[float] = []
        for start in range(0, n - horizon_days, max(1, step)):
            initial = values[start]
            outcome = None
            for k in range(1, total_obs + 1):
                worst = _basket_perf(values[start + k * period_days] / initial, mode, weights)
                if k < total_obs and worst >= autocall:
                    called_at[k] = called_at.get(k, 0) + 1
                    lives.append(k / per_year)
                    outcome = "called"
                    break
            lost = False
            if outcome is None:
                worst = _basket_perf(values[start + horizon_days] / initial, mode, weights)
                lives.append(total_obs / per_year)
                if worst >= barrier:
                    survived += 1
                else:
                    losses.append(worst - 1.0)
                    lost = True
            if start >= lookback:
                trailing = values[start - lookback : start].mean(axis=0)
                entries.append(
                    (_basket_perf(initial / trailing, mode, weights), lost)
                )

        windows = sum(called_at.values()) + survived + len(losses)

        # Terciles of entry state, with today's own position located among them.
        entry_analysis = {"available": False}
        if len(entries) >= 60 and n > lookback:
            entries.sort(key=lambda e: e[0])
            third = len(entries) // 3
            groups = [entries[:third], entries[third : 2 * third], entries[2 * third :]]
            today_trailing = values[n - lookback : n].mean(axis=0)
            today_entry = _basket_perf(values[-1] / today_trailing, mode, weights)
            buckets = []
            for label, group in zip(("bas", "median", "haut"), groups, strict=True):
                if not group:
                    continue
                buckets.append(
                    {
                        "label": label,
                        "low": group[0][0],
                        "high": group[-1][0],
                        "count": len(group),
                        "loss_rate": sum(1 for _, lost in group if lost) / len(group),
                    }
                )
            today_bucket = next(
                (b["label"] for b in buckets if b["low"] <= today_entry <= b["high"]),
                "haut" if buckets and today_entry > buckets[-1]["high"] else "bas",
            )
            entry_analysis = {
                "available": True,
                "buckets": buckets,
                "today": today_entry,
                "today_bucket": today_bucket,
                "covered": len(entries),
                # Coverage matters more than it looks. The breakdown needs three
                # years of trailing data, so it silently drops the oldest windows
                # — often the ones holding the worst crisis in the sample. A
                # headline loss rate of 30% sitting above three terciles at 0%
                # is not a paradox, it is two different samples.
                "coverage": len(entries) / windows if windows else 0.0,
            }

        # Overlapping windows are not independent observations. What limits the
        # precision is the span of distinct start dates divided by the holding
        # period: seven years of history behind a six-year product leaves barely
        # one non-overlapping trial, whatever the window count suggests.
        span_years = (n - horizon_days) / _TRADING_DAYS
        independent = span_years / max(horizon_years, 0.5)

        return {
            "span_years": span_years,
            "independent": independent,
            "entry_analysis": entry_analysis,
            "windows": windows,
            "called_at": called_at,
            "called_total": sum(called_at.values()),
            "survived": survived,
            "losses": len(losses),
            "loss_rate": len(losses) / windows if windows else float("nan"),
            "avg_loss": float(np.mean(losses)) if losses else 0.0,
            "worst_loss": float(np.min(losses)) if losses else 0.0,
            "avg_life": float(np.mean(lives)) if lives else float("nan"),
            "max_coupon": total_obs * coupon,
            "total_obs": total_obs,
        }

    # ------------------------------------------------------------------
    # tools exposed to the model
    # ------------------------------------------------------------------

    async def get_basket_profile(
        self,
        symbols: str,
        basket_mode: str = "",
        as_of_date: str = "",
        __event_emitter__: Callable[[dict], Any] | None = None,
    ) -> str:
        """
        Returns the risk profile of an autocall's underlying basket: each underlying's
        level, annualised volatility, worst historical drawdown and position in its
        52-week range, plus the pairwise correlation matrix.

        Correlation is the number to read first, but it reads in opposite directions
        depending on the basket: on a worst-of, underlyings that drift apart drag the
        weakest one down, so low correlation is the main danger; on a best-of the same
        dispersion works in the holder's favour. Pass the basket mode so the reading
        matches the product.

        :param symbols: Underlyings, comma separated. Index names are accepted
            (EuroStoxx50, CAC40, DAX, SP500, Nikkei) as well as individual share tickers
            (MC.PA, BNP.PA, NVDA).
        :param basket_mode: How the underlyings combine: average (an equally weighted or
            weighted basket) which is the default and the commonest shape, worst_of (the
            weakest underlying decides alone), best_of, or mono for a single underlying.
            Pass it explicitly whenever the term sheet states it.
        :param as_of_date: Analysis date in YYYY-MM-DD form. No data after this date is
            used. Leave empty for today.
        """
        if _IMPORT_ERROR:
            return _IMPORT_ERROR
        tickers = tuple(_normalize(s) for s in _split(symbols))
        if not tickers:
            return "Aucun sous-jacent fourni. Indiquer les sous-jacents separes par des virgules."
        as_of = _resolve_as_of(as_of_date)
        mode = _resolve_mode(basket_mode)
        if not mode:
            return _MODE_UNKNOWN
        defaulted = not (basket_mode or "").strip()
        await self._emit(__event_emitter__, f"Profil du panier ({len(tickers)} sous-jacents)...")
        try:
            closes = await self._run(self._closes, tickers, as_of, int(self.valves.HISTORY_YEARS))
        except Exception as exc:
            return f"Echec du chargement du panier: {exc or type(exc).__name__}"
        if closes is None:
            return (
                _unresolved_message(tickers, as_of, "niveaux")
            )

        conflict = _mono_conflict(basket_mode, len(closes.columns))
        if conflict:
            return conflict

        missing = [t for t in tickers if t not in closes.columns]
        sources = closes.attrs.get("sources") or {}
        distinct = sorted(set(sources.values()))
        provenance = distinct[0] if len(distinct) == 1 else ", ".join(distinct)
        lines = [
            f"# Panier — {as_of:%Y-%m-%d}",
            f"Source des cours : {provenance or 'n/a'}"
            + (f"  — {self._td_note}" if self._td_note else "")
            + (
                "  — **serie non rafraichie** : le fournisseur etait indisponible, les "
                "derniers jours peuvent manquer."
                if any("perime" in v for v in sources.values())
                else ""
            ),
            f"Mode : **{_mode_label(mode, len(closes.columns), defaulted)}**",
            f"{len(closes.columns)} sous-jacent{'s' if len(closes.columns) > 1 else ''}, {len(closes)} seances communes "
            f"({closes.index[0]:%Y-%m-%d} -> {closes.index[-1]:%Y-%m-%d})",
            "",
            "| Sous-jacent | Dernier | Vol. annualisee | Drawdown max | vs plus haut 52s |",
            "| --- | --- | --- | --- | --- |",
        ]
        for col in closes.columns:
            serie = closes[col]
            last = float(serie.iloc[-1])
            year = serie.tail(_TRADING_DAYS)
            drawdown = float(((serie / serie.cummax()) - 1).min())
            gap_high = (last - float(year.max())) / float(year.max()) if len(year) else float("nan")
            lines.append(
                f"| {col} | {_fmt(last)} | {_pct(self._annualized_vol(serie))} | "
                f"{_pct(drawdown)} | {_pct(gap_high)} |"
            )

        mean_corr = None
        if len(closes.columns) > 1:
            returns = np.log(closes / closes.shift(1)).dropna()
            corr = returns.corr()
            lines += ["", "## Correlation des rendements quotidiens", ""]
            lines.append("| | " + " | ".join(closes.columns) + " |")
            lines.append("| --- |" + " --- |" * len(closes.columns))
            for row in closes.columns:
                lines.append(
                    f"| **{row}** | "
                    + " | ".join(_fmt(corr.loc[row, c]) for c in closes.columns)
                    + " |"
                )
            off_diagonal = corr.to_numpy()[np.triu_indices(len(closes.columns), k=1)]
            mean_corr = float(np.mean(off_diagonal))
            del off_diagonal
            level = "elevee" if mean_corr >= 0.7 else "moderee" if mean_corr >= 0.4 else "faible"
            # The same number is good or bad news depending on the payoff: on a
            # worst-of dispersion drags the weakest underlying down, on a best-of
            # it lifts the strongest, and on an average basket it mostly cancels.
            readings = {
                "worst_of": {
                    "elevee": "Les sous-jacents bougent ensemble : le worst-of s'ecarte peu "
                    "du panier. C'est la configuration la plus favorable pour ce type de produit.",
                    "moderee": "Dispersion reelle : le worst-of sera sensiblement plus faible "
                    "que la moyenne du panier.",
                    "faible": "Forte dispersion. C'est le facteur de risque principal d'un "
                    "worst-of — il suffit qu'un seul sous-jacent decroche — et celui que les "
                    "documents commerciaux mentionnent le moins.",
                },
                "best_of": {
                    "elevee": "Les sous-jacents bougent ensemble : la dispersion n'apporte "
                    "presque rien, le best-of se comporte comme un mono-sous-jacent.",
                    "moderee": "Dispersion reelle : elle joue ici en faveur du porteur, le "
                    "best-of retient le plus fort.",
                    "faible": "Forte dispersion. Sur un best-of elle est un avantage : plus "
                    "les trajectoires divergent, plus le meilleur sous-jacent tire le produit "
                    "vers le haut.",
                },
                "average": {
                    "elevee": "Les sous-jacents bougent ensemble : le panier se comporte comme "
                    "un seul actif, sans benefice de diversification.",
                    "moderee": "Dispersion reelle : la moyenne lisse une partie des ecarts.",
                    "faible": "Forte dispersion, largement neutralisee par la moyenne : c'est "
                    "le mode de panier le moins sensible a la correlation.",
                },
            }
            lines += [
                "",
                f"Correlation moyenne entre paires : **{_fmt(mean_corr)}** ({level}). "
                + readings[mode][level],
            ]
        else:
            lines += [
                "",
                "Un seul sous-jacent : aucun effet de panier, le mode d'agregation est sans "
                "objet. En contrepartie, tout le risque est idiosyncratique — un avertissement "
                "sur resultats ou une operation sur titre frappe le produit sans amortisseur.",
            ]

        # Quality screen on the underlyings themselves. This is where a product
        # should be turned down — not on a marginally high loss frequency, but on
        # an underlying that has no business carrying a capital barrier: too
        # volatile to stay above it, historically prone to deep falls, or already
        # in structural decline.
        flags: list[str] = []
        for col in closes.columns:
            serie = closes[col]
            vol = self._annualized_vol(serie)
            drawdown = float(((serie / serie.cummax()) - 1).min())
            trailing = serie.tail(3 * _TRADING_DAYS)
            versus_mean = (
                float(serie.iloc[-1] / trailing.mean()) if len(trailing) > 60 else float("nan")
            )
            if vol == vol and vol > 0.35:
                flags.append(
                    f"**{col}** : volatilite de {_pct(vol)}, au-dela de 35%. Un sous-jacent "
                    "aussi mobile franchit une barriere sans qu'aucun evenement de credit "
                    "ne soit en cause."
                )
            if drawdown < -0.60:
                flags.append(
                    f"**{col}** : a deja perdu {_pct(drawdown)} depuis un sommet. Une chute "
                    "de cette ampleur repasse sous la plupart des barrieres de marche."
                )
            if versus_mean == versus_mean and versus_mean < 0.70:
                flags.append(
                    f"**{col}** : cote {_pct(versus_mean)} de sa moyenne trois ans, soit une "
                    "tendance durablement degradee. Le produit demarrerait deja bas."
                )
        # Instrument identity. A bare ticker is not a harmless shorthand here:
        # "SAN" resolves to Banco Santander before Sanofi, "BNP" to Danone,
        # "MC" to a Thai group before LVMH. The analysis would run to completion
        # on the wrong company and look entirely plausible.
        resolved = closes.attrs.get("resolved") or {}
        for col in closes.columns:
            if not _venue_is_single_stock(col):
                continue
            suffix = col.rpartition(".")[2] if "." in col else ""
            meta = resolved.get(col) or {}
            if not suffix:
                place = (
                    f" Le fournisseur a repondu : {meta.get('exchange', '?')} / "
                    f"{meta.get('currency', '?')}."
                    if meta
                    else ""
                )
                flags.append(
                    f"**{col}** : ticker sans code de place. Il ne designe pas une ligne "
                    "de cotation unique — 'SAN' renvoie Banco Santander avant Sanofi, "
                    "'BNP' renvoie Danone. Preciser la place (`SAN.PA`) et verifier que "
                    f"l'instrument analyse est bien celui du produit.{place}"
                )
                continue
            expected = _CURRENCY_BY_SUFFIX.get(suffix.upper())
            got = (meta.get("currency") or "").upper()
            if expected and got and got != expected:
                flags.append(
                    f"**{col}** : cote en {got} alors que la place '{suffix}' traite en "
                    f"{expected}. L'instrument resolu n'est probablement pas la ligne "
                    "attendue — verifier avant toute conclusion."
                )

        # Data integrity before anything else: a discontinuity invalidates every
        # statistic computed downstream, so it is not one risk signal among others.
        for col in closes.columns:
            breaks = _discontinuities(closes[col])
            if breaks:
                worst_break = max(breaks, key=lambda b: abs(b[1] - 1))
                flags.append(
                    f"**{col}** : discontinuite de {_pct(worst_break[1] - 1)} en une seule "
                    f"seance le {worst_break[0]:%Y-%m-%d}"
                    + (f" (et {len(breaks) - 1} autre(s))" if len(breaks) > 1 else "")
                    + ". Un mouvement de cette ampleur en un jour est presque toujours une "
                    "operation sur titre — division du nominal, regroupement — que la source "
                    "n'a pas retraitee. Le rejeu la compterait comme une perte reelle. "
                    "**Verifier la serie avant d'exploiter le moindre chiffre.**"
                )

        # Dividend drag. A barrier observes the price, not the total return, so a
        # generous payer walks its own price down towards the barrier every year
        # without anything going wrong in the business. The replay embeds this
        # for the past; a yield well above its own history does not show up there.
        payouts = closes.attrs.get("payouts") or {}
        unchecked = []
        for col in closes.columns:
            paid = payouts.get(col)
            if paid is None or paid.empty:
                # No dividend history is not the same as no dividends. Twelve
                # Data returns none at all, so on that path the check simply did
                # not run — saying "no signal" would claim a clean bill of health
                # for an examination nobody performed.
                if _venue_is_single_stock(col):
                    unchecked.append(col)
                continue
            serie = closes[col]
            spot = float(serie.iloc[-1])
            recent = paid[paid.index >= serie.index[-1] - pd.Timedelta(days=365)]
            current_yield = float(recent.sum()) / spot if spot else 0.0
            five = paid[paid.index >= serie.index[-1] - pd.Timedelta(days=5 * 365)]
            window = serie[serie.index >= serie.index[-1] - pd.Timedelta(days=5 * 365)]
            average_yield = (
                float(five.sum()) / 5 / float(window.mean())
                if len(window) > 60 and float(window.mean())
                else None
            )
            if current_yield > 0.05:
                flags.append(
                    f"**{col}** : rendement du dividende de {_pct(current_yield)}. La "
                    "barriere observe le cours, pas la performance totale : un tel "
                    f"detachement fait deriver le cours d'environ "
                    f"{_pct(current_yield * 6)} sur six ans, contre la barriere, sans "
                    "qu'aucune mauvaise nouvelle ne soit necessaire."
                )
            elif (
                average_yield
                and current_yield > 0.03
                and current_yield > 1.5 * average_yield
            ):
                flags.append(
                    f"**{col}** : rendement de {_pct(current_yield)}, soit plus de une "
                    f"fois et demie sa moyenne cinq ans ({_pct(average_yield)}). Un "
                    "rendement qui s'envole traduit le plus souvent un cours qui a "
                    "baisse, ou un versement qui ne tiendra pas."
                )

        # Structural signals: the combination, not each underlying on its own.
        # A worst-of adds a failure point with every extra name — the weakest one
        # decides for all — and that is the shape most often sold to retail.
        shares = [c for c in closes.columns if _venue_is_single_stock(c)]
        if len(closes.columns) == 1 and shares:
            flags.append(
                f"**{shares[0]}** : action unique, sans effet de panier. Un avertissement "
                "sur resultats ou une operation sur titre frappe le produit sans amortisseur."
            )
        elif mode == "worst_of" and len(shares) >= 2:
            flags.append(
                f"**Worst-of sur {len(shares)} actions individuelles** ({', '.join(shares)}). "
                "Chaque titre est un point de rupture supplementaire : il suffit qu'un seul "
                "decroche pour que le produit en subisse les consequences, la performance "
                "des autres etant sans effet. Le risque croit avec le nombre de noms, pas "
                "avec leur qualite moyenne."
            )
        if mode == "worst_of" and mean_corr is not None and mean_corr < 0.4:
            flags.append(
                f"**Correlation moyenne de {_fmt(mean_corr)}** sur un worst-of. Une "
                "dispersion aussi forte augmente mecaniquement la probabilite qu'un "
                "sous-jacent au moins passe sous la barriere."
            )

        lines += ["", "## Crible de qualite des sous-jacents", ""]
        if unchecked:
            flags.append(
                f"**Dividendes non verifies** pour {', '.join(unchecked)} : la source "
                "utilisee ne fournit pas l'historique des detachements. Le controle de "
                "derive par le dividende n'a pas pu etre effectue — absence de controle, "
                "pas absence de risque. Sur une action a fort rendement, relancer une fois "
                "Yahoo disponible."
            )
        if flags:
            lines.append(
                "Points d'attention releves — a traiter avant de retenir le produit pour "
                "un profil prudent :"
            )
            lines += [f"- {f}" for f in flags]
        else:
            lines.append(
                "Aucun signal d'alerte. Ont ete verifies : volatilite de chaque "
                "sous-jacent, ampleur des chutes historiques, tendance face a la moyenne "
                "trois ans, derive induite par le dividende, structure du panier et "
                "identite des instruments resolus. Le panier ne presente pas de fragilite "
                "propre au-dela du risque de marche ordinaire."
            )

        spans = closes.attrs.get("spans") or {}
        if spans:
            shortest = min(spans.items(), key=lambda kv: kv[1][2])
            longest = max(spans.values(), key=lambda v: v[2])[2]
            # Only worth saying when one underlying is materially shorter than the
            # others: comparing against the joined length flagged every basket,
            # including those whose series all start on the same day.
            if len(spans) > 1 and shortest[1][2] < longest * 0.9:
                lines += [
                    "",
                    f"*Historique commun limite par **{shortest[0]}**, dont la serie commence "
                    f"le {shortest[1][0]:%Y-%m-%d}. Une action recemment cotee raccourcit le "
                    "rejeu de tout le panier.*",
                ]

        if missing:
            lines += ["", f"Sous-jacents introuvables et exclus : {', '.join(missing)}."]
        await self._emit(__event_emitter__, "Profil du panier calcule", done=True)
        return self._cap("\n".join(lines))

    async def get_barrier_distance(
        self,
        symbols: str,
        barrier_pct: float,
        initial_levels: str = "",
        autocall_level_pct: float = 100.0,
        basket_mode: str = "",
        weights: str = "",
        as_of_date: str = "",
        __event_emitter__: Callable[[dict], Any] | None = None,
    ) -> str:
        """
        Measures how far the basket sits from the capital barrier and from the autocall
        threshold, both in percent and in standard deviations of the underlying.

        The standard-deviation reading is the one that matters: 20% of headroom on an
        underlying with 15% volatility is a comfortable margin, while the same 20% on one
        with 40% volatility is barely half a year's normal movement.

        :param symbols: Underlyings, comma separated.
        :param barrier_pct: Capital protection barrier as a percentage of the initial
            level, for example 60 for a barrier at 60%.
        :param initial_levels: Initial fixing level of each underlying, comma separated,
            in the same order as symbols. Leave empty for a product not yet issued: the
            current levels are then used as the reference.
        :param autocall_level_pct: Early redemption threshold as a percentage of the
            initial level. Defaults to 100.
        :param basket_mode: average (default), worst_of, best_of or mono. This decides which
            underlying actually governs the payoff, so pass it whenever it is known.
        :param weights: Basket weights, comma separated, in the same order as symbols.
            Only used when basket_mode is average. Leave empty for equal weights.
        :param as_of_date: Analysis date in YYYY-MM-DD form. Leave empty for today.
        """
        if _IMPORT_ERROR:
            return _IMPORT_ERROR
        tickers = tuple(_normalize(s) for s in _split(symbols))
        if not tickers:
            return "Aucun sous-jacent fourni."
        as_of = _resolve_as_of(as_of_date)
        for message in (
            _check_pct("barrier_pct", barrier_pct, 10, 100),
            _check_pct("autocall_level_pct", autocall_level_pct, 50, 150),
        ):
            if message:
                return message
        barrier = float(barrier_pct) / 100.0
        autocall = float(autocall_level_pct) / 100.0
        mode = _resolve_mode(basket_mode)
        if not mode:
            return _MODE_UNKNOWN
        defaulted = not (basket_mode or "").strip()
        basket_weights = _numbers(weights) or None
        await self._emit(__event_emitter__, "Distance aux barrieres...")
        try:
            closes = await self._run(self._closes, tickers, as_of, 3)
        except Exception as exc:
            return f"Echec du chargement des sous-jacents: {exc or type(exc).__name__}"
        if closes is None:
            return (
                _unresolved_message(tickers, as_of, "niveaux")
            )

        conflict = _mono_conflict(basket_mode, len(closes.columns)) or _check_weights(
            basket_weights or [], len(closes.columns)
        )
        if conflict:
            return conflict

        fixings = _numbers(initial_levels)
        if fixings and any(f <= 0 for f in fixings):
            return (
                "Les niveaux initiaux doivent etre strictement positifs. Un fixing a zero "
                "rendrait toute performance relative incalculable."
            )
        columns = list(closes.columns)
        if fixings and len(fixings) != len(columns):
            return (
                f"{len(fixings)} niveaux initiaux fournis pour {len(columns)} sous-jacents "
                f"resolus ({', '.join(columns)}). Fournir un niveau par sous-jacent, dans "
                "le meme ordre, ou aucun."
            )

        lines = [
            f"# Distance aux barrieres — {as_of:%Y-%m-%d}",
            f"Mode : **{_mode_label(mode, len(closes.columns), defaulted)}**",
            "Source des cours : "
            + (
                ", ".join(sorted(set((closes.attrs.get("sources") or {}).values())))
                or "n/a"
            ),
            f"Barriere capital : {_fmt(barrier_pct, 1)}% · Seuil de rappel : "
            f"{_fmt(autocall_level_pct, 1)}%",
            "",
            "| Sous-jacent | Niveau initial | Actuel | % de l'initial | Marge / barriere | En ecarts-types |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        perfs = []
        for i, col in enumerate(columns):
            serie = closes[col]
            spot = float(serie.iloc[-1])
            initial = fixings[i] if fixings else spot
            perf = spot / initial if initial else float("nan")
            perfs.append(perf)
            margin = (perf - barrier) / perf if perf else float("nan")
            vol = self._annualized_vol(serie)
            # A near-zero volatility makes the ratio explode into a meaningless
            # figure (thousands of sigma). Report it as unavailable instead:
            # a distance in standard deviations has no meaning without movement.
            sigmas = float("nan")
            if vol == vol and vol > 1e-4 and perf > 0 and perf > barrier:
                sigmas = float(np.log(perf / barrier) / vol)
            lines.append(
                f"| {col} | {_fmt(initial)} | {_fmt(spot)} | {_pct(perf)} | "
                f"{_pct(margin)} | {_fmt(sigmas)} σ |"
            )

        if not perfs:
            return "Aucune performance calculable."
        basket = _basket_perf(np.array(perfs), mode, basket_weights)
        if mode == "worst_of":
            driver = f"le plus faible ({columns[perfs.index(min(perfs))]})"
        elif mode == "best_of":
            driver = f"le plus fort ({columns[perfs.index(max(perfs))]})"
        else:
            driver = "la moyenne des sous-jacents"
        worst = basket
        lines += [
            "",
            f"**Niveau du panier ({mode}) : {_pct(basket)} de l'initial**, determine par "
            f"{driver}.",
            f"- Barriere capital atteinte si le worst-of passe sous {_fmt(barrier_pct, 1)}% "
            f"(soit une baisse supplementaire de {_pct((worst - barrier) / worst if worst else float('nan'))}).",
            f"- Rappel anticipe declenche si le worst-of repasse au-dessus de "
            f"{_fmt(autocall_level_pct, 1)}% "
            f"(soit une hausse de {_pct((autocall - worst) / worst if worst else float('nan'))}).",
            "",
            (
                "Le worst-of gouverne tout le produit : le sous-jacent le plus faible decide "
                "seul du remboursement, quelle que soit la performance des autres."
                if mode == "worst_of"
                else "Le best-of ne retient que le sous-jacent le plus fort : les autres "
                "peuvent s'effondrer sans consequence sur le remboursement."
                if mode == "best_of"
                else "Le panier est agrege en moyenne : une baisse marquee d'un seul "
                "sous-jacent est partiellement compensee par les autres."
            ),
        ]
        if not fixings:
            lines += [
                "",
                "*Aucun niveau initial fourni : les niveaux actuels servent de reference, "
                "ce qui suppose une emission a la date d'analyse. Pour un produit deja "
                "emis, fournir les fixings d'origine — les distances changent du tout au tout.*",
            ]
        await self._emit(__event_emitter__, "Distances calculees", done=True)
        return self._cap("\n".join(lines))

    async def simulate_autocall_history(
        self,
        symbols: str,
        barrier_pct: float,
        coupon_pct_per_period: float,
        autocall_level_pct: float = 100.0,
        observations_per_year: int = 1,
        horizon_years: float = 6.0,
        basket_mode: str = "",
        weights: str = "",
        as_of_date: str = "",
        __event_emitter__: Callable[[dict], Any] | None = None,
    ) -> str:
        """
        Replays the product's own rules over every historical window and counts what
        would have happened: how often it was called early and in which year, how often
        it reached maturity with capital intact, and how often it lost capital and by how
        much.

        This is the single most decision-relevant figure for an autocall, because it
        answers with observed data what a brochure answers with a scenario. Call it before
        forming any view on whether the coupon compensates the risk.

        :param symbols: Underlyings, comma separated.
        :param barrier_pct: Capital protection barrier as a percentage of the initial
            level (60 means capital is at risk below 60%).
        :param coupon_pct_per_period: Coupon paid per observation period, in percent.
        :param autocall_level_pct: Early redemption threshold in percent of initial.
        :param observations_per_year: Observation frequency: 1 annual, 2 semi-annual,
            4 quarterly, 12 monthly.
        :param horizon_years: Maximum maturity of the product in years.
        :param basket_mode: average (default), worst_of, best_of or mono. Getting it wrong
            misstates the risk in either direction, so read it off the term sheet.
        :param weights: Basket weights, comma separated, in the same order as symbols.
            Only used when basket_mode is average. Leave empty for equal weights.
        :param as_of_date: Analysis date in YYYY-MM-DD form. Leave empty for today.
        """
        if _IMPORT_ERROR:
            return _IMPORT_ERROR
        tickers = tuple(_normalize(s) for s in _split(symbols))
        if not tickers:
            return "Aucun sous-jacent fourni."
        as_of = _resolve_as_of(as_of_date)
        for message in (
            _check_pct("barrier_pct", barrier_pct, 10, 100),
            _check_pct("autocall_level_pct", autocall_level_pct, 50, 150),
            # Zero is legitimate: some autocalls pay no periodic coupon at all
            # and compensate with a redemption premium. Refusing it would block
            # a real product shape.
            _check_pct("coupon_pct_per_period", coupon_pct_per_period, 0, 50),
        ):
            if message:
                return message
        per_year = max(1, min(int(observations_per_year or 1), 12))
        horizon = max(0.5, min(float(horizon_years or 6), 15.0))
        mode = _resolve_mode(basket_mode)
        if not mode:
            return _MODE_UNKNOWN
        defaulted = not (basket_mode or "").strip()
        basket_weights = _numbers(weights) or None
        await self._emit(__event_emitter__, "Rejeu historique du payoff...")
        try:
            closes = await self._run(
                self._closes, tickers, as_of, int(self.valves.HISTORY_YEARS)
            )
        except Exception as exc:
            return f"Echec du chargement de l'historique: {exc or type(exc).__name__}"
        if closes is None:
            return (
                _unresolved_message(tickers, as_of, "probabilites")
            )

        conflict = _mono_conflict(basket_mode, len(closes.columns)) or _check_weights(
            basket_weights or [], len(closes.columns)
        )
        if conflict:
            return conflict

        stats = await self._run(
            self._replay,
            closes,
            float(autocall_level_pct) / 100.0,
            float(barrier_pct) / 100.0,
            float(coupon_pct_per_period),
            per_year,
            horizon,
            int(self.valves.WINDOW_STEP_DAYS),
            mode,
            basket_weights,
        )
        if not stats.get("windows"):
            return (
                f"Historique insuffisant : il faut plus de {stats.get('needed_years', horizon):.1f} "
                f"annees de seances communes aux {len(closes.columns)} sous-jacents pour "
                f"rejouer un produit de {horizon:.1f} ans. Serie disponible : "
                f"{len(closes) / _TRADING_DAYS:.1f} annees. Ne pas extrapoler."
            )

        windows = stats["windows"]
        called = stats["called_total"]
        lines = [
            f"# Rejeu historique — {', '.join(closes.columns)}",
            f"Mode : **{_mode_label(mode, len(closes.columns), defaulted)}**",
            "Source des cours : "
            + (
                ", ".join(sorted(set((closes.attrs.get("sources") or {}).values())))
                or "n/a"
            )
            + (f"  — {self._td_note}" if self._td_note else ""),
            f"Produit : barriere {_fmt(barrier_pct, 0)}%, rappel {_fmt(autocall_level_pct, 0)}%, "
            f"coupon {_fmt(coupon_pct_per_period, 2)}% par periode, "
            f"{per_year} observation(s)/an, {horizon:.1f} ans maximum.",
            f"Fenetres testees : **{windows}** depuis {closes.index[0]:%Y-%m-%d} "
            f"(un depart tous les {int(self.valves.WINDOW_STEP_DAYS)} jours de bourse).",
            f"Elles se recouvrent : les dates de depart s'etalent sur "
            f"{_fmt(stats['span_years'], 1)} an(s), soit environ "
            f"**{_fmt(stats['independent'], 1)} periode(s) reellement independante(s)** "
            f"de {horizon:.1f} ans.",
            "",
            "## Issues",
            "",
            "| Issue | Frequence |",
            "| --- | --- |",
            f"| Rappel anticipe | {called} soit {_pct(called / windows)} |",
            f"| Echeance, capital intact | {stats['survived']} soit {_pct(stats['survived'] / windows)} |",
            f"| **Perte en capital** | **{stats['losses']} soit {_pct(stats['loss_rate'])}** |",
            "",
            "## Rappel anticipe, par periode d'observation",
            "",
            "| Periode | Rappels | Part |",
            "| --- | --- | --- |",
        ]
        for k in sorted(stats["called_at"]):
            count = stats["called_at"][k]
            lines.append(f"| {k} ({k / per_year:.2f} an) | {count} | {_pct(count / windows)} |")

        entry = stats.get("entry_analysis") or {}
        if entry.get("available"):
            lines += [
                "",
                "## Selon le point d'entree",
                "",
                "Toutes les fenetres ne se valent pas : etre emis quand le sous-jacent est "
                "deja tendu par rapport a sa moyenne des trois dernieres annees n'expose pas "
                "au meme risque qu'une emission apres une correction. Les fenetres sont ici "
                "reparties en trois tiers selon ce niveau d'entree.",
                "",
                f"**Attention a l'echantillon** : ce decoupage porte sur "
                f"{entry['covered']} des {windows} fenetres ({_pct(entry['coverage'])}). "
                "Les trois premieres annees de l'historique servent a calculer la moyenne "
                "mobile et ne peuvent donc pas fournir de fenetre de depart.",
                "",
                "| Tiers d'entree | Niveau vs moyenne 3 ans | Fenetres | Perte en capital |",
                "| --- | --- | --- | --- |",
            ]
            for bucket in entry["buckets"]:
                mark = " ← **aujourd'hui**" if bucket["label"] == entry["today_bucket"] else ""
                lines.append(
                    f"| {bucket['label']}{mark} | {_pct(bucket['low'])} a {_pct(bucket['high'])} "
                    f"| {bucket['count']} | {_pct(bucket['loss_rate'])} |"
                )
            worst_b = max(entry["buckets"], key=lambda b: b["loss_rate"])
            best_b = min(entry["buckets"], key=lambda b: b["loss_rate"])
            today_rate = next(
                b["loss_rate"] for b in entry["buckets"] if b["label"] == entry["today_bucket"]
            )
            # The exclusion is not neutral when it removes a third of the sample,
            # and it is least neutral exactly when it removed the bad years.
            unreliable = entry["coverage"] < 0.85 and (
                stats["loss_rate"] - max(b["loss_rate"] for b in entry["buckets"]) > 0.05
            )
            lines += [
                "",
                f"Le panier vaut aujourd'hui **{_pct(entry['today'])}** de sa moyenne trois "
                f"ans, ce qui le place dans le tiers **{entry['today_bucket']}**. "
                f"Historiquement, les emissions dans ce tiers ont perdu du capital "
                f"{_pct(today_rate)} du temps, contre {_pct(best_b['loss_rate'])} pour le "
                f"tiers le plus favorable ({best_b['label']}) et {_pct(worst_b['loss_rate'])} "
                f"pour le plus defavorable ({worst_b['label']}).",
            ]
            if unreliable:
                lines += [
                    "",
                    f"> **Ne pas substituer ces taux au taux global.** Le decoupage ignore "
                    f"{_pct(1 - entry['coverage'])} des fenetres, et tous ses tiers "
                    f"ressortent sous le taux d'ensemble de {_pct(stats['loss_rate'])} : les "
                    "annees exclues concentrent les episodes de perte. La reference reste "
                    f"**{_pct(stats['loss_rate'])}**, le decoupage n'etant ici qu'indicatif.",
                ]

        # Return side, stated explicitly rather than left to be inferred. Two
        # products can be equally safe and worth entirely different things: one
        # that pays 2% and redeems after a year serves no one looking for yield.
        coupon_annual = float(coupon_pct_per_period) * per_year
        avg_life = stats["avg_life"]
        loss_annual = (
            abs(stats["avg_loss"]) * 100 * stats["loss_rate"] / avg_life
            if avg_life and avg_life == avg_life and stats["losses"]
            else 0.0
        )
        first_call = stats["called_at"].get(1, 0) / stats["windows"]
        # Below one full independent period the frequency rests on a single
        # non-overlapping trial. Above it the figure stays imprecise — which the
        # line above states in every report — but is no longer meaningless.
        if stats["independent"] < 1:
            lines += [
                "",
                "> **Echantillon insuffisant pour une frequence.** Avec moins de deux "
                "periodes independantes, les pourcentages ci-dessus n'ont pas la precision "
                "qu'ils affichent : ajouter ou retirer une annee d'historique peut les "
                "faire passer de 0% a 20%. A lire comme une illustration de ce qui a pu "
                "arriver, jamais comme une probabilite. Ne pas fonder de recommandation "
                "au-dessus du profil 6 sur ces chiffres.",
            ]

        lines += [
            "",
            "## Rendement et duree",
            "",
            f"- Coupon annualise si le produit vit : **{_fmt(coupon_annual, 2)}% par an**."
            + (
                "  **Verifier ce chiffre** : un tel niveau est rarissime sur un autocall "
                "et provient le plus souvent d'un coupon annuel saisi comme coupon par "
                f"periode ({_fmt(float(coupon_pct_per_period), 2)}% x {per_year} "
                "observations)."
                if coupon_annual > 25
                else ""
            ),
            f"- Perte esperee annualisee : **{_fmt(loss_annual, 2)}% par an** "
            f"(frequence de perte x perte moyenne, repartie sur la duree de vie).",
            f"- Ecart : **{_fmt(coupon_annual - loss_annual, 2)} point(s) par an**. "
            + (
                "Le coupon paie le risque historique."
                if coupon_annual > loss_annual
                else "**Le coupon ne paie pas le risque historique.**"
            ),
            f"- Duree de vie moyenne observee : **{_fmt(avg_life, 2)} ans** "
            f"(coupon cumule moyen d'environ {_fmt(avg_life * coupon_annual, 2)}%).",
            f"- Rappel des la premiere observation : {_pct(first_call)} des fenetres."
            + (
                " C'est le fonctionnement nominal du produit : coupon encaisse, capital "
                "rendu, risque referme rapidement. A prevoir cote allocation, le capital "
                "revenant tot et devant etre replace."
                if first_call > 0.5
                else ""
            ),
            f"- Coupon maximal si le produit va au terme : {_fmt(stats['max_coupon'], 2)}%.",
        ]
        if stats["losses"]:
            lines += [
                f"- Quand il y a eu perte, elle valait en moyenne "
                f"**{_pct(stats['avg_loss'])}** du capital, et jusqu'a "
                f"**{_pct(stats['worst_loss'])}** dans le pire cas.",
                f"- Une perte moyenne de {_pct(stats['avg_loss'])} efface "
                f"{_fmt(abs(stats['avg_loss'] * 100) / max(float(coupon_pct_per_period), 0.01), 1)} "
                "periodes de coupon.",
            ]
        else:
            lines.append(
                "- Aucune perte en capital sur l'historique teste. A ne pas lire comme une "
                "impossibilite : l'historique ne contient qu'un nombre limite de crises, "
                "et les fenetres se recouvrent largement."
            )
        lines += [
            "",
            "> **Portee et limites.** Les fenetres se recouvrent, donc ces frequences ne sont "
            "pas des probabilites independantes. Le rejeu ignore le risque de credit de "
            "l'emetteur, les frais d'entree et de structuration, la liquidite du marche "
            "secondaire, et suppose des coupons non conditionnels hors rappel. Il ne "
            "remplace pas la documentation du produit.",
        ]
        await self._emit(__event_emitter__, f"{windows} fenetres rejouees", done=True)
        return self._cap("\n".join(lines))
