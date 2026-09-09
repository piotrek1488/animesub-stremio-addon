"""
Stremio Addon – Polskie napisy do anime z animesub.info
Przepisany na Python/FastAPI na podstawie działającego addonu JS.
"""

import os
import re
import io
import time
import logging
import zipfile
from typing import Optional
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, PlainTextResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from datetime import datetime


with open("version", "r") as f:
    app_version = f.read().strip()

# ── Konfiguracja ──────────────────────────────────────────────

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8080")
ANIMESUB_BASE = os.environ.get("ANIMESUB_BASE_URL", "http://animesub.info").rstrip("/")
SEARCH_URL = f"{ANIMESUB_BASE}/szukaj.php"
DOWNLOAD_URL = f"{ANIMESUB_BASE}/sciagnij.php"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("animesub")
year = str(datetime.now().year)

# ── Cache ─────────────────────────────────────────────────────

search_cache: dict[str, dict] = {}
CACHE_TTL = 60 * 30  # 30 minut

# ── FastAPI ───────────────────────────────────────────────────

app = FastAPI(title="AnimeSub.info Stremio Addon")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

MANIFEST = {
    "id": "org.stremio.addon.info.animesub",
    "version": app_version,
    "name": "AnimeSub.info Subtitles",
    "description": "Dodatek wyszukuje polskie napisy do anime z animesub.info",
    "logo": f"{BASE_URL}/static/icon.jpg",
    "resources": ["subtitles"],
    "types": ["movie", "series", "anime"],
    "idPrefixes": ["tt", "kitsu"],
    "contactEmail": "piotrek1488@gmail.com",
    "catalogs": [],
    "behaviorHints": {"configurable": False, "configurationRequired": False},
    "stremioAddonsConfig": {
        "issuer": "https://stremio-addons.net",
        "signature": "eyJhbGciOiJkaXIiLCJlbmMiOiJBMTI4Q0JDLUhTMjU2In0..aSYEHxT9ElzC3NMzieMURQ.NBGSJWCtIlnD50iQgc5N7MSXn10fJWNsHQzQGFrdLmiyaqFpNUeqLVr9m5YHMXBaZEZwGV5_jvvGxldsB_RnJVrhnxm5xQp5zN_JnXzr6q6USTNHIX540TcAJOR9yv4z.0xbyk_xjDoiR2uzvuOUkbg"
    }
}

if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/version")
async def version():
    try:
        with open("version") as f: return PlainTextResponse(f.read().strip())
    except: return PlainTextResponse("?", status_code=404)

async def index(request: Request):
    host = request.headers.get("host", "127.0.0.1:8080")
    protocol = "https" if "duckdns.org" in host else "http"
    full_url = f"{protocol}://{host}"
    try:
        with open("static/index.html", "r", encoding="utf-8") as f:
            content = f.read()
        content = content.replace("{public_url}", full_url)
        content = content.replace("{stremio_url}", f"stremio://{host}/manifest.json")
        content = content.replace("{version_placeholder}", app_version)
        content = content.replace("{current_year}", year)
        return HTMLResponse(content=content)
    except FileNotFoundError:
        return HTMLResponse("<h1>static/index.html not found</h1>", status_code=404)
app.add_api_route("/", index, methods=["GET", "HEAD"])

@app.get("/manifest.json")
@app.get("/")
async def manifest():
    return JSONResponse(content=MANIFEST)

@app.get("/static/icon.jpg")
async def logo():
    return FileResponse("icon.jpg", media_type="image/jpeg")

# ══════════════════════════════════════════════════════════════
#  METADATA: IMDB/Kitsu → tytuł anime
# ══════════════════════════════════════════════════════════════

async def get_meta_info(content_type: str, content_id: str) -> dict:
    """
    Pobiera tytuł i info o sezonie/odcinku.
    Obsługuje zarówno IMDB (tt...) jak i Kitsu (kitsu:...) ID.
    Używa Cinemeta (nie wymaga klucza API).
    """
    parts = content_id.split(":")
    prefix = parts[0]

    result = {
        "title": None, "year": None,
        "season": None, "episode": None,
        "imdb_id": None, "kitsu_id": None,
    }

    if prefix == "kitsu":
        kitsu_id = parts[1]
        result["kitsu_id"] = kitsu_id
        result["season"] = 1
        result["episode"] = int(parts[2]) if len(parts) >= 3 else None

        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(
                    f"https://kitsu.io/api/edge/anime/{kitsu_id}",
                    headers={
                        "Accept": "application/vnd.api+json",
                        "Content-Type": "application/vnd.api+json",
                    },
                )
                if resp.status_code == 200:
                    anime = resp.json()["data"]["attributes"]
                    titles = anime.get("titles", {})
                    result["title"] = (
                        titles.get("en") or titles.get("en_jp")
                        or anime.get("canonicalTitle") or titles.get("ja_jp")
                    )
                    start_date = anime.get("startDate", "")
                    result["year"] = int(start_date[:4]) if start_date else None
                    log.info(f"[Kitsu] {result['title']} ({result['year']})")
        except Exception as e:
            log.error(f"[Kitsu] Błąd: {e}")
    else:
        result["imdb_id"] = parts[0]
        if content_type == "series" and len(parts) >= 3:
            result["season"] = int(parts[1])
            result["episode"] = int(parts[2])

        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(
                    f"https://v3-cinemeta.strem.io/meta/{content_type}/{result['imdb_id']}.json"
                )
                if resp.status_code == 200:
                    meta = resp.json().get("meta", {})
                    result["title"] = meta.get("name")
                    result["year"] = meta.get("year")
                    log.info(f"[Cinemeta] {result['title']} ({result['year']})")
        except Exception as e:
            log.error(f"[Cinemeta] Błąd: {e}")

    return result


