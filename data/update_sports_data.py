#!/usr/bin/env python3
"""Aggiorna i dati pubblici LBA e Serie C per GitHub Pages."""
import datetime as dt
import json
import os
import re
import time
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


def goal_scorers(events, match=None):
    """Mostra reti verificate dal punteggio, evitando rigori falliti e gol annullati."""
    cancelled = [e for e in events if e.get("type") == "Var" and "goal cancelled" in str(e.get("detail") or "").lower()]

    def is_cancelled(goal):
        for var in cancelled:
            var_team = var.get("team") or {}
            goal_team = goal.get("team") or {}
            same_team = (
                var_team.get("id") == goal_team.get("id") if var_team.get("id") is not None and goal_team.get("id") is not None
                else bool(var_team.get("name")) and str(var_team.get("name")).casefold() == str(goal_team.get("name") or "").casefold()
            )
            goal_player = goal.get("player") or {}
            var_player = var.get("player") or {}
            same_player = (goal_player.get("id") and goal_player.get("id") == var_player.get("id")) or (
                goal_player.get("name") and goal_player.get("name") == var_player.get("name")
            )
            goal_minute = (goal.get("time") or {}).get("elapsed")
            var_minute = (var.get("time") or {}).get("elapsed")
            close = goal_minute is not None and var_minute is not None and abs(goal_minute - var_minute) <= 10
            if same_team and (same_player or (not var_player.get("id") and not var_player.get("name") and close)):
                return True
        return False

    goals = [
        e for e in events
        if e.get("type") == "Goal" and e.get("detail") != "Missed Penalty" and not is_cancelled(e)
    ]
    expected = {}
    if match:
        expected = {
            str(match["home"]).casefold(): int(match["homeScore"]),
            str(match["away"]).casefold(): int(match["awayScore"]),
        }
        counts = {}
        for event in goals:
            team_name = str((event.get("team") or {}).get("name") or "").casefold()
            counts[team_name] = counts.get(team_name, 0) + 1
        # Se il provider include reti annullate senza evento VAR, non attribuirle a un marcatore.
        inconsistent = {team for team, count in counts.items() if team in expected and count > expected[team]}
        goals = [e for e in goals if str((e.get("team") or {}).get("name") or "").casefold() not in inconsistent]

    labels = []
    for event in goals:
        player = (event.get("player") or {}).get("name") or "Marcatore non indicato"
        team = (event.get("team") or {}).get("name") or ""
        time = event.get("time") or {}
        minute = str(time["elapsed"]) if time.get("elapsed") is not None else "—"
        if time.get("extra"):
            minute += f"+{time['extra']}"
        detail = (event.get("detail") or "").lower()
        kind = " (aut.)" if "own goal" in detail else (" (rig.)" if "penalty" in detail else "")
        assist = (event.get("assist") or {}).get("name")
        label = f"{player} {minute}'{kind}"
        if team:
            label += f" · {team}"
        if assist:
            label += f" (assist: {assist})"
        labels.append(label)
    if expected and labels and len(labels) < sum(expected.values()):
        labels[0] = "Dati parziali — " + labels[0]
    return labels


def fixture_goal_details(fixture_id, key, match=None):
    """L'endpoint fixtures/events?fixture restituisce i gol di una partita."""
    query = urllib.parse.urlencode({"fixture": fixture_id})
    data = get_json(f"{API_FOOTBALL_URL}/events?{query}", {"x-apisports-key": key})
    if data.get("errors"):
        raise RuntimeError(f"API-Football eventi: {data['errors']}")
    if not isinstance(data.get("response"), list):
        raise RuntimeError("API-Football eventi: risposta non valida")
    return goal_scorers(data["response"], match)


def enrich_serie_c_scorers(matches, key, previous, now):
    """Aggiorna i marcatori per gara; massimo 20 richieste al giorno sul piano gratuito."""
    old_cache = previous.get("serieCScorersCache") or {}
    ids = {str(m["fixtureId"]) for m in matches if m.get("fixtureId")}
    cache = {match_id: old_cache[match_id] for match_id in ids if match_id in old_cache}
    day = now.astimezone(ZoneInfo("Europe/Rome")).date().isoformat()
    old_quota = previous.get("serieCScorerQuota") or {}
    quota = {"date": day, "used": old_quota.get("used", 0) if old_quota.get("date") == day else 0}
    pending = []
    for match in matches:
        match_id = match.get("fixtureId")
        if not match_id or match.get("source") != "API-Football":
            continue
        try:
            event_time = dt.datetime.fromisoformat(match["time"])
            goals = int(match["homeScore"]) + int(match["awayScore"])
            recent = abs((now - event_time).total_seconds()) <= 24 * 3600
        except (ValueError, TypeError, KeyError):
            continue
        if not recent or goals <= 0:
            continue
        entry = cache.get(str(match_id)) or {}
        score = f"{match['homeScore']}-{match['awayScore']}"
        if entry.get("score") != score:
            pending.append(match)
        elif len(entry.get("scorers") or []) > goals:
            pending.append(match)  # Cache precedente con gol annullati: correggi subito.
        elif len(entry.get("scorers") or []) < goals and entry.get("attempts", 0) < 2:
            try:
                age = (now - dt.datetime.fromisoformat(entry["fetchedAt"])).total_seconds()
            except (ValueError, TypeError, KeyError):
                age = float("inf")
            if age >= 6 * 3600:
                pending.append(match)

    errors = []
    pending.sort(key=lambda m: (not str(m.get("status", "")).startswith("LIVE"), -dt.datetime.fromisoformat(m["time"]).timestamp()))
    calls_this_run = 0
    for match in pending:
        if quota["used"] >= 20:
            break
        if calls_this_run:
            time.sleep(10)  # Il piano gratuito limita anche le chiamate al minuto.
        quota["used"] += 1  # Conta anche una richiesta fallita; protegge il piano gratuito.
        calls_this_run += 1
        try:
            scorers = fixture_goal_details(match["fixtureId"], key, match)
        except Exception as exc:
            errors.append(str(exc))
            break
        match_id = str(match["fixtureId"])
        score = f"{match['homeScore']}-{match['awayScore']}"
        prior = cache.get(match_id) or {}
        attempts = prior.get("attempts", 0) + 1 if prior.get("score") == score else 1
        cache[match_id] = {
            "score": score,
            "scorers": scorers,
            "fetchedAt": now.isoformat(),
            "attempts": attempts,
        }
    for match in matches:
        entry = cache.get(str(match.get("fixtureId"))) or {}
        try:
            score_total = int(match["homeScore"]) + int(match["awayScore"])
        except (ValueError, TypeError, KeyError):
            score_total = 0
        if entry.get("scorers") and len(entry["scorers"]) <= score_total:
            match["scorers"] = entry["scorers"]
    return cache, quota, "; ".join(errors)


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
        if matches:
            scorers_cache, quota, scorer_error = enrich_serie_c_scorers(matches, key, old, now)
            result["serieCScorersCache"] = scorers_cache
            result["serieCScorerQuota"] = quota
            if scorer_error:
                result["serieCScorerError"] = scorer_error
    if not result["serieC"]:
        result["serieC"] = serie_c_games()
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with DATA_FILE.open("w", encoding="utf-8") as file:
        json.dump(result, file, ensure_ascii=False, indent=2)
        file.write("\n")


if __name__ == "__main__":
    main()

