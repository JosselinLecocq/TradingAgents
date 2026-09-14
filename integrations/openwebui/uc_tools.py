"""
title: UC Fund Analysis Data
author: TradingAgents
version: 0.2.0
required_open_webui_version: 0.7.0
requirements: pandas, pypdf, pymupdf
description: Data for analysing a unit-linked support (unite de compte) from its ISIN alone — PRIIPs key information document (risk class, costs, scenarios) fetched from the AMF GECO database for French funds or read from a KID attached to the chat, official NAV history, volatility and drawdown, plus contract fees, retrocessions and peer ranking read from the Swiss Life unit-linked lists kept in an Open WebUI knowledge base. Companion to the uc-analysis skill.
licence: MIT
"""

# Design notes for maintainers
# ---------------------------
# * Any ISIN, three sources, each doing what the others cannot.
#   - The PRIIPs key information document (KID / DIC) is the regulatory truth
#     for risk class, costs and scenarios. French funds: the AMF's public GECO
#     database serves it, with the official NAV history, legal nature, status
#     and target subscribers. Luxembourg, Irish and other funds are not in GECO:
#     the adviser attaches the KID PDF to the chat and the same parser reads it.
#   - The insurer's unit-linked lists add what no KID carries: the contract's
#     own fees, the share of fees retroceded to the distributor, net-of-all-fees
#     performance, and the ranking within the category. They are the PDFs the
#     swisslife-veille-documentaire tool keeps in the knowledge base "Listes des
#     UC des contrats SwissLife" (one per contract: general annexes, Vie
#     Generation, PER...). They are read through Open WebUI's own API with the
#     caller's session, so the knowledge base's sharing rules apply, parsed once
#     per edition and cached by content hash.
#   - A NAV series is what volatility and drawdown need. GECO's official NAVs
#     come first. Elsewhere no free API maps an ISIN to a NAV series reliably, so
#     Yahoo candidates are accepted only if their calendar-year returns reproduce
#     the insurer's figures; without that reference they are labelled NOT
#     validated.
# * KID figures are read from text, and text extraction of PDF tables is noisy.
#   Every figure is therefore checked by an identity the regulation guarantees:
#   the cost rows must add up to the one-year cost impact, the scenarios must be
#   ordered stress <= unfavourable <= moderate <= favourable. What fails a check
#   is withheld or flagged, never published as read.
# * Performance is quoted from the insurer document or from official NAVs, never
#   from a market series: a sibling share class is a sound proxy for risk (same
#   portfolio) but not for returns (fees, distribution, hedging differ).
# * Yahoo is called over plain HTTP rather than through yfinance, whose session
#   handshake trips the per-IP throttle after a single request. Yahoo's fund
#   fees and inception dates are wrong often enough to be ignored entirely.
# * Standalone by necessity: Open WebUI tools cannot import one another.

import asyncio
import contextlib
import io
import json
import math
import os
import re
import statistics
import tempfile
import time
import unicodedata
from collections.abc import Callable
from typing import Any

import requests
from pydantic import BaseModel, Field

try:
    import pandas as pd

    _IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - surfaced to the model at call time
    _IMPORT_ERROR = f"pandas unavailable: {exc}"

try:
    from pypdf import PdfReader
except Exception:  # pragma: no cover - attached KIDs then rely on Open WebUI's own extraction
    PdfReader = None


_YAHOO_SEARCH = "https://query2.finance.yahoo.com/v1/finance/search"
_YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_OPENFIGI = "https://api.openfigi.com/v3/mapping"
_GECO = "https://geco.amf-france.org/back-office"
_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}
_TRADING_DAYS = 252

# Calendar-year agreement between a candidate series and the insurer document.
# Calibrated on real funds: exact share classes matched within 0.05pt, siblings
# with other fees within about 0.6pt, distributing / hedged / other-currency
# classes from 2pt upward.
_EXACT_MATCH_PT = 0.35
_SIBLING_MATCH_PT = 1.0

# PRIIPs requires a KID to be reviewed at least every twelve months; past this
# age the document in hand is probably not the one in force.
_KID_MAX_AGE_DAYS = 425

# PRIIPs market-risk classes by annualised volatility (VaR-equivalent), used as
# an indicative coherence check against the declared SRI.
_MRM_BOUNDS = [(0.5, 1), (5.0, 2), (12.0, 3), (20.0, 4), (30.0, 5), (80.0, 6)]

_UNLISTED = ("immobilier non cote", "non cote / capital-investissement")


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def _fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
    number = _num(value)
    return "n/c" if number is None else f"{number:,.{digits}f}{suffix}".replace(",", " ")


def _pts(value: Any) -> str:
    """A figure already expressed in percent (12.24 -> '12.24%')."""
    return _fmt(value, 2, "%")


def _days_since(date_text: str | None) -> float | None:
    with contextlib.suppress(TypeError, ValueError):
        return (time.time() - time.mktime(time.strptime(str(date_text)[:10], "%Y-%m-%d"))) / 86_400
    return None