# ══════════════════════════════════════════════════════════════
#  WYSZUKIWANIE na animesub.info
# ══════════════════════════════════════════════════════════════

COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Charset": "ISO-8859-2,utf-8;q=0.7,*;q=0.3",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pl,en;q=0.9",
}


async def search_subtitles(title: str, title_type: str = "en") -> list[dict]:
    """
    Szuka napisów na animesub.info.
    Przeszukuje kilka stron wyników i różne sortowania.
    """
    cache_key = f"{title}:{title_type}"
    cached = search_cache.get(cache_key)
    if cached and time.time() - cached["timestamp"] < CACHE_TTL:
        log.info(f"[Cache hit] {title}")
        return cached["results"]

    log.info(f'[Szukanie] "{title}" (typ: {title_type})')
    all_results = []
    seen_ids = set()

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            # Próbuj różne sortowania i strony
            for sort in ["pobrn", "t_ang"]:
                for page in range(3):  # max 3 strony
                    params = {
                        "szukane": title,
                        "pTitle": title_type,
                        "pSortuj": sort,
                    }
                    if page > 0:
                        params["od"] = page

                    resp = await client.get(
                        SEARCH_URL, params=params, headers=COMMON_HEADERS,
                    )
                    html = resp.content.decode("iso-8859-2", errors="replace")
                    results = _parse_search_results(html)

                    if not results:
                        break  # brak wyników = koniec stron

                    new_count = 0
                    for r in results:
                        if r["id"] not in seen_ids:
                            seen_ids.add(r["id"])
                            r["sort"] = sort
                            r["page"] = page
                            all_results.append(r)
                            new_count += 1

                    log.info(f"[Szukanie] sort={sort} page={page} → {len(results)} wyników ({new_count} nowych)")

                    if new_count == 0:
                        break  # same duplikaty = koniec

            search_cache[cache_key] = {"results": all_results, "timestamp": time.time()}
            log.info(f"[Znaleziono] {len(all_results)} napisów łącznie")
            return all_results

    except Exception as e:
        log.error(f"[Szukanie] Błąd: {e}")
        return []


def _parse_search_results(html: str) -> list[dict]:
    """
    Parsuje HTML wyników wyszukiwania animesub.info.
    Struktura: table.Napisy > tr.KNap (3 wiersze) + tr.KKom (formularz)
    """
    soup = BeautifulSoup(html, "html.parser")
    subtitles = []

    for table in soup.find_all(
        "table", class_="Napisy",
        style=lambda s: s and "text-align:center" in s
    ):
        try:
            rows = table.find_all("tr", class_="KNap")
            if len(rows) < 3:
                continue

            # Wiersz 1: tytuł oryginalny, data, format
            r1 = rows[0].find_all("td")
            title_org = r1[0].get_text(strip=True) if len(r1) > 0 else ""
            format_type = r1[3].get_text(strip=True) if len(r1) > 3 else ""

            # Wiersz 2: tytuł angielski, autor
            r2 = rows[1].find_all("td")
            title_eng = r2[0].get_text(strip=True) if len(r2) > 0 else ""
            author_el = r2[1].find("a") if len(r2) > 1 else None
            author = (
                author_el.get_text(strip=True) if author_el
                else r2[1].get_text(strip=True).lstrip("~") if len(r2) > 1
                else ""
            )

            # Wiersz 3: tytuł alternatywny, pobrania
            r3 = rows[2].find_all("td")
            title_alt = r3[0].get_text(strip=True) if len(r3) > 0 else ""
            download_count = 0
            if len(r3) > 3:
                m = re.match(r"(\d+)", r3[3].get_text(strip=True))
                if m:
                    download_count = int(m.group(1))

            # Formularz pobierania w tr.KKom
            dl_row = table.find("tr", class_="KKom")
            if not dl_row:
                continue
            form = dl_row.find("form", attrs={"method": "POST"})
            if not form:
                continue

            id_inp = form.find("input", attrs={"name": "id"})
            sh_inp = form.find("input", attrs={"name": "sh"})
            if not id_inp or not sh_inp:
                continue

            sub_id = id_inp.get("value", "")
            dl_hash = sh_inp.get("value", "")
            if not sub_id or not dl_hash:
                continue

            # Opis
            desc_cell = dl_row.find("td", class_="KNap", attrs={"align": "left"})
            description = desc_cell.get_text(strip=True) if desc_cell else ""

            ep_info = _parse_episode_info(title_org, title_eng, title_alt)

            subtitles.append({
                "id": sub_id, "hash": dl_hash,
                "title_org": title_org, "title_eng": title_eng,
                "title_alt": title_alt, "author": author,
                "format_type": format_type,
                "download_count": download_count,
                "description": description,
                **ep_info,
            })
        except Exception as e:
            log.warning(f"Błąd parsowania tabeli: {e}")

    return subtitles


