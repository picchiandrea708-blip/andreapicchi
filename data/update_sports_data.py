#!/usr/bin/env python3
"""Aggiorna i dati pubblici LBA e Serie C per GitHub Pages."""
import datetime as dt
import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

UA = {"User-Agent": "SportLive/1.0 (public data updater)"}
DATA_FILE = Path("data/italian.json")
API_FOOTBALL_URL = "https://v3.football.api-sports.io/fixtures"
SERIE_C_NAME = re.compile(
    r"^Serie C(?:\s*-\s*(?:Girone\s*[ABC]|Group\s*[ABC]|Promotion.*|Relegation.*|Supercoppa.*))?$",
    re.IGNORECASE,
)


def get_json(url, headers=None):
    req = urllib.request.Request(
        url, headers={**UA, "Accept": "application/json", **(headers or {})}
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


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
    current = [
        x for x in seasons
        if x.get("cserie_code") == "A1" and x.get("status") == 1
    ]
    if not current:
        current = [x for x in seasons if x.get("cserie_code") == "A1"]
    championship_id = max(current, key=lambda x: x.get("year", 0)).get("id")
    payload = get_json(
        "https://www.legabasket.it/api/championships/"
        f"get-championships-calendar-by-id?id={championship_id}"
    )
    out = []
    for m in payload.get("matches", []):
        status = str(m.get("game_status", ""))
        if status == "2":
            label = "Finale"
        elif status in {"1", "3"}:
            label = "LIVE"
        else:
            label = "In programma"
        has_score = status in {"1", "2", "3"} and all(
            m.get(key) is not None
            for key in ("home_final_score", "visitor_final_score")
        )
        out.append({
            "home": m.get("h_team_name", "?"),
            "away": m.get("v_team_name", "?"),
            "homeScore": m.get("home_final_score", ""),
            "awayScore": m.get("visitor_final_score", ""),
            "hasScore": has_score,
            "status": label,
            "time": m.get("match_datetime", ""),
            "competition": "Serie A LBA",
            "source": "LBA",
            "sourceUrl": "https://www.legabasket.it/calendario/1/serie-a",
        })
    return out


def api_football_match(item):
    fixture = item.get("fixture") or {}
    league = item.get("league") or {}
    teams = item.get("teams") or {}
    goals = item.get("goals") or {}
    status = (fixture.get("status") or {}).get("short", "NS")
    ended = status in {"FT", "AET", "PEN", "AWD", "WO"}
    not_started = status in {"NS", "TBD", "PST", "CANC", "ABD"}
    return {
        "home": (teams.get("home") or {}).get("name", "?"),
        "away": (teams.get("away") or {}).get("name", "?"),
        "homeScore": goals.get("home"),
        "awayScore": goals.get("away"),
        "hasScore": not not_started and goals.get("home") is not None and goals.get("away") is not None,
        "status": "FT" if ended else ("In programma" if not_started else f"LIVE · {status}"),
        "time": fixture.get("date", ""),
        "competition": "Serie C",
        "group": league.get("name", ""),
        "source": "API-Football",
        "sourceUrl": "https://www.lega-pro.com/",
        "fixtureId": fixture.get("id"),
    }


def api_football_day(day, key):
    query = urllib.parse.urlencode({"date": day, "timezone": "Europe/Rome"})
    data = get_json(
        f"{API_FOOTBALL_URL}?{query}",
        {"x-apisports-key": key},
    )
    errors = data.get("errors")
    if errors:
        raise RuntimeError(f"API-Football: {errors}")
    if not isinstance(data.get("response"), list):
        raise RuntimeError("API-Football: risposta fixtures non valida")
    if (data.get("paging") or {}).get("total", 1) > 1:
        raise RuntimeError("API-Football: risultati suddivisi in più pagine")
    return [
        api_football_match(item)
        for item in data["response"]
        if (item.get("league") or {}).get("country") == "Italy"
        and SERIE_C_NAME.match((item.get("league") or {}).get("name") or "")
    ]


def api_football_games(key, previous, now):
    """Al massimo circa 52 chiamate/giorno: oggi ogni 30 min, domani ogni 12 h, ieri ogni 24 h."""
    today = now.astimezone(ZoneInfo("Europe/Rome")).date()
    targets = [(today, 30), (today + dt.timedelta(days=1), 720), (today - dt.timedelta(days=1), 1440)]
    old_cache = previous.get("serieCApiCache") or {}
    cache = {day.isoformat(): old_cache[day.isoformat()] for day, _ in targets if day.isoformat() in old_cache}
    errors = []
    for day, ttl_minutes in targets:
        day_key = day.isoformat()
        entry = old_cache.get(day_key) or {}
        last_attempt = entry.get("attemptedAt")
        try:
            age = (now - dt.datetime.fromisoformat(last_attempt)).total_seconds() / 60 if last_attempt else float("inf")
        except (ValueError, TypeError):
            age = float("inf")
        if age >= ttl_minutes:
            try:
                matches = api_football_day(day_key, key)
                entry = {"attemptedAt": now.isoformat(), "matches": matches}
            except Exception as exc:
                errors.append(str(exc))
                # In caso di errore non consumare richieste a ogni esecuzione del workflow.
                entry = {**entry, "attemptedAt": now.isoformat()}
                if "API-Football" in str(exc):
                    cache[day_key] = entry
                    break
        cache[day_key] = entry
    matches = []
    for entry in cache.values():
        matches.extend(entry.get("matches") or [])
    unique = {}
    for match in matches:
        key = match.get("fixtureId") or (match["home"], match["away"], match["time"])
        unique[key] = match
    return list(unique.values()), cache, "; ".join(errors)


def serie_c_games():
    """ESPN gratuito come riserva se API-Football non restituisce Serie C."""
    out = []
    today = dt.datetime.now(ZoneInfo("Europe/Rome")).date()
    for offset in range(-1, 2):
        day = today + dt.timedelta(days=offset)
        url = (
            "https://site.web.api.espn.com/apis/site/v2/sports/soccer/ita.3/"
            f"scoreboard?dates={day:%Y%m%d}&limit=40"
        )
        try:
            events = get_json(url).get("events", [])
        except Exception:
            continue
        for event in events:
            comp = (event.get("competitions") or [{}])[0]
            teams = comp.get("competitors") or []
            home = next((x for x in teams if x.get("homeAway") == "home"), {})
            away = next((x for x in teams if x.get("homeAway") == "away"), {})
            state = (event.get("status") or {}).get("type") or {}
            out.append({
                "home": (home.get("team") or {}).get("displayName", "?"),
                "away": (away.get("team") or {}).get("displayName", "?"),
                "homeScore": home.get("score", ""),
                "awayScore": away.get("score", ""),
                "hasScore": state.get("state") != "pre",
                "status": state.get("shortDetail") or state.get("description") or "",
                "time": event.get("date", ""),
                "competition": "Serie C",
                "source": "ESPN",
                "sourceUrl": "https://www.lega-pro.com/",
            })
    return out


def main():
    now = dt.datetime.now(dt.timezone.utc)
    old = existing_data()
    result = {"updatedAt": now.isoformat(), "lba": old.get("lba", []), "serieC": []}
    try:
        result["lba"] = lba_games()
    except Exception as exc:
        result["lbaError"] = str(exc)
    key = os.environ.get("API_FOOTBALL_KEY", "").strip()
    if key:
        matches, cache, error = api_football_games(key, old, now)
        result["serieCApiCache"] = cache
        if error:
            result["serieCApiError"] = error
        result["serieC"] = matches
    if not result["serieC"]:
        result["serieC"] = serie_c_games()
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with DATA_FILE.open("w", encoding="utf-8") as file:
        json.dump(result, file, ensure_ascii=False, indent=2)
        file.write("\n")


if __name__ == "__main__":
    main()