def isin_is_valid(isin: str) -> bool:
    """ISO 6166 check digit. Catches a mistyped ISIN before it matches a stranger."""
    code = (isin or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", code):
        return False
    digits = "".join(str(int(ch, 36)) for ch in code[:-1])
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10 == int(code[-1])


def _normalise_name(name: str) -> str:
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


_CLASS_TOKEN = re.compile(
    r"^(?:[A-Z]{1,2}\d?|Acc|Dis|Dist|Inc|Cap|C|D|H|EURH?|USDH?|GBPH?|CHFH?|JPYH?|HAcc|HDis|"
    r"[A-Z0-9]{1,3}(?:-[A-Za-z0-9]{1,5})+|€|\$|Hdg|Hedged)$"
)


def search_queries(name: str) -> list[str]:
    """The full name, then the fund's core name without its share-class suffix.

    Search engines choke on class strings: "Fidelity Global Technology A-Dis-EUR"
    returns nothing, stripped naively it became "A--EUR" and still nothing, while
    "Fidelity Global Technology" lists every share class of the fund — which the
    calendar-year check then sorts out.
    """
    tokens = (name or "").split()
    core = list(tokens)
    while len(core) > 2 and _CLASS_TOKEN.match(core[-1]):
        core.pop()
    queries = [name] if name else []
    if core and " ".join(core) != name:
        queries.append(" ".join(core))
    # Accented names ("Carmignac Sécurité") can miss entirely; the ASCII
    # spelling is how most quote databases store them.
    for query in list(queries):
        folded = unicodedata.normalize("NFKD", query).encode("ascii", "ignore").decode()
        if folded != query:
            queries.append(folded)
    return queries


def mrm_class(volatility_pct: float) -> int:
    for bound, cls in _MRM_BOUNDS:
        if volatility_pct < bound:
            return cls
    return 7


def vehicle_family(record: dict, nature: str = "") -> str:
    """Vehicle family from the insurer's legal form or the AMF legal nature."""
    form = (record.get("forme_juridique") or "").upper()
    name = (record.get("nom") or "").upper()
    nature = _normalise_name(nature)
    if record.get("type") == "action":
        return "action"
    if any(re.fullmatch(rf"{f}\b.*", form) for f in ("SCPI", "SCI", "OPCI")) or re.search(
        r"\b(scpi|opci|oppci|sci|immobili)", nature
    ):
        return "immobilier non cote"
    if any(re.fullmatch(rf"{f}\b.*", form) for f in ("FCPR", "FPS", "SAS", "SC")) or re.search(
        r"\b(fcpr|fpci|fcpi|fip|slp|fps|capital investissement|a risques)\b", nature
    ):
        return "non cote / capital-investissement"
    if "ETF" in name or "UCITS ETF" in name:
        return "ETF"
    return "OPCVM"


# ----------------------------------------------------------------------
# PRIIPs KID parser
# ----------------------------------------------------------------------

_ISIN_ANY = re.compile(r"(?=([A-Z]{2}[A-Z0-9]{9}\d))")
_PCT = r"(-?\d{1,3}(?:[.,]\d{1,3})?)%"

# PRIIPs Annex III narrative for each summary risk class. Some KIDs show the
# class only as a highlighted box on the 1-7 scale, which no text extraction
# carries; the mandatory narrative that accompanies it still names the level.
_LEVEL_PHRASES = [
    ("entrefaibleetmoyen", 3), ("entremoyenetelev", 5), ("tresfaible", 1), ("treselev", 7),
    ("niveaufaible", 2), ("niveaumoyen", 4), ("niveauelev", 6),
]
_CLASS_WORDS = [
    ("laplusbasse", 1), ("entrebasseetmoyenne", 3), ("entremoyenneetelevee", 5), ("laplusele", 7),
    ("classederisquebasse", 2), ("classederisquemoyenne", 4), ("classederisqueelevee", 6),
]
_SCENARIOS = ["tensions", "defavorable", "intermediaire", "favorable"]


def compact_text(text: str) -> str:
    """Lowercase, no accents, no apostrophes, no whitespace.

    Text extractors disagree on spacing — one glues words ('Autrementdit'), another
    splits them ('vo tre') — so patterns are matched with every space removed,
    which makes both failure modes disappear.
    """
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = text.replace("’", "").replace("'", "").replace("`", "")
    text = re.sub(r"\s+", "", text)
    # Line-break hyphenation ("transac-tion") would hide a label; a hyphen
    # between two letters never carries a number, so it can go.
    return re.sub(r"(?<=[a-z])-(?=[a-z])", "", text)


def _dec(value: str | None) -> float | None:
    return float(value.replace(",", ".")) if value is not None else None


_UNIT = r"(ans?|annees?|mois|semaines?|jours?)"


def _years(value: str, unit: str) -> float:
    per_year = {"m": 12, "s": 52, "j": 365}.get(unit[0], 1)
    return _dec(value) / per_year


def duration_text(years: float | None) -> str:
    if not years:
        return "la periode recommandee"
    if years >= 1:
        return f"{years:g} an(s)"
    if years >= 1 / 12 - 1e-9:
        return f"{years * 12:.0f} mois"
    return f"{max(1, round(years * 52))} semaine(s)"


def _scenario_returns(segment: str, horizons: list[float], example: float) -> list[float]:
    """Average annual returns of one scenario row, in column order.

    Some extractions glue the amount to the return ("eur102402.4%" for 10 240 EUR
    and 2.4%). A digit run too long to be a percentage is split where the amount
    it leaves reproduces the return over that column's horizon.
    """
    values: list[float] = []
    for match in re.finditer(r"(-?)(\d+)((?:[.,]\d{1,3})?)%", segment):
        sign, digits, fraction = match.groups()
        if len(digits) <= 3:
            values.append(_dec(sign + digits + fraction))
            continue
        column = len(values)
        years = horizons[column] if column < len(horizons) else (1.0 if column == 0 else None)
        split = None
        for size in (1, 2, 3):
            amount, rate = float(digits[:-size]), _dec(sign + digits[-size:] + fraction)
            if years and abs(amount - example * (1 + rate / 100) ** years) <= 0.02 * amount:
                split = rate
                break
        if split is None:
            break
        values.append(split)
    return values


def parse_kid(raw: str) -> dict:
    """Extract the regulated figures of a French-language PRIIPs KID."""
    c = compact_text(raw)
    out: dict = {
        # The title is mandatory; a prospectus or a factsheet also speaks of a
        # risk indicator but is not the regulated document.
        "is_kid": bool(re.search(r"documentdinformationscles|keyinformationdocument|classederisque[1-7]sur7", c)),
        # Extraction glues codes to neighbouring words ("FR0010738120Document"),
        # so candidates are taken without word boundaries, case-sensitively (an
        # ISIN is printed in capitals, prose is not) and kept only when their
        # check digit is right — one random string in ten passes that test alone.
        "isins": sorted({code for code in _ISIN_ANY.findall(re.sub(r"[\s-]+", "", raw or ""))
                         if isin_is_valid(code)}),
    }
    # Those candidates can still include capitalised prose ("FLORNOY FERRI 8789"),
    # harmless when testing for a known ISIN but not fit to be quoted back. Codes
    # shown to the reader are those standing alone or right after an "ISIN" label.
    spaced = re.sub(r"\s+", " ", raw or "")
    shown = set(re.findall(r"(?<![A-Za-z0-9])([A-Z]{2}[A-Z0-9]{9}\d)(?![A-Za-z0-9])", spaced))
    shown |= set(re.findall(r"ISIN\W{0,5}([A-Z]{2}[A-Z0-9]{9}\d)", spaced, flags=re.IGNORECASE))
    out["isins_lus"] = sorted(code for code in shown if isin_is_valid(code))

    # The vehicle as the KID names it: for a fund absent from GECO and from the
    # insurer list, this is the only source telling a SCPI from a UCITS.
    # Only the title and the "Type" line count, and the first vehicle named
    # wins: a UCITS may well say it invests in SCPI without being one.
    kind = re.search(r"enquoiconsisteceproduit\??(.{0,160})", c)
    head = c[:300] + re.split(r"objectif", kind.group(1))[0] if kind else c[:300]
    hits = []
    for pattern, label in (
        (r"opcvm|sicav|fondscommundeplacement(?!arisques)|fcp(?![ier])", None),
        (r"societecivilede(?:placement)?immobili|scpi", "SCPI"),
        (r"placementcollectifimmobilier|opci", "OPCI"),
        (r"societecivileimmobili", "SCI"),
        (r"fondscommundeplacementarisques|fcpr|fpci|fcpi|fondsdinvestissementdeproximite", "FCPR"),
    ):
        found = re.search(pattern, head)
        if found:
            hits.append((found.start(), label))
    first_hit = min(hits, key=lambda hit: hit[0]) if hits else (0, None)
    if first_hit[1]:
        out["vehicule"] = first_hit[1]

    m = re.search(r"classederisque([1-7])(?:sur7)?(?![\d,.])", c) or re.search(r"riskclass([1-7])outof7", c)
    if m:
        out["sri"], out["sri_source"] = int(m.group(1)), "chiffre"
    else:
        for phrase, cls in _CLASS_WORDS + _LEVEL_PHRASES:
            if phrase in c:
                out["sri"], out["sri_source"] = cls, "qualificatif reglementaire"
                break

    m = re.search(
        r"(?:periode|duree)(?:de)?(?:detention|dinvestissement|placement)recommandee"
        r"(?:\(rhp\))?[:,]?(?:est)?(?:de|soit)?\(?(\d+(?:[.,]\d+)?)" + _UNIT, c
    ) or re.search(r"sivoussortezapres(\d+(?:[.,]\d+)?)" + _UNIT + r"\(periodededetentionrecommandee\)", c)
    if m:
        out["duree_detention_ans"] = _years(m.group(1), m.group(2))
    # The exit horizons of the scenario and cost tables. Usually 1 year and the
    # holding period, but long-dated funds show e.g. 5 and 10 years: the columns
    # must be labelled from the document, not assumed.
    # Tables run from the shortest exit to the longest; the prose before them
    # may name the holding period first, so the order is the tables', not the text's.
    # A lookahead: in "apres1ansivoussortez" the "s" of "si" must stay available.
    exits = {_years(value, unit)
             for value, unit in re.findall(r"(?=sivoussortezapres(\d+(?:[.,]\d+)?)" + _UNIT + ")", c)}
    # The holding period is always the last column, even when the text only
    # labels it "at the end of the recommended holding period".
    rhp = out.get("duree_detention_ans")
    if rhp:
        exits.add(rhp)
        # PRIIPs puts a one-year column before a longer holding period. When the
        # shorter exit cannot be read (ligatures turn "sortez" into "souez"),
        # that is the column it is.
        if rhp > 1 and min(exits) >= rhp:
            exits.add(1.0)
    horizons: list[float] = sorted({min(exits), max(exits)}) if exits else []
    out["horizons_ans"] = horizons

    m = re.search(r"exempledinvestissement:?(\d{1,3}(?:\d{3})*)(?:€|eur)", c)
    example = float(m.group(1)) if m else 10_000.0

    def one_off(label: str, next_rows: str) -> float | None:
        seg = re.search(label + r"(.{0,260})", c)
        if not seg:
            return None
        # The row stops where the next cost row starts, so a neighbour's
        # percentage is never borrowed by a row that states no figure. Only the
        # following rows' labels cut it: the row's own label is often repeated
        # inside its sentence ("nous ne facturons pas de cout d'entree").
        body = re.split(next_rows, seg.group(1))[0]
        pct = re.match(r".{0,160}?" + _PCT, body)
        zero = re.search(r"nefactur|aucun|pasdecout|pasdefrais|neprelev", body[:160])
        # "We charge none, but the person selling you the product may charge up
        # to 3%": the investor's maximum is the 3%, and the cost impact counts it.
        if pct and (not zero or zero.start() > pct.start(1) or "mais" in body[zero.start():pct.start(1)]):
            return _dec(pct.group(1))
        if zero:
            return 0.0
        amount = re.match(r".{0,40}?(?:jusqua)?(\d{1,6})(?:€|eur)", body)
        return round(float(amount.group(1)) / example * 100, 2) if amount else None

    out["couts_entree"] = one_off(r"couts?dentree", r"couts?desortie|coutsrecurrents|fraisdegestionetautres")
    out["couts_sortie"] = one_off(r"couts?desortie", r"couts?dentree|coutsrecurrents|fraisdegestionetautres")
    def recurring(label: str) -> float | None:
        m = re.search(label + r"\**(?:\(\*+\))?:?" + _PCT, c)
        if m:
            return _dec(m.group(1))
        # Some tables give the yearly amount on the example investment only.
        m = re.search(label + r"\**(?:\(\*+\))?:?(\d{1,5})(?:€|eur)", c)
        return round(float(m.group(1)) / example * 100, 2) if m else None

    out["frais_gestion_exploitation"] = recurring(r"fraisdegestionetautres[a-z]{0,50}?")
    out["couts_transaction"] = recurring(r"couts?detransaction(?:deportefeuille)?")

    seg = re.search(r"commissions?liees?auxresultats(?:etcommissiondinteressement)?(.{0,420})", c)
    if seg:
        body = seg.group(1)
        # Two different numbers live here: the contractual rate ("15% of the
        # outperformance") and the annual cost as a share of the investment
        # ("0.12% of the value of your investment"). Only the second is a cost;
        # reading the first as one once put 15 points into the cost total.
        annual = re.search(_PCT + r"(?:paran)?(?:de|du)(?:la)?(?:valeur|montant)(?:de)?(?:votre|linvestissement)", body) \
            or re.match(r"\**:?" + _PCT + r"(?=au\d{1,2}/\d{2}/\d{4}|paran|enmoyenne)", body)
        rate = re.search(_PCT + r"(?:ttc|ht)?(?:de|du)?(?:la)?(?:sur)?performance", body)
        if re.match(r".{0,60}?(aucune|pasde|nexiste|neprelev)", body):
            out["commission_performance"] = 0.0
        elif annual:
            out["commission_performance"] = _dec(annual.group(1))
        if rate:
            out["commission_performance_taux"] = _dec(rate.group(1))

    # "Incidence des couts annuels": one figure per exit horizon (1 year, then
    # the recommended holding period); a product held less than a year has one.
    # Labels vary ("incidence des couts", "(RIY)", "y compris le maximum des
    # droits d'entree soit 4%"), so the label's own percentage is dropped first.
    for m in re.finditer(r"incidences?descouts(?:annuels)?", c):
        tail = re.sub(r"^[^%\d]{0,80}?soit\d{1,2}(?:[.,]\d+)?%", "", c[m.end():m.end() + 160])
        # A footnote call "(1)" right after the label would pass for a figure.
        tail = re.sub(r"^\**\((?:\d|\*+)\)", "", tail)
        values = re.match(r"[^%\d]{0,60}?" + _PCT + r"(?:[^%\d]{0,40}?" + _PCT + ")?", tail)
        if not values:
            continue
        first, second = _dec(values.group(1)), _dec(values.group(2))
        if second is not None:
            out["incidence_couts_1an"], out["incidence_couts_periode"] = first, second
        elif len(horizons) == 1:
            # A single exit column: the holding period itself, a year or less.
            out["incidence_couts_periode"] = first
            if horizons[0] == 1:
                out["incidence_couts_1an"] = first
        else:
            continue
        break

    rows = list(re.finditer(
        r"(?:scenario(?:de)?)?(tensions?|(?<!de)favorable|defavorable|intermediaire)"
        r"(?=[\d*()]{0,4}cequevouspourriez(?:obtenir|recuperer))", c))
    scenarios: dict[str, tuple] = {}
    for i, row in enumerate(rows):
        end = rows[i + 1].start() if i + 1 < len(rows) else row.end() + 400
        yearly = re.split(r"rendement(?:annuel)?(?:moyen|enpourcentage)(?:chaqueannee|paran)?",
                          c[row.end():end], maxsplit=1)
        label = "tensions" if row.group(1) == "tension" else row.group(1)
        if len(yearly) == 2 and label not in scenarios:
            values = _scenario_returns(yearly[1], horizons, example)
            # A product held a year or less has a single exit column.
            if len(values) >= 2 and len(horizons) != 1:
                scenarios[label] = (values[0], values[1])
            elif values and len(horizons) == 1:
                scenarios[label] = (values[0], None)
    # PRIIPs orders the scenarios by construction, at both horizons. A table read
    # out of order by the text extractor fails this test and is withheld rather
    # than published wrong.
    if len(scenarios) == 4 and all(
        scenarios[_SCENARIOS[i]][h] <= scenarios[_SCENARIOS[i + 1]][h]
        for i in range(3) for h in (0, 1) if scenarios[_SCENARIOS[i]][h] is not None
    ):
        out["scenarios"] = scenarios
    else:
        out["scenarios"] = {}
        out["scenarios_illisibles"] = bool(scenarios)

    parts = [out.get(k) for k in ("couts_entree", "frais_gestion_exploitation", "couts_transaction")]
    # The cost rows add up to the first-horizon impact only when that horizon is
    # one year; over five years the entry cost is spread and the sum means nothing.
    one_year = not horizons or horizons[0] == 1
    if one_year and all(v is not None for v in parts) and out.get("incidence_couts_1an") is not None:
        total = sum(parts) + (out.get("commission_performance") or 0) + (out.get("couts_sortie") or 0)
        out["ecart_couts"] = round(total - out["incidence_couts_1an"], 2)
        # PRIIPs computes the exit cost on the value after a year of charges and
        # rounds each row, so the identity holds to a few percent of the total.
        out["couts_coherents"] = abs(out["ecart_couts"]) <= max(0.15, 0.03 * out["incidence_couts_1an"])

    articles = []
    for m in re.finditer(r"sfdr|2019/2088", c):
        window = c[max(0, m.start() - 200): m.end() + 200]
        articles += re.findall(r"article(6|8|9)(?![\d.])", window)
    out["sfdr_article"] = int(max(set(articles), key=articles.count)) if articles else None

    dates = []
    for day, month, year in re.findall(r"(?<!\d)(\d{2})/(\d{2})/(20\d{2})(?!\d)", c):
        with contextlib.suppress(ValueError):
            stamp = time.strptime(f"{year}-{month}-{day}", "%Y-%m-%d")
            if stamp <= time.localtime():
                dates.append(time.strftime("%Y-%m-%d", stamp))
    out["date_la_plus_recente"] = max(dates) if dates else None
    return out


def kid_score(parsed: dict) -> int:
    """How much of a parse survived its checks; used to pick between extractions."""
    return (
        (3 if parsed.get("sri") else 0)
        + (2 if parsed.get("couts_coherents") else 0)
        + (1 if parsed.get("scenarios") else 0)
        + sum(parsed.get(k) is not None for k in ("frais_gestion_exploitation", "couts_transaction"))
    )


_KID_FULL_SCORE = 8


def pdf_text(data: bytes) -> str:
    if PdfReader is None:
        return ""
    reader = PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def total_return_index(navs: list[dict], coupons: list[dict]):
    """Official NAVs with distributions reinvested on their ex-date.

    GECO reports each coupon with the NAV of its detachment day, which is
    already ex-coupon; growing every later NAV by (1 + coupon / ex-NAV) gives
    the return a holder who reinvests actually earns.
    """
    rows = [(n.get("valuationDate"), _num(n.get("netAssetValue"))) for n in navs if not n.get("cancellation")]
    rows = [(d, v) for d, v in rows if d and v and v > 0]
    if not rows:
        return None, 0
    closes = pd.Series([v for _, v in rows], index=pd.to_datetime([d[:10] for d, _ in rows])).sort_index()
    closes = closes[~closes.index.duplicated(keep="last")]
    factor = pd.Series(1.0, index=closes.index)
    applied = 0
    for coupon in coupons:
        amount, ex_nav = _num(coupon.get("couponAmountInEuro")), _num(coupon.get("exCouponNetAssetValue"))
        if coupon.get("cancellation") or not amount or not ex_nav or not coupon.get("couponDate"):
            continue
        factor[factor.index >= pd.Timestamp(coupon["couponDate"][:10])] *= 1 + amount / ex_nav
        applied += 1
    return closes * factor, applied


def calendar_returns(closes) -> dict[str, float]:
    """Calendar-year returns, only for years the series genuinely spans.

    Frequency-agnostic: a weekly or fortnightly NAV series qualifies as well as a
    daily one, provided both the year and the one before it end in December.
    """
    out = {}
    for year in sorted(set(closes.index.year)):
        this = closes[closes.index.year == year]
        prior = closes[closes.index.year == year - 1]
        if len(this) >= 20 and len(prior) and prior.index[-1].month == 12 and this.index[-1].month == 12 \
                and this.index[0].month == 1:
            out[str(year)] = (this.iloc[-1] / prior.iloc[-1] - 1) * 100
    return out


def risk_measures(closes) -> dict:
    span = (closes.index[-1] - closes.index[0]).days / 365.25
    gaps = closes.index.to_series().diff().dt.days.dropna()
    median_gap = float(gaps.median()) if len(gaps) else 1.0
    per_year = (len(closes) - 1) / span if span > 0 else _TRADING_DAYS
    frequency = ("quotidienne" if median_gap <= 4 else "hebdomadaire" if median_gap <= 8
                 else "bimensuelle" if median_gap <= 17 else "mensuelle ou moins frequente")
    returns = closes.pct_change().dropna()
    peak = closes.cummax()
    drawdown = closes / peak - 1
    trough_date = drawdown.idxmin()
    peak_date = closes[:trough_date].idxmax()
    recovered = closes[trough_date:] >= closes[peak_date]
    anchors = closes.index - pd.DateOffset(years=1)
    valid = anchors >= closes.index[0]
    worst_12m = None
    if valid.any():
        base = closes.asof(anchors[valid]).to_numpy()
        worst_12m = float((closes[valid].to_numpy() / base - 1).min() * 100)
    return {
        "span": span,
        "frequency": frequency,
        "vol": float(returns.std() * math.sqrt(per_year) * 100),
        "max_drawdown": float(drawdown.min() * 100),
        "trough_date": trough_date,
        "peak_date": peak_date,
        # When the worst fall starts on the very first value, the real peak lies
        # before the series began: the drawdown is truncated, and quoting it as
        # "from the peak of <first date>" presents a data limit as a market fact.
        "truncated": (peak_date - closes.index[0]).days <= 7 + median_gap,
        "recovery_date": recovered[recovered].index.min() if recovered.any() else None,
        "worst_12m": worst_12m,
    }


# ----------------------------------------------------------------------
# Insurer unit-linked list parser (Swiss Life eligibility annexes)
# ----------------------------------------------------------------------
#
# French life-insurance contracts publish, as a regulatory annex, the list of
# unit-linked supports they accept, with what no market data vendor gathers in
# one place: SRI, SFDR, legal form, the fund's fees with the share retroceded to
# the distributor, the contract's own fees, gross and net performance, five
# calendar years. Parsing works on word coordinates rather than extracted text:
# the tables wrap long cells over several lines, and a text dump interleaves
# them with the neighbouring rows. Column positions are learned from each
# page's own header and complete rows, so the layout differences between the
# insurer's lists (an extra "PEA PME" column, columns shifted by 20pt) do not
# shift a value into the wrong column.

ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
PCT_RE = re.compile(r"^-?\d+(?:,\d+)?%\)?$")
DATE_RE = re.compile(r"(\d{1,2})\s*(?:er)?\s*(janvier|fevrier|février|mars|avril|mai|juin|juillet|"
                     r"aout|août|septembre|octobre|novembre|decembre|décembre)\s+(\d{4})", re.I)
MONTHS = {"janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
          "juillet": 7, "aout": 8, "août": 8, "septembre": 9, "octobre": 10, "novembre": 11,
          "decembre": 12, "décembre": 12}

# Category headings ("Monétaire EUR", "Secteur Technologies") are 8.2pt italic in
# the brand colour. The column headers are the same size and also italic, but
# printed in white on a coloured band — size and style alone once labelled 44%
# of the funds with the header fragment "max (%)". Colour is what separates them.
# The size varies between the insurer's lists (8.2pt on one, 6.7pt on another),
# the rest does not: italic, in colour, smaller than the page title.
TITLE_SIZE = 10.0
WHITE = 16777215
BLACK = 0


def is_category(word: dict) -> bool:
    return word["italic"] and word["size"] < TITLE_SIZE and word["color"] not in (WHITE, BLACK)


def pct(text: str) -> float | None:
    """'12,24%' -> 12.24 ; anything else -> None."""
    cleaned = text.replace("(dont", "").replace(")", "").replace("%", "").strip()
    try:
        return float(cleaned.replace(",", "."))
    except ValueError:
        return None


def words_of(page) -> list[dict]:
    """Words with their exact boxes, annotated with the font of their span.

    Exact boxes matter: in the cost table two neighbouring cells sit 2pt apart
    while the words inside one cell sit 1.2pt apart, so any estimated width
    would merge or split cells at random.
    """
    spans = [
        (fitz_rect(span["bbox"]), span["size"], bool(span["flags"] & 2), span["color"])
        for block in page.get_text("dict")["blocks"]
        for line in block.get("lines", [])
        for span in line["spans"]
    ]
    out = []
    for x0, y0, x1, y1, text, *_ in page.get_text("words"):
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        size, italic, color = next(
            ((sz, it, co) for (bx0, by0, bx1, by1), sz, it, co in spans
             if bx0 <= cx <= bx1 and by0 <= cy <= by1),
            (0.0, False, 0),
        )
        out.append({"text": text, "x0": x0, "x1": x1, "y": cy, "size": size,
                    "italic": italic, "color": color})
    return out


def fitz_rect(bbox) -> tuple[float, float, float, float]:
    return (bbox[0], bbox[1], bbox[2], bbox[3])


def logical_cells(tokens: list[dict]) -> list[dict]:
    """Group a row's words into cells by grammar rather than by distance.

    A cell is a percentage, optionally followed by '(dont X%)' for the share
    retroceded to the distributor, or the two words 'Création YYYY' for a fund
    too young to have the figure. Distance cannot tell these apart reliably.
    """
    cells: list[dict] = []
    ordered = sorted(tokens, key=lambda t: t["x0"])
    i = 0
    while i < len(ordered):
        tok = ordered[i]
        text, x0, x1 = tok["text"], tok["x0"], tok["x1"]
        if text == "Création" and i + 1 < len(ordered) and re.fullmatch(r"20\d\d", ordered[i + 1]["text"]):
            text, x1 = f"Création {ordered[i + 1]['text']}", ordered[i + 1]["x1"]
            i += 1
        elif (i + 2 < len(ordered) and ordered[i + 1]["text"] == "(dont"
              and PCT_RE.match(ordered[i + 2]["text"])):
            text, x1 = f"{text} (dont {ordered[i + 2]['text']}", ordered[i + 2]["x1"]
            i += 2
        cells.append({"text": text, "x0": x0, "x1": x1})
        i += 1
    return cells


def page_kind(text: str, previous: str) -> str:
    """Which of the annex tables a page carries.

    Headers are not printed on every page — the IB fund list shows its column
    titles once — so a page with ISINs and no recognisable title continues the
    table of the page before it.
    """
    if "Historique des performances" in text:
        return "HIST"
    if "Information sur chaque actif" in text:
        return "INFO"
    if "Liste des entrées" in text:
        return "ENTREES"
    if "Liste des sorties" in text:
        return "SORTIES"
    if "Devise de cotation" in text:
        return "SHARES"
    if "Dénomination" in text or "Code ISIN" in text:
        return "LIST"
    if previous in ("LIST", "SHARES", "ENTREES", "SORTIES", "INFO", "HIST"):
        return previous
    return "?"


# ---------------------------------------------------------------------------
# Fund list (annexes IA / IB)
# ---------------------------------------------------------------------------

LIST_HEADERS = [
    ("isin", "ISIN"), ("label", "Label"), ("sfdr", "Classification"),
    ("nom", "Dénomination"), ("forme_juridique", "Forme"), ("societe_gestion", "Société"),
    ("devise", "Devise"), ("indice_reference", "Indice"), ("site", "Adresse"),
    ("sri", "Profil"), ("frais_gestion_max", "Frais"), ("pea_pme", "PEA"),
]


# Footnote markers of the annex carry eligibility and liquidity facts. Left in
# the text they are noise ("FCP (2)"); turned into flags they answer questions a
# CGP actually has to check before recommending the support. Their numbers are
# not stable: "(7)" means "not eligible to retirement products" on one list and
# "not in free allocation" on another, so the meaning is read from each
# document's own legend.
FOOTNOTE_MEANINGS = [
    (r"hebdo|bi ?mensuelle", ("valorisation", "hebdomadaire ou bimensuelle")),
    (r"avenant", ("avenant_specifique_requis", True)),
    (r"retraite individuelle|cadre fiscal", ("non_eligible_retraite_et_contrats_fiscaux_specifiques", True)),
    (r"retraite collective", ("non_eligible_retraite_collective", True)),
    (r"allocation libre", ("hors_allocation_libre", True)),
]


def footnote_legend(text: str) -> dict[str, tuple[str, object]]:
    """Marker number -> flag, from the legend printed under the fund list."""
    legend: dict[str, tuple[str, object]] = {}
    flat = re.sub(r"\s+", " ", text)
    for number, body in re.findall(r"\((\d)\)\s*(.+?)(?=\(\d\)\s|$)", flat):
        for pattern, flag in FOOTNOTE_MEANINGS:
            # The last note runs into the page's fund rows: its first sentence is enough.
            if re.search(pattern, body[:300], re.I):
                legend.setdefault(number, flag)
                break
    return legend


def apply_footnotes(record: dict, legend: dict) -> None:
    for field in ("forme_juridique", "categorie", "nom"):
        text = record.get(field) or ""
        for number in re.findall(r"\((\d)\)", text):
            if number in legend:
                key, value = legend[number]
                record[key] = value
        record[field] = re.sub(r"\s*\(\d\)", "", text).strip()
    if record.get("forme_juridique", "").startswith("Compagnie"):
        record["forme_juridique"] = "Compagnie d'investissement de fonds ouverts"
        # The phrase spans three centred lines; its "de" lands inside the name
        # column on every one of the 41 funds concerned.
        record["nom"] = re.sub(r"\s+de$", "", record.get("nom", "")).strip()


def list_columns(words: list[dict], header_y: float) -> list[tuple[str, float]]:
    """Left edge of each column, read from this page's own header row."""
    band = [w for w in words if abs(w["y"] - header_y) <= 14]
    cols = []
    for key, label in LIST_HEADERS:
        hits = [w for w in band if w["text"].startswith(label)]
        if hits:
            cols.append((key, min(h["x0"] for h in hits)))
    return sorted(cols, key=lambda c: c[1])


def parse_list_page(page, annex: str, category: str, out: dict, state: dict) -> str:
    words = words_of(page)
    isin_words = [w for w in words if ISIN_RE.match(w["text"])]
    if not isin_words:
        return category
    headers = [w for w in words if w["text"] == "ISIN"]
    if headers:
        cols = list_columns(words, headers[0]["y"])
        state["list_cols"] = cols
        top = headers[0]["y"] + 18
    else:
        # No header on this page: same table as before, same columns.
        cols = state.get("list_cols") or []
        top = min(w["y"] for w in isin_words) - 12
    if not cols:
        return category
    last_row = max(w["y"] for w in isin_words)
    # Only footer lines below the last fund count: a five-digit figure inside a
    # benchmark name once cut the page short and dropped its final row.
    footer = [w["y"] for w in words
              if w["y"] > last_row + 3 and (w["text"].startswith("SwissLife") or re.match(r"^\d{5}$", w["text"]))]
    bottom = min(footer) - 2 if footer else page.rect.height - 30

    categories = sorted({round(w["y"]) for w in words if is_category(w) and top < w["y"] < bottom})
    cat_text = {
        y: " ".join(w["text"] for w in sorted(words, key=lambda w: w["x0"])
                    if is_category(w) and round(w["y"]) == y)
        for y in categories
    }
    rows = sorted(isin_words, key=lambda w: w["y"])
    # Band limits: midpoints between consecutive rows, clipped by any category
    # heading in between, so a wrapped cell is attached to the right fund.
    marks = sorted([(r["y"], "row", r) for r in rows] + [(y, "cat", None) for y in categories])
    for i, (y, kind, row) in enumerate(marks):
        if kind == "cat":
            category = cat_text[round(y)]
            continue
        prev_y = marks[i - 1][0] if i > 0 else top
        next_y = marks[i + 1][0] if i + 1 < len(marks) else bottom
        lo = (prev_y + y) / 2 if i > 0 and marks[i - 1][1] == "row" else prev_y + 1
        hi = (y + next_y) / 2 if i + 1 < len(marks) and marks[i + 1][1] == "row" else next_y - 1
        cell_words = [w for w in words if lo <= w["y"] <= hi and not is_category(w)]
        record = {"annexe": annex, "categorie": category}
        for idx, (key, x_left) in enumerate(cols):
            x_right = cols[idx + 1][1] if idx + 1 < len(cols) else 10_000
            # By centre, not by left edge: a wrapped cell is centred in its column
            # and may be wider than it. "d'investissement", from the three-line
            # legal form "Compagnie d'investissement de fonds ouverts", starts
            # inside the name column — by left edge it ended up in the fund name,
            # which is exactly the string later used to find the NAV series.
            parts = sorted((w for w in cell_words if x_left - 3 <= (w["x0"] + w["x1"]) / 2 < x_right - 3),
                           key=lambda w: (round(w["y"]), w["x0"]))
            record[key] = " ".join(p["text"] for p in parts).strip()
        isin = row["text"]
        record["isin"] = isin
        apply_footnotes(record, state.get("legend") or {})
        record["pea_pme"] = bool(record.get("pea_pme"))
        record["sri"] = int(record["sri"]) if record.get("sri", "").isdigit() else None
        record["frais_gestion_max"] = pct(record.get("frais_gestion_max", ""))
        record["sfdr"] = record.get("sfdr", "").replace("Article", "Article ").replace("  ", " ").strip()
        out.setdefault(isin, {}).update({k: v for k, v in record.items() if v not in ("", None)})
    return category


# ---------------------------------------------------------------------------
# Costs and performance (information table)
# ---------------------------------------------------------------------------

INFO_SLOTS = ["perf_brute_n1", "perf_brute_5a", "frais_uc", "perf_nette_n1", "perf_nette_5a",
              "frais_contrat", "frais_totaux", "perf_nette_nette_n1", "perf_nette_nette_5a"]


VALUE_TOKEN = re.compile(r"^(?:-?\d+(?:,\d+)?%\)?|\(dont|Création|ND%?)$")


def value_cells(words: list[dict], y: float) -> list[dict]:
    """The row's figures, whatever the width of the name and company columns.

    Fixed x offsets held on one list and missed every value on the next, whose
    columns sit 20pt to the right. Figures are recognised by their grammar
    instead; a year counts only after "Création", so "Keren 2032" stays a name.
    """
    line = sorted((w for w in words if abs(w["y"] - y) <= 2.5), key=lambda w: w["x0"])
    kept = []
    for w in line:
        if VALUE_TOKEN.match(w["text"]) or (re.fullmatch(r"20\d\d", w["text"]) and kept
                                            and kept[-1]["text"] == "Création"):
            kept.append(w)
    return logical_cells(kept)


def learn_slots(rows_cells: list[list[dict]], count: int) -> list[float]:
    """Right edge of each value column, from the rows that fill every column."""
    full = [c for c in rows_cells if len(c) == count]
    if not full:
        return []
    return [statistics.median(r[i]["x1"] for r in full) for i in range(count)]


def assign(cells: list[dict], slots: list[float]) -> dict[int, str]:
    """Nearest column by right edge. Numbers are right-aligned, so x1 discriminates."""
    placed: dict[int, str] = {}
    for cell in cells:
        idx = min(range(len(slots)), key=lambda i: abs(slots[i] - cell["x1"]))
        placed[idx] = cell["text"]
    return placed


def parse_info_page(page, out: dict) -> None:
    words = words_of(page)
    rows = sorted((w for w in words if ISIN_RE.match(w["text"])), key=lambda w: w["y"])
    if not rows:
        return
    # SRI: the lone digit 1-7 just before the first figure of the row.
    parsed = []
    for row in rows:
        cells = value_cells(words, row["y"])
        first_x = cells[0]["x0"] if cells else 10_000
        line = sorted((w for w in words if abs(w["y"] - row["y"]) <= 2.5 and w["x1"] <= first_x),
                      key=lambda w: w["x0"])
        sri = next((w for w in reversed(line) if re.fullmatch(r"[1-7]", w["text"])), None)
        parsed.append((row["text"], sri["text"] if sri else None, cells))
    slots = learn_slots([p[2] for p in parsed], len(INFO_SLOTS))
    if not slots:
        return
    for isin, sri, cells in parsed:
        placed = assign(cells, slots)
        info: dict = {"sri_document": int(sri) if sri else None}
        for idx, key in enumerate(INFO_SLOTS):
            text = placed.get(idx, "")
            if text.startswith("Création"):
                info[key] = None
                info.setdefault("creation", text.split()[-1])
            elif "(dont" in text:
                total, _, retro = text.partition("(dont")
                info[key] = pct(total)
                info[key + "_dont_retrocession"] = pct(retro)
            else:
                info[key] = pct(text)
        out.setdefault(isin, {})["couts_performances"] = info


# ---------------------------------------------------------------------------
# Calendar-year history
# ---------------------------------------------------------------------------

def parse_hist_page(page, out: dict) -> None:
    words = words_of(page)
    rows = sorted((w for w in words if ISIN_RE.match(w["text"])), key=lambda w: w["y"])
    if not rows:
        return
    first_y = rows[0]["y"]
    parsed = [(r["text"], value_cells(words, r["y"])) for r in rows]
    slots = learn_slots([p[1] for p in parsed], 5)
    if not slots:
        return
    header_years = [w for w in words if re.fullmatch(r"20\d\d", w["text"]) and w["y"] < first_y - 4]
    years = []
    for x1 in slots:
        near = min(header_years, key=lambda w: abs(w["x1"] - x1), default=None)
        years.append(near["text"] if near else None)
    for isin, cells in parsed:
        placed = assign(cells, slots)
        history = {}
        for idx, year in enumerate(years):
            if not year:
                continue
            text = placed.get(idx, "")
            history[year] = None if text.startswith("Création") or not text else pct(text)
        out.setdefault(isin, {})["performances_annuelles"] = history


# ---------------------------------------------------------------------------
# Individual shares (annex IC)
# ---------------------------------------------------------------------------

SHARE_HEADERS = [("nom", "Libellé"), ("devise", "Devise"), ("secteur", "Secteur"),
                 ("pays", "Pays"), ("notation_emetteur", "Notation")]


def parse_share_page(page, annex: str, out: dict, state: dict) -> None:
    """Individual shares eligible as direct holdings — not funds.

    Recorded so that a CGP who types a share ISIN into the fund analysis is told
    it is a share and pointed at the right tool, instead of getting a fund
    report with every field empty.
    """
    words = words_of(page)
    rows = [w for w in words if ISIN_RE.match(w["text"])]
    if not rows:
        return
    header_y = next((w["y"] for w in words if w["text"] == "Libellé"), None)
    if header_y is not None:
        band = [w for w in words if abs(w["y"] - header_y) <= 14]
        centres = sorted(
            ((key, statistics.mean((w["x0"] + w["x1"]) / 2 for w in band if w["text"].startswith(label)))
             for key, label in SHARE_HEADERS if any(w["text"].startswith(label) for w in band)),
            key=lambda c: c[1],
        )
        # These headers are centred over their column while the values are not
        # aligned on them, so a header's left edge cuts into the neighbouring
        # value ("3i Group Plc" lost "3i"). Boundaries go half-way between
        # consecutive header centres instead.
        isin_right = max(w["x1"] for w in rows)
        edges = [(isin_right + centres[0][1]) / 2] + [
            (centres[i][1] + centres[i + 1][1]) / 2 for i in range(len(centres) - 1)
        ]
        state["share_cols"] = [(key, edges[i]) for i, (key, _) in enumerate(centres)]
    cols = state.get("share_cols") or []
    for row in rows:
        line = [w for w in words if abs(w["y"] - row["y"]) <= 2.5 and w is not row]
        record = {"isin": row["text"], "annexe": annex, "type": "action"}
        for idx, (key, x_left) in enumerate(cols):
            x_right = cols[idx + 1][1] if idx + 1 < len(cols) else 10_000
            record[key] = " ".join(w["text"] for w in sorted(line, key=lambda w: w["x0"])
                                   if x_left <= (w["x0"] + w["x1"]) / 2 < x_right)
        out.setdefault(row["text"], {}).update({k: v for k, v in record.items() if v})


# ---------------------------------------------------------------------------

def document_date(doc) -> str | None:
    """Publication date from the first page title, as YYYY-MM-DD."""
    match = DATE_RE.search(doc[0].get_text())
    if not match:
        return None
    day, month, year = match.groups()
    return f"{int(year):04d}-{MONTHS[month.lower()]:02d}-{int(day):02d}"


_LIST_PARSER_VERSION = 2


def parse_uc_list(data: bytes, name: str) -> dict:
    """An insurer's unit-linked eligibility list (PDF) turned into a reference base."""
    import fitz  # PyMuPDF, imported late: only this parser needs it

    doc = fitz.open(stream=data, filetype="pdf")
    funds: dict = {}
    entrees: dict = {}
    sorties: dict = {}
    category = ""
    kind = "?"
    annex = "IA"
    # The legend is printed from the second page on, while the first page
    # already carries markers: read it before parsing anything.
    state: dict = {"legend": footnote_legend(next((p.get_text() for p in doc if "(1) FCP" in p.get_text()), ""))}
    for page in doc:
        text = page.get_text()
        kind = page_kind(text, kind)
        if "Annexe IB" in text:
            annex = "IB"
        elif "Annexe IC" in text:
            annex = "IC"
        elif "Annexe IA" in text:
            annex = "IA"
        if kind == "LIST":
            category = parse_list_page(page, annex, category, funds, state)
        elif kind == "INFO":
            parse_info_page(page, funds)
        elif kind == "HIST":
            parse_hist_page(page, funds)
        elif kind == "SHARES":
            parse_share_page(page, annex, funds, state)
        elif kind == "ENTREES":
            parse_list_page(page, "entree", "", entrees, state)
        elif kind == "SORTIES":
            parse_list_page(page, "sortie", "", sorties, state)

    # The entry and exit lists travel with each record, stated as facts rather
    # than resolved into a status: the same fund can appear in the entries, the
    # exits and the active list of one publication (a share class swap, a
    # scheduled removal). Declaring it "closed" on the exit list alone would
    # contradict the document itself.
    for isin, record in entrees.items():
        target = funds.setdefault(isin, {"isin": isin, "nom": record.get("nom")})
        target["liste_entrees"] = True
    for isin, record in sorties.items():
        target = funds.setdefault(isin, {"isin": isin, "nom": record.get("nom")})
        target["liste_sorties"] = True
        target["dans_liste_active"] = "sri" in target and target.get("annexe") in ("IA", "IB")

    lines = [line.strip() for line in doc[1 if doc.page_count > 1 else 0].get_text().splitlines()]
    title_at = next((i for i, line in enumerate(lines) if "Liste des Unités de Compte" in line), None)
    title = lines[title_at] if title_at is not None else ""
    # The contract the list belongs to is printed just above its title.
    contract = lines[title_at - 1] if title_at else ""
    return {
        "source": {
            "document": name,
            "titre": title,
            "contrat": contract,
            "date_document": document_date(doc),
            "genere_le": time.strftime("%Y-%m-%d"),
            "nombre_supports": len(funds),
        },
        "supports": funds,
    }


def request_token(request: Any) -> str:
    """The caller's Open WebUI session: bearer header, cookie, or automation state."""
    if request is None:
        return ""
    with contextlib.suppress(Exception):
        header = request.headers.get("authorization") or ""
        if header.lower().startswith("bearer "):
            return header[7:].strip()
    with contextlib.suppress(Exception):
        if request.cookies.get("token"):
            return request.cookies.get("token")
    with contextlib.suppress(Exception):
        token = getattr(request.state, "token", None)
        if token is not None:
            return getattr(token, "credentials", "") or ""
    return ""


class GecoUnavailable(RuntimeError):
    """The AMF database is down or refusing requests, which says nothing about the fund."""


class Tools:
    """Unit-linked support analysis data."""

    class Valves(BaseModel):
        UC_LISTS_KNOWLEDGE: str = Field(
            default="Listes des UC des contrats SwissLife",
            description=(
                "Knowledge base holding the insurer's unit-linked lists (PDF), as kept by the "
                "swisslife-veille-documentaire tool. Adds contract fees, retrocessions, "
                "net-of-all-fees performance and category ranking. Empty disables it."
            ),
        )
        UC_LISTS_KNOWLEDGE_ID: str = Field(
            default="", description="Exact knowledge base id, takes precedence over the name (optional)."
        )
        OPENWEBUI_URL: str = Field(
            default="",
            description="Open WebUI API address as seen from the server. Empty = http://127.0.0.1:$PORT.",
        )
        OPENWEBUI_API_KEY: str = Field(
            default="",
            description=(
                "API key (sk-...) of an account allowed to read the lists. Empty = the session of "
                "the user calling the tool, so the knowledge base must be shared with them."
            ),
            json_schema_extra={"input": {"type": "password"}},
        )
        GECO_ENABLED: bool = Field(
            default=True,
            description="Query the AMF's public GECO database (KID, official NAV) for French funds.",
        )
        CACHE_DIR: str = Field(
            default="",
            description=(
                "Directory for cached NAV series and KID texts. Empty uses a temporary "
                "folder; point it at a mounted volume so they survive restarts."
            ),
        )
        HISTORY_YEARS: int = Field(
            default=10, description="Years of NAV history requested for risk measures."
        )
        REQUEST_TIMEOUT: int = Field(default=30, description="Seconds per provider call.")
        MAX_OUTPUT_CHARS: int = Field(default=9000, description="Cap per tool response.")
        UNIVERSE_PAGE_CHARS: int = Field(
            default=24000,
            description=(
                "Page size of list_uc_universe, in characters. The listing is never truncated: "
                "it is paginated, and every page must be read."
            ),
        )

    def __init__(self):
        """Initialize the Tool."""
        self.valves = self.Valves()
        self._lists: dict[str, dict] = {}  # content key -> parsed list, with its peer stats
        self._lists_checked: dict[str, tuple[float, list[str], list[str]]] = {}  # token -> (when, keys, errors)
        self._geco_memo: dict[str, tuple[float, dict]] = {}
        self._pdf_memo: dict[tuple, str] = {}

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------

    def _cap(self, text: str) -> str:
        limit = max(800, int(self.valves.MAX_OUTPUT_CHARS))
        return text if len(text) <= limit else text[:limit] + "\n\n[... sortie tronquee ...]"

    def _cap_card(self, body: list[str], tail: list[str]) -> str:
        """Cap a card by shortening its body, never its end.

        The constraints and screening signals come last; a plain cut would drop
        exactly what the verdicts depend on.
        """
        limit = max(800, int(self.valves.MAX_OUTPUT_CHARS))
        body_text, tail_text = "\n".join(body), "\n".join(tail)
        if len(body_text) + len(tail_text) + 1 <= limit:
            return body_text + "\n" + tail_text
        room = max(400, limit - len(tail_text) - 60)
        cut = body_text[:room].rsplit("\n", 1)[0]
        return cut + "\n\n[... fiche tronquee : sections intermediaires coupees ...]\n" + tail_text

    async def _emit(self, emitter: Callable[[dict], Any] | None, text: str, done: bool = False):
        if emitter is None:
            return
        with contextlib.suppress(Exception):
            await emitter({"type": "status", "data": {"description": text, "done": done}})

    async def _run(self, func: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.wait_for(
            asyncio.to_thread(func, *args), timeout=max(15, int(self.valves.REQUEST_TIMEOUT)) * 6
        )

    def _cache_file(self, name: str) -> str:
        base = (self.valves.CACHE_DIR or "").strip() or os.path.join(tempfile.gettempdir(), "uc-nav")
        try:
            os.makedirs(base, exist_ok=True)
        except OSError:
            return ""
        return os.path.join(base, re.sub(r"[^A-Za-z0-9._-]", "_", name))

    def _owui(self, token: str, method: str, path: str, **kwargs: Any) -> requests.Response:
        base = (self.valves.OPENWEBUI_URL or "").strip() or f"http://127.0.0.1:{os.environ.get('PORT') or '8080'}"
        session = requests.Session()
        # The API is this very process: never through a corporate proxy.
        session.trust_env = False
        response = session.request(
            method, base.rstrip("/") + path, headers={"Authorization": f"Bearer {token}"},
            timeout=int(self.valves.REQUEST_TIMEOUT) * 2, **kwargs,
        )
        response.raise_for_status()
        return response

    def _load_lists(self, request: Any) -> tuple[list[dict], list[str]]:
        """The insurer's lists from the knowledge base, parsed once per edition.

        The file list is re-read every ten minutes, so a new edition dropped by
        the documentary watch is picked up without a restart; a file whose content
        hash is already known is not downloaded again.
        """
        name = (self.valves.UC_LISTS_KNOWLEDGE or "").strip()
        knowledge_id = (self.valves.UC_LISTS_KNOWLEDGE_ID or "").strip()
        if not name and not knowledge_id:
            return [], []
        token = (self.valves.OPENWEBUI_API_KEY or "").strip() or request_token(request)
        if not token:
            raise RuntimeError("aucune session Open WebUI transmise a l'outil pour lire les listes d'UC")
        checked = self._lists_checked.get(token)
        if checked and time.time() - checked[0] < 600:
            return self._known_lists(token)

        if not knowledge_id:
            found, page = [], 1
            while page < 50:
                body = self._owui(token, "GET", "/api/v1/knowledge/", params={"page": page}).json()
                items = body if isinstance(body, list) else body.get("items") or []
                found += [k for k in items if (k.get("name") or "").strip() == name]
                if isinstance(body, list) or not items or page * max(1, len(items)) >= (body.get("total") or 0):
                    break
                page += 1
            if not found:
                raise RuntimeError(f"connaissance « {name} » introuvable ou non partagee avec cet utilisateur")
            candidates = [k["id"] for k in found]
        else:
            candidates = [knowledge_id]
        files: list[dict] = []
        # Two knowledge bases can share the name (a personal copy next to the
        # shared one): the first that actually holds files is the one to read.
        for knowledge_id in candidates:
            files = self._knowledge_files(token, knowledge_id)
            if files:
                break

        keys, errors = [], []
        for file in files:
            meta = file.get("meta") or {}
            filename = file.get("filename") or meta.get("name") or ""
            if not filename.lower().endswith(".pdf"):
                continue
            data = meta.get("data") if isinstance(meta.get("data"), dict) else {}
            content_key = data.get("sha256") or file.get("hash") or f"{file.get('id')}:{file.get('updated_at')}"
            key = re.sub(r"[^A-Za-z0-9]", "", str(content_key))[:64]
            keys.append(key)
            if key in self._lists:
                continue
            # The parser version is part of the name: a corrected parser must not
            # keep serving what its predecessor read.
            path = self._cache_file(f"uc_list_v{_LIST_PARSER_VERSION}_{key}.json")
            parsed = None
            if path and os.path.exists(path):
                with contextlib.suppress(OSError, ValueError), open(path, encoding="utf-8") as handle:
                    parsed = json.load(handle)
            if parsed is None:
                try:
                    content = self._owui(token, "GET", f"/api/v1/files/{file['id']}/content").content
                    parsed = parse_uc_list(content, filename)
                except Exception as exc:
                    # One unreadable or foreign PDF (the knowledge base also takes
                    # files added by hand) must not take the other lists down.
                    errors.append(f"{filename} : {str(exc) or type(exc).__name__}")
                    continue
                if path:
                    with contextlib.suppress(OSError), open(path, "w", encoding="utf-8") as handle:
                        json.dump(parsed, handle, ensure_ascii=False)
            parsed["peers"] = self._peer_stats(parsed.get("supports") or {})
            self._lists[key] = parsed
        # Sessions come and go, editions are replaced: forget both after ten
        # minutes. A PDF that is not a unit-linked list stays known (and empty),
        # so it is not downloaded again at every check.
        self._lists_checked = {t: v for t, v in self._lists_checked.items() if time.time() - v[0] < 600}
        self._lists_checked[token] = (time.time(), keys, errors)
        live = {key for _, known, _ in self._lists_checked.values() for key in known}
        self._lists = {key: value for key, value in self._lists.items() if key in live}
        return self._known_lists(token)

    def _known_lists(self, token: str) -> tuple[list[dict], list[str]]:
        _, keys, errors = self._lists_checked[token]
        lists = [self._lists[key] for key in keys if (self._lists.get(key) or {}).get("supports")]
        if errors and not lists:
            raise RuntimeError("aucune liste lisible dans la connaissance (" + " ; ".join(errors[:3]) + ")")
        return lists, errors

    def _knowledge_files(self, token: str, knowledge_id: str) -> list[dict]:
        files, page = [], 1
        try:
            while page < 100:
                body = self._owui(token, "GET", f"/api/v1/knowledge/{knowledge_id}/files",
                                  params={"page": page}).json()
                items = body.get("items") or []
                files += items
                if not items or len(files) >= (body.get("total") or 0):
                    break
                page += 1
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code not in (404, 405):
                raise
            # Versions before paginated file lists return them with the knowledge base.
            files = self._owui(token, "GET", f"/api/v1/knowledge/{knowledge_id}").json().get("files") or []
        return files

    @staticmethod
    def _peer_stats(supports: dict) -> dict:
        """Per-category medians, so a fund is judged against its own kind."""
        groups: dict[str, list[dict]] = {}
        for record in supports.values():
            if record.get("type") == "action" or not record.get("categorie") or record.get("sri") is None:
                continue
            groups.setdefault(record["categorie"], []).append(record)
        stats = {}
        for category, members in groups.items():
            fees = [m["couts_performances"]["frais_uc"] for m in members
                    if _num((m.get("couts_performances") or {}).get("frais_uc")) is not None]
            perf5 = [m["couts_performances"]["perf_nette_5a"] for m in members
                     if _num((m.get("couts_performances") or {}).get("perf_nette_5a")) is not None]
            stats[category] = {
                "n": len(members),
                "fees": sorted(fees), "perf5": sorted(perf5),
                "median_fees": statistics.median(fees) if fees else None,
                "median_perf5": statistics.median(perf5) if perf5 else None,
            }
        return stats

    @staticmethod
    def _percentile(sorted_values: list[float], value: float) -> float | None:
        if not sorted_values or value is None:
            return None
        below = sum(1 for v in sorted_values if v < value)
        equal = sum(1 for v in sorted_values if v == value)
        return (below + equal / 2) / len(sorted_values)

    def _openfigi(self, isin: str) -> dict:
        response = requests.post(
            _OPENFIGI, json=[{"idType": "ID_ISIN", "idValue": isin}],
            headers={"Content-Type": "application/json"}, timeout=int(self.valves.REQUEST_TIMEOUT),
        )
        if not response.ok:
            return {}
        data = (response.json() or [{}])[0].get("data") or []
        return data[0] if data else {}

    # ---- AMF GECO -------------------------------------------------------

    @staticmethod
    def _geco_check(response: requests.Response) -> None:
        # While unavailable (maintenance observed, possibly throttling as well)
        # GECO answers HTTP 418 with an empty result set. Read as "not found",
        # that would tell the adviser a French fund is foreign.
        if response.status_code in (418, 429, 503):
            raise GecoUnavailable("indisponibilite momentanee, maintenance ou limitation de requetes")
        response.raise_for_status()

    def _geco(self, isin: str) -> dict:
        """Compartment, share and KID list of an ISIN in the AMF database."""
        memo = self._geco_memo.get(isin)
        if memo and time.time() - memo[0] < 3600:
            return memo[1]
        # Kept a day on disk as well: GECO has outages, and a fund's share record
        # and document list change far less often than advisers ask about them.
        path = self._cache_file(f"geco_lookup_{isin}.json")
        if path and os.path.exists(path) and time.time() - os.path.getmtime(path) < 86_400:
            with contextlib.suppress(OSError, ValueError), open(path, encoding="utf-8") as handle:
                found = json.load(handle)
                self._geco_memo[isin] = (time.time(), found)
                return found
        timeout = int(self.valves.REQUEST_TIMEOUT)
        headers = {**_HEADERS, "Accept": "application/json"}
        # The search endpoint rejects a body without sort order and filters.
        response = requests.post(
            f"{_GECO}/funds/search", params={"keyword": isin},
            json={"first": 0, "rows": 10, "sortOrder": 1, "filters": {}}, headers=headers, timeout=timeout,
        )
        self._geco_check(response)
        found: dict = {}
        for compartment in response.json().get("compartmentDtos") or []:
            if isin not in (compartment.get("sharesIsins") or []):
                continue
            shares = requests.get(
                f"{_GECO}/funds/compartment/{compartment['cmpId']}/shares", headers=headers, timeout=timeout,
            )
            self._geco_check(shares)
            share = next((s for s in shares.json() or [] if s.get("isin") == isin), None)
            if share:
                documents = [d for d in share.get("documentEsEntities") or []
                             if re.search(r"\bDIC", d.get("docTypeLib") or "") and d.get("idInterne")]
                documents.sort(key=lambda d: d.get("dateEffet") or "", reverse=True)
                found = {"compartment": compartment, "share": share, "kids": documents}
                break
        self._geco_memo[isin] = (time.time(), found)
        if path:
            with contextlib.suppress(OSError), open(path, "w", encoding="utf-8") as handle:
                json.dump(found, handle)
        return found

    def _geco_kid(self, document: dict) -> dict:
        """Download and parse a GECO KID. Downloads go by idInterne; docId answers 500."""
        path = self._cache_file(f"kid_{document['idInterne']}.txt")
        text = ""
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        if not text:
            response = requests.get(
                f"{_GECO}/document/download/{document['idInterne']}",
                headers=_HEADERS, timeout=int(self.valves.REQUEST_TIMEOUT) * 2,
            )
            self._geco_check(response)
            if not response.content.startswith(b"%PDF"):
                return {}
            text = pdf_text(response.content)
            if path and text:
                with contextlib.suppress(OSError), open(path, "w", encoding="utf-8") as handle:
                    handle.write(text)
        return parse_kid(text) if text else {}

    def _geco_nav(self, share: dict) -> dict:
        """Official NAV history of a share, distributions reinvested."""
        path = self._cache_file(f"geco_{share['parId']}.csv")
        meta_path = path + ".meta.json" if path else ""
        if path and os.path.exists(path) and time.time() - os.path.getmtime(path) < 86_400:
            with contextlib.suppress(OSError, ValueError), open(meta_path, encoding="utf-8") as handle:
                meta = json.load(handle)
                closes = pd.read_csv(path, index_col=0, parse_dates=True).iloc[:, 0]
                return {**meta, "closes": closes}
        end = time.strftime("%Y-%m-%d")
        start = f"{int(end[:4]) - int(self.valves.HISTORY_YEARS)}{end[4:].replace('-02-29', '-02-28')}"
        response = requests.get(
            f"{_GECO}/funds/vlsCouponsAndOst/{share['parId']}", params={"startDate": start, "endDate": end},
            headers={**_HEADERS, "Accept": "application/json"}, timeout=int(self.valves.REQUEST_TIMEOUT) * 4,
        )
        self._geco_check(response)
        payload = response.json() or {}
        navs = payload.get("netAssetValueDTO") or []
        coupons = payload.get("couponDTOS") or []
        # Coupons are reported in euros: against a NAV in another currency the
        # reinvestment ratio would be wrong, so they are then left out and said so.
        euro_share = (share.get("parRefDevCode") or "EUR").upper() == "EUR"
        closes, applied = total_return_index(navs, coupons if euro_share else [])
        if closes is None or len(closes) < 2:
            return {}
        meta: dict = {
            "coupons": len([c for c in coupons if not c.get("cancellation")]),
            "coupons_reinvested": applied,
            "corporate_actions": len(payload.get("corporateActionDTOS") or []),
            "currency": share.get("parRefDevCode"),
        }
        latest = max((n for n in navs if not n.get("cancellation") and _num(n.get("shareNumber"))),
                     key=lambda n: n.get("valuationDate") or "", default=None)
        if latest:
            meta["part_size"] = _num(latest.get("netAssetValue")) * _num(latest.get("shareNumber"))
            meta["part_size_date"] = (latest.get("valuationDate") or "")[:10]
        # A split or merger shows as a jump no fund makes in one valuation. The
        # series is kept only after the last such break, rather than measured
        # across it.
        jumps = closes.pct_change().abs()
        breaks = jumps[jumps > 0.5]
        if len(breaks):
            meta["break_date"] = f"{breaks.index[-1]:%Y-%m-%d}"
            closes = closes[breaks.index[-1]:]
        if path:
            with contextlib.suppress(OSError):
                closes.rename("nav").to_csv(path)
                with open(meta_path, "w", encoding="utf-8") as handle:
                    json.dump(meta, handle)
        return {**meta, "closes": closes}

    # ---- attached KID ---------------------------------------------------

    def _file_sources(self, item: dict, token: str) -> tuple[str, str, Callable[[], bytes] | None]:
        """Name, extracted text and a reader for the PDF bytes of a chat attachment."""
        file = item.get("file") if isinstance(item.get("file"), dict) else {}
        file_id = file.get("id") or item.get("id") or ""
        meta = file.get("meta") or {}
        name = file.get("filename") or item.get("name") or meta.get("name") or "fichier joint"
        text = (file.get("data") or {}).get("content") or ""
        path = file.get("path") or ""
        if not text and file_id and token:
            # Depending on the version, the chat payload may carry only the file
            # id; its extracted text then comes from Open WebUI's API, which also
            # applies the caller's access rights.
            with contextlib.suppress(Exception):
                text = ((self._owui(token, "GET", f"/api/v1/files/{file_id}").json().get("data") or {})
                        .get("content") or "")
        is_pdf = name.lower().endswith(".pdf") or "pdf" in str(meta.get("content_type") or "")
        if not is_pdf:
            return name, text, None

        def read() -> bytes:
            # Local storage is readable in place; S3, GCS or Azure paths are not,
            # and the API serves those too.
            key = (file_id or path, os.path.getmtime(path) if path and os.path.exists(path) else 0)
            if key not in self._pdf_memo:
                if path and os.path.exists(path):
                    with open(path, "rb") as handle:
                        content = handle.read()
                elif file_id and token:
                    content = self._owui(token, "GET", f"/api/v1/files/{file_id}/content").content
                else:
                    raise FileNotFoundError(name)
                if len(self._pdf_memo) > 200:
                    self._pdf_memo.clear()  # a long-running server sees many attachments
                self._pdf_memo[key] = pdf_text(content)
            return self._pdf_memo[key]

        return name, text, read

    def _attached_kid(self, isin: str, files: list, token: str = "") -> tuple[dict, dict, list[str]]:
        """The best-read attached KID that names this ISIN, with notes on the others."""
        best: dict = {}
        source: dict = {}
        notes: list[str] = []
        for item in files or []:
            if not isinstance(item, dict):
                continue
            name, text, read_pdf = self._file_sources(item, token)
            readings = [("texte extrait par Open WebUI", parse_kid(text))] if text else []
            first = readings[0][1] if readings else {}
            # Open WebUI's document parser varies with the installation; when its
            # text does not pass every check (scenario tables are the first
            # casualty), the PDF itself is read again and the better reading kept.
            # A long text that is plainly not a KID (a prospectus, a report) is
            # not worth re-reading on every call.
            worth_rereading = not text or first.get("is_kid") or len(text) < 1000
            if kid_score(first) < _KID_FULL_SCORE and worth_rereading and read_pdf is not None:
                with contextlib.suppress(Exception):
                    readings.append(("PDF relu par l'outil", parse_kid(read_pdf())))
            kids = [(label, parsed) for label, parsed in readings if parsed.get("is_kid")]
            matching = [(label, parsed) for label, parsed in kids if isin in parsed["isins"]]
            if kids and not matching:
                others = sorted({code for _, parsed in kids for code in parsed["isins_lus"]})
                notes.append(
                    f"Document joint « {name} » ignore : "
                    + (f"c'est le DIC d'un autre code ({', '.join(others[:3])})." if others
                       else "l'ISIN demande n'y figure pas, la correspondance n'est pas verifiable.")
                )
            for label, parsed in matching:
                if kid_score(parsed) > kid_score(best):
                    best, source = parsed, {"origine": f"DIC joint « {name} »", "extraction": label}
        # Notes about other documents only matter when no attachment fitted.
        return best, source, ([] if best else notes)

    # ---- NAV series from Yahoo -----------------------------------------

    def _yahoo_search(self, query: str) -> list[dict]:
        response = requests.get(
            _YAHOO_SEARCH, params={"q": query, "quotesCount": 10, "newsCount": 0},
            headers=_HEADERS, timeout=int(self.valves.REQUEST_TIMEOUT),
        )
        if not response.ok:
            return []
        return [q for q in response.json().get("quotes", []) if q.get("quoteType") in ("MUTUALFUND", "ETF")]

    def _yahoo_series(self, symbol: str):
        path = self._cache_file(symbol + ".csv")
        meta_path = path + ".meta.json" if path else ""
        if path and os.path.exists(path) and time.time() - os.path.getmtime(path) < 86_400:
            cached = pd.read_csv(path, index_col=0, parse_dates=True).iloc[:, 0]
            # The quote currency lives beside the CSV: a CSV keeps no attributes,
            # and a series read back from cache used to report "devise n/c".
            currency = None
            with contextlib.suppress(OSError, ValueError), open(meta_path, encoding="utf-8") as handle:
                currency = json.load(handle).get("currency")
            return cached, currency
        response = requests.get(
            _YAHOO_CHART.format(symbol=symbol),
            params={"range": f"{int(self.valves.HISTORY_YEARS)}y", "interval": "1d"},
            headers=_HEADERS, timeout=int(self.valves.REQUEST_TIMEOUT),
        )
        if not response.ok:
            return None, None
        try:
            result = response.json()["chart"]["result"][0]
            closes = pd.Series(
                result["indicators"]["quote"][0]["close"],
                index=pd.to_datetime(result["timestamp"], unit="s").normalize(),
            ).dropna()
        except (KeyError, IndexError, TypeError, ValueError):
            return None, None
        closes = closes[closes > 0]
        closes = closes[~closes.index.duplicated(keep="last")]
        currency = result.get("meta", {}).get("currency")
        if path and len(closes):
            with contextlib.suppress(OSError):
                closes.rename("close").to_csv(path)
                with open(meta_path, "w", encoding="utf-8") as handle:
                    json.dump({"currency": currency, "symbol": symbol}, handle)
        return closes, currency

    def _find_series(self, isin: str, record: dict) -> dict:
        """Candidates by ISIN and by name, kept only if they reproduce the document."""
        reference = {y: v for y, v in (record.get("performances_annuelles") or {}).items()
                     if _num(v) is not None}
        name = record.get("nom") or ""
        queries = [isin, *search_queries(name)]
        candidates: dict[str, str] = {}
        via_isin: set[str] = set()
        for query in queries:
            for quote in self._yahoo_search(query):
                candidates.setdefault(quote["symbol"], quote.get("longname") or quote.get("shortname") or "")
                if query == isin:
                    via_isin.add(quote["symbol"])
            time.sleep(0.8)

        target = _normalise_name(name)
        ranked = sorted(candidates.items(),
                        key=lambda kv: (kv[0] not in via_isin, _normalise_name(kv[1]) != target))
        tested = []
        for symbol, label in ranked[:6]:
            closes, currency = self._yahoo_series(symbol)
            time.sleep(0.8)
            if closes is None or len(closes) < _TRADING_DAYS:
                tested.append({"symbol": symbol, "label": label, "status": "historique insuffisant"})
                continue
            years = calendar_returns(closes)
            common = [y for y in reference if y in years]
            gap = (sum(abs(years[y] - reference[y]) for y in common) / len(common)) if common else None
            tested.append({
                "symbol": symbol, "label": label, "currency": currency, "closes": closes,
                "common_years": len(common), "gap": gap,
                "same_name": _normalise_name(label) == target,
                "via_isin": symbol in via_isin,
                "status": "aucune annee commune avec le document" if gap is None else "",
            })
        validated = [t for t in tested if t.get("gap") is not None and t["gap"] <= _SIBLING_MATCH_PT]
        if validated:
            best = min(validated, key=lambda t: (t["gap"], not t["same_name"]))
            best["status"] = ("serie de la part exacte" if best["gap"] <= _EXACT_MATCH_PT and best["same_name"]
                              else "part soeur, valide comme proxy de risque")
            return {"chosen": best, "tested": tested, "reference_years": len(reference)}
        if not reference:
            # Nothing to validate against (no insurer history for this support).
            # A hit returned for the ISIN itself is strong identification, a name
            # match weaker; neither is a validation, and the status says which.
            usable = [t for t in tested if t.get("closes") is not None]
            by_isin = [t for t in usable if t.get("via_isin")]
            by_name = [t for t in usable if t.get("same_name")]
            if by_isin or by_name:
                pool = by_isin or by_name
                best = max(pool, key=lambda t: len(t["closes"]))
                best["status"] = (
                    "identifiee par l'ISIN, NON validee (pas d'historique de reference)"
                    if best in by_isin
                    else "identifiee par le nom seul, NON validee (pas d'historique de reference)"
                )
                return {"chosen": best, "tested": tested, "reference_years": 0}
        return {"chosen": None, "tested": tested, "reference_years": len(reference)}

    # ---- everything known about an ISIN ------------------------------------

    def _context(self, isin: str, files: list, request: Any = None) -> dict:
        ctx: dict = {"isin": isin, "base": None, "record": {}, "listes": [], "lists_loaded": False,
                     "geco": {}, "kid": {}, "kid_source": {}, "kid_notes": [], "errors": []}
        try:
            lists, list_errors = self._load_lists(request)
            ctx["lists_loaded"] = bool(lists)
            ctx["errors"] += [f"liste d'UC ignoree, {error}" for error in list_errors]
        except Exception as exc:
            lists = []
            ctx["errors"].append(f"listes d'UC de l'assureur non lues ({str(exc) or type(exc).__name__})")
        # Every contract list naming the ISIN, the widest list first: its
        # categories give the most meaningful peer group, and it is the one whose
        # descriptive fields (category, benchmark...) the card shows.
        matches = sorted(((base, base["supports"][isin]) for base in lists if isin in (base.get("supports") or {})),
                         key=lambda m: (m[1].get("sri") is None, -len(m[0].get("supports") or {})))
        ctx["listes"] = matches
        if matches:
            ctx["base"], ctx["record"] = matches[0]
        if ctx["record"].get("type") == "action":
            return ctx
        if self.valves.GECO_ENABLED:
            try:
                ctx["geco"] = self._geco(isin)
            except Exception as exc:
                ctx["geco_error"] = True
                ctx["errors"].append(f"base GECO de l'AMF injoignable ({str(exc) or type(exc).__name__})")
        token = (self.valves.OPENWEBUI_API_KEY or "").strip() or request_token(request)
        kid, source, notes = self._attached_kid(isin, files, token)
        ctx["kid_notes"] = notes
        geco_kids = (ctx["geco"] or {}).get("kids") or []
        if geco_kids:
            try:
                geco_kid = self._geco_kid(geco_kids[0])
            except Exception as exc:
                geco_kid = {}
                ctx["errors"].append(f"DIC GECO non telecharge ({str(exc) or type(exc).__name__})")
            geco_date = geco_kids[0].get("dateEffet")
            # A KID the adviser attached is taken as the one in force, as long as
            # it could be read; the AMF copy then only serves as a cross-check.
            if not kid.get("sri") and kid_score(geco_kid) > kid_score(kid):
                kid, source = geco_kid, {
                    "origine": f"DIC depose a l'AMF (GECO), en vigueur au {geco_date or 'n/c'}",
                    "date": geco_date, "geco": True,
                }
            elif geco_kid.get("sri") and kid.get("sri") and geco_kid["sri"] != kid["sri"]:
                ctx["geco_kid_sri"] = (geco_kid["sri"], geco_date)
        ctx["kid"], ctx["kid_source"] = kid, source
        return ctx

    @staticmethod
    def _contract(base: dict) -> str:
        source = base.get("source") or {}
        return (source.get("contrat") or source.get("document") or "liste d'UC").strip()

    @staticmethod
    def _list_flag(constraint: str) -> bool:
        """Constraints that come from a contract list rather than from the fund itself."""
        return constraint.startswith(("valeur liquidative", "souscription soumise", "non accessible en allocation",
                                      "non eligible", "figure dans la", "entree recente", "eligible PEA-PME"))

    @staticmethod
    def _nature(ctx: dict) -> str:
        compartment = (ctx["geco"] or {}).get("compartment") or {}
        nature = f"{compartment.get('prdNatureLib') or ''} {compartment.get('prdSsNatureLib') or ''}".strip()
        return nature or ctx["kid"].get("vehicule") or ""

    @staticmethod
    def _name(ctx: dict) -> str:
        record, geco = ctx["record"], ctx["geco"] or {}
        if record.get("nom"):
            return record["nom"]
        if geco:
            share_name = (geco["share"].get("parNom") or "").strip()
            if re.search(r"renseigner", share_name, re.I):
                share_name = ""
            fund = (geco["compartment"].get("cmpNom") or "").strip()
            already = share_name and re.search(rf"(?<!\w){re.escape(share_name.upper())}(?!\w)", fund.upper())
            return f"{fund} {share_name}".strip() if share_name and not already else fund
        return ""

    @staticmethod
    def _retained_sri(ctx: dict) -> tuple[int | None, str]:
        """The SRI the grid uses. Two documents that disagree: the higher one, until
        the adviser checks which is in force."""
        kid_sri = ctx["kid"].get("sri")
        list_sris = [record.get("sri") for _, record in ctx["listes"] if record.get("sri")]
        others = [s for s in (*list_sris, (ctx.get("geco_kid_sri") or (None,))[0]) if s]
        if (kid_sri and any(s != kid_sri for s in others)) or len(set(list_sris)) > 1:
            return max([s for s in (kid_sri, *others) if s]), "le plus eleve des documents, tant que l'ecart n'est pas leve"
        if kid_sri:
            return kid_sri, ctx["kid_source"].get("origine", "DIC")
        if list_sris:
            return list_sris[0], "liste de l'assureur"
        return None, ""

    # ------------------------------------------------------------------
    # tools exposed to the model
    # ------------------------------------------------------------------

    async def get_uc_card(
        self,
        isin: str,
        __files__: list | None = None,
        __metadata__: dict | None = None,
        __request__: Any = None,
        __event_emitter__: Callable[[dict], Any] | None = None,
    ) -> str:
        """
        Returns the card of a unit-linked fund from its ISIN alone: identity, legal
        nature and status, PRIIPs risk class (SRI 1-7), recommended holding period,
        PRIIPs costs and cost impact, performance scenarios, SFDR article, official NAV
        performance, and — for each Swiss Life contract listing the fund — contract fees,
        retrocessions and category ranking. French funds are read from the AMF database; for other
        funds, a KID PDF attached to the conversation is read. Ends with tagged signals
        for the suitability screen. Call this first for any ISIN.

        :param isin: The 12-character ISIN code of the support, for example LU0099574567.
        """
        if _IMPORT_ERROR:
            return _IMPORT_ERROR
        code = (isin or "").strip().upper().replace(" ", "")
        if not isin_is_valid(code):
            return (
                f"ISIN invalide : '{isin}'. Le code ne respecte pas le format ISO 6166 ou "
                "sa cle de controle est fausse — le plus souvent une faute de frappe. "
                "Verifier le code sur le DIC plutot que de chercher un fonds approchant."
            )
        files = __files__ or (__metadata__ or {}).get("files") or []
        await self._emit(__event_emitter__, f"Fiche {code} : DIC, AMF, liste assureur...")
        try:
            ctx = await self._run(self._context, code, files, __request__)
        except Exception as exc:
            # The status line must not spin forever on a failed call.
            await self._emit(__event_emitter__, "Echec de la collecte", done=True)
            return f"Echec de la collecte des donnees : {str(exc) or type(exc).__name__}"
        record, geco, kid = ctx["record"], ctx["geco"] or {}, ctx["kid"]

        if record.get("type") == "action":
            await self._emit(__event_emitter__, "Action, pas un fonds", done=True)
            return (
                f"{code} designe une **action**, pas un fonds : {record.get('nom')} "
                f"({record.get('pays') or 'n/c'}, {record.get('devise') or 'n/c'}, secteur "
                f"{record.get('secteur') or 'n/c'}, notation emetteur "
                f"{record.get('notation_emetteur') or 'n/c'}). L'analyse d'UC ne s'applique "
                "pas : utiliser l'analyse d'actions (skill trading-agents)."
            )

        nav: dict = {}
        if geco:
            await self._emit(__event_emitter__, "Valeurs liquidatives officielles...")
            try:
                nav = await self._run(self._geco_nav, geco["share"])
            except Exception as exc:
                ctx["errors"].append(f"VL officielles indisponibles ({str(exc) or type(exc).__name__})")

        in_base = bool(record.get("sri") is not None)
        if not in_base and not geco and not kid:
            figi = {}
            with contextlib.suppress(Exception):
                figi = await self._run(self._openfigi, code)
            lines = [f"# {code} — aucune donnee reglementaire trouvee"]
            if figi:
                lines += ["", "Identification (OpenFIGI) :", f"- Nom : {figi.get('name')}",
                          f"- Type : {figi.get('securityType')} / {figi.get('securityType2')}"]
            lines += [
                "",
                ("- Consultation de la base GECO de l'AMF desactivee" if not self.valves.GECO_ENABLED
                 else "- **Base GECO de l'AMF injoignable** : l'absence de donnees ne signifie pas que le fonds "
                      "n'y est pas — relancer plus tard pour un fonds francais"
                 if ctx.get("geco_error")
                 else "- Le fonds n'est pas dans la base GECO de l'AMF (reservee aux fonds de droit "
                      "francais : les fonds luxembourgeois ou irlandais n'y sont pas)")
                + (" ; il n'est pas non plus dans les listes d'UC de l'assureur." if ctx["lists_loaded"]
                   else " ; les listes d'UC de l'assureur ne sont pas disponibles."),
                "- Aucun DIC joint a la conversation ne porte cet ISIN.",
                "",
                "**Pour poursuivre : joindre le DIC (document d'informations cles) de la part en PDF** "
                "a la conversation, puis relancer la fiche. Ne pas estimer le SRI, les frais ni les "
                "performances.",
            ]
            if record.get("liste_sorties"):
                lines.append("- Ce code figure uniquement dans la liste des **sorties** de la liste de l'assureur.")
            lines += [f"- {note}" for note in ctx["kid_notes"] + ctx["errors"]]
            await self._emit(__event_emitter__, "Aucune donnee reglementaire", done=True)
            return self._cap("\n".join(lines))

        compartment, share = geco.get("compartment") or {}, geco.get("share") or {}
        nature = self._nature(ctx)
        name = self._name(ctx) or code
        family = vehicle_family({**record, "nom": name}, nature)
        sri, sri_basis = self._retained_sri(ctx)
        signals: list[str] = []

        sources = []
        if ctx["kid_source"]:
            sources.append(ctx["kid_source"]["origine"] + (f" ({ctx['kid_source']['extraction']})"
                                                           if ctx["kid_source"].get("extraction") else ""))
        if geco:
            sources.append("base GECO de l'AMF")
        for base, _ in ctx["listes"]:
            sources.append(f"liste d'UC « {self._contract(base)} » du {base['source'].get('date_document')}")
        lines = [f"# {name} — {code}", f"*Sources : {' ; '.join(sources)}*", "", "## Identite"]
        if compartment:
            # A fund in liquidation keeps its shares "Vivant": the worse of the two.
            levels = [s for s in (compartment.get("cmpStatutLib"), share.get("parStatutLib")) if s]
            status = next((s for s in levels if s != "Vivant"), levels[0] if levels else "n/c")
            lines += [
                f"- Nature juridique (AMF) : {nature or 'n/c'} — famille : **{family}**",
                f"- Classification AMF : {compartment.get('cmpClssFndAmfLib') or 'n/c'}",
                f"- Societe de gestion : {compartment.get('gestionnaire') or 'n/c'}",
                f"- Domicile : {compartment.get('prdDomcltnLib') or compartment.get('prdDomcltn') or 'n/c'}",
                "- Part : " + ", ".join(filter(None, [
                    # GECO keeps placeholders such as "A Renseigner 49642" for unnamed shares.
                    None if re.search(r"renseigner", share.get("parNom") or "", re.I) else share.get("parNom"),
                    share.get("parRefDevCode"), share.get("parAffctnRevnuLib"),
                    f"creee le {share['parDateCreation']}" if share.get("parDateCreation") else None,
                ])),
                f"- Statut : {status} — "
                f"souscripteurs : {compartment.get('cmpSouscrDedLib') or 'n/c'}",
            ]
            if nav.get("part_size"):
                lines.append(f"- Encours de la part : {nav['part_size'] / 1e6:,.1f} M"
                             f"{' ' + nav['currency'] if nav.get('currency') else ''} au {nav.get('part_size_date')}"
                             .replace(",", " "))
        if in_base:
            lines += [
                f"- Categorie (liste assureur) : {record.get('categorie') or 'n/c'}",
                f"- Forme juridique : {record.get('forme_juridique') or 'n/c'}"
                + ("" if compartment else f" — famille : **{family}**"),
                f"- Indice de reference : {record.get('indice_reference') or 'aucun'}",
            ]
            if not compartment:
                lines += [f"- Societe de gestion : {record.get('societe_gestion') or 'n/c'}",
                          f"- Devise : {record.get('devise') or 'n/c'}"]
        if not compartment and not in_base:
            figi = {}
            with contextlib.suppress(Exception):
                figi = await self._run(self._openfigi, code)
            if figi:
                name = name if name != code else (figi.get("name") or code)
                lines[0] = f"# {name} — {code}"
                lines.append(f"- Identification (OpenFIGI) : {figi.get('name')}, {figi.get('securityType')}")
            lines.append("- Fonds absent de la base GECO de l'AMF et de la liste assureur : identite, statut et "
                         "public vise a verifier sur le DIC et le prospectus")

        lines += ["", "## Risque et durabilite"]
        if sri:
            lines.append(f"- **SRI retenu : {sri} / 7** ({sri_basis})")
        else:
            lines.append("- **SRI inconnu** : ni le DIC ni la liste de l'assureur ne l'ont fourni")
        list_sris = {self._contract(base): rec.get("sri") for base, rec in ctx["listes"] if rec.get("sri")}
        if (kid.get("sri") and any(s != kid["sri"] for s in list_sris.values())) or len(set(list_sris.values())) > 1:
            detail = ([f"DIC {kid['sri']}"] if kid.get("sri") else []) + [f"liste « {c} » {s}" for c, s in list_sris.items()]
            lines.append(f"  - {' ; '.join(detail)}")
            signals.append(f"[COUVERTURE] SRI different selon les documents ({', '.join(detail)}) : verifier le DIC "
                           "en vigueur")
        if ctx.get("geco_kid_sri"):
            geco_sri, geco_date = ctx["geco_kid_sri"]
            lines.append(f"  - DIC joint : {kid['sri']} / 7 ; DIC depose a l'AMF ({geco_date}) : {geco_sri} / 7")
            signals.append(f"[COUVERTURE] SRI du DIC joint ({kid['sri']}) different de celui du DIC depose a l'AMF "
                           f"({geco_sri}) : verifier lequel est en vigueur")
        if kid.get("sri_source") == "qualificatif reglementaire":
            lines.append("  - lu dans le libelle reglementaire du niveau de risque (l'echelle du DIC est graphique)")
        if kid.get("duree_detention_ans"):
            lines.append(f"- Periode de detention recommandee : {duration_text(kid['duree_detention_ans'])}")
        sfdr = f"article {kid['sfdr_article']}" if kid.get("sfdr_article") else record.get("sfdr")
        lines.append(f"- Classification SFDR : {sfdr or 'n/c'}")
        if record.get("label"):
            lines.append(f"- Label(s) : {record['label']}")

        if kid:
            lines += self._kid_cost_lines(kid, signals)

        costs = record.get("couts_performances") or {}
        priced = [(base, rec) for base, rec in ctx["listes"] if rec.get("couts_performances")]
        if priced:
            # One row per contract: the fund's own fees are the same everywhere,
            # the contract fee and hence the net-of-all-fees return are not.
            lines += [
                "",
                "## Frais annuels et performance nette selon les listes de l'assureur",
                "| Contrat (liste du) | Frais courants du support (dont retrocedes) | Frais du contrat | Total annuel "
                "| Perf. nette de tous frais N-1 | idem, moyenne 5 ans |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
            for base, rec in priced:
                c = rec["couts_performances"]
                retro = (f" ({_pts(c['frais_uc_dont_retrocession'])})"
                         if _num(c.get("frais_uc_dont_retrocession")) is not None else "")
                lines.append(
                    f"| {self._contract(base)} ({base['source'].get('date_document')}) | {_pts(c.get('frais_uc'))}{retro} "
                    f"| {_pts(c.get('frais_contrat'))} | **{_pts(c.get('frais_totaux'))}** "
                    f"| {_pts(c.get('perf_nette_nette_n1'))} | {_pts(c.get('perf_nette_nette_5a'))} |"
                )
            lines.append(
                # Not a ceiling on the fees above: the annex column is the
                # prospectus management fee, while the cost table gives total
                # ongoing charges.
                f"\nCommission de gestion maximale prevue au prospectus : {_pts(record.get('frais_gestion_max'))} "
                "*(composante des frais courants, hors frais de fonctionnement)*"
            )
            if kid.get("frais_gestion_exploitation") is not None and kid.get("couts_transaction") is not None:
                kid_running = kid["frais_gestion_exploitation"] + kid["couts_transaction"]
                if _num(costs.get("frais_uc")) is not None and abs(kid_running - costs["frais_uc"]) > 0.1:
                    lines.append(f"\n*Frais courants du DIC (gestion + transaction) : {_pts(kid_running)}, contre "
                                 f"{_pts(costs['frais_uc'])} sur la liste : date d'arrete ou perimetre (couts de "
                                 "transaction) differents. Le DIC en vigueur fait foi.*")
        elif kid:
            why = ("support absent des listes d'UC de l'assureur" if ctx["lists_loaded"]
                   else "listes d'UC de l'assureur non disponibles")
            lines += ["", f"*Frais du contrat d'assurance et part retrocedee au distributeur : non fournis ({why}) "
                      "— a obtenir aupres de l'assureur.*"]

        lines += self._performance_lines(
            record, costs, nav, self._contract(ctx["base"]) if ctx["base"] else "",
            (ctx["base"] or {}).get("source", {}).get("date_document") or "",
        )

        peers = (ctx["base"].get("peers") or {}).get(record.get("categorie") or "") if in_base else None
        if peers and peers["n"] >= 5:
            fee_rank = self._percentile(peers["fees"], _num(costs.get("frais_uc")))
            perf_rank = self._percentile(peers["perf5"], _num(costs.get("perf_nette_5a")))
            lines += [
                "",
                f"## Comparaison a la categorie ({peers['n']} supports « {record.get('categorie')} » de la liste "
                f"« {self._contract(ctx['base'])} »)",
                f"- Frais courants du support : {_pts(costs.get('frais_uc'))} contre une mediane de "
                f"{_pts(peers['median_fees'])}"
                + (f" — plus cher que {fee_rank:.0%} des supports de la categorie" if fee_rank is not None else ""),
            ]
            if perf_rank is not None:
                lines.append(f"- Performance nette 5 ans : {_pts(costs.get('perf_nette_5a'))} contre une mediane de "
                             f"{_pts(peers['median_perf5'])} — meilleure que {perf_rank:.0%} de la categorie")
            if fee_rank is not None and fee_rank > 0.75:
                signals.append("[RISQUE] Frais courants plus eleves que 75% de la categorie")
            if perf_rank is not None and perf_rank < 0.25:
                signals.append("[RISQUE] Performance nette 5 ans moins bonne que 75% de la categorie")

        signals += self._identity_signals(ctx, family, sri, nav)
        constraints = self._constraints(record, family, sri, nav)
        if len(ctx["listes"]) > 1:
            # Eligibility flags belong to a contract: a fund can need an amendment
            # on one contract and not on another, so each flag names its list.
            constraints = [f"« {self._contract(ctx['base'])} » : {c}" if self._list_flag(c) else c
                           for c in constraints]
            for base, rec in ctx["listes"][1:]:
                constraints += [f"« {self._contract(base)} » : {c}"
                                for c in self._constraints(rec, family, None, {}) if self._list_flag(c)]
        tail: list[str] = []
        if constraints:
            tail += ["", "## Contraintes et points d'eligibilite"] + [f"- {c}" for c in constraints]
        tail += ["", "## Signaux pour le crible"] + ([f"- {s}" for s in signals] or ["- aucun"])
        notes = ctx["kid_notes"] + ctx["errors"]
        if notes:
            tail += ["", "Remarques de collecte :"] + [f"- {n}" for n in notes]
        await self._emit(__event_emitter__, "Fiche chargee", done=True)
        return self._cap_card(lines, tail)

    @staticmethod
    def _kid_cost_lines(kid: dict, signals: list[str]) -> list[str]:
        horizons = kid.get("horizons_ans") or []
        first = duration_text(horizons[0] if horizons else 1)
        horizon = duration_text(horizons[1] if len(horizons) > 1 else kid.get("duree_detention_ans"))
        perf = kid.get("commission_performance")
        rate = kid.get("commission_performance_taux")
        perf_text = _pts(perf) + " / an" if perf is not None else "n/c"
        if rate is not None:
            perf_text += f" (taux contractuel : {_pts(rate)} de la surperformance)"
        lines = [
            "",
            "## Couts PRIIPs (DIC, pour 10 000 EUR investis)",
            "| Poste | Valeur |",
            "| --- | --- |",
            f"| Couts d'entree (maximum) | {_pts(kid.get('couts_entree'))} |",
            f"| Couts de sortie | {_pts(kid.get('couts_sortie'))} |",
            f"| Frais de gestion et autres frais administratifs et d'exploitation | {_pts(kid.get('frais_gestion_exploitation'))} / an |",
            f"| Couts de transaction | {_pts(kid.get('couts_transaction'))} / an |",
            f"| Commissions liees aux resultats | {perf_text} |",
        ]
        if len(horizons) == 1:
            lines.append(f"| **Incidence des couts**, sortie apres {first} | {_pts(kid.get('incidence_couts_periode'))} |")
        else:
            lines += [
                f"| **Incidence annuelle des couts**, sortie apres {first} | {_pts(kid.get('incidence_couts_1an'))} |",
                f"| **Incidence annuelle des couts**, sortie apres {horizon} | {_pts(kid.get('incidence_couts_periode'))} |",
            ]
        if kid.get("frais_gestion_exploitation") is not None and kid.get("couts_transaction") is not None:
            lines.append(f"\nFrais courants (gestion + transaction) : "
                         f"**{_pts(kid['frais_gestion_exploitation'] + kid['couts_transaction'])} / an**")
        if kid.get("couts_coherents") is False:
            lines.append(f"\n*Les postes ne reconstituent pas l'incidence a 1 an (ecart de {kid['ecart_couts']:+.2f} pt) : "
                         "lecture a confirmer sur le DIC, ou couts accessoires non detailles.*")
            signals.append("[COUVERTURE] Couts du DIC non reconcilies : a confirmer sur le document")
        elif kid.get("couts_coherents") is None:
            riy = ("incidence_couts_periode", "incidence des couts") if len(horizons) == 1 \
                else ("incidence_couts_1an", f"incidence a {first}")
            missing = [label for key, label in (("couts_entree", "entree"), ("frais_gestion_exploitation", "gestion"),
                                                ("couts_transaction", "transaction"), riy) if kid.get(key) is None]
            if missing:
                lines.append(f"\n*Non lus dans le DIC : {', '.join(missing)} — a relever sur le document.*")
                signals.append("[COUVERTURE] Couts du DIC incomplets : " + ", ".join(missing))

        scenarios = kid.get("scenarios") or {}
        if scenarios:
            labels = {"tensions": "Tensions", "defavorable": "Defavorable",
                      "intermediaire": "Intermediaire", "favorable": "Favorable"}
            single = all(scenarios[k][1] is None for k in _SCENARIOS)
            lines += ["", "## Scenarios de performance (DIC, rendement apres couts, annuel moyen au-dela d'un an)",
                      f"| Scenario | Sortie apres {first} |" + ("" if single else f" Sortie apres {horizon} |"),
                      "| --- | --- |" + ("" if single else " --- |")]
            lines += [f"| {labels[k]} | {_pts(scenarios[k][0])} |" + ("" if single else f" {_pts(scenarios[k][1])} |")
                      for k in _SCENARIOS]
        elif kid.get("scenarios_illisibles"):
            lines.append("\n*Scenarios du DIC non restitues : le tableau extrait n'est pas ordonne comme le "
                         "prevoit la reglementation, il est donc omis plutot que publie faux.*")
        return lines

    @staticmethod
    def _performance_lines(record: dict, costs: dict, nav: dict, contract: str = "", list_date: str = "") -> list[str]:
        lines: list[str] = []
        history = record.get("performances_annuelles") or {}
        perf_keys = ("perf_brute_n1", "perf_brute_5a", "perf_nette_n1", "perf_nette_5a")
        has_figures = any(_num(costs.get(k)) is not None for k in perf_keys)
        if has_figures or any(v is not None for v in history.values()):
            # Without a history table (ETF annex), N-1 is the year before the list's.
            latest_year = max(history) if history else (
                str(int(list_date[:4]) - 1) if list_date[:4].isdigit() else "N-1")
            lines += ["", f"## Performances (liste de l'assureur{f' « {contract} »' if contract else ''})"]
        if has_figures:
            lines += [
                f"| | Annee {latest_year} | Moyenne annualisee 5 ans |",
                "| --- | --- | --- |",
                f"| Brute de frais du support | {_pts(costs.get('perf_brute_n1'))} | {_pts(costs.get('perf_brute_5a'))} |",
                f"| Nette de frais du support | {_pts(costs.get('perf_nette_n1'))} | {_pts(costs.get('perf_nette_5a'))} |",
                f"| Nette de tous frais (support + contrat) | {_pts(costs.get('perf_nette_nette_n1'))} | {_pts(costs.get('perf_nette_nette_5a'))} |",
            ]
        if has_figures or any(v is not None for v in history.values()):
            if any(v is not None for v in history.values()):
                years = sorted(history, reverse=True)
                lines += ["", "Historique net de frais du support :",
                          "| " + " | ".join(years) + " |", "|" + " --- |" * len(years),
                          "| " + " | ".join("creation" if history[y] is None else _pts(history[y]) for y in years) + " |"]
            if costs.get("creation"):
                lines.append(f"\n*Support cree en {costs['creation']} : historique partiel.*")
        elif costs.get("creation"):
            lines.append(f"\n*Support cree en {costs['creation']} : pas encore de performance publiee.*")
        if not lines and nav.get("closes") is not None and calendar_returns(nav["closes"]):
            closes = nav["closes"]
            years = calendar_returns(closes)
            recent = sorted(years, reverse=True)[:6]
            lines += ["", "## Performances (valeurs liquidatives officielles AMF, nettes de frais du support)"]
            if recent:
                lines += ["| " + " | ".join(recent) + " |", "|" + " --- |" * len(recent),
                          "| " + " | ".join(_pts(years[y]) for y in recent) + " |"]
            span = (closes.index[-1] - closes.index[0]).days / 365.25
            if span >= 5:
                start = closes.asof(closes.index[-1] - pd.DateOffset(years=5))
                lines.append(f"- Moyenne annualisee sur 5 ans au {closes.index[-1]:%Y-%m-%d} : "
                             f"{_pts(((closes.iloc[-1] / start) ** (1 / 5) - 1) * 100)}")
            if nav.get("coupons"):
                lines.append(
                    f"- {nav['coupons_reinvested']} distribution(s) reinvestie(s) sur {nav['coupons']}"
                    + ("" if nav["coupons_reinvested"] == nav["coupons"]
                       else " — les autres, en devise, ne sont pas reinvesties : performance minoree")
                )
            lines.append("- *Hors frais du contrat d'assurance : la performance percue par l'assure est inferieure.*")
        return lines

    @staticmethod
    def _identity_signals(ctx: dict, family: str, sri: int | None, nav: dict) -> list[str]:
        record, geco, kid, source = ctx["record"], ctx["geco"] or {}, ctx["kid"], ctx["kid_source"]
        compartment, share = geco.get("compartment") or {}, geco.get("share") or {}
        signals = []
        # A fund in liquidation keeps its shares "Vivant": both levels are read.
        status = next((s for s in (compartment.get("cmpStatutLib"), share.get("parStatutLib"))
                       if s and s != "Vivant"), None)
        if status:
            signals.append(f"[IDENTITE] Part au statut « {status} » dans la base de l'AMF : plus souscriptible")
        nature = (compartment.get("prdNatureLib") or "").lower()
        dedicated = (compartment.get("cmpSouscrDedCode") or "TOUS") != "TOUS"
        if "professionnel" in nature or dedicated:
            what = (f"Fonds professionnel ({compartment.get('prdNature') or 'FIA'})" if "professionnel" in nature
                    else f"Fonds {(compartment.get('cmpSouscrDedLib') or 'dedie').lower()}")
            # An insurer may hold such a fund in a unit-linked contract as the
            # professional investor; listed on its contract, eligibility is settled.
            signals.append(
                f"[INFO] {what} : accessible ici parce que l'assureur le reference sur son contrat"
                if record.get("sri") is not None
                else f"[IDENTITE] {what} : reserve a des investisseurs designes ; eligible en unite de compte "
                     "seulement si l'assureur le reference sur le contrat"
            )
        if "formule" in (compartment.get("cmpClssFndAmfLib") or "").lower():
            signals.append("[INFO] Fonds a formule : son profil de gain depend d'une formule, a lire aussi avec "
                           "l'analyse de produits structures (autocall-analysis)")
        if record.get("liste_sorties") and record.get("dans_liste_active"):
            signals.append("[IDENTITE] Support a la fois dans la liste des sorties et dans la liste active : "
                           "eligibilite a confirmer aupres de l'assureur")
        if any(error.startswith("listes d'UC") for error in ctx["errors"]):
            signals.append("[INFO] Listes d'UC de l'assureur non lues : frais du contrat, retrocessions et "
                           "comparaison a la categorie absents")
        if ctx.get("geco_error"):
            # With an SRI from another document the verdicts stand; without one,
            # the outage is the reason there is nothing to judge.
            signals.append(("[INFO]" if sri else "[COUVERTURE]") + " Base GECO de l'AMF injoignable : DIC et valeurs "
                           "liquidatives officielles non consultes, relancer plus tard")
        if not kid:
            where = ("aucun DIC depose a l'AMF pour cette part" if geco
                     else "DIC non consulte (base GECO injoignable) et aucun DIC joint" if ctx.get("geco_error")
                     else "aucun DIC joint a la conversation")
            # With the insurer's SRI the verdicts stand; only the PRIIPs costs and
            # scenarios are missing, which the adviser can fill by attaching the KID.
            signals.append(
                f"[INFO] {where[0].upper() + where[1:]} : couts PRIIPs et scenarios absents, joindre le DIC pour les obtenir"
                if record.get("sri") is not None
                else f"[COUVERTURE] {where[0].upper() + where[1:]} : SRI, couts et scenarios manquants — joindre le DIC"
            )
        if source.get("geco"):
            age = _days_since(source.get("date"))
            if age and age > _KID_MAX_AGE_DAYS:
                signals.append(f"[COUVERTURE] DIC depose a l'AMF date du {source['date']} (plus de 14 mois) : "
                               "une version plus recente existe probablement")
        elif kid.get("date_la_plus_recente"):
            age = _days_since(kid["date_la_plus_recente"])
            if age and age > _KID_MAX_AGE_DAYS:
                signals.append(f"[COUVERTURE] Le DIC joint ne cite aucune date posterieure au "
                               f"{kid['date_la_plus_recente']} : verifier qu'il est en vigueur")
        if source.get("geco") and ctx["isin"] not in kid.get("isins", []):
            signals.append("[COUVERTURE] Le DIC depose a l'AMF ne mentionne pas l'ISIN de la part"
                           + (f" (codes lus : {', '.join(kid['isins_lus'][:3])})" if kid.get("isins_lus") else "")
                           + " : document a verifier")
        if (ctx["base"] or {}).get("source") and record.get("sri") is not None:
            age = _days_since(ctx["base"]["source"].get("date_document"))
            # The insurer republishes the list monthly; past six weeks, fees or
            # eligibility may have moved without the base knowing.
            if age and age > 45:
                # Stale fees and eligibility are a disclosure matter; an SRI over
                # six months old with no KID to confirm it is a coverage gap.
                tag = "[COUVERTURE]" if not kid.get("sri") and age > 182 else "[INFO]"
                signals.append(f"{tag} Liste de l'assureur vieille de {age / 30.4:.0f} mois : "
                               + ("SRI, frais et eligibilite" if tag == "[COUVERTURE]" else "frais et eligibilite")
                               + " a confirmer sur la liste en vigueur")
        creation = share.get("parDateCreation")
        created_age = _days_since(creation)
        list_creation = str((record.get("couts_performances") or {}).get("creation") or "")
        if created_age is not None and created_age < 3 * 365:
            signals.append(f"[COUVERTURE] Part creee le {creation} : moins de 3 ans d'historique")
        elif created_age is None and list_creation.isdigit() and int(list_creation) >= time.localtime().tm_year - 2:
            signals.append(f"[COUVERTURE] Support cree en {list_creation} : moins de 3 ans d'historique")
        if family in _UNLISTED:
            signals.append(f"[RISQUE] Liquidite reduite : {family}")
        elif record.get("valorisation"):
            signals.append(f"[RISQUE] Liquidite reduite : valeur liquidative {record['valorisation']}")
        elif nav.get("closes") is not None and len(nav["closes"]) > 20:
            frequency = risk_measures(nav["closes"])["frequency"]
            if frequency != "quotidienne":
                signals.append(f"[RISQUE] Liquidite reduite : valeur liquidative {frequency}")
        currency = (share.get("parRefDevCode") or record.get("devise") or "").upper()
        share_name = f"{share.get('parNom') or ''} {record.get('nom') or ''}"
        if currency and currency != "EUR" and not re.search(r"\bH\b|HEDGED|COUVERT", share_name.upper()):
            signals.append(f"[RISQUE] Part libellee en {currency} sans couverture apparente : risque de change")
        if sri is None:
            signals.append("[COUVERTURE] SRI inconnu : aucun verdict possible sans lui")
        return signals

    @staticmethod
    def _constraints(record: dict, family: str, sri: int | None, nav: dict) -> list[str]:
        constraints = []
        if record.get("valorisation"):
            constraints.append(f"valeur liquidative {record['valorisation']} : liquidite reduite")
        if record.get("avenant_specifique_requis"):
            constraints.append("souscription soumise a un avenant specifique")
        if record.get("hors_allocation_libre"):
            constraints.append("non accessible en allocation libre ni en arbitrages libres")
        if record.get("non_eligible_retraite_collective"):
            constraints.append("non eligible aux produits de retraite collective")
        if record.get("non_eligible_retraite_et_contrats_fiscaux_specifiques"):
            constraints.append("non eligible aux produits retraite et aux contrats a fiscalite specifique (PEP, PEA assurance...)")
        if sri and sri > 2:
            constraints.append("non accessible aux souscripteurs de 85 ans et plus ni aux majeurs proteges "
                               "(reserve aux SRI 1 a 2, sauf accord du juge des tutelles)")
        if record.get("liste_sorties"):
            constraints.append(
                "figure dans la **liste des sorties** du document"
                + (" alors qu'il est encore dans la liste active" if record.get("dans_liste_active") else "")
            )
        if record.get("liste_entrees"):
            constraints.append("entree recente au contrat")
        if record.get("pea_pme"):
            constraints.append("eligible PEA-PME sur ce contrat")
        if family in _UNLISTED:
            constraints.append(f"{family} : pas de cotation quotidienne, risque de liquidite et de valorisation propre")
        if nav.get("break_date"):
            constraints.append(f"rupture dans l'historique des VL le {nav['break_date']} (operation sur titres) : "
                               "historique retenu a partir de cette date")
        return constraints

    # ------------------------------------------------------------------
    # the whole universe of one list
    # ------------------------------------------------------------------

    _NOT_SUBSCRIBABLE = ("fonds dedies reserves", "fonds monetaire n ayant pas vocation a etre souscrit")

    @staticmethod
    def _universe_flags(record: dict) -> list[str]:
        flags = []
        if record.get("hors_allocation_libre"):
            flags.append("hors allocation libre")
        if record.get("non_eligible_retraite_et_contrats_fiscaux_specifiques"):
            flags.append("non eligible retraite et contrats fiscaux")
        elif record.get("non_eligible_retraite_collective"):
            flags.append("non eligible retraite collective")
        if record.get("avenant_specifique_requis"):
            flags.append("avenant")
        if record.get("valorisation"):
            flags.append("VL non quotidienne")
        if record.get("liste_sorties"):
            flags.append("liste des sorties")
        elif record.get("liste_entrees"):
            flags.append("entree recente")
        created = (record.get("couts_performances") or {}).get("creation")
        if created:
            flags.append(f"cree en {created}")
        # Article 9 only: 8 is the norm of these lists (667 of 829 in annex IA), and
        # repeating it on every line cost a page of listing for no information.
        if "9" in str(record.get("sfdr") or ""):
            flags.append("SFDR 9")
        return flags

    @staticmethod
    def _universe_row(record: dict) -> str:
        costs = record.get("couts_performances") or {}
        total, retro = _num(costs.get("frais_totaux")), _num(costs.get("frais_totaux_dont_retrocession"))
        if total is None and _num(costs.get("frais_uc")) is not None:
            fees = f"{_pts(costs.get('frais_uc'))} support seul"
        else:
            fees = _pts(total) + (f" ({_pts(retro)})" if retro is not None else "")
        history = {y: _num(v) for y, v in (record.get("performances_annuelles") or {}).items() if _num(v) is not None}
        # A "worst year" out of one or two published years says nothing.
        worst = min(history.items(), key=lambda item: item[1]) if len(history) >= 3 else None
        cells = [
            record.get("isin") or "",
            (record.get("nom") or "").replace("|", "/"),
            str(record.get("sri") or "?"),
            fees,
            _pts(costs.get("perf_nette_nette_5a")) if _num(costs.get("perf_nette_nette_5a")) is not None else "-",
            f"{_pts(worst[1])} ({worst[0]})" if worst else "-",
            ", ".join(Tools._universe_flags(record)),
        ]
        return "| " + " | ".join(cells) + " |"

    def _universe(self, request: Any, contract: str, annex: str, category: str, max_sri: int, page: int) -> str:
        lists, errors = self._load_lists(request)
        if not lists:
            return "Aucune liste d'UC lisible dans la connaissance." + (f" ({'; '.join(errors[:3])})" if errors else "")

        def catalogue(bases: list[dict], intro: str) -> str:
            lines = [intro, "", "| Contrat (liste) | Document | Date | Supports par annexe |", "| --- | --- | --- | --- |"]
            for base in bases:
                src = base.get("source") or {}
                counts: dict[str, int] = {}
                for record in (base.get("supports") or {}).values():
                    if record.get("type") == "action" or not record.get("categorie"):
                        continue
                    counts[record.get("annexe") or "?"] = counts.get(record.get("annexe") or "?", 0) + 1
                lines.append(f"| {src.get('contrat') or 'n/c'} | {src.get('document') or 'n/c'} | "
                             f"{src.get('date_document') or 'n/c'} | "
                             + ", ".join(f"{k} : {v}" for k, v in sorted(counts.items())) + " |")
            lines.append("\nRappeler avec `contract` = un fragment du nom du contrat ou du document.")
            return "\n".join(lines)

        wanted = _normalise_name(contract)
        if not wanted:
            return catalogue(lists, "# Listes d'UC disponibles")
        # Word by word rather than as one substring: « retraite et epargne » must find
        # « Produits de retraite et d'épargne », whose apostrophe splits the words.
        def names(base: dict) -> list[set[str]]:
            src = base.get("source") or {}
            return [set(_normalise_name(src.get(k) or "").split()) for k in ("contrat", "document")]

        matches = [b for b in lists if any(set(wanted.split()) <= words for words in names(b))]
        if not matches:
            return catalogue(lists, f"# Aucune liste ne correspond a « {contract} »")
        if len(matches) > 1:
            return catalogue(matches, f"# Plusieurs listes correspondent a « {contract} » : preciser")
        base = matches[0]
        src = base.get("source") or {}

        annex_code = (annex or "").strip().upper()
        needles = [_normalise_name(c) for c in (category or "").split("|") if _normalise_name(c)]
        excluded = {"actions en direct": 0, "hors annexe demandee": 0, "non souscriptibles (dedies, monetaire reserve)": 0,
                    "sorties de liste sans ligne active": 0, "hors categorie demandee": 0, "SRI au-dessus du plafond ou inconnu": 0}
        groups: dict[str, list[dict]] = {}
        for record in (base.get("supports") or {}).values():
            if record.get("type") == "action":
                excluded["actions en direct"] += 1
                continue
            cat = record.get("categorie")
            if not cat:
                excluded["sorties de liste sans ligne active"] += 1
                continue
            if annex_code and (record.get("annexe") or "").upper() != annex_code:
                excluded["hors annexe demandee"] += 1
                continue
            if _normalise_name(cat) in self._NOT_SUBSCRIBABLE:
                excluded["non souscriptibles (dedies, monetaire reserve)"] += 1
                continue
            if needles and not any(n in _normalise_name(cat) for n in needles):
                excluded["hors categorie demandee"] += 1
                continue
            if max_sri and not (record.get("sri") and int(record["sri"]) <= int(max_sri)):
                excluded["SRI au-dessus du plafond ou inconnu"] += 1
                continue
            groups.setdefault(cat, []).append(record)

        total = sum(len(v) for v in groups.values())
        annexes = sorted({(r.get("annexe") or "?") for r in (base.get("supports") or {}).values()
                          if r.get("categorie") and r.get("type") != "action"})
        header = [
            f"# Univers — {src.get('contrat') or 'n/c'}" + (f", annexe {annex_code}" if annex_code else ", toutes annexes"),
            f"*Liste « {src.get('document') or 'n/c'} » du {src.get('date_document') or 'n/c'} — {total} supports retenus*",
        ]
        census = ["", "## Recensement par categorie", "| Categorie | Supports | SRI |", "| --- | --- | --- |"]
        for cat, members in groups.items():
            sris = [int(m["sri"]) for m in members if m.get("sri")]
            census.append(f"| {cat} | {len(members)} | " + (f"{min(sris)} a {max(sris)}" if sris else "?") + " |")
        if not annex_code and len(annexes) > 1:
            census.insert(0, f"\n> Cette liste compte plusieurs annexes ({', '.join(annexes)}) et toutes sont "
                             "affichees : un contrat n'y a pas forcement acces. Preciser `annex` si besoin.")
        census.append("\nEcartes : " + (", ".join(f"{k} {v}" for k, v in excluded.items() if v) or "aucun") + ".")
        census.append(
            "\nColonnes : frais annuels totaux (support + contrat de cette liste, dont retrocession) ; performance "
            "annualisee 5 ans nette de tous frais ; pire annee publiee (sur trois ans publies au moins), nette des "
            "frais du support. Tri par "
            "performance 5 ans decroissante dans chaque categorie. Pour la fiche complete d'un support : get_uc_card."
        )

        table_head = ["| ISIN | Nom | SRI | Frais totaux (retro) | Perf. 5 ans | Pire annee | Remarques |",
                      "| --- | --- | --- | --- | --- | --- | --- |"]
        blocks: list[tuple[str, int, list[str]]] = []
        for cat, members in groups.items():
            members.sort(key=lambda r: (_num((r.get("couts_performances") or {}).get("perf_nette_nette_5a")) is None,
                                        -(_num((r.get("couts_performances") or {}).get("perf_nette_nette_5a")) or 0)))
            blocks.append((cat, len(members), [self._universe_row(m) for m in members]))

        budget = max(4000, int(self.valves.UNIVERSE_PAGE_CHARS))
        pages: list[list[str]] = [[]]
        size = len("\n".join(census))
        for cat, n, rows in blocks:
            chunk = [f"### {cat} ({n})"] + table_head
            for row in rows:
                if size + len("\n".join(chunk)) + len(row) > budget and len(chunk) > len(table_head) + 1:
                    pages[-1] += [""] + chunk
                    pages.append([])
                    size = 0
                    chunk = [f"### {cat} ({n}, suite)"] + table_head
                chunk.append(row)
            pages[-1] += [""] + chunk
            size += len("\n".join(chunk))
        count = len(pages)
        page = min(max(1, int(page or 1)), count)
        out = header + [f"**Page {page} / {count}**" + (" — lire toutes les pages avant de selectionner." if count > 1 else "")]
        if page == 1:
            out += census
        out += pages[page - 1]
        if page < count:
            out.append(f"\n*Suite : rappeler avec page={page + 1}.*")
        if errors:
            out.append("\n*Listes illisibles : " + "; ".join(errors[:3]) + "*")
        return "\n".join(out)

    async def list_uc_universe(
        self,
        contract: str = "",
        annex: str = "",
        category: str = "",
        max_sri: int = 0,
        page: int = 1,
        __request__: Any = None,
        __event_emitter__: Callable[[dict], Any] | None = None,
    ) -> str:
        """
        Lists every unit-linked fund of one Swiss Life contract list, exhaustively, one
        compact line per fund grouped by category: ISIN, name, SRI, total annual fees
        (fund + contract, retrocession), 5-year net-of-all-fees performance, worst
        published year, eligibility remarks. Read from the same parsed lists as
        get_uc_card. Use it to survey the whole universe before selecting funds, instead
        of searching the knowledge base. The output is paginated, never truncated: read
        every page. Individual shares, dedicated funds and funds only in the exit list are
        left out and counted.

        :param contract: Fragment of the contract or list name, e.g. "retraite et epargne", "Vie Generation", "PER Entreprise". Empty returns the catalogue of available lists.
        :param annex: Annex to keep, e.g. "IA". For the list common to Swiss Life savings and retirement contracts, only annex IA is accessible under free allocation. Empty keeps all annexes.
        :param category: Optional category filter, case and accent insensitive substring; several separated by "|", e.g. "Obligation|Emprunts".
        :param max_sri: Optional maximum SRI from 1 to 7. 0 keeps every SRI.
        :param page: Page to return when the listing spans several pages.
        """
        if _IMPORT_ERROR:
            return _IMPORT_ERROR
        await self._emit(__event_emitter__, "Lecture des listes d'UC...")
        try:
            text = await self._run(self._universe, __request__, contract, annex, category, max_sri, page)
        except Exception as exc:
            await self._emit(__event_emitter__, "Echec de la lecture des listes", done=True)
            return f"Echec de la lecture des listes d'UC : {str(exc) or type(exc).__name__}"
        await self._emit(__event_emitter__, "Listes d'UC lues", done=True)
        return text

    async def get_uc_risk(
        self,
        isin: str,
        __files__: list | None = None,
        __metadata__: dict | None = None,
        __request__: Any = None,
        __event_emitter__: Callable[[dict], Any] | None = None,
    ) -> str:
        """
        Measures the observed risk of a unit-linked fund from its NAV history:
        annualised volatility, maximum drawdown with its dates and whether it has been
        recovered, worst rolling twelve months, and whether the measured volatility is
        coherent with the declared SRI. Official AMF NAVs are used for French funds;
        otherwise a market series is used only once validated against the insurer's
        figures, and labelled otherwise. Not applicable to SCPI, SCI, OPCI or private
        equity vehicles.

        :param isin: The 12-character ISIN code of the support.
        """
        if _IMPORT_ERROR:
            return _IMPORT_ERROR
        code = (isin or "").strip().upper().replace(" ", "")
        if not isin_is_valid(code):
            return f"ISIN invalide : '{isin}'. Verifier le code avant toute mesure de risque."
        files = __files__ or (__metadata__ or {}).get("files") or []
        await self._emit(__event_emitter__, f"Recherche de l'historique de {code}...")
        try:
            ctx = await self._run(self._context, code, files, __request__)
        except Exception as exc:
            # The status line must not spin forever on a failed call.
            await self._emit(__event_emitter__, "Echec de la collecte", done=True)
            return f"Echec de la collecte des donnees : {str(exc) or type(exc).__name__}"
        record, geco = ctx["record"], ctx["geco"] or {}
        if record.get("type") == "action":
            await self._emit(__event_emitter__, "Action, pas un fonds", done=True)
            return f"{code} est une action : utiliser l'analyse d'actions."
        family = vehicle_family(record, self._nature(ctx))
        name = self._name(ctx)
        if family in _UNLISTED:
            await self._emit(__event_emitter__, "Support non cote : pas de mesure", done=True)
            return (
                f"{name or code} est un support **{family}**. Il n'a pas de valeur liquidative "
                "quotidienne cotee : volatilite et drawdown ne sont pas mesurables et ne doivent pas "
                "etre estimes. Son risque tient a la liquidite (delais de rachat, possibles "
                "suspensions), a la valorisation des actifs sous-jacents et, pour l'immobilier, aux "
                "frais d'entree et au delai de jouissance."
            )

        chosen: dict | None = None
        tested_lines: list[str] = [f"- {error}" for error in ctx["errors"]]
        nav: dict = {}
        if geco:
            try:
                nav = await self._run(self._geco_nav, geco["share"])
            except Exception as exc:
                tested_lines.append(f"- VL officielles AMF indisponibles : {str(exc) or type(exc).__name__}")
            closes = nav.get("closes")
            if closes is not None and (closes.index[-1] - closes.index[0]).days >= 365:
                reference = {y: v for y, v in (record.get("performances_annuelles") or {}).items()
                             if _num(v) is not None}
                years = calendar_returns(closes)
                common = [y for y in reference if y in years]
                gap = (sum(abs(years[y] - reference[y]) for y in common) / len(common)) if common else None
                chosen = {"symbol": f"AMF GECO {geco['share'].get('parId')}", "label": "VL officielles de la part",
                          "currency": nav.get("currency"), "closes": closes, "gap": gap,
                          "common_years": len(common),
                          "status": "valeurs liquidatives officielles de la part (base GECO de l'AMF)"}
            elif closes is not None:
                tested_lines.append("- VL officielles AMF : moins d'un an d'historique")
        if chosen is None:
            if not name:
                with contextlib.suppress(Exception):
                    name = (await self._run(self._openfigi, code)).get("name", "")
            try:
                found = await self._run(self._find_series, code, {**record, "nom": name})
            except Exception as exc:
                await self._emit(__event_emitter__, "Echec de la recherche de serie", done=True)
                return f"Echec de la recherche de serie : {str(exc) or type(exc).__name__}"
            chosen = found["chosen"]
            tested_lines += [
                f"- `{t['symbol']}` {t['label'][:50]} : "
                + (f"ecart moyen {t['gap']:.2f} pt sur {t['common_years']} an(s)"
                   if t.get("gap") is not None else t.get("status", "non comparable"))
                for t in found["tested"]
            ]
        if not chosen:
            await self._emit(__event_emitter__, "Aucune serie exploitable", done=True)
            return self._cap("\n".join([
                f"# Risque mesure — {name or code}",
                "",
                "**Aucune serie de valeur liquidative exploitable.** Volatilite et drawdown ne "
                "sont pas disponibles pour ce support : absence de mesure, pas absence de "
                "risque. Le SRI du DIC reste la reference.",
                "",
                "Sources examinees :" if tested_lines else "Aucun candidat trouve.",
                *tested_lines,
            ]))

        closes = chosen["closes"]
        m = risk_measures(closes)
        sri, sri_basis = self._retained_sri(ctx)
        measured_class = mrm_class(m["vol"])
        lines = [
            f"# Risque mesure — {name or code}",
            f"Serie : `{chosen['symbol']}` ({chosen['label'][:60]}), {chosen.get('currency') or 'devise n/c'}, "
            f"{len(closes)} valeurs du {closes.index[0]:%Y-%m-%d} au {closes.index[-1]:%Y-%m-%d} "
            f"({m['span']:.1f} ans), valorisation {m['frequency']}",
            f"Statut : **{chosen['status']}**"
            + (f" — ecart moyen de {chosen['gap']:.2f} pt avec la liste de l'assureur sur {chosen['common_years']} an(s)"
               if chosen.get("gap") is not None else ""),
            "",
            "## Mesures",
            f"- Volatilite annualisee : **{m['vol']:.1f}%**",
            (
                f"- Perte maximale observee : **{m['max_drawdown']:.1f}%** jusqu'au creux du "
                f"{m['trough_date']:%Y-%m-%d} — **minorante** : la baisse etait deja engagee au debut de "
                f"la serie ({closes.index[0]:%Y-%m-%d}), le vrai sommet est anterieur et la perte "
                "reelle depuis ce sommet a ete plus forte"
                if m["truncated"]
                else f"- Perte maximale historique : **{m['max_drawdown']:.1f}%**, du sommet du "
                f"{m['peak_date']:%Y-%m-%d} au creux du {m['trough_date']:%Y-%m-%d}"
            ),
            "- Recuperation : "
            + (f"sommet retrouve le {m['recovery_date']:%Y-%m-%d}, soit "
               f"{(m['recovery_date'] - m['trough_date']).days / 30.4:.0f} mois apres le creux"
               if m["recovery_date"] is not None else "**sommet toujours pas retrouve** a la derniere valeur"),
            f"- Pire performance sur 12 mois glissants : {_fmt(m['worst_12m'], 1, '%')}",
        ]
        if sri:
            gap = measured_class - sri
            lines += [
                "",
                "## Coherence avec le SRI",
                f"- SRI retenu : {sri} / 7 ({sri_basis}) — classe de risque de marche correspondant a la "
                f"volatilite mesuree : {measured_class} / 7",
                "- " + (
                    "Coherent."
                    if abs(gap) <= 1
                    else f"**Ecart de {abs(gap)} classes** : la volatilite observee est "
                         + ("plus elevee" if gap > 0 else "plus faible")
                         + " que ne le laisse attendre le SRI. Le SRI integre aussi le risque de "
                         "credit et se calcule sur une methode reglementaire propre ; l'ecart "
                         "merite une explication avant de s'appuyer sur l'un ou l'autre."
                ),
            ]
        fund_currency = ((geco.get("share") or {}).get("parRefDevCode") or record.get("devise") or "").upper()
        series_currency = (chosen.get("currency") or "").upper()
        if fund_currency and series_currency and fund_currency != series_currency:
            # Same fund, another listing: the measured volatility then carries the
            # exchange rate between the two currencies, which the SRI does not.
            lines.append(
                f"\n*Serie cotee en {series_currency} pour un support libelle en {fund_currency} : la "
                "volatilite et la perte mesurees incluent les variations de change entre ces devises.*"
            )
        if m["span"] < 5:
            lines.append(
                f"\n*Historique de {m['span']:.1f} ans : il ne couvre pas un cycle complet, et "
                "la perte maximale observee peut sous-estimer la perte possible.*"
            )
        if nav.get("break_date") and chosen.get("closes") is nav.get("closes"):
            lines.append(f"\n*Rupture de serie le {nav['break_date']} (operation sur titres probable) : mesures "
                         "calculees apres cette date.*")
        if nav.get("coupons") and nav.get("coupons_reinvested", 0) < nav["coupons"] and chosen.get("closes") is nav.get("closes"):
            lines.append("\n*Distributions en devise non reinvesties : la perte maximale peut etre legerement "
                         "surestimee.*")
        if chosen.get("gap") is not None and chosen["gap"] > _SIBLING_MATCH_PT and "GECO" in chosen["symbol"]:
            lines.append(f"\n*Les performances calculees sur les VL officielles s'ecartent de {chosen['gap']:.2f} pt "
                         "par an de la liste de l'assureur : la liste retient peut-etre une autre convention "
                         "(distributions, dates d'arrete).*")
        if "part soeur" in chosen["status"]:
            lines.append(
                "\n*Serie d'une part soeur du meme portefeuille : valable pour la volatilite et "
                "le drawdown, pas pour la performance, qui se lit sur la fiche.*"
            )
        if "NON validee" in chosen["status"]:
            lines.append(
                "\n> **Mesures a prendre avec reserve** : la serie n'a pas pu etre confrontee "
                "a un historique de reference. Signal de couverture, pas de risque."
            )
        if tested_lines:
            lines += ["", "Sources examinees :", *tested_lines]
        await self._emit(__event_emitter__, "Risque mesure", done=True)
        return self._cap("\n".join(lines))
