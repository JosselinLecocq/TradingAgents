"""
title: TradingAgents Data
author: TradingAgents
author_url: https://github.com/TauricResearch/TradingAgents
version: 0.2.0
required_open_webui_version: 0.7.0
requirements: yfinance
description: Market data toolkit (identity, prices, technical indicators, news, fundamentals, macro context) powering the TradingAgents multi-agent analysis skill. Alpha Vantage first for date-bounded news and point-in-time financial statements, yfinance as a keyless fallback. Results are truncated at the analysis date so agents cannot see the future.
licence: MIT
"""

# Design notes for maintainers
# ---------------------------
# * Every public method is `async` because Open WebUI is moving to fully async
#   tool execution; the blocking yfinance calls run in a worker thread so they
#   never stall the event loop.
# * Two vendors, chosen per category. Alpha Vantage is preferred where it is
#   strictly better at avoiding look-ahead: NEWS_SENTIMENT bounds the feed
#   server-side with time_from/time_to, and the statement endpoints expose
#   `fiscalDateEnding`, so a fiscal period ending after the analysis date can be
#   dropped. yfinance stays the keyless fallback and keeps serving the macro
#   dashboard, which would otherwise cost one Alpha Vantage call per index.
# * Alpha Vantage answers are cached per process and per (function, params): the
#   free tier allows 25 calls a day, and prices + indicators would otherwise pay
#   twice for one identical series. Indicators are computed locally with pandas
#   rather than through AV's per-indicator endpoints, which cost one call each.
# * Only `yfinance` is declared in `requirements` on purpose (requests ships with
#   Open WebUI), to keep the risk of clashing with its own pins as small as
#   possible.
# * Outputs are compact Markdown, not raw dumps: a sub-agent has a bounded
#   context and a bounded output budget, so a 200-row OHLCV table is wasted.
# * `as_of_date` is the look-ahead guard. It mirrors the `trade_date` discipline
#   of the upstream TradingAgents graph: an agent analysing 2024-05-10 must not
#   receive a single data point from 2024-05-11.

import asyncio
import contextlib
import json
import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import requests
from pydantic import BaseModel, Field

try:
    import pandas as pd
    import yfinance as yf

    _IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - surfaced to the model at call time
    _IMPORT_ERROR = f"yfinance/pandas unavailable: {exc}"


_AV_URL = "https://www.alphavantage.co/query"

# Alpha Vantage cache bounds. The TTL exists so a second analysis of the same
# ticker later in the day still sees a refreshed session; the size cap exists
# because a full daily series is heavy and Open WebUI processes are long-lived.
_CACHE_TTL_SECONDS = 900
_CACHE_MAX_ENTRIES = 64

# Yahoo throttles per IP and yfinance does not retry 429 itself. Same shape as
# the upstream `yf_retry`, with a shorter budget so the backoff still fits
# inside REQUEST_TIMEOUT.
_YF_RETRIES = 2
_YF_BACKOFF_SECONDS = 1.5

# A vendor answering with rows that stop well before the analysis date is worse
# than one answering nothing: the prices look real. Same threshold as the
# upstream `MAX_OHLCV_STALE_DAYS`.
_MAX_STALE_DAYS = 10

# Instruments Alpha Vantage does not serve through its equity endpoints.
_NON_EQUITY_HINTS = ("-USD", "=F", "=X", "^")


class AlphaVantageError(RuntimeError):
    """Alpha Vantage refused the request (bad key, exhausted quota, premium)."""


# Symbols the user is likely to type in a chat, mapped to what Yahoo actually
# serves. Mirrors tradingagents/dataflows/symbol_utils.py in spirit.
_SYMBOL_ALIASES = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
    "XRP": "XRP-USD",
    "DOGE": "DOGE-USD",
    "XAUUSD": "GC=F",
    "GOLD": "GC=F",
    "XAGUSD": "SI=F",
    "OIL": "CL=F",
    "WTI": "CL=F",
    "BRENT": "BZ=F",
}

# Macro dashboard. Yahoo-only on purpose: seven Alpha Vantage calls for a
# backdrop table would eat a third of the free daily quota.
#
# The backdrop is regional. Reading a Paris-listed stock against the S&P, the
# Nasdaq and the dollar index describes someone else's market: the CAC, the
# Euro Stoxx and EUR/USD are what actually moves it. Risk appetite (VIX) and
# the two commodities stay in every profile because they are global.
_MACRO_GLOBAL = [
    ("^VIX", "VIX (volatilite implicite)"),
    ("CL=F", "Petrole WTI"),
    ("GC=F", "Or"),
]
_MACRO_PROFILES = {
    "us": [
        ("^GSPC", "S&P 500"),
        ("^IXIC", "Nasdaq Composite"),
        ("^TNX", "Taux 10 ans US"),
        ("DX-Y.NYB", "Dollar index"),
    ],
    "euro": [
        ("^FCHI", "CAC 40"),
        ("^STOXX50E", "Euro Stoxx 50"),
        ("^GDAXI", "DAX"),
        ("EURUSD=X", "EUR/USD"),
    ],
    "uk": [
        ("^FTSE", "FTSE 100"),
        ("^STOXX50E", "Euro Stoxx 50"),
        ("GBPUSD=X", "GBP/USD"),
        ("^GSPC", "S&P 500 (entrainement mondial)"),
    ],
    "ch": [
        ("^SSMI", "SMI"),
        ("^STOXX50E", "Euro Stoxx 50"),
        ("CHF=X", "USD/CHF"),
        ("^GSPC", "S&P 500 (entrainement mondial)"),
    ],
}
# Yahoo exchange suffixes. Needed as an explicit set rather than a rule because
# single letters are ambiguous: `.L` is London but `.B` is a share class, and
# both are one character. Anything listed here is a venue, never a class.
_YAHOO_VENUE_SUFFIXES = frozenset({
    # Europe
    "PA", "DE", "F", "AS", "BR", "MI", "MC", "LS", "VI", "IR", "HE", "ST",
    "OL", "CO", "L", "IL", "SW", "AT", "WA", "PR", "BD",
    # Americas
    "TO", "V", "NE", "CN", "MX", "SA", "BA", "SN",
    # Asia-Pacific, Middle East, Africa
    "T", "HK", "SS", "SZ", "AX", "NZ", "NS", "BO", "KS", "KQ", "TW", "TWO",
    "JK", "SI", "BK", "KL", "TA", "SR", "QA", "JO", "CA", "IS",
})

# Yahoo venue suffix -> macro profile.
_SUFFIX_PROFILES = {
    "PA": "euro", "DE": "euro", "F": "euro", "AS": "euro", "BR": "euro",
    "MI": "euro", "MC": "euro", "LS": "euro", "VI": "euro", "IR": "euro",
    "HE": "euro", "ST": "euro", "OL": "euro", "CO": "euro",
    "L": "uk", "IL": "uk",
    "SW": "ch",
}


def _venue_suffix(symbol: str) -> str:
    """The exchange suffix of a Yahoo-style ticker, or '' when there is none."""
    base, _, suffix = symbol.rpartition(".")
    return suffix if base and suffix.isalpha() else ""


def _normalize_symbol(symbol: str) -> str:
    """Uppercase, strip, and map common aliases to a Yahoo-resolvable ticker."""
    cleaned = (symbol or "").strip().upper().replace("$", "")
    if cleaned in _SYMBOL_ALIASES:
        return _SYMBOL_ALIASES[cleaned]
    # Share classes take a dash on Yahoo, not a dot: BRK.B is BRK-B. Only a
    # single letter that is not a known exchange code qualifies, otherwise this
    # would mangle London's TSCO.L into TSCO-L.
    suffix = _venue_suffix(cleaned)
    if len(suffix) == 1 and suffix not in _YAHOO_VENUE_SUFFIXES:
        # Only the final dot: a blanket replace would mangle a multi-dot symbol.
        base, _, cls = cleaned.rpartition(".")
        return f"{base}-{cls}"
    return cleaned


def _macro_profile(symbol: str) -> str:
    """Regional macro profile for a ticker, from its Yahoo venue suffix."""
    return _SUFFIX_PROFILES.get(_venue_suffix(symbol), "us")


def _av_can_serve(symbol: str) -> bool:
    """Whether Alpha Vantage's equity endpoints can serve this symbol.

    The two vendors do not share a suffix convention: London is `TSCO.L` on
    Yahoo and `TSCO.LON` on Alpha Vantage, Frankfurt is `.FRK` there. Yahoo
    venue codes are one or two letters, Alpha Vantage's are three or more, so
    the length tells them apart. Sending `AIR.PA` to Alpha Vantage only spends
    a request to be told the symbol does not exist — and in strict mode it
    aborts the run. A user who wants Alpha Vantage on a European line can pass
    its own symbol (`TSCO.LON`), which routes there as intended.
    """
    if any(hint in symbol for hint in _NON_EQUITY_HINTS):
        return False
    suffix = _venue_suffix(symbol)
    if not suffix:
        return True
    if suffix in _YAHOO_VENUE_SUFFIXES:
        return False
    # Three letters and unlisted: an Alpha Vantage venue code (LON, FRK, ...).
    # One or two letters and unlisted: most likely a Yahoo venue this table does
    # not know yet, so stay on the vendor that can serve it.
    return len(suffix) >= 3