def _parse_episode_info(title_org: str, title_eng: str, title_alt: str) -> dict:
    """Wyciąga numer sezonu i odcinka z tytułów."""
    season = None
    episode = None

    for title in [title_org, title_eng, title_alt]:
        if not title:
            continue
        if episode is None:
            m = re.search(r"(?:ep|episode)\s*(\d+)", title, re.I)
            if m:
                episode = int(m.group(1))
        if season is None:
            m = re.search(r"(?:Season|S)\s*(\d+)|(\d+)(?:nd|rd|th)\s+Season", title, re.I)
            if m:
                season = int(m.group(1) or m.group(2))
        if season is None and episode is not None:
            m = re.search(r"\s(\d)\s+ep\d+", title, re.I)
            if m:
                season = int(m.group(1))

    return {"season": season, "episode": episode}


# ══════════════════════════════════════════════════════════════
#  STRATEGIE WYSZUKIWANIA
# ══════════════════════════════════════════════════════════════

def generate_search_strategies(title: str, season: Optional[int], episode: Optional[int]) -> list[dict]:
    """Lista strategii wyszukiwania — od najdokładniejszej do najszerszej."""
    strategies = []
    clean = re.sub(r"\s+", " ", title.replace("-", " ")).strip()

    if episode is not None:
        ep_padded = str(episode).zfill(2)
        ep_raw = str(episode)

        if season and season > 1:
            strategies.append({"type": "en", "query": f"{clean} Season {season} ep {ep_padded}"})
            strategies.append({"type": "org", "query": f"{clean} {season} ep {ep_padded}"})

        # Ze spacją i bez (animesub rozróżnia!)
        strategies.append({"type": "org", "query": f"{clean} ep {ep_padded}"})
        strategies.append({"type": "en", "query": f"{clean} ep {ep_padded}"})
        strategies.append({"type": "org", "query": f"{clean} ep{ep_padded}"})
        strategies.append({"type": "en", "query": f"{clean} ep{ep_padded}"})

        if season and season > 1:
            strategies.append({"type": "en", "query": f"{clean} Season {season}"})

    strategies.append({"type": "org", "query": clean})
    strategies.append({"type": "en", "query": clean})
    return strategies


