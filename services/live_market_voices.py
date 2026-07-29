"""Live Market voices — direct-source only (no generated quotes).

Credibility is a deterministic *source-tier* score, not a claim that the
content is true. Every row must carry a primary URL from the issuer/exchange.
If a feed fails, it is recorded as unavailable — never invented.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from io import StringIO
from pathlib import Path
from typing import Any

import feedparser
import pandas as pd
import requests

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data" / "live_market"
VOICES_PATH = DATA_DIR / "voices.parquet"
SNAPSHOT_PATH = DATA_DIR / "snapshot.json"
NIFTY100_PATH = DATA_DIR / "nifty100.csv"
SOURCE_HEALTH_PATH = DATA_DIR / "source_health.json"

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/xml,application/xml,application/rss+xml,*/*",
    "Accept-Language": "en-IN,en;q=0.9",
}

# Source-tier credibility (documented, fixed). Higher = closer to primary issuer.
CREDIBILITY = {
    "central_bank_primary": 0.98,
    "regulator_exchange": 0.95,
    "govt_official": 0.92,
    "company_exchange_filing": 0.94,
    "multilateral_primary": 0.90,
    "foreign_central_bank": 0.93,
}

HORIZONS = {
    "day": timedelta(days=1),
    "week": timedelta(days=7),
    "month": timedelta(days=31),
    "quarter": timedelta(days=92),
    "year": timedelta(days=366),
}

# 80% India / 20% global — enforced at display selection, not by inventing rows.
INDIA_DISPLAY_SHARE = 0.80

# Material NSE announcement types for equity "voices".
NSE_MATERIAL_DESC = {
    "Press Release",
    "Financial Results",
    "Investor Presentation",
    "Analysts/Institutional Investor Meet/Con. Call Updates",
    "Board Meeting",
    "Outcome of Board Meeting",
    "Acquisition",
    "Updates",
    "Change in Directors/ Key Managerial Personnel/ Auditor/ Compliance Officer/ Share Transfer Agent",
    "Resignation of Director",
    "Appointment",
    "Earnings Call Transcript",
    "Integrated Filing- Financials",
    "Integrated Filing- Governance",
}


@dataclass(frozen=True)
class SourceDef:
    source_id: str
    name: str
    voice_class: str  # regulator | policymaker | company | executive | economist
    region: str  # INDIA | GLOBAL
    tier: str
    kind: str  # rss | nse_announcements | nifty100_csv
    url: str
    weight: float = 1.0  # relative pull priority within region


SOURCES: list[SourceDef] = [
    # --- India regulators / policy (heavy weight) ---
    SourceDef(
        "rbi_press",
        "Reserve Bank of India — Press Releases",
        "regulator",
        "INDIA",
        "central_bank_primary",
        "rss",
        "https://www.rbi.org.in/pressreleases_rss.xml",
        1.2,
    ),
    SourceDef(
        "rbi_speeches",
        "Reserve Bank of India — Speeches",
        "economist",
        "INDIA",
        "central_bank_primary",
        "rss",
        "https://www.rbi.org.in/speeches_rss.xml",
        1.3,
    ),
    SourceDef(
        "rbi_notifications",
        "Reserve Bank of India — Notifications",
        "regulator",
        "INDIA",
        "central_bank_primary",
        "rss",
        "https://www.rbi.org.in/notifications_rss.xml",
        1.0,
    ),
    SourceDef(
        "niti",
        "NITI Aayog — Official updates",
        "policymaker",
        "INDIA",
        "govt_official",
        "rss",
        "https://www.niti.gov.in/rss.xml",
        0.9,
    ),
    SourceDef(
        "pib_finance",
        "Press Information Bureau — Finance / related",
        "policymaker",
        "INDIA",
        "govt_official",
        "rss",
        "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3",
        1.0,
    ),
    SourceDef(
        "nse_nifty100",
        "NSE — Corporate filings (Nifty 100 only)",
        "company",
        "INDIA",
        "company_exchange_filing",
        "nse_announcements",
        "https://www.nseindia.com/api/corporate-announcements",
        1.4,
    ),
    # --- Global (~20%) ---
    SourceDef(
        "fed_press",
        "US Federal Reserve — Press Releases",
        "regulator",
        "GLOBAL",
        "foreign_central_bank",
        "rss",
        "https://www.federalreserve.gov/feeds/press_all.xml",
        1.0,
    ),
    SourceDef(
        "fed_speeches",
        "US Federal Reserve — Speeches",
        "economist",
        "GLOBAL",
        "foreign_central_bank",
        "rss",
        "https://www.federalreserve.gov/feeds/speeches.xml",
        1.1,
    ),
    SourceDef(
        "ecb_press",
        "European Central Bank — Press / Speeches RSS",
        "economist",
        "GLOBAL",
        "foreign_central_bank",
        "rss",
        "https://www.ecb.europa.eu/rss/press.html",
        0.9,
    ),
    SourceDef(
        "boe_news",
        "Bank of England — News",
        "regulator",
        "GLOBAL",
        "foreign_central_bank",
        "rss",
        "https://www.bankofengland.co.uk/rss/news",
        0.8,
    ),
    SourceDef(
        "boe_speeches",
        "Bank of England — Speeches",
        "economist",
        "GLOBAL",
        "foreign_central_bank",
        "rss",
        "https://www.bankofengland.co.uk/rss/speeches",
        0.9,
    ),
    SourceDef(
        "bis",
        "Bank for International Settlements — Publications",
        "economist",
        "GLOBAL",
        "multilateral_primary",
        "rss",
        "https://www.bis.org/doclist/rss_all_categories.rss",
        0.85,
    ),
]


@dataclass
class VoiceItem:
    item_id: str
    published_at: str
    title: str
    summary: str
    url: str
    source_id: str
    source_name: str
    voice_class: str
    region: str
    credibility: float
    credibility_tier: str
    speaker_or_issuer: str
    raw_published: str = ""
    attachment_url: str = ""
    symbol: str = ""
    fetch_status: str = "ok"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LiveMarketSnapshot:
    asof: str
    policy: str
    source_health: list[dict[str, Any]] = field(default_factory=list)
    counts_by_horizon: dict[str, int] = field(default_factory=dict)
    counts_by_class: dict[str, int] = field(default_factory=dict)
    india_share_latest_week: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", text or "")
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    s = str(value).strip()
    # NSE: 29-Jul-2026 08:36:48
    for fmt in (
        "%d-%b-%Y %H:%M:%S",
        "%d-%b-%Y",
        "%d %b %Y",
        "%d %B %Y",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            raw = s
            if fmt.endswith("%z") and s.endswith("Z"):
                raw = s.replace("Z", "+0000")
            # PIB uses uppercase month abbreviations: 28 JUL 2026
            if "%b" in fmt or "%B" in fmt:
                raw = re.sub(
                    r"\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\b",
                    lambda m: m.group(1).title(),
                    raw,
                    flags=re.I,
                )
            dt = datetime.strptime(raw, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        dt = parsedate_to_datetime(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        dt = pd.to_datetime(s, utc=True).to_pydatetime()
        return dt if isinstance(dt, datetime) else None
    except Exception:
        return None


def _item_id(*parts: str) -> str:
    h = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return h[:20]


def _speaker_from_title(title: str, default_issuer: str) -> str:
    """Extract a named speaker only when the title itself contains a clear pattern.

    Never invents a person. Falls back to the issuer institution name.
    """
    t = title or ""
    patterns = [
        r"(?i)speech by ([A-Z][A-Za-z.\-']+(?:\s+[A-Z][A-Za-z.\-']+){0,3})",
        r"(?i)keynote address by ([A-Z][A-Za-z.\-']+(?:\s+[A-Z][A-Za-z.\-']+){0,3})",
        r"(?i)^([A-Z][A-Za-z.\-']+(?:\s+[A-Z][A-Za-z.\-']+){0,3}):\s+",
        r"(?i),\s+([A-Z][A-Za-z.\-']+(?:\s+[A-Z][A-Za-z.\-']+){0,2})$",
    ]
    for pat in patterns:
        m = re.search(pat, t)
        if m:
            name = m.group(1).strip(" -:|,")
            if 3 <= len(name) <= 60 and name.lower() not in {"the", "press", "release"}:
                return name
    return default_issuer


def fetch_nifty100_symbols() -> set[str]:
    _ensure_dirs()
    url = "https://archives.nseindia.com/content/indices/ind_nifty100list.csv"
    try:
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        NIFTY100_PATH.write_text(r.text, encoding="utf-8")
        df = pd.read_csv(StringIO(r.text))
    except Exception:
        logger.exception("Nifty 100 download failed; using cache if present")
        if not NIFTY100_PATH.is_file():
            return set()
        df = pd.read_csv(NIFTY100_PATH)
    col = "Symbol" if "Symbol" in df.columns else df.columns[2]
    return {str(x).strip().upper() for x in df[col].dropna().tolist()}


def _nse_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            **UA,
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
        }
    )
    try:
        s.get("https://www.nseindia.com", timeout=20)
    except Exception:
        pass
    return s


def fetch_nse_announcements(
    *,
    symbols: set[str],
    lookback_days: int = 31,
) -> list[VoiceItem]:
    """NSE equity corporate announcements filtered to Nifty 100 + material types."""
    src = next(s for s in SOURCES if s.source_id == "nse_nifty100")
    cred = CREDIBILITY[src.tier]
    session = _nse_session()
    end = date.today()
    start = end - timedelta(days=lookback_days)
    params = {
        "index": "equities",
        "from_date": start.strftime("%d-%m-%Y"),
        "to_date": end.strftime("%d-%m-%Y"),
    }
    r = session.get(src.url, params=params, timeout=60)
    r.raise_for_status()
    payload = r.json()
    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected NSE announcements payload type: {type(payload)}")

    skip_desc = {
        "Shareholding Pattern",
        "Copy of Newspaper Publication",
        "Newspaper Publication",
        "Certificate under SEBI (Depositories and Participants) Regulations, 2018",
        "Record Date",
        "ESOP/ESOS/ESPS",
    }
    items: list[VoiceItem] = []
    for row in payload:
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol or (symbols and symbol not in symbols):
            # Without a Nifty 100 symbol we cannot claim top-100 membership
            continue
        desc = str(row.get("desc") or "").strip()
        if desc in skip_desc:
            continue
        if desc and desc not in NSE_MATERIAL_DESC:
            if not any(k in desc for k in ("Press", "Result", "Investor", "Board", "Director", "Transcript", "Acquisition")):
                continue
        sm_name = str(row.get("sm_name") or "").strip()

        title_bits = [sm_name or symbol, desc]
        body = _strip_html(str(row.get("attchmntText") or ""))
        title = " — ".join(x for x in title_bits if x)
        if body and len(body) < 280:
            display_title = f"{title}: {body}" if title else body
        else:
            display_title = title or body[:200]
        published_raw = str(row.get("an_dt") or row.get("exchdisstime") or "")
        dt = _parse_dt(published_raw)
        if not dt:
            continue
        url = str(row.get("attchmntFile") or "").strip()
        if not url:
            # Exchange filing without attachment still has issuer text — link to NSE announcements page
            url = (
                "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
                f"?symbol={symbol}"
            )
        speaker = sm_name or symbol
        # Executive cue only from exchange text
        voice_class = "executive" if re.search(r"(?i)\b(CEO|MD|managing director|CFO|chairman)\b", body + " " + desc) else "company"
        items.append(
            VoiceItem(
                item_id=_item_id(src.source_id, symbol, published_raw, display_title[:120], url),
                published_at=dt.astimezone(timezone.utc).isoformat(),
                title=display_title[:500],
                summary=body[:1200],
                url=url,
                source_id=src.source_id,
                source_name=src.name,
                voice_class=voice_class,
                region=src.region,
                credibility=cred,
                credibility_tier=src.tier,
                speaker_or_issuer=speaker,
                raw_published=published_raw,
                attachment_url=str(row.get("attchmntFile") or ""),
                symbol=symbol,
            )
        )
    return items


def _pib_published_at(url: str, session: requests.Session) -> datetime | None:
    """Pull publication timestamp from the PIB HTML page itself (never invent)."""
    try:
        resp = session.get(url, timeout=20)
        if resp.status_code != 200:
            return None
        html = resp.text
        # Common PIB patterns
        patterns = [
            r"(?i)Posted On:\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\s+\d{1,2}:\d{2}(?:\s*[AP]M)?)",
            r"(?i)Posted On:\s*(\d{1,2}[-/]\d{1,2}[-/]\d{4})",
            r'(?i)property="article:published_time"\s+content="([^"]+)"',
            r"(?i)(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})",
            r"(?i)(\d{1,2}\s+(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s+\d{4})",
        ]
        for pat in patterns:
            m = re.search(pat, html)
            if not m:
                continue
            dt = _parse_dt(m.group(1))
            if dt:
                return dt
    except Exception:
        logger.debug("PIB date scrape failed for %s", url, exc_info=True)
    return None


def fetch_rss_source(src: SourceDef, *, max_entries: int = 80) -> list[VoiceItem]:
    r = requests.get(src.url, headers=UA, timeout=35)
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    cred = CREDIBILITY[src.tier]
    issuer = src.name.split("—")[0].strip()
    items: list[VoiceItem] = []
    pib_session = requests.Session()
    pib_session.headers.update(UA)
    pib_enriched = 0
    for entry in feed.entries[:max_entries]:
        title = _strip_html(str(entry.get("title") or ""))
        if not title:
            continue
        link = str(entry.get("link") or "").strip()
        if not link:
            continue
        raw_summary = str(entry.get("summary") or entry.get("description") or "")
        if not raw_summary and entry.get("content"):
            try:
                raw_summary = str(entry.content[0].get("value") or "")
            except Exception:
                raw_summary = ""
        summary = _strip_html(raw_summary)[:1200]
        published_raw = str(entry.get("published") or entry.get("updated") or "")
        dt = None
        if entry.get("published_parsed"):
            try:
                dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)  # type: ignore[index]
            except Exception:
                dt = None
        if dt is None and entry.get("updated_parsed"):
            try:
                dt = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)  # type: ignore[index]
            except Exception:
                dt = None
        if dt is None:
            dt = _parse_dt(published_raw)
        # PIB RSS often omits dates — enrich from the primary HTML page (still direct source)
        if dt is None and src.source_id.startswith("pib") and pib_enriched < 15:
            dt = _pib_published_at(link, pib_session)
            pib_enriched += 1
            time.sleep(0.2)
            if dt:
                published_raw = dt.isoformat()
        if dt is None:
            # Refuse undated items — cannot place on day/week/month axes without inventing time
            continue
        speaker = _speaker_from_title(title, issuer)
        voice_class = src.voice_class
        if src.source_id.startswith(("rbi", "fed", "ecb", "boe")):
            if re.search(r"(?i)\bspeech|keynote|address by\b", title):
                voice_class = "economist" if "Governor" not in title else "regulator"
        items.append(
            VoiceItem(
                item_id=_item_id(src.source_id, link, published_raw, title),
                published_at=dt.astimezone(timezone.utc).isoformat(),
                title=title[:500],
                summary=summary,
                url=link,
                source_id=src.source_id,
                source_name=src.name,
                voice_class=voice_class,
                region=src.region,
                credibility=cred,
                credibility_tier=src.tier,
                speaker_or_issuer=speaker,
                raw_published=published_raw,
            )
        )
    return items


def refresh_live_market(*, nse_lookback_days: int = 31) -> LiveMarketSnapshot:
    """Fetch all curated direct sources and persist. Never fabricates items."""
    _ensure_dirs()
    symbols = fetch_nifty100_symbols()
    all_items: list[VoiceItem] = []
    health: list[dict[str, Any]] = []

    for src in SOURCES:
        started = time.time()
        status = "ok"
        err = ""
        n = 0
        try:
            if src.kind == "rss":
                rows = fetch_rss_source(src)
            elif src.kind == "nse_announcements":
                rows = fetch_nse_announcements(symbols=symbols, lookback_days=nse_lookback_days)
            else:
                rows = []
                status = "skipped"
            n = len(rows)
            all_items.extend(rows)
        except Exception as exc:
            status = "error"
            err = f"{type(exc).__name__}: {exc}"
            logger.warning("Source %s failed: %s", src.source_id, exc)
        health.append(
            {
                "source_id": src.source_id,
                "name": src.name,
                "region": src.region,
                "voice_class": src.voice_class,
                "credibility_tier": src.tier,
                "credibility": CREDIBILITY.get(src.tier),
                "status": status,
                "items": n,
                "error": err,
                "elapsed_ms": int((time.time() - started) * 1000),
                "url": src.url,
            }
        )
        time.sleep(0.25)

    # Deduplicate by item_id
    by_id: dict[str, VoiceItem] = {}
    for it in all_items:
        by_id[it.item_id] = it
    items = list(by_id.values())
    df = pd.DataFrame([i.to_dict() for i in items])
    if not df.empty:
        df["published_at"] = pd.to_datetime(df["published_at"], utc=True)
        df = df.sort_values("published_at", ascending=False).reset_index(drop=True)
        df.to_parquet(VOICES_PATH, compression="zstd", index=False)
    else:
        # Persist empty schema so UI can show honest empty state
        pd.DataFrame(
            columns=[
                "item_id",
                "published_at",
                "title",
                "summary",
                "url",
                "source_id",
                "source_name",
                "voice_class",
                "region",
                "credibility",
                "credibility_tier",
                "speaker_or_issuer",
                "raw_published",
                "attachment_url",
                "symbol",
                "fetch_status",
            ]
        ).to_parquet(VOICES_PATH, compression="zstd", index=False)

    SOURCE_HEALTH_PATH.write_text(json.dumps(health, indent=2), encoding="utf-8")

    now = datetime.now(timezone.utc)
    counts: dict[str, int] = {}
    if not df.empty:
        for name, delta in HORIZONS.items():
            cutoff = now - delta
            counts[name] = int((df["published_at"] >= cutoff).sum())
        week = df[df["published_at"] >= now - HORIZONS["week"]]
        india_share = float((week["region"] == "INDIA").mean()) if len(week) else None
        class_counts = df["voice_class"].value_counts().to_dict()
    else:
        india_share = None
        class_counts = {}

    snap = LiveMarketSnapshot(
        asof=now.isoformat(),
        policy=(
            "Direct-source only. Titles/summaries are copied verbatim from issuer/exchange feeds. "
            "Credibility is a fixed source-tier score (not AI judgment). "
            "No paraphrased quotes. Failed sources are listed, never filled with synthetic content. "
            f"Display targeting ~{int(INDIA_DISPLAY_SHARE*100)}% India / {int((1-INDIA_DISPLAY_SHARE)*100)}% global."
        ),
        source_health=health,
        counts_by_horizon=counts,
        counts_by_class={str(k): int(v) for k, v in class_counts.items()},
        india_share_latest_week=india_share,
    )
    SNAPSHOT_PATH.write_text(json.dumps(snap.to_dict(), indent=2), encoding="utf-8")
    return snap


def load_voices() -> pd.DataFrame:
    if not VOICES_PATH.is_file():
        return pd.DataFrame()
    df = pd.read_parquet(VOICES_PATH)
    if not df.empty and "published_at" in df.columns:
        df["published_at"] = pd.to_datetime(df["published_at"], utc=True)
    return df


def load_snapshot() -> LiveMarketSnapshot | None:
    if not SNAPSHOT_PATH.is_file():
        return None
    raw = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    return LiveMarketSnapshot(**raw)


def filter_horizon(df: pd.DataFrame, horizon: str) -> pd.DataFrame:
    if df.empty:
        return df
    delta = HORIZONS.get(horizon)
    if not delta:
        return df
    cutoff = datetime.now(timezone.utc) - delta
    out = df[df["published_at"] >= cutoff].copy()
    return out.sort_values(["credibility", "published_at"], ascending=[False, False])


def balance_india_global(df: pd.DataFrame, *, limit: int = 60) -> pd.DataFrame:
    """Select rows toward 80/20 India/global without inventing content.

    If one region lacks enough rows, returns whatever exists (honest shortfall).
    """
    if df.empty or limit <= 0:
        return df.head(0)
    n_india = int(round(limit * INDIA_DISPLAY_SHARE))
    n_global = limit - n_india
    india = df[df["region"] == "INDIA"].head(n_india)
    global_ = df[df["region"] == "GLOBAL"].head(n_global)
    # Backfill unused slots from the other region if short
    if len(india) < n_india:
        need = n_india - len(india)
        extra = df[df["region"] == "GLOBAL"].iloc[len(global_) : len(global_) + need]
        global_ = pd.concat([global_, extra], ignore_index=True)
    if len(global_) < n_global:
        need = n_global - len(global_)
        extra = df[df["region"] == "INDIA"].iloc[len(india) : len(india) + need]
        india = pd.concat([india, extra], ignore_index=True)
    out = pd.concat([india, global_], ignore_index=True)
    return out.sort_values(["credibility", "published_at"], ascending=[False, False]).head(limit)


def credibility_legend() -> list[dict[str, Any]]:
    return [
        {"tier": k, "score": v, "meaning": m}
        for k, v, m in [
            ("central_bank_primary", 0.98, "Domestic central-bank primary feed (RBI)"),
            ("company_exchange_filing", 0.94, "NSE corporate filing / attachment"),
            ("foreign_central_bank", 0.93, "Fed / ECB / BoE primary feed"),
            ("govt_official", 0.92, "Official government portal (PIB, NITI)"),
            ("multilateral_primary", 0.90, "BIS / similar primary publications"),
            ("regulator_exchange", 0.95, "Market regulator / exchange primary"),
        ]
    ]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    snap = refresh_live_market()
    print(json.dumps(snap.to_dict(), indent=2)[:2500])
    df = load_voices()
    print("rows", len(df))
    if not df.empty:
        print(df[["published_at", "region", "voice_class", "credibility", "title"]].head(8).to_string())