def _resolve_as_of(as_of_date: str) -> datetime:
    """Parse the analysis date, defaulting to today. Invalid input falls back.

    A date in the future is clamped to now: it is always a model slip, and left
    alone it would stamp a report header with a day that has not happened yet.
    """
    now = datetime.now()
    if as_of_date:
        try:
            return min(datetime.strptime(as_of_date.strip()[:10], "%Y-%m-%d"), now)
        except ValueError:
            pass
    return now


def _fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
    """Format a number for prompt consumption, or 'n/a' when missing."""
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number != number:  # NaN
        return "n/a"
    return f"{number:,.{digits}f}{suffix}"


def _fmt_big(value: Any) -> str:
    """Human-readable large number (market cap, revenue, ...)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if number != number:
        return "n/a"
    for threshold, unit in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(number) >= threshold:
            return f"{number / threshold:,.2f}{unit}"
    return f"{number:,.0f}"


def _pct(value: Any, digits: int = 2) -> str:
    """Format a 0-1 ratio as a percentage."""
    try:
        return f"{float(value) * 100:,.{digits}f}%"
    except (TypeError, ValueError):
        return "n/a"


def _split_warning(source: str) -> list[str]:
    """Banner for a price series that is not adjusted for stock splits."""
    if "NON AJUSTEE" not in source:
        return []
    return [
        "",
        "> **Avertissement** — serie non ajustee des splits. Une division du "
        "nominal sur la periode apparait comme une chute brutale du cours qui "
        "n'a jamais eu lieu, et fausse moyennes mobiles, RSI, ATR et drawdown. "
        "Ne pas conclure sur cette base: repasser DATA_VENDOR sur 'auto'.",
    ]


def _is_rate_limited(exc: BaseException) -> bool:
    """Whether a vendor exception is a throttle rather than a real failure."""
    if "RateLimit" in type(exc).__name__:
        return True
    text = str(exc).lower()
    return "too many requests" in text or "rate limited" in text or "429" in text


def _stale_detail(frame: Any, as_of: datetime) -> str:
    """Describe how stale a price frame is, or '' when it is fresh enough."""
    # getattr rather than a bare .empty: a vendor shim returning an unexpected
    # shape must degrade to "not stale", never crash the price path.
    if frame is None or getattr(frame, "empty", True):
        return ""
    latest = frame.index.max()
    latest = latest.tz_localize(None) if getattr(latest, "tzinfo", None) else latest
    gap = (as_of - latest.to_pydatetime()).days
    if gap > _MAX_STALE_DAYS:
        return f"derniere seance {latest:%Y-%m-%d}, soit {gap} jours avant la date d'analyse"
    return ""


def _no_data_message(symbol: str, as_of: datetime, reason: str = "") -> str:
    """The one instructive sentinel returned when no vendor could serve a symbol.

    Wording follows the upstream router: an agent that reads "unavailable" must
    report unavailability, never estimate a plausible-looking price.
    """
    detail = f" ({reason})" if reason else ""
    return (
        f"DONNEES_INDISPONIBLES : aucune donnee de marche exploitable pour "
        f"'{symbol}' au {as_of:%Y-%m-%d} chez les fournisseurs configures{detail}. "
        "Le symbole peut etre invalide, delisted, non couvert, ou le fournisseur "
        "temporairement indisponible. Ne pas estimer ni inventer de valeur : "
        "signaler que la donnee est indisponible."
    )


def _err(exc: BaseException) -> str:
    """Readable error text. Some exceptions (TimeoutError) carry an empty message."""
    return str(exc) or type(exc).__name__


def _num(value: Any) -> float | None:
    """Coerce a provider value to a float. Alpha Vantage sends '-' and 'None'."""
    if value in (None, "", "-", "None", "none"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _ratio(numerator: Any, denominator: Any) -> float | None:
    """Safe division for derived ratios."""
    top, bottom = _num(numerator), _num(denominator)
    if top is None or not bottom:
        return None
    return top / bottom


def _yf_dividend_ratio(info: dict) -> float | None:
    """Dividend yield as a ratio, across incompatible yfinance conventions.

    `dividendYield` used to be a ratio (0.0215) and became a percentage (2.15)
    in later releases, so reading it blind turns a 2% yield into 215%. Prefer
    `trailingAnnualDividendYield`, which stayed a ratio in every version, and
    fall back to a magnitude heuristic only when it is missing.
    """
    trailing = _num(info.get("trailingAnnualDividendYield"))
    if trailing is not None:
        return trailing
    raw = _num(info.get("dividendYield"))
    if raw is None:
        return None
    # Above 1 the value cannot be a ratio: a 100%+ dividend yield is not a thing.
    return raw / 100 if raw > 1 else raw


class Tools:
    """Market data capability for the `trading-agents` skill."""

    class Valves(BaseModel):
        ALPHA_VANTAGE_API_KEY: str = Field(
            default="",
            description=(
                "Alpha Vantage API key. Unlocks date-bounded news and point-in-time "
                "financial statements. Falls back to the ALPHA_VANTAGE_API_KEY "
                "environment variable. Without a key everything runs on yfinance."
            ),
            # Masks the value in the Open WebUI form. Storage is unaffected:
            # valve values sit in the database as plaintext JSON unless the
            # admin sets ENABLE_VALVE_ENCRYPTION=true.
            json_schema_extra={"input": {"type": "password"}},
        )
        DATA_VENDOR: Literal["auto", "alpha_vantage", "yfinance"] = Field(
            default="auto",
            description=(
                "'auto' (recommended): Alpha Vantage for prices, news and fundamentals "
                "when a key is set, yfinance for the macro dashboard and as fallback. "
                "'alpha_vantage': never fall back, surface the error instead. "
                "'yfinance': ignore Alpha Vantage entirely."
            ),
        )
        NEWS_LOOKBACK_DAYS: int = Field(
            default=14,
            description="How far back the news window opens before the analysis date.",
        )
        LOOKBACK_DAYS: int = Field(
            default=180,
            description="Calendar days of price history loaded for indicators and trend stats.",
        )
        NEWS_LIMIT: int = Field(
            default=8,
            description="Maximum number of headlines returned per news call.",
        )
        MAX_OUTPUT_CHARS: int = Field(
            default=6000,
            description=(
                "Hard cap on the characters returned by any single call. Keeps a "
                "sub-agent's context (and the parent chat) from being flooded."
            ),
        )
        REQUEST_TIMEOUT: int = Field(
            default=30,
            description="Seconds before a data provider call is abandoned.",
        )

    class UserValves(BaseModel):
        LOOKBACK_DAYS: int = Field(
            default=0,
            description="Per-user override for the history window. 0 = use the admin setting.",
        )

    def __init__(self):
        """Initialize the Tool."""
        self.valves = self.Valves()
        # Per-process Alpha Vantage cache. The free tier allows 25 calls a day,
        # and one run would otherwise fetch the same daily series twice (once
        # for prices, once for indicators).
        self._av_cache: dict[tuple, tuple[float, Any]] = {}
        # Set once TIME_SERIES_DAILY_ADJUSTED comes back as premium on this
        # plan. Errors are not cached, so without this flag every price call
        # would keep spending a request on an endpoint known to be refused.
        self._av_adjusted_unavailable = False

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _lookback(self, __user__: dict | None, requested: int = 0) -> int:
        """Resolve the history window: explicit argument > user valve > admin valve."""
        if requested and requested > 0:
            return min(requested, 3650)
        # Open WebUI injects a UserValves object, but several call paths (and
        # every test harness) hand over a plain dict. Accept both.
        user_valves = (__user__ or {}).get("valves")
        if isinstance(user_valves, dict):
            user_days = user_valves.get("LOOKBACK_DAYS", 0) or 0
        else:
            user_days = getattr(user_valves, "LOOKBACK_DAYS", 0) or 0
        try:
            user_days = int(user_days)
        except (TypeError, ValueError):
            user_days = 0
        return user_days if user_days > 0 else int(self.valves.LOOKBACK_DAYS)

    def _cap(self, text: str) -> str:
        """Truncate an output that would blow past the configured budget."""
        limit = max(500, int(self.valves.MAX_OUTPUT_CHARS))
        if len(text) <= limit:
            return text
        return text[:limit] + "\n\n[... sortie tronquee par MAX_OUTPUT_CHARS ...]"

    async def _emit(
        self, emitter: Callable[[dict], Any] | None, description: str, done: bool = False
    ) -> None:
        """Push a status line into the chat. Status is the one event type that is
        fully supported in Native (agentic) function-calling mode."""
        if emitter is None:
            return
        # A broken emitter must never take the data call down with it.
        with contextlib.suppress(Exception):
            await emitter(
                {"type": "status", "data": {"description": description, "done": done}}
            )

    async def _run(self, func: Callable[..., Any], *args: Any, budget: int = 1) -> Any:
        """Run a blocking provider call in a worker thread, under REQUEST_TIMEOUT.

        `budget` multiplies the allowance for calls that fetch several symbols in
        one go. The worker thread itself cannot be killed, but the caller is freed
        so a stalled provider never pins a sub-agent for minutes.
        """
        timeout = max(5, int(self.valves.REQUEST_TIMEOUT)) * max(1, budget)
        return await asyncio.wait_for(asyncio.to_thread(func, *args), timeout=timeout)

    @staticmethod
    def _yf_retry(func: Callable[[], Any]) -> Any:
        """Run a yfinance call, backing off on Yahoo's per-IP 429 throttle.

        yfinance surfaces the throttle but never retries it, so one burst of
        parallel sub-agents can empty a whole report. Anything that is not a
        throttle propagates immediately — retrying a bad symbol is waste.
        """
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

    def _yf_history(self, symbol: str, as_of: datetime, days: int):
        """Blocking OHLCV fetch, bounded at `as_of` (inclusive)."""
        start = (as_of - timedelta(days=days)).strftime("%Y-%m-%d")
        # yfinance's `end` is exclusive, so +1 day makes as_of inclusive.
        end = (as_of + timedelta(days=1)).strftime("%Y-%m-%d")
        frame = self._yf_retry(
            lambda: yf.Ticker(symbol).history(start=start, end=end, auto_adjust=False)
        )
        if frame is None or frame.empty:
            return None
        return frame

    def _yf_info(self, symbol: str) -> dict:
        """Blocking metadata fetch, tolerant of yfinance version differences."""
        ticker = yf.Ticker(symbol)
        for accessor in ("get_info", "info"):
            try:
                candidate = getattr(ticker, accessor)
                data = self._yf_retry(candidate) if callable(candidate) else candidate
                if isinstance(data, dict) and data:
                    return data
            except Exception:
                continue
        return {}

    def _av_key(self) -> str:
        """Alpha Vantage key from the valve, else from the environment."""
        return (self.valves.ALPHA_VANTAGE_API_KEY or os.getenv("ALPHA_VANTAGE_API_KEY", "")).strip()

    def _vendor(self, symbol: str = "") -> str:
        """Which vendor to try first for this symbol."""
        mode = (self.valves.DATA_VENDOR or "auto").strip().lower()
        if mode == "yfinance":
            return "yfinance"
        if mode == "alpha_vantage":
            return "alpha_vantage"
        if self._av_key() and (not symbol or _av_can_serve(symbol)):
            return "alpha_vantage"
        return "yfinance"

    def _av_only_blocked(self, symbol: str = "") -> str:
        """Why an Alpha-Vantage-only tool cannot run, naming the actual cause.

        Three distinct reasons — the operator disabled the vendor, no key is
        set, or the venue is not covered — and telling them apart matters:
        "il faut une cle" sent to someone who deliberately set DATA_VENDOR to
        yfinance is a wrong answer to a question they did not ask.
        """
        if self._vendor(symbol) != "alpha_vantage":
            if (self.valves.DATA_VENDOR or "").strip().lower() == "yfinance":
                return "la valve DATA_VENDOR est reglee sur 'yfinance', qui desactive Alpha Vantage"
            if not self._av_key():
                return "aucune cle Alpha Vantage n'est configuree"
        if not self._av_key():
            return "aucune cle Alpha Vantage n'est configuree"
        if symbol and not _av_can_serve(symbol):
            return (
                f"Alpha Vantage ne couvre pas '{symbol}' (actions americaines uniquement "
                "pour cet endpoint)"
            )
        return ""

    def _unservable(self, symbol: str) -> str:
        """Why strict Alpha Vantage mode cannot serve this symbol, or ''.

        Without this the run reports "identite introuvable, verifie le symbole"
        for a perfectly valid ticker, blaming the user for a vendor routing
        decision — and strict mode is exactly what one turns on to diagnose a
        key problem.
        """
        if not self._strict_av() or _av_can_serve(symbol):
            return ""
        suffix = _venue_suffix(symbol)
        if suffix:
            return (
                f"{symbol} n'est pas servi par Alpha Vantage en mode strict: la place "
                f"'{suffix}' suit la convention Yahoo, alors qu'Alpha Vantage emploie la "
                "sienne (Londres est TSCO.L chez Yahoo, TSCO.LON chez Alpha Vantage). Le "
                "symbole est valide — c'est le fournisseur qui ne le couvre pas. Repasse "
                "DATA_VENDOR sur 'auto', ou fournis le symbole Alpha Vantage."
            )
        return (
            f"{symbol} sort du perimetre des endpoints actions d'Alpha Vantage (crypto, "
            "future, indice ou paire de devises). Repasse DATA_VENDOR sur 'auto' pour "
            "que yfinance prenne le relais."
        )

    def _strict_av(self) -> bool:
        """True when the operator asked for Alpha Vantage with no silent fallback."""
        return (self.valves.DATA_VENDOR or "").strip().lower() == "alpha_vantage"

    def _av_request(self, function: str, params: dict) -> Any:
        """Blocking Alpha Vantage call, cached per process.

        Alpha Vantage answers HTTP 200 with an "Information"/"Note" field when
        something is wrong, so the payload has to be classified rather than the
        status code. Rate-limit phrasing is tested first because those notices
        also mention the API key ("your API key ... 25 requests per day").
        """
        key = self._av_key()
        if not key:
            raise AlphaVantageError("aucune cle Alpha Vantage configuree")

        cache_key = (function, tuple(sorted(params.items())))
        cached = self._av_cache.get(cache_key)
        if cached is not None:
            stamp, payload = cached
            if time.monotonic() - stamp < _CACHE_TTL_SECONDS:
                return payload
            del self._av_cache[cache_key]

        query = {**params, "function": function, "apikey": key, "source": "trading_agents"}
        response = requests.get(
            _AV_URL, params=query, timeout=max(5, int(self.valves.REQUEST_TIMEOUT))
        )
        response.raise_for_status()
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError:
            payload = response.text

        if isinstance(payload, dict):
            notice = (
                payload.get("Information")
                or payload.get("Note")
                or payload.get("Error Message")
            )
            if notice:
                low = str(notice).lower()
                if any(m in low for m in ("rate limit", "requests per day", "call frequency")):
                    raise AlphaVantageError(f"quota Alpha Vantage epuise: {notice}")
                if "premium" in low:
                    raise AlphaVantageError(f"endpoint Alpha Vantage premium: {notice}")
                if "api key" in low or "apikey" in low:
                    raise AlphaVantageError(f"cle Alpha Vantage invalide: {notice}")
                raise AlphaVantageError(f"Alpha Vantage: {notice}")

        # Oldest-first eviction: dicts preserve insertion order, so the first
        # key is the least recently stored.
        while len(self._av_cache) >= _CACHE_MAX_ENTRIES:
            self._av_cache.pop(next(iter(self._av_cache)))
        self._av_cache[cache_key] = (time.monotonic(), payload)
        return payload

    def _av_daily(self, symbol: str, as_of: datetime, days: int):
        """Daily OHLCV from Alpha Vantage, shaped like a yfinance frame.

        TIME_SERIES_DAILY_ADJUSTED comes first because split-adjusted closes are
        what indicators need. It is a premium endpoint on the free plan — verified
        against the live API — and the unadjusted TIME_SERIES_DAILY that remains
        is genuinely dangerous here: it quotes prices as traded, so a window
        spanning a split (NVDA's 10-for-1 in June 2024) carries a 90% cliff that
        is not a market move. Every moving average, RSI, ATR and drawdown
        computed across it would be wrong.

        So when the adjusted endpoint is refused, this returns None and lets the
        caller fall back to yfinance, which always adjusts for splits. Only
        strict Alpha Vantage mode, where falling back is forbidden, takes the
        unadjusted series — flagged via `frame.attrs` so the report can warn.
        """
        # "compact" returns the latest 100 points counted from today, not from
        # the analysis date: a short window on a past date would come back empty
        # and silently demote the run to yfinance. Size on the distance between
        # today and the start of the requested window instead.
        window_start = as_of - timedelta(days=days)
        recent = (datetime.now() - window_start).days < 100
        params = {"symbol": symbol, "outputsize": "compact" if recent else "full"}
        adjusted = not self._av_adjusted_unavailable
        if adjusted:
            try:
                payload = self._av_request("TIME_SERIES_DAILY_ADJUSTED", params)
            except AlphaVantageError as exc:
                if "premium" not in str(exc):
                    raise
                # Remember it for the life of the process: on the free tier this
                # would otherwise burn one of 25 daily requests on every call.
                self._av_adjusted_unavailable = True
                adjusted = False
        if not adjusted:
            if not self._strict_av():
                # yfinance is split-safe and costs nothing: prefer it outright
                # rather than serving indicators computed on raw traded prices.
                return None
            payload = self._av_request("TIME_SERIES_DAILY", params)

        series = payload.get("Time Series (Daily)") if isinstance(payload, dict) else None
        if not series:
            return None

        close_key = "5. adjusted close" if adjusted else "4. close"
        volume_key = "6. volume" if adjusted else "5. volume"
        rows = {}
        for day, values in series.items():
            close = _num(values.get(close_key))
            if close is None:
                close = _num(values.get("4. close"))
            if close is None:
                continue
            rows[day] = {
                "Open": _num(values.get("1. open")),
                "High": _num(values.get("2. high")),
                "Low": _num(values.get("3. low")),
                "Close": close,
                "Volume": _num(values.get(volume_key)) or 0.0,
            }
        if not rows:
            return None

        frame = pd.DataFrame.from_dict(rows, orient="index")
        frame.index = pd.to_datetime(frame.index)
        frame = frame.sort_index()
        frame.attrs["unadjusted"] = not adjusted
        start = as_of - timedelta(days=days)
        frame = frame[(frame.index >= start) & (frame.index <= as_of)]
        return frame if not frame.empty else None

    def _prices(self, symbol: str, as_of: datetime, days: int) -> tuple[Any, str]:
        """OHLCV from the preferred vendor, falling back unless told not to."""
        if self._vendor(symbol) == "alpha_vantage":
            try:
                frame = self._av_daily(symbol, as_of, days)
                if frame is not None:
                    label = "Alpha Vantage"
                    if frame.attrs.get("unadjusted"):
                        label += " — SERIE NON AJUSTEE DES SPLITS"
                    return frame, label
                if self._strict_av():
                    return None, "Alpha Vantage"
            except AlphaVantageError:
                if self._strict_av():
                    raise
        frame = self._yf_history(symbol, as_of, days)
        # Present-but-stale rows are the dangerous case: they look like real
        # prices. Treated as no data, exactly as the upstream router does.
        stale = _stale_detail(frame, as_of)
        if stale:
            return None, f"stale:{stale}"
        return frame, "Yahoo Finance"

    def _identity(self, symbol: str) -> tuple[dict, str]:
        """Instrument identity, normalised across vendors."""
        if self._vendor(symbol) == "alpha_vantage":
            try:
                overview = self._av_request("OVERVIEW", {"symbol": symbol})
                if isinstance(overview, dict) and overview.get("Symbol"):
                    return {
                        "name": overview.get("Name"),
                        "quote_type": overview.get("AssetType"),
                        "sector": overview.get("Sector"),
                        "industry": overview.get("Industry"),
                        "exchange": overview.get("Exchange"),
                        "currency": overview.get("Currency"),
                        "country": overview.get("Country"),
                        "market_cap": _num(overview.get("MarketCapitalization")),
                    }, "Alpha Vantage"
                if self._strict_av():
                    return {}, "Alpha Vantage"
            except AlphaVantageError:
                if self._strict_av():
                    raise
        info = self._yf_info(symbol)
        if not info:
            return {}, "Yahoo Finance"
        return {
            "name": info.get("longName") or info.get("shortName"),
            "quote_type": info.get("quoteType"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "exchange": info.get("fullExchangeName") or info.get("exchange"),
            "currency": info.get("currency"),
            "country": info.get("country"),
            "market_cap": _num(info.get("marketCap")),
        }, "Yahoo Finance"

    @staticmethod
    def _latest_report(payload: Any, as_of: datetime) -> dict:
        """Most recent statement whose fiscal period ended on or before `as_of`.

        This is the point-in-time guard yfinance cannot offer. It keys on the
        period end, not the publication date: a quarter closing three days before
        the analysis date was not yet public then. An approximation, and a far
        better one than today's snapshot, but not certain knowledge.

        Quarterly reports come first, annual ones second: plenty of issuers
        outside the US only publish annually, and ignoring `annualReports` left
        them with no point-in-time statement at all.
        """
        if not isinstance(payload, dict):
            return {}
        cutoff = as_of.strftime("%Y-%m-%d")
        for key, kind in (("quarterlyReports", "trimestre"), ("annualReports", "exercice")):
            reports = [
                r
                for r in (payload.get(key) or [])
                if isinstance(r, dict) and r.get("fiscalDateEnding", "") <= cutoff
            ]
            if reports:
                latest = dict(max(reports, key=lambda r: r.get("fiscalDateEnding", "")))
                latest["_period_kind"] = kind
                return latest
        return {}

    def _av_fundamentals(self, symbol: str, as_of: datetime) -> dict:
        """Current multiples from OVERVIEW plus point-in-time statements."""
        overview = self._av_request("OVERVIEW", {"symbol": symbol})
        if not isinstance(overview, dict) or not overview.get("Symbol"):
            return {}

        income = self._latest_report(
            self._av_request("INCOME_STATEMENT", {"symbol": symbol}), as_of
        )
        balance = self._latest_report(
            self._av_request("BALANCE_SHEET", {"symbol": symbol}), as_of
        )
        cashflow = self._latest_report(self._av_request("CASH_FLOW", {"symbol": symbol}), as_of)

        equity = _num(balance.get("totalShareholderEquity"))
        debt = _num(balance.get("shortLongTermDebtTotal"))
        operating_cf = _num(cashflow.get("operatingCashflow"))
        capex = _num(cashflow.get("capitalExpenditures"))

        return {
            "source": "Alpha Vantage",
            "name": overview.get("Name"),
            "quote_type": overview.get("AssetType"),
            "period": income.get("fiscalDateEnding") or balance.get("fiscalDateEnding"),
            "period_kind": income.get("_period_kind") or balance.get("_period_kind") or "periode",
            "market_cap": _num(overview.get("MarketCapitalization")),
            "trailing_pe": _num(overview.get("TrailingPE")) or _num(overview.get("PERatio")),
            "forward_pe": _num(overview.get("ForwardPE")),
            "peg": _num(overview.get("PEGRatio")),
            "ps": _num(overview.get("PriceToSalesRatioTTM")),
            "pb": _num(overview.get("PriceToBookRatio")),
            "ev_ebitda": _num(overview.get("EVToEBITDA")),
            "gross_margin": _ratio(overview.get("GrossProfitTTM"), overview.get("RevenueTTM")),
            "operating_margin": _num(overview.get("OperatingMarginTTM")),
            "profit_margin": _num(overview.get("ProfitMargin")),
            "roe": _num(overview.get("ReturnOnEquityTTM")),
            "revenue_growth": _num(overview.get("QuarterlyRevenueGrowthYOY")),
            "earnings_growth": _num(overview.get("QuarterlyEarningsGrowthYOY")),
            "target_mean": _num(overview.get("AnalystTargetPrice")),
            "target_low": None,
            "target_high": None,
            "recommendation": None,
            "analysts": None,
            "dividend_yield": _num(overview.get("DividendYield")),
            "beta": _num(overview.get("Beta")),
            "revenue": _num(income.get("totalRevenue")),
            "operating_income": _num(income.get("operatingIncome")),
            "net_income": _num(income.get("netIncome")),
            "cash": _num(balance.get("cashAndCashEquivalentsAtCarryingValue")),
            "debt": debt,
            "debt_to_equity_pct": (debt / equity * 100) if (debt is not None and equity) else None,
            "current_ratio": _ratio(
                balance.get("totalCurrentAssets"), balance.get("totalCurrentLiabilities")
            ),
            "fcf": (operating_cf - capex)
            if (operating_cf is not None and capex is not None)
            else None,
        }

    def _yf_fundamentals(self, symbol: str) -> dict:
        """Normalised fundamentals from yfinance. Everything is a current snapshot."""
        info = self._yf_info(symbol)
        if not info:
            return {}
        return {
            "source": "Yahoo Finance",
            "name": info.get("longName") or info.get("shortName"),
            "quote_type": info.get("quoteType"),
            "period": None,
            "period_kind": None,
            "market_cap": _num(info.get("marketCap")),
            "trailing_pe": _num(info.get("trailingPE")),
            "forward_pe": _num(info.get("forwardPE")),
            "peg": _num(info.get("trailingPegRatio")),
            "ps": _num(info.get("priceToSalesTrailing12Months")),
            "pb": _num(info.get("priceToBook")),
            "ev_ebitda": _num(info.get("enterpriseToEbitda")),
            "gross_margin": _num(info.get("grossMargins")),
            "operating_margin": _num(info.get("operatingMargins")),
            "profit_margin": _num(info.get("profitMargins")),
            "roe": _num(info.get("returnOnEquity")),
            "revenue_growth": _num(info.get("revenueGrowth")),
            "earnings_growth": _num(info.get("earningsGrowth")),
            "target_mean": _num(info.get("targetMeanPrice")),
            "target_low": _num(info.get("targetLowPrice")),
            "target_high": _num(info.get("targetHighPrice")),
            "recommendation": info.get("recommendationKey"),
            "analysts": info.get("numberOfAnalystOpinions"),
            "dividend_yield": _yf_dividend_ratio(info),
            "beta": _num(info.get("beta")),
            "revenue": _num(info.get("totalRevenue")),
            "operating_income": None,
            "net_income": None,
            "cash": _num(info.get("totalCash")),
            "debt": _num(info.get("totalDebt")),
            # yfinance already reports this as a percentage (172.5 meaning 1.73x).
            "debt_to_equity_pct": _num(info.get("debtToEquity")),
            "current_ratio": _num(info.get("currentRatio")),
            "fcf": _num(info.get("freeCashflow")),
        }

    def _fundamentals(self, symbol: str, as_of: datetime) -> dict:
        """Fundamentals from the preferred vendor, falling back unless told not to."""
        if self._vendor(symbol) == "alpha_vantage":
            try:
                data = self._av_fundamentals(symbol, as_of)
                if data:
                    return data
                if self._strict_av():
                    return {}
            except AlphaVantageError:
                if self._strict_av():
                    raise
        return self._yf_fundamentals(symbol)

    def _av_news(self, symbol: str, as_of: datetime, days: int, limit: int) -> list:
        """Headlines bounded server-side by Alpha Vantage's time_from/time_to."""
        start = as_of - timedelta(days=days)
        payload = self._av_request(
            "NEWS_SENTIMENT",
            {
                "tickers": symbol,
                "time_from": start.strftime("%Y%m%dT0000"),
                "time_to": as_of.strftime("%Y%m%dT2359"),
                "limit": str(max(limit, 50)),
                "sort": "LATEST",
            },
        )
        feed = payload.get("feed") if isinstance(payload, dict) else None
        if not feed:
            return []

        items = []
        for entry in feed:
            title = entry.get("title")
            if not title:
                continue
            published = None
            raw = entry.get("time_published")
            if raw:
                with contextlib.suppress(ValueError):
                    published = datetime.strptime(str(raw)[:15], "%Y%m%dT%H%M%S")
            sentiment = None
            for block in entry.get("ticker_sentiment") or []:
                if (block.get("ticker") or "").upper() == symbol.upper():
                    sentiment = block.get("ticker_sentiment_label")
                    break
            items.append(
                {
                    "title": title,
                    "publisher": entry.get("source") or "source inconnue",
                    "date": published.strftime("%Y-%m-%d") if published else "date inconnue",
                    "url": entry.get("url") or "",
                    "summary": (entry.get("summary") or "")[:280],
                    "sentiment": sentiment or entry.get("overall_sentiment_label"),
                }
            )
            if len(items) >= limit:
                break
        return items

    def _yf_news(self, symbol: str, as_of: datetime, limit: int) -> list:
        """Headlines from yfinance, filtered client-side at the analysis date."""
        raw = list(getattr(yf.Ticker(symbol), "news", None) or [])
        cutoff = as_of + timedelta(days=1)
        items = []
        for entry in raw:
            # yfinance changed its news schema: newer builds nest everything
            # under "content", older ones keep flat keys. Support both.
            body = entry.get("content") if isinstance(entry.get("content"), dict) else entry
            title = body.get("title") or entry.get("title")
            if not title:
                continue

            published = None
            raw_date = body.get("pubDate") or body.get("displayTime")
            if raw_date:
                try:
                    published = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
                    published = published.replace(tzinfo=None)
                except ValueError:
                    published = None
            if published is None and entry.get("providerPublishTime"):
                try:
                    published = datetime.fromtimestamp(
                        int(entry["providerPublishTime"]), tz=timezone.utc
                    ).replace(tzinfo=None)
                except (TypeError, ValueError, OSError):
                    published = None

            # Drop anything after the analysis date. An item with no usable
            # timestamp is kept but flagged, so the agent can weigh it itself.
            if published is not None and published >= cutoff:
                continue

            # Nested schema first, then the flat legacy keys. An empty dict is
            # still a dict, so each branch has to fall through explicitly.
            provider = body.get("provider")
            publisher = None
            if isinstance(provider, dict):
                publisher = provider.get("displayName")
            elif isinstance(provider, str):
                publisher = provider
            publisher = publisher or entry.get("publisher") or "source inconnue"

            url_field = body.get("canonicalUrl") or body.get("clickThroughUrl")
            url = url_field.get("url") if isinstance(url_field, dict) else url_field
            url = url or entry.get("link") or ""

            items.append(
                {
                    "title": title,
                    "publisher": publisher,
                    "date": published.strftime("%Y-%m-%d") if published else "date inconnue",
                    "url": url or "",
                    "summary": (body.get("summary") or "")[:280],
                    "sentiment": None,
                }
            )
            if len(items) >= limit:
                break

        return items

    def _av_topic_news(self, as_of: datetime, days: int, limit: int) -> list:
        """Market-wide news, by topic rather than by ticker.

        The upstream news analyst reads both: what is happening TO the company
        and what is happening IN the market are different inputs, and a report
        built on the first alone misses the regime it is trading into.
        """
        start = as_of - timedelta(days=days)
        payload = self._av_request(
            "NEWS_SENTIMENT",
            {
                "topics": "financial_markets,economy_macro,economy_monetary",
                "time_from": start.strftime("%Y%m%dT0000"),
                "time_to": as_of.strftime("%Y%m%dT2359"),
                "limit": str(max(limit, 50)),
                "sort": "LATEST",
            },
        )
        feed = payload.get("feed") if isinstance(payload, dict) else None
        items = []
        for entry in feed or []:
            title = entry.get("title")
            if not title:
                continue
            published = None
            raw = entry.get("time_published")
            if raw:
                with contextlib.suppress(ValueError):
                    published = datetime.strptime(str(raw)[:15], "%Y%m%dT%H%M%S")
            items.append(
                {
                    "title": title,
                    "publisher": entry.get("source") or "source inconnue",
                    "date": published.strftime("%Y-%m-%d") if published else "date inconnue",
                    "url": entry.get("url") or "",
                    "summary": (entry.get("summary") or "")[:280],
                    "sentiment": entry.get("overall_sentiment_label"),
                }
            )
            if len(items) >= limit:
                break
        return items

    def _av_insiders(self, symbol: str, as_of: datetime, limit: int) -> list:
        """Insider transactions, dropped past the analysis date.

        Alpha Vantage returns every filing it has, including ones dated after
        the analysis date, so the look-ahead filter is applied here rather than
        trusted to the endpoint.
        """
        payload = self._av_request("INSIDER_TRANSACTIONS", {"symbol": symbol})
        rows = payload.get("data") if isinstance(payload, dict) else None
        cutoff = as_of.strftime("%Y-%m-%d")
        out = []
        for row in rows or []:
            date = (row.get("transaction_date") or "").strip()
            if not date or date > cutoff:
                continue
            out.append(
                {
                    "date": date,
                    "who": row.get("executive") or "n/a",
                    "role": row.get("executive_title") or "n/a",
                    "sense": "achat" if (row.get("acquisition_or_disposal") or "") == "A" else "vente",
                    "shares": _num(row.get("shares")),
                    "price": _num(row.get("share_price")),
                }
            )
            if len(out) >= limit:
                break
        return out

    def _news(self, symbol: str, as_of: datetime, days: int, limit: int) -> tuple[list, str]:
        """Headlines from the preferred vendor, falling back unless told not to."""
        if self._vendor(symbol) == "alpha_vantage":
            try:
                items = self._av_news(symbol, as_of, days, limit)
                if items:
                    return items, "Alpha Vantage"
                if self._strict_av():
                    return [], "Alpha Vantage"
            except AlphaVantageError:
                if self._strict_av():
                    raise
        return self._yf_news(symbol, as_of, limit), "Yahoo Finance"

    @staticmethod
    def _indicators(frame) -> dict:
        """Compute the indicator set the market analyst reasons over."""
        close = frame["Close"]
        high, low, volume = frame["High"], frame["Low"], frame["Volume"]

        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        macd_signal = macd.ewm(span=9, adjust=False).mean()

        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        # Wilder smoothing, the convention every charting package uses for RSI.
        avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        # Dividing by a zero average loss yields inf, and 100 - 100/(1+inf) is
        # exactly the RSI of 100 the convention calls for, so the division is
        # left alone. Only a window with no movement at all (0/0 -> NaN) needs
        # handling: that is a flat tape, which is neutral, not overbought.
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        rsi = rsi.mask((avg_gain == 0) & (avg_loss == 0), 50.0)

        prev_close = close.shift(1)
        true_range = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
        ).max(axis=1)
        atr = true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()

        sma20 = close.rolling(20).mean()
        std20 = close.rolling(20).std()

        last = close.iloc[-1]
        return {
            "close": last,
            "sma20": sma20.iloc[-1],
            "sma50": close.rolling(50).mean().iloc[-1],
            "sma200": close.rolling(200).mean().iloc[-1] if len(close) >= 200 else None,
            "rsi14": rsi.iloc[-1],
            "macd": macd.iloc[-1],
            "macd_signal": macd_signal.iloc[-1],
            "macd_hist": macd.iloc[-1] - macd_signal.iloc[-1],
            "atr14": atr.iloc[-1],
            "atr_pct": (atr.iloc[-1] / last) if last else None,
            "boll_upper": sma20.iloc[-1] + 2 * std20.iloc[-1],
            "boll_lower": sma20.iloc[-1] - 2 * std20.iloc[-1],
            # 252 sessions ~ one trading year; on a shorter history the caller
            # relabels the line rather than passing off 30 sessions as a year.
            "range_sessions": min(len(close), 252),
            "high_52w": high.tail(252).max(),
            "low_52w": low.tail(252).min(),
            "vol_avg20": volume.tail(20).mean(),
            "vol_avg60": volume.tail(60).mean(),
        }

    # ------------------------------------------------------------------
    # tools exposed to the model
    # ------------------------------------------------------------------

    async def get_instrument_identity(
        self,
        symbol: str,
        __event_emitter__: Callable[[dict], Any] | None = None,
    ) -> str:
        """
        Resolves what a ticker actually is: company name, sector, industry, exchange
        and quote currency. Call this FIRST, before any analysis, so the whole team
        anchors on the real instrument instead of guessing a company from a price chart.

        :param symbol: Ticker symbol, for example NVDA, AIR.PA, BTC or GC=F.
        """
        if _IMPORT_ERROR:
            return _IMPORT_ERROR
        resolved = _normalize_symbol(symbol)
        blocked = self._unservable(resolved)
        if blocked:
            return blocked
        await self._emit(__event_emitter__, f"Identification de {resolved}...")
        try:
            identity, source = await self._run(self._identity, resolved)
        except Exception as exc:
            return f"Impossible de resoudre {resolved}: {_err(exc)}"

        if not identity:
            return (
                f"Aucune identite trouvee pour '{symbol}' (resolu en '{resolved}'). "
                "Verifie le symbole avant de poursuivre l'analyse."
            )

        name = identity.get("name") or "n/a"
        lines = [
            f"# Identite — {resolved}",
            f"- Nom: {name}",
            f"- Type: {identity.get('quote_type') or 'n/a'}",
            f"- Secteur: {identity.get('sector') or 'n/a'}",
            f"- Industrie: {identity.get('industry') or 'n/a'}",
            f"- Place de cotation: {identity.get('exchange') or 'n/a'}",
            f"- Devise: {identity.get('currency') or 'n/a'}",
            f"- Pays: {identity.get('country') or 'n/a'}",
            f"- Capitalisation: {_fmt_big(identity.get('market_cap'))}",
            f"- Source: {source}",
        ]
        if resolved != (symbol or "").strip().upper():
            lines.append(f"- Note: '{symbol}' a ete normalise en '{resolved}'.")
        if source == "Yahoo Finance" and self._av_key() and not _av_can_serve(resolved):
            lines.append(
                f"- Note: la place '{_venue_suffix(resolved)}' suit la convention Yahoo ; "
                "Alpha Vantage emploie un suffixe different, donc actualites bornees et "
                "fondamentaux point-in-time ne sont pas disponibles sur cette ligne."
            )
        await self._emit(__event_emitter__, f"{name} identifie", done=True)
        return self._cap("\n".join(lines))

    async def get_price_history(
        self,
        symbol: str,
        as_of_date: str = "",
        lookback_days: int = 0,
        __user__: dict | None = None,
        __event_emitter__: Callable[[dict], Any] | None = None,
    ) -> str:
        """
        Returns the price action of an instrument: recent daily closes, period return,
        realised volatility, maximum drawdown and volume trend. Use it to describe what
        the tape has actually been doing before interpreting anything.

        :param symbol: Ticker symbol, for example NVDA, AIR.PA or BTC-USD.
        :param as_of_date: Analysis date in YYYY-MM-DD form. No data after this date is
            returned, which is what prevents look-ahead bias. Leave empty for today.
        :param lookback_days: Calendar days of history. Leave at 0 for the configured default.
        """
        if _IMPORT_ERROR:
            return _IMPORT_ERROR
        resolved = _normalize_symbol(symbol)
        as_of = _resolve_as_of(as_of_date)
        days = self._lookback(__user__, lookback_days)
        blocked = self._unservable(resolved)
        if blocked:
            return blocked
        await self._emit(
            __event_emitter__, f"Cours de {resolved} au {as_of:%Y-%m-%d}..."
        )
        try:
            frame, source = await self._run(self._prices, resolved, as_of, days)
        except Exception as exc:
            return f"Echec du telechargement des cours de {resolved}: {_err(exc)}"
        if frame is None:
            reason = source[6:] if source.startswith("stale:") else ""
            return _no_data_message(resolved, as_of, reason)

        close = frame["Close"]
        first, last = float(close.iloc[0]), float(close.iloc[-1])
        period_return = (last - first) / first if first else None
        daily = close.pct_change().dropna()
        volatility = float(daily.std() * (252**0.5)) if len(daily) > 5 else None
        drawdown = float(((close / close.cummax()) - 1).min())

        tail = frame.tail(10)
        rows = [
            f"| {idx:%Y-%m-%d} | {_fmt(row['Open'])} | {_fmt(row['High'])} | "
            f"{_fmt(row['Low'])} | {_fmt(row['Close'])} | {_fmt_big(row['Volume'])} |"
            for idx, row in tail.iterrows()
        ]

        out = [
            f"# Cours — {resolved} (arrete au {as_of:%Y-%m-%d}, source {source})",
            *_split_warning(source),
            f"Fenetre: {frame.index[0]:%Y-%m-%d} -> {frame.index[-1]:%Y-%m-%d} "
            f"({len(frame)} seances)",
            "",
            f"- Dernier cours: {_fmt(last)}",
            f"- Performance sur la periode: {_pct(period_return)}",
            f"- Volatilite annualisee: {_pct(volatility)}",
            f"- Drawdown maximal: {_pct(drawdown)}",
            f"- Volume moyen (20 seances): {_fmt_big(frame['Volume'].tail(20).mean())}",
            "",
            "## 10 dernieres seances",
            "| Date | Ouv. | Haut | Bas | Clot. | Volume |",
            "| --- | --- | --- | --- | --- | --- |",
            *rows,
        ]
        await self._emit(__event_emitter__, f"Cours de {resolved} recuperes", done=True)
        return self._cap("\n".join(out))

    async def get_technical_indicators(
        self,
        symbol: str,
        as_of_date: str = "",
        __user__: dict | None = None,
        __event_emitter__: Callable[[dict], Any] | None = None,
    ) -> str:
        """
        Computes the standard technical indicator panel: moving averages (20/50/200),
        RSI(14), MACD with its signal line, ATR(14), Bollinger bands, the 52-week range
        and the volume trend. All values are computed from data up to the analysis date only.

        :param symbol: Ticker symbol to analyse.
        :param as_of_date: Analysis date in YYYY-MM-DD form. Indicators use no data after
            this date. Leave empty for today.
        """
        if _IMPORT_ERROR:
            return _IMPORT_ERROR
        resolved = _normalize_symbol(symbol)
        as_of = _resolve_as_of(as_of_date)
        # 300 calendar days minimum so a 200-session SMA has something to chew on.
        days = max(self._lookback(__user__), 400)
        blocked = self._unservable(resolved)
        if blocked:
            return blocked
        await self._emit(__event_emitter__, f"Indicateurs techniques de {resolved}...")
        try:
            frame, source = await self._run(self._prices, resolved, as_of, days)
        except Exception as exc:
            return f"Echec du calcul des indicateurs de {resolved}: {_err(exc)}"
        if frame is None:
            reason = source[6:] if source.startswith("stale:") else ""
            return _no_data_message(resolved, as_of, reason)
        if len(frame) < 20:
            return (
                f"Historique insuffisant pour calculer des indicateurs sur {resolved} "
                f"au {as_of:%Y-%m-%d} : {len(frame)} seances, il en faut au moins 20. "
                "Ne pas estimer les indicateurs manquants."
            )

        ind = self._indicators(frame)
        close = ind["close"]
        _range_label = (
            "52 semaines"
            if ind["range_sessions"] >= 252
            else f"sur {ind['range_sessions']} seances (historique disponible)"
        )

        def _vs(reference) -> str:
            if reference is None or reference != reference or not reference:
                return "n/a"
            gap = (close - reference) / reference
            return f"{_fmt(reference)} ({'au-dessus' if gap >= 0 else 'en-dessous'}, {_pct(gap)})"

        def _rsi_label(value) -> str:
            # Explicit None/NaN test: a legitimate RSI of 0.0 is falsy, and an
            # `or` fallback would have relabelled the most oversold reading
            # there is as "neutre".
            if value is None or value != value:
                return "n/a"
            if value > 70:
                return "surachat"
            return "survente" if value < 30 else "neutre"

        out = [
            f"# Indicateurs techniques — {resolved} (arrete au {as_of:%Y-%m-%d}, source {source})",
            *_split_warning(source),
            f"Dernier cours: {_fmt(close)} | {len(frame)} seances utilisees",
            "",
            "## Tendance",
            f"- SMA 20: {_vs(ind['sma20'])}",
            f"- SMA 50: {_vs(ind['sma50'])}",
            f"- SMA 200: {_vs(ind['sma200'])}",
            "",
            "## Momentum",
            f"- RSI(14): {_fmt(ind['rsi14'])} ({_rsi_label(ind['rsi14'])})",
            f"- MACD: {_fmt(ind['macd'], 3)} | signal {_fmt(ind['macd_signal'], 3)} | "
            f"histogramme {_fmt(ind['macd_hist'], 3)}",
            "",
            "## Volatilite et bornes",
            f"- ATR(14): {_fmt(ind['atr14'])} soit {_pct(ind['atr_pct'])} du cours",
            f"- Bandes de Bollinger (20, 2): {_fmt(ind['boll_lower'])} / {_fmt(ind['boll_upper'])}",
            f"- Plus haut {_range_label}: {_fmt(ind['high_52w'])}",
            f"- Plus bas {_range_label}: {_fmt(ind['low_52w'])}",
            "",
            "## Volume",
            f"- Moyenne 20 seances: {_fmt_big(ind['vol_avg20'])}",
            f"- Moyenne 60 seances: {_fmt_big(ind['vol_avg60'])}",
        ]
        await self._emit(__event_emitter__, "Indicateurs calcules", done=True)
        return self._cap("\n".join(out))

    async def get_company_news(
        self,
        symbol: str,
        as_of_date: str = "",
        limit: int = 0,
        __event_emitter__: Callable[[dict], Any] | None = None,
    ) -> str:
        """
        Returns recent headlines about an instrument, each with its publisher, publication
        date and link, plus a sentiment label when the provider supplies one. Headlines
        published after the analysis date are excluded.

        :param symbol: Ticker symbol to search news for.
        :param as_of_date: Analysis date in YYYY-MM-DD form. Anything published after this
            date is discarded. Leave empty for today.
        :param limit: Maximum number of headlines. Leave at 0 for the configured default.
        """
        if _IMPORT_ERROR:
            return _IMPORT_ERROR
        resolved = _normalize_symbol(symbol)
        as_of = _resolve_as_of(as_of_date)
        # Bounded: the model is free to ask for 500 headlines, MAX_OUTPUT_CHARS
        # would silently swallow them, and the fetch would be wasted work.
        count = min(limit, 30) if limit and limit > 0 else int(self.valves.NEWS_LIMIT)
        window = max(1, int(self.valves.NEWS_LOOKBACK_DAYS))
        blocked = self._unservable(resolved)
        if blocked:
            return blocked
        await self._emit(__event_emitter__, f"Actualites de {resolved}...")
        try:
            items, source = await self._run(self._news, resolved, as_of, window, count)
        except Exception as exc:
            return f"Echec de la recuperation des actualites de {resolved}: {_err(exc)}"
        if not items:
            return (
                f"Aucune actualite anterieure au {as_of:%Y-%m-%d} pour {resolved} "
                # Only Alpha Vantage bounds the window server-side; claiming a
                # window on the yfinance path would describe a filter that never ran.
                + (
                    f"(source {source}, fenetre de {window} jours). "
                    if source == "Alpha Vantage"
                    else f"(source {source}, sans fenetre glissante). "
                )
                + "Ne pas en deduire une absence de catalyseur: le fil peut "
                "simplement etre vide."
            )

        out = [
            f"# Actualites — {resolved} (publiees jusqu'au {as_of:%Y-%m-%d}, source {source})",
            "",
        ]
        for item in items:
            out.append(f"### {item['title']}")
            meta = f"*{item['publisher']} — {item['date']}"
            if item.get("sentiment"):
                meta += f" — sentiment: {item['sentiment']}"
            out.append(meta + "*")
            if item["summary"]:
                out.append(item["summary"])
            if item["url"]:
                out.append(f"<{item['url']}>")
            out.append("")
        await self._emit(__event_emitter__, f"{len(items)} actualites recuperees", done=True)
        return self._cap("\n".join(out))

    async def get_global_news(
        self,
        as_of_date: str = "",
        limit: int = 0,
        __event_emitter__: Callable[[dict], Any] | None = None,
    ) -> str:
        """
        Returns market-wide headlines — financial markets, macroeconomics, monetary
        policy — rather than news about one company. Read it alongside the company feed:
        what is happening TO a stock and what is happening IN the market are different
        inputs, and a thesis built on the first alone misses the regime it trades into.
        Requires an Alpha Vantage key.

        :param as_of_date: Analysis date in YYYY-MM-DD form. Nothing published after this
            date is returned. Leave empty for today.
        :param limit: Maximum number of headlines. Leave at 0 for the configured default.
        """
        if _IMPORT_ERROR:
            return _IMPORT_ERROR
        as_of = _resolve_as_of(as_of_date)
        count = min(limit, 30) if limit and limit > 0 else int(self.valves.NEWS_LIMIT)
        window = max(1, int(self.valves.NEWS_LOOKBACK_DAYS))
        blocked = self._av_only_blocked()
        if blocked:
            return (
                f"Actualites de marche indisponibles : {blocked}. Cet outil s'appuie sur "
                "les fils thematiques d'Alpha Vantage, sans equivalent yfinance. Se "
                "rabattre sur get_market_context pour le regime de marche, sans inventer "
                "de titres."
            )
        await self._emit(__event_emitter__, "Actualites de marche...")
        try:
            items = await self._run(self._av_topic_news, as_of, window, count)
        except Exception as exc:
            return f"Echec de la recuperation des actualites de marche: {_err(exc)}"
        if not items:
            return (
                f"Aucune actualite de marche avant le {as_of:%Y-%m-%d} sur la fenetre de "
                f"{window} jours. Ne pas en deduire un marche sans evenement."
            )

        out = [f"# Actualites de marche — jusqu'au {as_of:%Y-%m-%d} (source Alpha Vantage)", ""]
        for item in items:
            out.append(f"### {item['title']}")
            meta = f"*{item['publisher']} — {item['date']}"
            if item.get("sentiment"):
                meta += f" — sentiment: {item['sentiment']}"
            out.append(meta + "*")
            if item["summary"]:
                out.append(item["summary"])
            if item["url"]:
                out.append(f"<{item['url']}>")
            out.append("")
        await self._emit(__event_emitter__, f"{len(items)} actualites de marche", done=True)
        return self._cap("\n".join(out))

    async def get_insider_transactions(
        self,
        symbol: str,
        as_of_date: str = "",
        limit: int = 0,
        __event_emitter__: Callable[[dict], Any] | None = None,
    ) -> str:
        """
        Returns recent share dealings by a company's own officers and directors — who
        traded, in what role, buying or selling, how many shares and at what price.
        Insider buying and selling is a signal ordinary price data does not carry.
        US equities only, and requires an Alpha Vantage key.

        :param symbol: Ticker symbol of the company.
        :param as_of_date: Analysis date in YYYY-MM-DD form. Filings dated after this day
            are excluded. Leave empty for today.
        :param limit: Maximum number of transactions. Leave at 0 for the default of 15.
        """
        if _IMPORT_ERROR:
            return _IMPORT_ERROR
        resolved = _normalize_symbol(symbol)
        as_of = _resolve_as_of(as_of_date)
        count = min(limit, 50) if limit and limit > 0 else 15
        blocked = self._av_only_blocked(resolved)
        if blocked:
            return (
                f"Transactions d'inities indisponibles pour {resolved} : {blocked}. "
                "Cet outil s'appuie sur Alpha Vantage, sans equivalent yfinance. "
                "Ne pas supposer une absence de transactions."
            )
        await self._emit(__event_emitter__, f"Transactions d'inities de {resolved}...")
        try:
            rows = await self._run(self._av_insiders, resolved, as_of, count)
        except Exception as exc:
            return f"Echec de la recuperation des transactions d'inities: {_err(exc)}"
        if not rows:
            return (
                f"Aucune transaction d'initie declaree pour {resolved} avant le "
                f"{as_of:%Y-%m-%d}. Absence de declaration, pas preuve d'absence d'operation."
            )

        achats = sum(1 for r in rows if r["sense"] == "achat")
        out = [
            f"# Transactions d'inities — {resolved} (jusqu'au {as_of:%Y-%m-%d})",
            f"{len(rows)} operations retenues : {achats} achats, {len(rows) - achats} ventes.",
            "",
            "| Date | Dirigeant | Fonction | Sens | Titres | Prix |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        out += [
            f"| {r['date']} | {r['who']} | {r['role']} | {r['sense']} | "
            f"{_fmt_big(r['shares'])} | {_fmt(r['price'])} |"
            for r in rows
        ]
        await self._emit(__event_emitter__, f"{len(rows)} transactions d'inities", done=True)
        return self._cap("\n".join(out))

    async def get_fundamentals(
        self,
        symbol: str,
        as_of_date: str = "",
        __event_emitter__: Callable[[dict], Any] | None = None,
    ) -> str:
        """
        Returns the fundamental profile of a company: valuation multiples, profitability,
        growth, balance-sheet strength, dividend and the analyst consensus target.

        With an Alpha Vantage key the financial statements are point-in-time: the latest
        fiscal period ending on or before the analysis date. Valuation multiples are always
        the provider's current snapshot and carry an explicit warning on a historical date.

        :param symbol: Ticker symbol of the company.
        :param as_of_date: Analysis date in YYYY-MM-DD form. Selects which fiscal period the
            statements come from, and triggers the staleness warning on current multiples.
        """
        if _IMPORT_ERROR:
            return _IMPORT_ERROR
        resolved = _normalize_symbol(symbol)
        as_of = _resolve_as_of(as_of_date)
        blocked = self._unservable(resolved)
        if blocked:
            return blocked
        await self._emit(__event_emitter__, f"Fondamentaux de {resolved}...")
        try:
            # budget=3: the Alpha Vantage path makes four calls (overview plus
            # three statements) behind this single timeout.
            data = await self._run(self._fundamentals, resolved, as_of, budget=3)
        except Exception as exc:
            return f"Echec de la recuperation des fondamentaux de {resolved}: {_err(exc)}"
        if not data:
            return f"Aucun fondamental disponible pour {resolved}."

        # The multiples are a live snapshot whatever the vendor, so a historical
        # analysis is told so outright rather than being left to assume.
        stale_days = (datetime.now() - as_of).days
        warning = []
        if stale_days > 7:
            warning = [
                "> **Avertissement** — les multiples de valorisation ci-dessous sont "
                f"l'instantane actuel du fournisseur, pas une reconstitution au "
                f"{as_of:%Y-%m-%d} ({stale_days} jours plus tot). Les traiter comme un "
                "ordre de grandeur, ne pas les presenter comme connus a cette date.",
                "",
            ]

        quote_type = (data.get("quote_type") or "").upper()
        if quote_type in {"CRYPTOCURRENCY", "FUTURE", "CURRENCY", "INDEX"}:
            return self._cap(
                f"# Fondamentaux — {resolved}\n\n"
                + ("\n".join(warning) + "\n" if warning else "")
                + f"Instrument de type {quote_type or 'non actions'}: il n'a pas d'etats "
                "financiers. L'analyse doit reposer sur le prix, le flux d'actualites et "
                "le contexte macro. Ne pas inventer de ratios."
            )

        period = data.get("period")
        statement_header = (
            f"## Etats financiers — {data.get('period_kind') or 'periode'} clos le {period} "
            "(anterieur a la date d'analyse)"
            if period
            else "## Bilan et flux (instantane courant, non date)"
        )
        debt_ratio = data.get("debt_to_equity_pct")

        out = [
            f"# Fondamentaux — {resolved} ({data.get('name') or 'n/a'})",
            f"*Source: {data.get('source')}*",
            "",
            *warning,
            "## Valorisation (instantane courant)",
            f"- Capitalisation: {_fmt_big(data.get('market_cap'))}",
            f"- PER (12 mois glissants): {_fmt(data.get('trailing_pe'))}",
            f"- PER prospectif: {_fmt(data.get('forward_pe'))}",
            f"- PEG: {_fmt(data.get('peg'))}",
            f"- Prix / ventes: {_fmt(data.get('ps'))}",
            f"- Prix / actif net: {_fmt(data.get('pb'))}",
            f"- VE / EBITDA: {_fmt(data.get('ev_ebitda'))}",
            "",
            "## Rentabilite",
            f"- Marge brute: {_pct(data.get('gross_margin'))}",
            f"- Marge operationnelle: {_pct(data.get('operating_margin'))}",
            f"- Marge nette: {_pct(data.get('profit_margin'))}",
            f"- ROE: {_pct(data.get('roe'))}",
            "",
            "## Croissance",
            f"- Croissance du CA: {_pct(data.get('revenue_growth'))}",
            f"- Croissance des benefices: {_pct(data.get('earnings_growth'))}",
            "",
            statement_header,
            f"- Chiffre d'affaires: {_fmt_big(data.get('revenue'))}",
        ]
        if data.get("operating_income") is not None:
            out.append(f"- Resultat operationnel: {_fmt_big(data.get('operating_income'))}")
        if data.get("net_income") is not None:
            out.append(f"- Resultat net: {_fmt_big(data.get('net_income'))}")
        out += [
            f"- Tresorerie: {_fmt_big(data.get('cash'))}",
            f"- Dette totale: {_fmt_big(data.get('debt'))}",
            "- Dette / fonds propres: "
            + (
                f"{_fmt(debt_ratio)} % (soit {_fmt(debt_ratio / 100)}x)"
                if debt_ratio is not None
                else "n/a"
            ),
            f"- Free cash flow: {_fmt_big(data.get('fcf'))}",
            f"- Ratio de liquidite generale: {_fmt(data.get('current_ratio'))}",
            "",
            "## Consensus et dividende",
        ]
        if data.get("recommendation"):
            out.append(
                f"- Recommandation: {data['recommendation']} "
                f"({data.get('analysts') or 'n/a'} analystes)"
            )
        target = f"- Objectif moyen: {_fmt(data.get('target_mean'))}"
        if data.get("target_low") is not None or data.get("target_high") is not None:
            target += f" (bas {_fmt(data.get('target_low'))} / haut {_fmt(data.get('target_high'))})"
        out += [
            target,
            f"- Rendement du dividende: {_pct(data.get('dividend_yield'))}",
            f"- Beta: {_fmt(data.get('beta'))}",
        ]
        await self._emit(__event_emitter__, "Fondamentaux recuperes", done=True)
        return self._cap("\n".join(out))


    async def get_market_context(
        self,
        symbol: str = "",
        as_of_date: str = "",
        __event_emitter__: Callable[[dict], Any] | None = None,
    ) -> str:
        """
        Returns the macro backdrop on the analysis date: the equity indices of the
        instrument's own region, its currency pair, the volatility index, oil and gold,
        each with its recent move. Use it to judge whether the market regime supports or
        fights the trade.

        :param symbol: Ticker being analysed. Selects the regional backdrop, so a
            Paris-listed share is read against the CAC 40, the Euro Stoxx and EUR/USD
            rather than the S&P and the dollar index. Leave empty for the US backdrop.
        :param as_of_date: Analysis date in YYYY-MM-DD form. Leave empty for today.
        """
        if _IMPORT_ERROR:
            return _IMPORT_ERROR
        as_of = _resolve_as_of(as_of_date)
        profile = _macro_profile(_normalize_symbol(symbol)) if symbol else "us"
        macro_symbols = _MACRO_PROFILES[profile] + _MACRO_GLOBAL
        await self._emit(__event_emitter__, f"Contexte de marche ({profile})...")

        def _snapshot() -> list:
            rows = []
            for ticker, label in macro_symbols:
                try:
                    # Yahoo on purpose: one Alpha Vantage call per index would
                    # burn a third of the free daily quota on a backdrop table.
                    frame = self._yf_history(ticker, as_of, 40)
                    if frame is None or len(frame) < 2:
                        rows.append((label, ticker, None, None, None))
                        continue
                    close = frame["Close"]
                    last = float(close.iloc[-1])
                    prev = float(close.iloc[-2])
                    month_ago = float(close.iloc[0])
                    rows.append(
                        (
                            label,
                            ticker,
                            last,
                            (last - prev) / prev if prev else None,
                            (last - month_ago) / month_ago if month_ago else None,
                        )
                    )
                except Exception:
                    rows.append((label, ticker, None, None, None))
            return rows

        try:
            rows = await self._run(_snapshot, budget=3)
        except Exception as exc:
            return f"Echec de la recuperation du contexte de marche: {_err(exc)}"

        out = [
            f"# Contexte de marche {profile} — {as_of:%Y-%m-%d} (source Yahoo Finance)",
            "",
            "| Indicateur | Dernier | Variation 1j | Variation ~1 mois |",
            "| --- | --- | --- | --- |",
        ]
        for label, ticker, last, day_change, month_change in rows:
            if last is None:
                out.append(f"| {label} ({ticker}) | n/a | n/a | n/a |")
            else:
                out.append(
                    f"| {label} ({ticker}) | {_fmt(last)} | {_pct(day_change)} | {_pct(month_change)} |"
                )
        await self._emit(__event_emitter__, "Contexte de marche recupere", done=True)
        return self._cap("\n".join(out))
