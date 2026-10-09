#!/usr/bin/env python3
"""Aggiorna i dati LBA e Serie C da fonti pubbliche, senza chiavi API."""
import datetime as dt
import html
import json
import re
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

UA = {"User-Agent": "SportLive/1.0 (public data updater; calendar source: Lega Pro)"}
DATA_FILE = Path("data/italian.json")
SERIE_C_URL = "https://www.seriec.com/calendario"
ROME = ZoneInfo("Europe/Rome")
MONTHS = {
    "gen": 1, "feb": 2, "mar": 3, "apr": 4, "mag": 5, "giu": 6,
    "lug": 7, "ago": 8, "set": 9, "ott": 10, "nov": 11, "dic": 12,
}
LIVE_CLASSES = {"live", "in-progress", "in_progress", "inplay", "in-play", "on-track", "match-live"}
LIVE_STATUS_TEXT = {"live", "in corso", "in progress", "in-play", "in play"}


def get_json(url):
    req = urllib.request.Request(url, headers={**UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def get_text(url):
    req = urllib.request.Request(url, headers={**UA, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=30) as response:
        body = response.read(5_000_000)
    return body.decode("utf-8", "replace")


def existing_data():
    try:
        with DATA_FILE.open(encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError):
        return {}


def lba_games():
    seasons = get_json(
        "https://www.legabasket.it/api/championships/get-championships?current=1&items=1000"
    ).get("competitions", [])
    current = [x for x in seasons if x.get("cserie_code") == "A1" and x.get("status") == 1]
    if not current:
        current = [x for x in seasons if x.get("cserie_code") == "A1"]
    if not current:
        raise RuntimeError("LBA: campionato corrente non trovato")
    championship_id = max(current, key=lambda x: x.get("year", 0)).get("id")
    payload = get_json(
        "https://www.legabasket.it/api/championships/"
        f"get-championships-calendar-by-id?id={championship_id}"
    )
    out = []
    for match in payload.get("matches", []):
        status = str(match.get("game_status", ""))
        label = "Finale" if status == "2" else ("LIVE" if status in {"1", "3"} else "In programma")
        has_score = status in {"1", "2", "3"} and all(
            match.get(key) is not None for key in ("home_final_score", "visitor_final_score")
        )
        out.append({
            "home": match.get("h_team_name", "?"),
            "away": match.get("v_team_name", "?"),
            "homeScore": match.get("home_final_score", ""),
            "awayScore": match.get("visitor_final_score", ""),
            "hasScore": has_score,
            "status": label,
            "time": match.get("match_datetime", ""),
            "competition": "Serie A LBA",
            "source": "LBA",
            "sourceUrl": "https://www.legabasket.it/calendario/1/serie-a",
        })
    return out


class SerieCMatchParser(HTMLParser):
    """Legge i blocchi partita server-rendered del calendario ufficiale, senza dipendenze esterne."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.current = None
        self.rows = []

    @staticmethod
    def _classes(frame):
        return set(frame.get("class", "").split())

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        frame = {"tag": tag, "id": attrs.get("id", ""), "class": attrs.get("class", ""), "attrs": attrs}
        classes = self._classes(frame)
        if self.current is not None:
            if classes & LIVE_CLASSES:
                self.current["explicitLive"] = True
            state = str(attrs.get("data-status") or attrs.get("data-state") or "").strip().lower()
            if state in LIVE_STATUS_TEXT:
                self.current["explicitLive"] = True
        if tag == "div" and "match-row" in classes:
            group = next((node["id"][-1].upper() for node in reversed(self.stack)
                          if node["id"] in {"girone-a", "girone-b", "girone-c"}), "")
            self.current = {
                "group": group,
                "rowClasses": sorted(classes),
                "date": "",
                "time": "",
                "home": "",
                "away": "",
                "scores": [],
                "statusText": [],
                "explicitLive": bool(classes & LIVE_CLASSES),
            }
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(frame)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data):
        if self.current is None:
            return
        value = " ".join(data.split())
        if not value:
            return
        classes = set().union(*(self._classes(node) for node in self.stack)) if self.stack else set()
        if "match-date" in classes:
            self.current["date"] += " " + value
        if "match-time" in classes:
            self.current["time"] += " " + value
        if "team-name-full" in classes:
            if "team-home" in classes:
                self.current["home"] += " " + value
            elif "team-away" in classes:
                self.current["away"] += " " + value
        if "score-num" in classes:
            self.current["scores"].append(value)
        if classes & {"match-status", "live-status", "session-state"}:
            self.current["statusText"].append(value)
            if value.strip().lower() in LIVE_STATUS_TEXT:
                self.current["explicitLive"] = True

    def handle_endtag(self, tag):
        index = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i]["tag"] == tag), None)
        if index is None:
            return
        closed = self.stack[index:]
        if self.current is not None and any(
            node["tag"] == "div" and "match-row" in self._classes(node) for node in closed
        ):
            self.rows.append(self.current)
            self.current = None
        del self.stack[index:]


def parse_serie_c_date(date_text, time_text, now):
    date_text = html.unescape(" ".join(date_text.split()))
    time_text = html.unescape(" ".join(time_text.split()))
    match = re.search(r"(\d{1,2})\s+([A-Za-zÀ-ÿ]{3,})(?:\s+(20\d{2}))?", date_text)
    clock = re.search(r"\b(\d{1,2}):(\d{2})\b", time_text)
    if not match or not clock:
        return None
    day = int(match.group(1))
    month_name = re.sub(r"[^a-z]", "", match.group(2).lower())[:3]
    month = MONTHS.get(month_name)
    if not month:
        return None
    if match.group(3):
        year = int(match.group(3))
    else:
        season_start = now.year if now.month >= 7 else now.year - 1
        year = season_start if month >= 7 else season_start + 1
    try:
        return dt.datetime(year, month, day, int(clock.group(1)), int(clock.group(2)), tzinfo=ROME)
    except ValueError:
        return None


def official_serie_c_games(now=None):
    now = now or dt.datetime.now(ROME)
    page = get_text(SERIE_C_URL)
    parser = SerieCMatchParser()
    parser.feed(page)
    parser.close()
    by_group = {letter: 0 for letter in "ABC"}
    for row in parser.rows:
        if row["group"] in by_group and row["home"].strip() and row["away"].strip() and row["date"].strip():
            by_group[row["group"]] += 1
    if len(parser.rows) < 30 or any(count < 5 for count in by_group.values()):
        raise RuntimeError(f"Lega Pro: struttura calendario inattesa ({len(parser.rows)} righe; gruppi {by_group})")

    start, end = now - dt.timedelta(hours=24), now + dt.timedelta(hours=24)
    matches = []
    seen = set()
    for row in parser.rows:
        group = row["group"]
        home, away = " ".join(row["home"].split()), " ".join(row["away"].split())
        kickoff = parse_serie_c_date(row["date"], row["time"], now)
        if group not in by_group or not home or not away or not kickoff or not start <= kickoff <= end:
            continue
        scores = [int(value) for value in row["scores"] if re.fullmatch(r"\d+", value)]
        has_score = len(scores) >= 2 and kickoff <= now
        status_text = " ".join(row["statusText"]).strip().lower()
        live = row["explicitLive"] or status_text in LIVE_STATUS_TEXT
        if live:
            status = "LIVE"
        elif has_score and kickoff <= now - dt.timedelta(hours=3):
            status = "FT"
        elif has_score:
            status = "Risultato ufficiale"
        elif kickoff > now:
            status = "In programma"
        elif now - kickoff <= dt.timedelta(hours=3):
            status = "Risultato non disponibile"
        else:
            status = "Dati non aggiornati"
        match = {
            "home": home,
            "away": away,
            "homeScore": scores[0] if has_score else "",
            "awayScore": scores[1] if has_score else "",
            "hasScore": has_score,
            "status": status,
            "time": kickoff.isoformat(),
            "eventDate": kickoff.isoformat(),
            "competition": "Serie C",
            "group": f"Girone {group}",
            "source": "Lega Pro",
            "sourceUrl": SERIE_C_URL,
        }
        key = (group, home.casefold(), away.casefold(), kickoff.isoformat())
        if key not in seen:
            seen.add(key)
            matches.append(match)
    return sorted(matches, key=lambda match: match["time"])


def main():
    now_utc = dt.datetime.now(dt.timezone.utc)
    now_rome = now_utc.astimezone(ROME)
    old = existing_data()
    result = {
        "updatedAt": now_utc.isoformat(),
        "lba": old.get("lba", []),
        "serieC": old.get("serieC", []),
    }
    try:
        result["lba"] = lba_games()
    except Exception as exc:
        result["lbaError"] = f"LBA: {exc}"
    try:
        result["serieC"] = official_serie_c_games(now_rome)
        result["serieCSource"] = "Lega Pro"
        result["serieCUpdatedAt"] = now_utc.isoformat()
    except Exception as exc:
        result["serieCError"] = str(exc)
        result["serieCSource"] = "Lega Pro"
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with DATA_FILE.open("w", encoding="utf-8") as file:
        json.dump(result, file, ensure_ascii=False, indent=2)
        file.write("\n")


if __name__ == "__main__":
    main()