def _normalize_title(title: str) -> str:
    """Normalizuje tytuł do porównywania: lowercase, bez interpunkcji, bez zbędnych spacji."""
    t = title.lower()
    t = re.sub(r"[:\-–—.,!?'\"()\[\]{}]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _title_matches(sub_titles: list[str], target_title: str) -> bool:
    """
    Sprawdza czy napisy pasują do szukanego anime.
    Odrzuca: inne serie, openingi, endingi, filmy, OVA, spin-offy.
    """
    target_norm = _normalize_title(target_title)

    non_episode_markers = {"opening", "ending", "op", "ed", "ost", "soundtrack",
                           "amv", "trailer", "pv", "movie", "film", "ova", "special"}
    different_series = {"shippuuden", "shippuden", "shipuden", "sd", "boruto",
                        "next generations", "rock lee"}

    # Faza 1: sprawdź WSZYSTKIE tytuły pod kątem markerów
    for raw_title in sub_titles:
        if not raw_title:
            continue
        sub_norm = _normalize_title(raw_title)
        sub_words = set(sub_norm.split())
        if sub_words & non_episode_markers:
            log.debug(f"[Filter] Odrzucam (marker): {raw_title}")
            return False

    # Faza 2: sprawdź czy którykolwiek tytuł pasuje
    for raw_title in sub_titles:
        if not raw_title:
            continue
        sub_norm = _normalize_title(raw_title)

        if sub_norm.startswith(target_norm):
            remainder = sub_norm[len(target_norm):].strip()
            if any(remainder.startswith(ds) for ds in different_series):
                log.debug(f"[Filter] Odrzucam (inna seria): {raw_title}")
                return False
            return True

        if target_norm in sub_norm:
            idx = sub_norm.index(target_norm)
            if idx == 0 or sub_norm[idx - 1] == " ":
                after = sub_norm[idx + len(target_norm):]
                if not after or after[0] == " ":
                    before = sub_norm[:idx].strip()
                    if before and before.split()[-1] in {"boruto", "sd"}:
                        return False
                    return True

    return False


def match_subtitles(
    subs: list[dict], target_title: str,
    target_season: Optional[int], target_episode: Optional[int]
) -> list[dict]:
    """Filtruje napisy po tytule, sezonie i odcinku."""
    matched = []
    for s in subs:
        # Filtr tytułu — najważniejszy
        sub_titles = [s.get("title_org", ""), s.get("title_eng", ""), s.get("title_alt", "")]
        if not _title_matches(sub_titles, target_title):
            continue

        # Filtr odcinka
        if target_episode is not None and s["episode"] is not None and s["episode"] != target_episode:
            continue

        # Filtr sezonu
        if target_season is not None and s["season"] is not None:
            if target_season != 1 and s["season"] != target_season:
                continue

        matched.append(s)
    return matched


# ══════════════════════════════════════════════════════════════
#  KONWERSJA ASS → SRT
# ══════════════════════════════════════════════════════════════

def _ass_time_to_ms(value: str) -> int:
    """Konwertuje czas ASS/SSA do milisekund."""
    match = re.fullmatch(r"\s*(\d+):(\d{1,2}):(\d{1,2})\.(\d{1,3})\s*", value)
    if not match:
        raise ValueError(f"Nieprawidłowy czas ASS: {value!r}")

    hours, minutes, seconds, fraction = match.groups()
    minutes_i = int(minutes)
    seconds_i = int(seconds)
    if minutes_i > 59 or seconds_i > 59:
        raise ValueError(f"Nieprawidłowy czas ASS: {value!r}")

    milliseconds = int(fraction.ljust(3, "0")[:3])
    return (((int(hours) * 60) + minutes_i) * 60 + seconds_i) * 1000 + milliseconds


def _ms_to_srt(value: int) -> str:
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def _strip_ass_tags(text: str) -> str:
    """
    Usuwa tagi ASS i fragmenty rysunkowe \\p1...\\p0.

    Wcześniej po usunięciu samego tagu {\\p1} komendy wektorowe mogły
    trafić do SRT jako zwykły tekst.
    """
    tag_pattern = re.compile(r"\{([^}]*)\}")
    drawing_pattern = re.compile(r"\\p(\d+)", re.I)

    output = []
    cursor = 0
    drawing_mode = 0

    for match in tag_pattern.finditer(text):
        if drawing_mode == 0:
            output.append(text[cursor:match.start()])

        drawing_tags = drawing_pattern.findall(match.group(1))
        if drawing_tags:
            drawing_mode = int(drawing_tags[-1])

        cursor = match.end()

    if drawing_mode == 0:
        output.append(text[cursor:])

    result = "".join(output)
    result = result.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
    return "\n".join(line.strip() for line in result.splitlines() if line.strip()).strip()

def _deoverlap_srt(srt_text: str, gap_ms: int = 50) -> str:
    """
    Przycina czas końca napisu, jeśli nachodzi na następny.

    ASS obsługuje równoległe warstwy i pozycjonowanie, SRT nie.
    Po konwersji kilka eventów może więc zostać wyświetlonych
    w tym samym miejscu. Ta funkcja ogranicza takie nakładanie.
    """

    # Normalizacja CRLF/LF
    text = srt_text.replace("\r\n", "\n").replace("\r", "\n").strip()

    blocks = re.split(r"\n\s*\n", text)
    parsed = []

    time_pattern = re.compile(
        r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*"
        r"(\d{2}:\d{2}:\d{2},\d{3})"
    )

    def parse_time(value: str) -> int:
        h, m, rest = value.split(":")
        s, ms = rest.split(",")
        return (
            int(h) * 3_600_000
            + int(m) * 60_000
            + int(s) * 1000
            + int(ms)
        )

    def format_time(value: int) -> str:
        value = max(0, value)

        h, remainder = divmod(value, 3_600_000)
        m, remainder = divmod(remainder, 60_000)
        s, ms = divmod(remainder, 1000)

        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    for block in blocks:
        lines = block.strip().split("\n")

        if len(lines) < 3:
            continue

        match = time_pattern.search(lines[1])
        if not match:
            continue

        parsed.append({
            "start": parse_time(match.group(1)),
            "end": parse_time(match.group(2)),
            "text": "\n".join(lines[2:]).strip(),
        })

    parsed.sort(key=lambda item: (item["start"], item["end"]))

    for i in range(len(parsed) - 1):
        current = parsed[i]
        nxt = parsed[i + 1]

        if current["end"] <= nxt["start"]:
            continue

        # Jeśli następny napis faktycznie zaczyna się później,
        # kończymy obecny 50 ms wcześniej.
        if nxt["start"] > current["start"]:
            current["end"] = max(
                current["start"] + 1,
                nxt["start"] - gap_ms,
            )

    output = []

    for i, item in enumerate(parsed, 1):
        output.extend([
            str(i),
            f"{format_time(item['start'])} --> {format_time(item['end'])}",
            item["text"],
            "",
        ])

    return "\r\n".join(output).rstrip() + "\r\n"

def convert_ass_to_srt(ass_content: str) -> str:
    """Konwertuje ASS/SSA -> SRT bez zmieniania celowych overlapów."""
    lines = ass_content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    in_events = False
    format_fields: list[str] = []
    dialogues = []
    seen = set()

    for raw_line in lines:
        line = raw_line.strip()
        lowered = line.lower()

        if lowered == "[events]":
            in_events = True
            format_fields = []
            continue

        if line.startswith("[") and line.endswith("]"):
            in_events = False
            continue

        if not in_events:
            continue

        if lowered.startswith("format:"):
            format_fields = [field.strip().lower() for field in line[7:].split(",")]
            continue

        if not lowered.startswith("dialogue:") or not format_fields:
            continue

        # maxsplit zachowuje przecinki znajdujące się w polu Text.
        values = line[9:].lstrip().split(",", len(format_fields) - 1)
        if len(values) != len(format_fields):
            continue

        event = dict(zip(format_fields, values))
        try:
            start_ms = _ass_time_to_ms(event["start"])
            end_ms = _ass_time_to_ms(event["end"])
        except (KeyError, ValueError):
            continue

        if end_ms <= start_ms:
            continue

        plain_text = _strip_ass_tags(event.get("text", ""))
        if not plain_text:
            continue

        # Warstwy ASS często duplikują ten sam tekst dla cienia/obrysu.
        key = (start_ms, end_ms, plain_text)
        if key in seen:
            continue
        seen.add(key)
        dialogues.append(key)

    if not dialogues:
        raise ValueError("ASS nie zawiera dialogów możliwych do konwersji")

    dialogues.sort(key=lambda item: (item[0], item[1], item[2]))

    output = []
    for index, (start_ms, end_ms, plain_text) in enumerate(dialogues, 1):
        output.extend([
            str(index),
            f"{_ms_to_srt(start_ms)} --> {_ms_to_srt(end_ms)}",
            plain_text,
            "",
        ])

    return "\r\n".join(output).rstrip() + "\r\n"


def _decode_subtitle_bytes(content: bytes) -> str:
    """Dekoduje najczęstsze kodowania napisów z AnimeSub."""
    for encoding in ("utf-8-sig", "windows-1250", "iso-8859-2"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise ValueError("Nie udało się rozpoznać kodowania napisów")


def _looks_like_html(text: str) -> bool:
    sample = text.lstrip("\ufeff \t\r\n").lower()[:1500]
    return (
        sample.startswith("<!doctype html")
        or sample.startswith("<html")
        or "<html" in sample
        or "<body" in sample
    )


SRT_TIMING_RE = re.compile(
    r"(?m)^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s+-->\s+"
    r"\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*$"
)


def _detect_subtitle_format(text: str, extension: Optional[str] = None) -> Optional[str]:
    """Wykrywa format po zawartości; rozszerzenie jest tylko fallbackiem."""
    sample = text.lstrip("\ufeff \t\r\n")

    if (
        re.search(r"(?mi)^\s*\[events\]\s*$", sample)
        and re.search(r"(?mi)^\s*dialogue\s*:", sample)
    ):
        return "ass"

    if SRT_TIMING_RE.search(sample):
        return "srt"

    if sample.startswith("WEBVTT"):
        return "vtt"

    if re.search(r"(?m)^\{\d+\}\{\d+\}", sample):
        return "microdvd"

    if re.search(r"(?m)^\d{1,2}:\d{2}:\d{2}[:|]", sample):
        return "tmplayer"

    ext = (extension or "").lower().lstrip(".")
    return {
        "ass": "ass",
        "ssa": "ass",
        "srt": "srt",
        "sub": "microdvd",
    }.get(ext)


def _normalize_srt(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return normalized.replace("\n", "\r\n") + "\r\n"


def _is_valid_srt(text: str) -> bool:
    if _looks_like_html(text):
        return False
    if re.search(r"(?mi)^\s*\[events\]\s*$", text):
        return False
    return bool(SRT_TIMING_RE.search(text))


def convert_microdvd_to_srt(content: str, fps: float = 23.976) -> str:
    """Konwertuje napisy MicroDVD ({start}{stop}tekst) do SRT."""
    pattern = re.compile(r"\{(\d+)\}\{(\d+)\}(.+)")
    dialogues = []

    for line in content.split("\n"):
        line = line.strip()
        m = pattern.match(line)
        if not m:
            continue
        start_frame, end_frame, text = int(m.group(1)), int(m.group(2)), m.group(3)

        # Pierwsza linia może zawierać info o FPS: {1}{1}23.976
        if start_frame <= 1 and end_frame <= 1:
            try:
                detected_fps = float(text.replace(",", "."))
                if 10 < detected_fps < 60:
                    fps = detected_fps
                    log.info(f"[MicroDVD] Wykryto FPS: {fps}")
            except ValueError:
                pass
            continue

        text = text.replace("|", "\n")

        def frames_to_time(frames: int) -> str:
            total_seconds = frames / fps
            h = int(total_seconds // 3600)
            m = int((total_seconds % 3600) // 60)
            s = int(total_seconds % 60)
            ms = int((total_seconds % 1) * 1000)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

        dialogues.append({
            "start": frames_to_time(start_frame),
            "end": frames_to_time(end_frame),
            "text": text,
        })

    out = []
    for i, d in enumerate(dialogues, 1):
        out.extend([str(i), f"{d['start']} --> {d['end']}", d["text"], ""])
    return "\n".join(out)

def convert_tmplayer_to_srt(content: str) -> str:
    """Konwertuje napisy TMPlayer (HH:MM:SS:tekst) do SRT."""
    pattern = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[:\|](.+)")
    entries = []

    for line in content.split("\n"):
        line = line.strip()
        m = pattern.match(line)
        if not m:
            continue
        h, mi, s, text = m.groups()
        text = text.replace("|", "\n")
        start_sec = int(h) * 3600 + int(mi) * 60 + int(s)
        entries.append({"start_sec": start_sec, "text": text})

    # Wylicz end na podstawie następnej linijki + długości tekstu
    dialogues = []
    for i, e in enumerate(entries):
        # Bazowy czas trwania — zależny od długości tekstu (ok. 15 znaków/sekunda + 1s zapasu)
        text_len = len(e["text"].replace("\n", " "))
        natural_duration = max(1.5, min(7.0, text_len / 15 + 1))

        if i + 1 < len(entries):
            # Zostaw małą przerwę przed następną (0.1s)
            max_duration = entries[i + 1]["start_sec"] - e["start_sec"] - 0.1
            duration = min(natural_duration, max_duration) if max_duration > 0.5 else max_duration
            if duration < 0.5:
                duration = 0.5  # minimum
        else:
            duration = natural_duration

        end_sec = e["start_sec"] + duration

        def fmt(t: float) -> str:
            h_ = int(t // 3600)
            m_ = int((t % 3600) // 60)
            s_ = int(t % 60)
            ms_ = int((t % 1) * 1000)
            return f"{h_:02d}:{m_:02d}:{s_:02d},{ms_:03d}"

        dialogues.append({
            "start": fmt(e["start_sec"]),
            "end": fmt(end_sec),
            "text": e["text"],
        })

    out = []
    for i, d in enumerate(dialogues, 1):
        out.extend([str(i), f"{d['start']} --> {d['end']}", d["text"], ""])
    return "\n".join(out)

# ══════════════════════════════════════════════════════════════
#  POBIERANIE NAPISÓW (proxy endpoint)
#
#  Kluczowa sprawa: animesub.info wiąże hash z sesją (ciasteczkami).
#  Trzeba:
#  1. Wejść na stronę wyszukiwania → ciasteczka + świeży hash
#  2. POSTnąć na sciagnij.php z tymi ciasteczkami i świeżym hashem
# ══════════════════════════════════════════════════════════════

@app.get("/subtitles/download")
async def download_subtitle(id: str, hash: str, query: str = "test", type: str = "org", sort: str = "pobrn", page: int = 0):
    """Proxy do pobierania napisów z animesub.info."""
    log.info(f"[Download] id={id}, query={query}")

    def find_hash(html: str) -> Optional[str]:
        soup = BeautifulSoup(html, "html.parser")
        for form in soup.find_all("form"):
            method = (form.get("method") or "").lower()
            action = (form.get("action") or "").split("?", 1)[0].rstrip("/")
            if method != "post" or not action.endswith("sciagnij.php"):
                continue

            form_id = form.find("input", attrs={"name": "id"})
            if not form_id or form_id.get("value") != str(id):
                continue

            sh = form.find("input", attrs={"name": "sh"})
            if sh and sh.get("value"):
                return sh.get("value")
        return None

    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            cookies=httpx.Cookies(),
        ) as client:
            # Krok 1: ta sama sesja musi pobrać stronę i świeży hash.
            search_params = {"szukane": query, "pTitle": type, "pSortuj": sort}
            if page > 0:
                search_params["od"] = page
            search_full_url = f"{SEARCH_URL}?{urlencode(search_params)}"

            log.info("[Download] Krok 1: Pobieram stronę wyszukiwania")
            search_resp = await client.get(search_full_url, headers=COMMON_HEADERS)
            if search_resp.status_code != 200:
                log.error(f"[Download] Search HTTP {search_resp.status_code}")
                return PlainTextResponse("AnimeSub search failed", status_code=502)

            search_html = search_resp.content.decode("iso-8859-2", errors="replace")
            fresh_hash = find_hash(search_html)

            # Gdy wynik z listy nie zawiera już ID, spróbuj bezpośredniej strony wpisu.
            if not fresh_hash:
                direct_url = f"{SEARCH_URL}?ID={id}"
                log.info(f"[Download] Hash nie znaleziony na liście, próbuję {direct_url}")
                direct_resp = await client.get(direct_url, headers=COMMON_HEADERS)
                if direct_resp.status_code == 200:
                    direct_html = direct_resp.content.decode("iso-8859-2", errors="replace")
                    fresh_hash = find_hash(direct_html)
                    if fresh_hash:
                        search_full_url = str(direct_resp.url)

            if not fresh_hash:
                log.error(f"[Download] Brak świeżego hasha dla id={id}")
                return PlainTextResponse("Fresh AnimeSub hash not found", status_code=502)

            log.info(f"[Download] ✓ Świeży hash dla id={id}")

            # Krok 2: POST musi iść od razu po HTTPS. Przy HTTP -> HTTPS
            # redirect 301/302 POST może zostać zamieniony na GET.
            log.info("[Download] Krok 2: Pobieram napisy")
            dl_resp = await client.post(
                DOWNLOAD_URL,
                data={"id": id, "sh": fresh_hash, "single_file": "Pobierz napisy"},
                headers={
                    **COMMON_HEADERS,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": search_full_url,
                    "Origin": ANIMESUB_BASE,
                },
            )

            if dl_resp.status_code != 200:
                log.error(
                    f"[Download] Download HTTP {dl_resp.status_code}; final_url={dl_resp.url}"
                )
                return PlainTextResponse("AnimeSub download failed", status_code=502)

            content = dl_resp.content
            content_type = dl_resp.headers.get("content-type", "")
            log.info(
                f"[Download] Pobrano {len(content)} bajtów; "
                f"content-type={content_type}; final_url={dl_resp.url}"
            )

            if not content:
                return PlainTextResponse("AnimeSub returned empty data", status_code=502)

            raw = content.decode("latin-1", errors="ignore")
            if "zabezpiecze" in raw or "Błąd" in raw or "B³±d" in raw:
                log.error("[Download] ✗ BŁĄD ZABEZPIECZEŃ")
                return PlainTextResponse("Security error", status_code=502)

            subtitle_ext: Optional[str] = None

            if zipfile.is_zipfile(io.BytesIO(content)):
                log.info("[Download] Rozpakowuję ZIP...")
                try:
                    with zipfile.ZipFile(io.BytesIO(content)) as zf:
                        all_files = [name for name in zf.namelist() if not name.endswith("/")]
                        subtitle_files = [
                            name for name in all_files
                            if re.search(r"\.(srt|ass|ssa|sub|txt)$", name, re.I)
                        ]
                        log.info(f"[Download] Pliki napisów w ZIP: {subtitle_files}")

                        if not subtitle_files:
                            return PlainTextResponse("No supported subtitle in ZIP", status_code=502)

                        priority = {".srt": 0, ".ass": 1, ".ssa": 1, ".sub": 2, ".txt": 3}
                        sub_name = min(
                            subtitle_files,
                            key=lambda name: priority.get(
                                "." + name.rsplit(".", 1)[-1].lower(), 99
                            ),
                        )
                        content = zf.read(sub_name)
                        subtitle_ext = "." + sub_name.rsplit(".", 1)[-1].lower()
                        log.info(f"[Download] Rozpakowano: {sub_name}")
                except zipfile.BadZipFile:
                    return PlainTextResponse("Bad ZIP", status_code=502)

            try:
                text = _decode_subtitle_bytes(content)
            except ValueError as exc:
                log.error(f"[Download] {exc}")
                return PlainTextResponse("Unsupported subtitle encoding", status_code=502)

            # Najważniejsza ochrona: nigdy nie wysyłaj strony PHP/HTML jako SRT.
            if _looks_like_html(text):
                log.error(
                    f"[Download] AnimeSub zwrócił HTML zamiast napisów; final_url={dl_resp.url}"
                )
                return PlainTextResponse("AnimeSub returned HTML instead of subtitles", status_code=502)

            subtitle_format = _detect_subtitle_format(text, subtitle_ext)
            log.info(f"[Download] Wykryty format: {subtitle_format}; ext={subtitle_ext}")

            try:
                if subtitle_format == "ass":
                    text = convert_ass_to_srt(text)
                elif subtitle_format == "microdvd":
                    text = convert_microdvd_to_srt(text)
                elif subtitle_format == "tmplayer":
                    text = convert_tmplayer_to_srt(text)
                elif subtitle_format == "srt":
                    text = _normalize_srt(text)
                elif subtitle_format == "vtt":
                    return PlainTextResponse("WebVTT is not supported yet", status_code=415)
                else:
                    return PlainTextResponse("Unsupported subtitle format", status_code=415)
            except Exception as exc:
                log.error(
                    f"[Download] Błąd konwersji {subtitle_format} -> SRT: {exc}",
                    exc_info=True,
                )
                return PlainTextResponse("Subtitle conversion failed", status_code=502)

            if subtitle_format in ("ass", "microdvd", "tmplayer"):
                text = _deoverlap_srt(text)

            # _deoverlap_srt nie jest uruchamiany. Overlapy w ASS są często celowe.
            if not _is_valid_srt(text):
                log.error("[Download] Wynik konwersji nie jest poprawnym SRT")
                return PlainTextResponse("Invalid SRT after conversion", status_code=502)

            log.info(
                f"[Download] ✓ Wysyłam SRT ({len(text)} znaków, źródło={subtitle_format})"
            )
            return Response(
                content=text.encode("utf-8"),
                media_type="text/srt; charset=utf-8",
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Content-Disposition": 'inline; filename="subtitle.srt"',
                    "X-Subtitle-Source-Format": subtitle_format or "unknown",
                },
            )

    except Exception as e:
        log.error(f"[Download] Błąd: {e}", exc_info=True)
        return PlainTextResponse("Download failed", status_code=500)


# ══════════════════════════════════════════════════════════════
#  GŁÓWNY ENDPOINT NAPISÓW DLA STREMIO
# ══════════════════════════════════════════════════════════════

@app.get("/subtitles/{content_type}/{content_id}/{extra:path}")
async def subtitles_handler_extra(content_type: str, content_id: str, extra: str = ""):
    return await subtitles_handler(content_type, content_id)


@app.get("/subtitles/{content_type}/{content_id}.json")
async def subtitles_handler(content_type: str, content_id: str):
    """Endpoint wywoływany przez Stremio."""
    log.info(f"\n[Request] type={content_type}, id={content_id}")

    try:
        meta = await get_meta_info(content_type, content_id)
        log.info(f'[Meta] title="{meta["title"]}", S{meta["season"]}E{meta["episode"]}')

        if not meta["title"]:
            return JSONResponse(content={"subtitles": []})

        strategies = generate_search_strategies(meta["title"], meta["season"], meta["episode"])
        all_subs = []
        seen = set()

        for strat in strategies:
            log.info(f'[Strategia] "{strat["query"]}" ({strat["type"]})')
            results = await search_subtitles(strat["query"], strat["type"])
            matched = match_subtitles(results, meta["title"], meta["season"], meta["episode"])

            for sub in matched:
                if sub["id"] not in seen:
                    seen.add(sub["id"])
                    all_subs.append({**sub, "sq": strat["query"], "st": strat["type"]})

            exact = any(
                s["episode"] == meta["episode"]
                and (meta["season"] in (None, 1) or s["season"] == meta["season"])
                for s in matched
            )
            if exact and matched:
                log.info("[Znaleziono] Dokładne dopasowanie")
                break
            if len(all_subs) >= 5:
                break

        all_subs.sort(key=lambda s: s.get("download_count", 0), reverse=True)

        stremio_subs = []
        for sub in all_subs[:10]:
            label = " | ".join(filter(None, [
                sub["title_eng"] or sub["title_org"],
                f"by {sub['author']}" if sub["author"] else None,
                sub["format_type"] or None,
                f"{sub['download_count']} pobrań" if sub["download_count"] else None,
            ]))

            params = urlencode({
                "id": sub["id"], "hash": sub["hash"],
                "query": sub["sq"], "type": sub["st"],
                "sort": sub.get("sort", "pobrn"),
                "page": sub.get("page", 0),
            })

            stremio_subs.append({
                "id": f"animesub-{sub['id']}",
                "url": f"{BASE_URL}/subtitles/download?{params}",
                "lang": "pol",
                "SubtitleName": label,
            })

        log.info(f"[Wynik] Zwracam {len(stremio_subs)} napisów")
        return JSONResponse(content={"subtitles": stremio_subs})

    except Exception as e:
        log.error(f"[Błąd] {e}", exc_info=True)
        return JSONResponse(content={"subtitles": []})


# ── Uruchomienie ──────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8080))

    if not os.environ.get("BASE_URL"):
        sh = os.environ.get("SPACE_HOST")
        si = os.environ.get("SPACE_ID")
        if sh:
            BASE_URL = f"https://{sh}"
        elif si:
            BASE_URL = f"https://{si.replace('/', '-').lower()}.hf.space"
        else:
            BASE_URL = f"http://localhost:{port}"

    log.info(f"BASE_URL: {BASE_URL}")
    uvicorn.run(app, host="0.0.0.0", port=port)