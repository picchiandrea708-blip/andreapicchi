#!/usr/bin/env python3
import datetime as dt
import json
import urllib.parse
import urllib.request

UA = {"User-Agent": "SportLive/1.0 (public data updater)"}


def get_json(url):
    req = urllib.request.Request(url, headers={**UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


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
        out.append({
            "home": m.get("h_team_name", "?"),
            "away": m.get("v_team_name", "?"),
            "homeScore": m.get("home_final_score", ""),
            "awayScore": m.get("visitor_final_score", ""),
           "hasScore": status in {"1", "2", "3"},
            "status": label,
            "time": m.get("match_datetime", ""),
            "competition": "Serie A LBA",
            "source": "LBA",
            "sourceUrl": "https://www.legabasket.it/calendario/1/serie-a",
        })
    return out


def serie_c_games():
    out = []
    today = dt.date.today()
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
    result = {"updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(), "lba": [], "serieC": []}
    try:
        result["lba"] = lba_games()
    except Exception as exc:
        result["lbaError"] = str(exc)
    result["serieC"] = serie_c_games()
    with open("data/italian.json", "w", encoding="utf-8") as file:
        json.dump(result, file, ensure_ascii=False, indent=2)
        file.write("\n")


if __name__ == "__main__":
    main()
