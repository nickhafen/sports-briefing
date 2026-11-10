#!/usr/bin/env python3
"""Daily sports briefing.

Everything factual (scores, records, standings, rankings, schedules) comes
straight from ESPN's public JSON API and is rendered by code. The only LLM step
is choosing WHICH already-fetched headlines are "major": the model returns item
numbers, never text, so it cannot add or alter a fact.

Usage:  python briefing.py [--no-llm] [--open] [--email]
Output: output/briefing.html, output/briefing.md, output/archive/YYYY-MM-DD.html
Python 3.9+, standard library only.
"""
import argparse
import gzip
import html
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SITE = "https://site.api.espn.com/apis/site/v2/sports/"
STAND = "https://site.api.espn.com/apis/v2/sports/"

# mode: how to rank a standings group. scope: which group to show.
# The order here is the section order when the page is organized by league or sport.
LEAGUES = {
    "nfl":  dict(label="NFL", sport="Football", path="football/nfl", mode="pct", scope="division", seed=True,
                 web="https://www.espn.com/nfl/standings", sb=""),
    "cfb":  dict(label="College football", sport="Football", path="football/college-football", mode="college",
                 web="https://www.espn.com/college-football/standings", sb="&groups=80&limit=300"),
    "nba":  dict(label="NBA", sport="Basketball", path="basketball/nba", mode="pct", scope="conference",
                 web="https://www.espn.com/nba/standings", sb=""),
    "mbb":  dict(label="Men's college basketball", sport="Basketball", path="basketball/mens-college-basketball",
                 mode="college", web="https://www.espn.com/mens-college-basketball/standings",
                 sb="&groups=50&limit=500"),
    "nhl":  dict(label="NHL", sport="Hockey", path="hockey/nhl", mode="points", scope="division",
                 web="https://www.espn.com/nhl/standings", sb=""),
    "mlb":  dict(label="MLB", sport="Baseball", path="baseball/mlb", mode="pct", scope="division",
                 web="https://www.espn.com/mlb/standings", sb=""),
    "mls":  dict(label="MLS", sport="Soccer", path="soccer/usa.1", mode="soccer", scope="division",
                 web="https://www.espn.com/soccer/standings/_/league/usa.1", sb=""),
    "nwsl": dict(label="NWSL", sport="Soccer", path="soccer/usa.nwsl", mode="soccer", scope="division",
                 web="https://www.espn.com/soccer/standings/_/league/usa.nwsl", sb=""),
}
# Ways to organize the team sections: value -> tab label.
VIEWS = {"location": "By location", "league": "By league", "sport": "By sport"}
CONF_ABBR = {"National Football Conference": "NFC", "American Football Conference": "AFC",
             "Eastern Conference": "East", "Western Conference": "West"}
JUNK_TITLE = re.compile(
    r"media availability|press conference|how to watch|game notes|photo gallery|highlights"
    r"|preview|podcast|postgame|tickets|\bodds\b|fantasy|mock draft|power rankings", re.I)

NOW = datetime.now().astimezone()
ISSUES = []          # things that failed to load; shown in the footer
_cache = {}


# ----------------------------------------------------------------- fetching

def _fetch(url):
    last = None
    for _ in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=25) as r:
                body = r.read()
            return gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body
        except Exception as e:  # noqa: BLE001 - any failure is reported, never guessed around
            last = e
    return last


def fetch_all(urls):
    todo = [u for u in dict.fromkeys(urls) if u not in _cache]
    with ThreadPoolExecutor(max_workers=10) as pool:
        for u, body in zip(todo, pool.map(_fetch, todo)):
            _cache[u] = body


def get_json(url, what):
    """Parsed JSON for a URL, or None (and a recorded issue) if it failed."""
    if url not in _cache:
        fetch_all([url])
    body = _cache[url]
    if isinstance(body, Exception):
        ISSUES.append(what)
        return None
    try:
        return json.loads(body)
    except ValueError:
        ISSUES.append(what)
        return None


# ------------------------------------------------------------------ helpers

def parse_dt(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def fmt_day(dt):
    d = dt.astimezone()
    return f"{d:%a} {d.month}/{d.day}"


def fmt_time(dt):
    d = dt.astimezone()
    return f"{d.hour % 12 or 12}:{d:%M} {'AM' if d.hour < 12 else 'PM'}"


def ordinal(n):
    n = int(n)
    suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def num(s, default=0.0):
    try:
        return float(str(s).replace("+", ""))
    except ValueError:
        return default


def score_of(comp):
    s = comp.get("score")
    if isinstance(s, dict):
        s = s.get("displayValue")
    return None if s in (None, "") else str(s)


def norm_event(e, soccer=False):
    """Flatten a schedule/scoreboard event into the few fields we display."""
    c = (e.get("competitions") or [{}])[0]
    st = (c.get("status") or e.get("status") or {}).get("type", {})
    sides = {}
    for comp in c.get("competitors", []):
        t = comp.get("team", {})
        sides[comp.get("homeAway", "home")] = dict(
            id=str(t.get("id")), abbr=t.get("abbreviation") or t.get("shortDisplayName") or "?",
            name=t.get("shortDisplayName") or t.get("displayName") or t.get("abbreviation") or "?",
            score=score_of(comp), winner=comp.get("winner"))
    if len(sides) < 2:
        return None
    link = next((l.get("href") for l in e.get("links", [])
                 if "summary" in l.get("rel", []) and "desktop" in l.get("rel", [])), None) \
        or next((l.get("href") for l in e.get("links", []) if str(l.get("href", "")).startswith("http")), None)
    season = e.get("seasonType") or e.get("season") or {}
    stext = f"{season.get('name', '')} {season.get('slug', '')}".lower()
    post = season.get("type") == 3 or "playoff" in stext or "final" in stext or "cup" in stext
    if soccer and season.get("slug") and "regular" not in stext:
        post = True
    tv = []
    for b in c.get("broadcasts", []) or []:
        tv += b.get("names", []) or ([b["media"]["shortName"]] if b.get("media", {}).get("shortName") else [])
    heads = c.get("headlines") or []
    return dict(
        id=str(e.get("id")), date=parse_dt(e.get("date")), time_valid=e.get("timeValid", True),
        state=st.get("state"), completed=bool(st.get("completed")),
        detail=st.get("shortDetail") or st.get("description") or "",
        home=sides["home"], away=sides["away"], link=link, post=post,
        note=((c.get("notes") or [{}])[0] or {}).get("headline"),
        series=(c.get("series") or {}).get("summary"),
        recap=(heads[0].get("shortLinkText") if heads else None),
        neutral=bool(c.get("neutralSite")), tv=", ".join(dict.fromkeys(tv)),
        stage=re.sub(r"^(Eastern|Western) Conference ", "", season.get("name") or "")
        if season.get("name") not in (None, "Regular Season") else None)


def stat_map(entry):
    m = {}
    for x in entry.get("stats", []):
        for k in (x.get("name"), x.get("type"), x.get("abbreviation")):
            if k:
                m.setdefault(k, x.get("displayValue"))
    return m


def leaves(node, path=()):
    p = path + (node.get("name") or node.get("abbreviation") or "",)
    if node.get("children"):
        for ch in node["children"]:
            yield from leaves(ch, p)
    elif "standings" in node:
        yield p, node["standings"].get("entries", [])


# ---------------------------------------------------------------- standings

def standings_for(lg_key, team_id, tracked_ids):
    """Returns dict(text, record, streak, table) for a pro team, or None."""
    lg = LEAGUES[lg_key]
    data = get_json(STAND + lg["path"] + "/standings?level=3", f"{lg['label']} standings")
    if not data:
        return None
    all_leaves = list(leaves(data))
    hit = next(((p, ents) for p, ents in all_leaves
                if any(str(e["team"]["id"]) == team_id for e in ents)), None)
    if not hit:
        return None
    path, ents = hit
    group = path[-1]
    if lg.get("scope") == "conference" and len(path) >= 2:
        group = path[-2]
        ents = [e for p, es in all_leaves if len(p) >= 2 and p[-2] == group for e in es]
    rows = [(e, stat_map(e)) for e in ents]
    mode = lg["mode"]
    if mode == "soccer":
        metric = lambda s: -num(s.get("rank"), 99)
    elif mode == "points":
        metric = lambda s: num(s.get("points"))
    else:
        metric = lambda s: num(s.get("winPercent"))
    rows.sort(key=lambda r: (-metric(r[1]), num(r[1].get("gamesPlayed"))))
    mine = next(s for e, s in rows if str(e["team"]["id"]) == team_id)
    better = sum(1 for _, s in rows if metric(s) > metric(mine))
    tied = sum(1 for _, s in rows if metric(s) == metric(mine)) > 1 and mode != "soccer"
    pos = int(num(mine.get("rank"), better + 1)) if mode == "soccer" else better + 1

    def record(s):
        if s.get("overall"):
            return s["overall"].split(",")[0]
        parts = [s.get("wins"), s.get("losses")]
        if s.get("ties") not in (None, "0"):
            parts.append(s["ties"])
        return "-".join(p for p in parts if p is not None)

    def extra(s):
        if mode in ("points", "soccer"):
            return f"{s.get('points', '?')} pts"
        gb = s.get("gamesBehind")
        return "—" if gb in (None, "-", "0") else f"{gb} GB"

    short_group = CONF_ABBR.get(group, group)
    text = f"{'T-' if tied else ''}{ordinal(pos)} in {short_group}"
    if mode in ("points", "soccer"):
        text += f" · {mine.get('points', '?')} pts"
    elif mine.get("gamesBehind") not in (None, "-", "0"):
        text += f" · {mine['gamesBehind']} GB"
    if lg.get("seed") and mine.get("playoffSeed") and len(path) >= 2 and path[-2] in CONF_ABBR:
        text += f" · {CONF_ABBR[path[-2]]} #{mine['playoffSeed']} seed"
    note = next((e.get("note", {}).get("description") for e, s in rows
                 if str(e["team"]["id"]) == team_id and e.get("note")), None)
    table = [dict(pos=i + 1, team=e["team"].get("shortDisplayName") or e["team"].get("displayName"),
                  record=record(s), extra=extra(s), mine=str(e["team"]["id"]) in tracked_ids)
             for i, (e, s) in enumerate(rows)]
    streak = mine.get("streak") if mine.get("streak") not in (None, "-") else None
    return dict(text=text, record=record(mine), streak=streak, note=note,
                table=dict(title=group, rows=table))


def college_standing(lg_key, team_id, team_json):
    lg = LEAGUES[lg_key]
    out = dict(text=(team_json or {}).get("standingSummary"), record=None, streak=None, note=None, table=None)
    data = get_json(STAND + lg["path"] + "/standings?level=3", f"{lg['label']} standings")
    for path, ents in leaves(data) if data else []:
        for e in ents:
            if str(e["team"]["id"]) == team_id:
                s = stat_map(e)
                out["record"] = s.get("overall")
                conf = s.get("vs. Conf.")
                if conf and conf != "0-0":
                    out["text"] = f"{out['text'] or path[-1]} ({conf} conf.)"
    ranks = []
    rk = get_json(SITE + lg["path"] + "/rankings", f"{lg['label']} rankings")
    for poll in (rk or {}).get("rankings", []):
        name = poll.get("shortName") or poll.get("name", "")
        if not re.search(r"AP|Playoff|CFP", name):
            continue
        for r in poll.get("ranks", []):
            if str(r.get("team", {}).get("id")) == team_id:
                tag = "CFP" if re.search(r"Playoff|CFP", name) else "AP"
                ranks.append(f"{tag} #{r['current']}")
    out["ranks"] = ranks
    return out


# -------------------------------------------------------------------- build

def scoreboard_urls(path, extra=""):
    days = [(NOW - timedelta(days=d)).strftime("%Y%m%d") for d in (2, 1, 0, -1)]
    return [f"{SITE}{path}/scoreboard?dates={d}{extra}" for d in days]


def team_urls(t):
    base = f"{SITE}{LEAGUES[t['league']]['path']}/teams/{t['id']}"
    urls = dict(team=base, sched=base + "/schedule", news=f"{SITE}{LEAGUES[t['league']]['path']}/news?team={t['id']}&limit=20")
    if LEAGUES[t["league"]]["mode"] == "soccer":
        urls["fixtures"] = base + "/schedule?fixture=true"
    return urls


def game_line(ev, team_id):
    """One game from the tracked team's point of view."""
    me, opp = (ev["home"], ev["away"]) if ev["home"]["id"] == team_id else (ev["away"], ev["home"])
    where = "vs" if me is ev["home"] else "at"
    if ev["neutral"]:
        where = "vs"
    d = dict(date=fmt_day(ev["date"]), where=where, opp=opp["name"], link=ev["link"], recap=ev["recap"],
             note=ev["note"] or (ev["stage"] if ev["post"] else None), series=ev["series"], tv=ev["tv"],
             time=fmt_time(ev["date"]) if ev["time_valid"] else "", detail="", result="", score="")
    if ev["state"] in ("post", "in") and me["score"] is not None and opp["score"] is not None:
        d["score"] = f"{me['score']}-{opp['score']}"
        if ev["completed"]:
            d["result"] = "W" if me["winner"] else ("L" if opp["winner"] else "T")
            if ev["detail"] not in ("Final", "FT"):
                d["detail"] = ev["detail"]
        else:
            d["detail"] = ev["detail"]
    return d


def build(cfg, use_llm):
    teams = cfg["teams"]
    lg_keys = list(dict.fromkeys([t["league"] for t in teams] + cfg["playoff_leagues"]))

    # ---- one parallel round of requests
    urls = []
    for t in teams:
        urls += team_urls(t).values()
    for k in lg_keys:
        lg = LEAGUES[k]
        urls += scoreboard_urls(lg["path"], lg["sb"])
        urls.append(STAND + lg["path"] + "/standings?level=3")
        if lg["mode"] == "college":
            urls.append(SITE + lg["path"] + "/rankings")
    urls += [f"{SITE}{ev['path']}/scoreboard" for ev in cfg["events"]]
    # a feed with "only_between" date windows (e.g. the Olympics) is skipped outside them
    today = f"{NOW:%Y-%m-%d}"
    cfg["feeds"] = [f for f in cfg["feeds"]
                    if "only_between" not in f or any(lo <= today <= hi for lo, hi in f["only_between"])]
    urls += [f["url"] for f in cfg["feeds"]]
    fetch_all(urls)

    # ---- scoreboards (recaps, playoff games, and a second source for team games)
    boards = {}
    for k in lg_keys:
        lg, evs = LEAGUES[k], {}
        for u in scoreboard_urls(lg["path"], lg["sb"]):
            data = get_json(u, f"{lg['label']} scoreboard")
            for e in (data or {}).get("events", []):
                n = norm_event(e, soccer=lg["mode"] == "soccer")
                if n and n["date"]:
                    evs[n["id"]] = n
        boards[k] = evs

    lookback = NOW - timedelta(hours=cfg["lookback_hours"])
    tracked = {k: {t["id"] for t in teams if t["league"] == k} for k in lg_keys}
    candidates = []      # headline candidates for the LLM filter
    cards = []
    for t in teams:
        lg, tid, u = LEAGUES[t["league"]], t["id"], team_urls(t)
        tj = (get_json(u["team"], f"{t['name']} team info") or {}).get("team") or {}
        events = {}
        sources = [get_json(u["sched"], f"{t['name']} schedule")]
        if "fixtures" in u:
            sources.append(get_json(u["fixtures"], f"{t['name']} fixtures"))
        sources.append({"events": tj.get("nextEvent", [])})
        for src in sources:
            for e in (src or {}).get("events", []):
                n = norm_event(e, soccer=lg["mode"] == "soccer")
                if n and n["date"]:
                    events[n["id"]] = n
        for eid, n in boards[t["league"]].items():      # scoreboard copy is freshest; keeps recap/series
            if tid in (n["home"]["id"], n["away"]["id"]):
                events[eid] = {**events.get(eid, {}), **{k: v for k, v in n.items() if v not in (None, "")}}
        evs = sorted(events.values(), key=lambda n: n["date"])
        done = [n for n in evs if n["completed"]]
        live = [n for n in evs if n["state"] == "in"]
        upcoming = [n for n in evs if n["state"] == "pre" and n["date"] > NOW - timedelta(hours=3)]
        recent = [n for n in done if n["date"] >= lookback]
        older = None
        if not recent and done and done[-1]["date"] >= NOW - timedelta(days=cfg["last_game_max_days"]):
            older = done[-1]
        active = bool(live or (done and done[-1]["date"] >= NOW - timedelta(days=10))
                      or (upcoming and upcoming[0]["date"] <= NOW + timedelta(days=14)))
        st = None
        if active:
            st = college_standing(t["league"], tid, tj) if lg["mode"] == "college" \
                else standings_for(t["league"], tid, tracked[t["league"]])
        record = (st or {}).get("record") or ((tj.get("record", {}).get("items") or [{}])[0].get("summary"))
        # Outside the location view "Lions" alone is unclear, so use ESPN's full name
        # ("Detroit Lions"). College names in config already say the sport ("BYU Football").
        full_name = t["name"] if lg["mode"] == "college" else (tj.get("displayName") or t["name"])
        card = dict(
            key=f"{t['league']}:{tid}", group=t["group"], name=t["name"], full_name=full_name,
            league_key=t["league"], league=lg["label"], sport=lg["sport"], active=active,
            record=record if active else None, standing=st, ranks=(st or {}).get("ranks", []),
            results=[game_line(n, tid) for n in recent], older=game_line(older, tid) if older else None,
            live=[game_line(n, tid) for n in live], next=game_line(upcoming[0], tid) if upcoming else None,
            team_url=next((l["href"] for l in tj.get("links", []) if "clubhouse" in l.get("rel", [])), None),
            standings_url=lg["web"], headlines=[], failed=not events and not tj)
        cards.append(card)

        news = get_json(u["news"], f"{t['name']} news")
        for a in (news or {}).get("articles", []):
            tags = [c.get("teamId") for c in a.get("categories", []) if c.get("type") == "team"]
            pub = parse_dt(a.get("published"))
            href = (a.get("links", {}).get("web") or {}).get("href")
            if (a.get("type") in ("HeadlineNews", "Story") and not a.get("premium") and href and pub
                    and pub >= NOW - timedelta(hours=cfg["news_hours"])
                    and int(tid) in tags and len(tags) <= 4):
                candidates.append(dict(uid=f"espn{a.get('id')}", where=card["key"], source=f"{t['name']} ({lg['label']})",
                                       title=a.get("headline", "").strip(), desc=(a.get("description") or "").strip(),
                                       link=href, date=pub, kind=a.get("type"), hint=""))

    # ---- RSS/Atom feeds (BYU other sports, MLR, Olympics)
    for f in cfg["feeds"]:
        body = _cache.get(f["url"])
        if isinstance(body, Exception) or not body:
            ISSUES.append(f"{f['label']} feed")
            continue
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            ISSUES.append(f"{f['label']} feed")
            continue
        n = 0
        for it in root.iter():
            tag = it.tag.split("}")[-1]
            if tag not in ("item", "entry"):
                continue
            kid = {c.tag.split("}")[-1]: c for c in it}
            title = "".join(kid["title"].itertext()).strip() if "title" in kid else ""
            link = (kid["link"].text or kid["link"].get("href") or "").strip() if "link" in kid else ""
            raw = next((kid[k].text for k in ("pubDate", "updated", "published") if k in kid and kid[k].text), None)
            try:
                pub = parsedate_to_datetime(raw) if raw and "," in raw else parse_dt(raw)
            except (TypeError, ValueError):
                pub = None
            if pub and pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
            desc = re.sub(r"<[^>]+>", " ", html.unescape("".join(kid["description"].itertext()))) if "description" in kid else ""
            if not (title and link.startswith("http") and pub) or pub < NOW - timedelta(hours=cfg["news_hours"]):
                continue
            if JUNK_TITLE.search(title) or n >= 40:
                continue
            n += 1
            candidates.append(dict(uid=f"feed{len(candidates)}", where=f"feed:{f['label']}", source=f["label"],
                                   title=title, desc=" ".join(desc.split())[:220], link=link, date=pub,
                                   kind="feed", hint=f["hint"]))

    # de-duplicate (same ESPN story can be tagged to two tracked teams)
    seen, uniq = set(), []
    for c in candidates:
        if c["uid"] not in seen and c["title"].lower() not in seen:
            seen.update([c["uid"], c["title"].lower()])
            uniq.append(c)
    kept, llm_note = pick_headlines(uniq, cfg, use_llm)
    by_key = {c["key"]: c for c in cards}
    more = {}
    for c in kept:
        item = dict(title=c["title"], link=c["link"], date=fmt_day(c["date"]))
        if c["where"] in by_key:
            by_key[c["where"]]["headlines"].append(item)
        else:
            more.setdefault(c["source"], []).append(item)

    # ---- playoffs across the major leagues
    playoffs = []
    for k in cfg["playoff_leagues"]:
        lines = []
        for n in sorted(boards.get(k, {}).values(), key=lambda n: n["date"]):
            if not n["post"]:
                continue
            if (n["completed"] and n["date"] >= lookback) or n["state"] == "in" or \
                    (n["state"] == "pre" and NOW - timedelta(hours=3) <= n["date"] <= NOW + timedelta(hours=36)):
                lines.append(neutral_line(n))
        if lines:
            playoffs.append(dict(name=f"{LEAGUES[k]['label']} playoffs", url=LEAGUES[k]["web"].replace("standings", "scoreboard"),
                                 lines=lines[:14], extra=max(0, len(lines) - 14)))

    # ---- marquee events
    events, second = [], []
    for ev in cfg["events"]:
        data = get_json(f"{SITE}{ev['path']}/scoreboard", f"{ev['name']} schedule")
        season = ((data or {}).get("leagues") or [{}])[0].get("season", {})
        start, end = parse_dt(season.get("startDate")), parse_dt(season.get("endDate"))
        if not (start and end) or not (start - timedelta(days=7) <= NOW <= end + timedelta(days=2)):
            continue
        ev["_start"] = start
        second += scoreboard_urls(ev["path"])
    fetch_all(second)
    for ev in cfg["events"]:
        if "_start" not in ev:
            continue
        lines = []
        for u in scoreboard_urls(ev["path"]):
            for e in (get_json(u, f"{ev['name']} scores") or {}).get("events", []):
                n = norm_event(e, soccer=True)
                if not n or not n["date"] or (ev.get("playoffs_only") and not n["post"]):
                    continue
                if (n["completed"] and n["date"] >= lookback) or n["state"] == "in" or \
                        (n["state"] == "pre" and NOW - timedelta(hours=3) <= n["date"] <= NOW + timedelta(hours=36)):
                    lines.append((n["date"], n["id"], neutral_line(n)))
        lines = [l for _, _, l in sorted({i: (d, i, l) for d, i, l in lines}.values(), key=lambda x: x[0])]
        if lines:
            events.append(dict(name=ev["name"], url=ev["web"], lines=lines[:10], extra=max(0, len(lines) - 10)))
        elif NOW < ev["_start"] and not ev.get("playoffs_only"):
            events.append(dict(name=ev["name"], url=ev["web"], lines=[], extra=0,
                               starts=f"Starts {fmt_day(ev['_start'])}"))

    return dict(generated=NOW, cards=cards, playoffs=playoffs, events=events, more=more,
                issues=list(dict.fromkeys(ISSUES)), llm_note=llm_note, groups=cfg["groups"],
                organize_by=cfg.get("organize_by", "location"))


def team_sections(b, by):
    """[(heading, cards)] for one way of organizing the teams. Teams keep config order
    within a section; idle teams with no major headline are left out."""
    shown = [c for c in b["cards"] if c["active"] or c["headlines"]]
    if by == "league":
        heads = [(LEAGUES[k]["label"], lambda c, k=k: c["league_key"] == k) for k in LEAGUES]
    elif by == "sport":
        heads = [(s, lambda c, s=s: c["sport"] == s)
                 for s in dict.fromkeys(lg["sport"] for lg in LEAGUES.values())]
    else:
        heads = [(g, lambda c, g=g: c["group"] == g) for g in b["groups"]]
    out = []
    for title, match in heads:
        cards = [c for c in shown if match(c)]
        if cards:
            out.append((title, cards))
    return out


def card_title(c, by):
    return c["name"] if by == "location" else c["full_name"]


def card_meta(c, by):
    st = c["standing"] or {}
    meta = [c["record"]] + c["ranks"] + [st.get("text"), st.get("streak")]
    if by == "sport":                       # e.g. tell NFL from college football
        meta.insert(0, c["league"])
    return [x for x in meta if x]


def neutral_line(n):
    a, h = n["away"], n["home"]
    label = n["note"] or n["stage"] or ""
    if n["state"] == "pre":
        text = f"{a['name']} at {h['name']} — {fmt_day(n['date'])} {fmt_time(n['date'])}" + (f" ({n['tv']})" if n["tv"] else "")
    else:
        first, second = (a, h) if (a["winner"] or not h["winner"]) else (h, a)
        text = f"{first['name']} {first['score']}, {second['name']} {second['score']}"
        text += f" ({n['detail']})" if (not n["completed"] or n["detail"] not in ("Final", "FT")) else ""
        text += f" — {fmt_day(n['date'])}"
    return dict(label=label, text=text, series=n["series"], link=n["link"], recap=n["recap"] if n["completed"] else None)


# --------------------------------------------------------- LLM headline pick

PROMPT = """You are filtering sports headlines for a personal daily briefing. Below are numbered
headlines that were already fetched from news feeds. Choose which ones to show.

Keep a headline ONLY if a casual fan would be expected to know about it: a major trade or signing,
an injury or suspension to a star player, a coach or GM firing/hiring, a notable milestone or award,
a championship, or a playoff berth/elimination.

Drop: game recaps and results (scores are shown separately), previews, rumors and speculation,
mock drafts, betting or fantasy content, power rankings, analysis/opinion columns, minor roster
moves, and anything not really about the listed team. If several headlines cover the same story,
keep only the single best one. When in doubt, drop it. Keeping nothing is a fine answer.

Some items carry an extra "rule" for their source; follow it.

Reply with ONLY a JSON object like {"keep": [3, 17]} using the item numbers. No other text.

HEADLINES
"""


def pick_headlines(cands, cfg, use_llm):
    """Returns (kept candidates, note). The model only returns numbers, so every
    headline shown is the fetched text and link, untouched."""
    if not cands:
        return [], None
    fallback = [c for c in cands if c["kind"] == "HeadlineNews"]
    if not (use_llm and cfg["llm"].get("enabled", True)):
        return fallback, "Headline filter off — showing ESPN's top team headlines unfiltered."
    lines = []
    for i, c in enumerate(cands, 1):
        lines.append(f"[{i}] ({c['source']}) {c['title']}" + (f" — {c['desc'][:200]}" if c["desc"] else "")
                     + (f"\n     rule: {c['hint']}" if c["hint"] else ""))
    prompt = PROMPT + "\n".join(lines)
    exe = shutil.which("claude")
    try:
        if os.environ.get("ANTHROPIC_API_KEY"):
            reply = ask_api(prompt, cfg)            # hosted runs (GitHub Actions)
        elif exe:
            reply = ask_cli(exe, prompt, cfg)       # local runs: your Claude login, no key
        else:
            return fallback, "No AI headline filter configured — showing ESPN's top team headlines unfiltered."
        m = re.search(r"\{[^{}]*\}", reply or "")
        keep = json.loads(m.group(0))["keep"]
        idx = sorted({int(k) for k in keep if 1 <= int(k) <= len(cands)})
        return [cands[i - 1] for i in idx], None
    except Exception as e:  # noqa: BLE001
        return fallback, f"Headline filter failed ({type(e).__name__}) — showing ESPN's top team headlines unfiltered."


LLM_SYSTEM = "You are a precise filter. You reply with a single JSON object and nothing else."


def ask_cli(exe, prompt, cfg):
    proc = subprocess.run(
        [exe, "-p", "--model", cfg["llm"].get("model", "haiku"), "--tools", "",
         "--strict-mcp-config", "--no-session-persistence", "--system-prompt", LLM_SYSTEM],
        input=prompt, capture_output=True, text=True, encoding="utf-8",
        timeout=cfg["llm"].get("timeout_seconds", 180), cwd=str(HERE),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return proc.stdout


def ask_api(prompt, cfg):
    import anthropic                                # only needed when an API key is set
    client = anthropic.Anthropic(timeout=cfg["llm"].get("timeout_seconds", 180))
    resp = client.messages.create(
        model=cfg["llm"].get("api_model", "claude-haiku-5-5"), max_tokens=4000,
        system=LLM_SYSTEM, output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}])
    if resp.stop_reason == "refusal":
        raise RuntimeError("model declined the request")
    return "".join(b.text for b in resp.content if b.type == "text")


# ---------------------------------------------------------------- rendering

def esc(s):
    return html.escape(str(s or ""), quote=True)


def a(text, href, cls=""):
    if not href:
        return esc(text)
    return f'<a href="{esc(href)}"{f" class={cls}" if cls else ""}>{esc(text)}</a>'


def game_html(g, kind):
    if kind == "next":
        bits = f"{g['date']}{' ' + g['time'] if g['time'] else ''} {g['where']} {esc(g['opp'])}"
        if g["tv"]:
            bits += f" · {esc(g['tv'])}"
        out = f'<span class="lbl">Next</span> <a href="{esc(g["link"])}">{bits}</a>' if g["link"] else f'<span class="lbl">Next</span> {bits}'
    else:
        res = f'<b class="{g["result"] or "live"}">{g["result"] or "LIVE"} {g["score"]}</b>'
        main = f'{res} {g["where"]} {esc(g["opp"])}'
        out = f'<span class="lbl">{g["date"]}</span> ' + (f'<a href="{esc(g["link"])}">{main}</a>' if g["link"] else main)
        if g["detail"]:
            out += f' <span class="dim">({esc(g["detail"])})</span>'
    extras = [x for x in (g["note"], g["series"]) if x]
    if extras:
        out += f' <span class="tag">{esc(" · ".join(extras))}</span>'
    if kind != "next" and g.get("recap"):
        out += f'<div class="recap">{esc(g["recap"])}</div>'
    return f'<div class="game">{out}</div>'


def card_html(c, by="location"):
    h = f'<div class="card"><div class="head"><span class="team">{a(card_title(c, by), c["team_url"])}</span>'
    h += f' <span class="meta">{esc(" · ".join(card_meta(c, by)))}</span></div>'
    for g in c["live"]:
        h += game_html(g, "live")
    for g in reversed(c["results"]):
        h += game_html(g, "result")
    if c["older"]:
        h += game_html(c["older"], "result")
    if c["next"] and c["active"]:
        h += game_html(c["next"], "next")
    note = (c["standing"] or {}).get("note")
    if note:
        h += f'<div class="game dim">{esc(note)}</div>'
    for n in c["headlines"]:
        h += f'<div class="news">▸ {a(n["title"], n["link"])}</div>'
    tb = (c["standing"] or {}).get("table")
    if tb:
        rows = "".join(f'<tr class="{"me" if r["mine"] else ""}"><td>{r["pos"]}</td><td>{esc(r["team"])}</td>'
                       f'<td>{esc(r["record"])}</td><td>{esc(r["extra"])}</td></tr>' for r in tb["rows"])
        h += (f'<details><summary>{esc(tb["title"])} standings</summary><table>{rows}</table>'
              f'<a class="dim" href="{esc(c["standings_url"])}">Full standings →</a></details>')
    elif c["active"]:
        h += f'<div class="links"><a class="dim" href="{esc(c["standings_url"])}">Standings →</a></div>'
    return h + "</div>"


def lines_html(sec):
    h = f'<div class="card"><div class="head"><span class="team">{a(sec["name"], sec["url"])}</span>'
    h += f' <span class="meta">{esc(sec.get("starts", ""))}</span></div>'
    for l in sec["lines"]:
        tag = " · ".join(x for x in (l["label"], l["series"]) if x)
        h += f'<div class="game">{a(l["text"], l["link"])}' + (f' <span class="tag">{esc(tag)}</span>' if tag else "")
        h += (f'<div class="recap">{esc(l["recap"])}</div>' if l["recap"] else "") + "</div>"
    if sec["extra"]:
        h += f'<div class="game dim">+{sec["extra"]} more — {a("full scoreboard", sec["url"])}</div>'
    return h + "</div>"


CSS = """
:root{color-scheme:light dark;--bg:#f6f7f9;--card:#fff;--fg:#1a1d21;--dim:#6b7280;--line:#e5e7eb;--link:#0b57d0;--w:#15803d;--l:#b91c1c;--tag:#eef2ff}
@media(prefers-color-scheme:dark){:root{--bg:#111316;--card:#1b1e23;--fg:#e8eaed;--dim:#9aa1ab;--line:#2c3138;--link:#8ab4f8;--w:#4ade80;--l:#f87171;--tag:#262b3a}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:720px;margin:0 auto;padding:20px 14px 40px}
h1{font-size:22px;margin:0 0 2px}h2{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);margin:22px 0 8px}
.sub{color:var(--dim);font-size:13px}a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin:8px 0}
.head{display:flex;flex-wrap:wrap;gap:4px 10px;align-items:baseline;margin-bottom:2px}
.team{font-weight:650;font-size:16px}.team a{color:var(--fg)}.meta{color:var(--dim);font-size:13.5px}
.game{margin:3px 0}.game a{color:var(--fg)}.lbl{display:inline-block;min-width:62px;color:var(--dim);font-size:13px}
.W{color:var(--w)}.L{color:var(--l)}.T,.live{color:var(--dim)}.dim{color:var(--dim);font-size:13px}
.tag{background:var(--tag);border-radius:4px;padding:1px 6px;font-size:12px;color:var(--dim)}
.recap{margin-left:66px;color:var(--dim);font-size:13px}.news{margin:4px 0}
details{margin-top:6px}summary{cursor:pointer;color:var(--dim);font-size:13px}
table{border-collapse:collapse;margin:6px 0;font-size:13.5px}td{padding:2px 12px 2px 0}tr.me td{font-weight:700}
footer{margin-top:26px;color:var(--dim);font-size:12.5px;border-top:1px solid var(--line);padding-top:10px}
.vr{position:absolute;opacity:0;pointer-events:none}.view{display:none}
.tabs{display:flex;gap:4px;margin:14px 0 0;flex-wrap:wrap}
.tabs label{cursor:pointer;font-size:13px;color:var(--dim);border:1px solid var(--line);border-radius:999px;padding:3px 11px;background:var(--card)}
"""
# The view switcher is radio buttons + CSS, so it works with scripts off; a tiny
# script only remembers the last choice. One rule set per view:
VIEW_CSS = "".join(
    f"#v-{v}:checked~.view-{v}{{display:block}}"
    f"#v-{v}:checked~.tabs label[for=v-{v}]{{color:var(--fg);border-color:var(--fg);font-weight:600}}"
    f"#v-{v}:focus-visible~.tabs label[for=v-{v}]{{outline:2px solid var(--link);outline-offset:2px}}"
    for v in VIEWS)
VIEW_JS = ('<script>try{var k="sports-briefing-view",s=localStorage.getItem(k),e=s&&document.getElementById("v-"+s);'
           'if(e)e.checked=true;document.querySelectorAll(".vr").forEach(function(r){r.addEventListener("change",'
           'function(){localStorage.setItem(k,r.id.slice(2))})})}catch(x){}</script>')


def views_html(b):
    """All three layouts of the team cards, with tabs to switch between them."""
    radios = "".join(f'<input type="radio" name="view" class="vr" id="v-{v}" aria-label="{esc(label)}"'
                     f'{" checked" if v == b["organize_by"] else ""}>' for v, label in VIEWS.items())
    tabs = '<div class="tabs">' + "".join(f'<label for="v-{v}">{esc(label)}</label>' for v, label in VIEWS.items()) + "</div>"
    bodies = ""
    for v in VIEWS:
        secs = "".join(f"<h2>{esc(t)}</h2>" + "".join(card_html(c, v) for c in cards) for t, cards in team_sections(b, v))
        bodies += f'<div class="view view-{v}">{secs}</div>'
    return f"<div>{radios}{tabs}{bodies}</div>"


def render_html(b, switcher=True):
    """switcher=False gives one fixed layout (organize_by), for email clients that drop CSS/scripts."""
    d = b["generated"]
    # <base target="_blank"> makes every link on the page open in a new tab.
    out = [f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<base target="_blank">'
           f'<title>Sports Briefing — {d:%a} {d.month}/{d.day}</title><style>{CSS}{VIEW_CSS if switcher else ""}</style></head><body><main>',
           f'<h1>Sports Briefing</h1><div class="sub">{d:%A, %B} {d.day}, {d.year} · updated {fmt_time(d)}</div>']
    if switcher:
        out.append(views_html(b))
    else:
        by = b["organize_by"]
        for t, cards in team_sections(b, by):
            out.append(f"<h2>{esc(t)}</h2>" + "".join(card_html(c, by) for c in cards))
    if b["playoffs"]:
        out.append("<h2>Playoffs</h2>" + "".join(lines_html(s) for s in b["playoffs"]))
    if b["events"]:
        out.append("<h2>Major events</h2>" + "".join(lines_html(s) for s in b["events"]))
    if b["more"]:
        out.append("<h2>More headlines</h2>")
        for label, items in b["more"].items():
            out.append(f'<div class="card"><div class="head"><span class="team">{esc(label)}</span></div>'
                       + "".join(f'<div class="news">▸ {a(n["title"], n["link"])}</div>' for n in items) + "</div>")
    quiet = [c["name"] for c in b["cards"] if not c["active"] and not c["headlines"] and not c["failed"]]
    foot = []
    if quiet:
        foot.append("Off-season / idle, nothing major: " + esc(", ".join(quiet)) + ".")
    bad = b["issues"] + [f"{c['name']} (no data)" for c in b["cards"] if c["failed"]]
    if bad:
        foot.append("<b>Could not load today:</b> " + esc(", ".join(dict.fromkeys(bad))) + ".")
    if b["llm_note"]:
        foot.append(esc(b["llm_note"]))
    foot.append('Scores, records, standings and rankings: <a href="https://www.espn.com">ESPN</a> data feed, rendered by script. '
                "Headlines are original titles from ESPN, byucougars.com and Google News; an AI model only chose which to show.")
    out.append("<footer>" + "<br>".join(foot) + "</footer></main>" + (VIEW_JS if switcher else "") + "</body></html>")
    return "".join(out)


# ------------------------------------------------------------ email layout
# Mail clients (Gmail especially) drop CSS variables, <details> and most <style>
# rules, so the email gets its own layout: tables plus inline styles only.

E = dict(bg="#f3f4f6", card="#ffffff", line="#e2e5e9", fg="#1a1d21", dim="#6b7280",
         link="#0b57d0", w="#15803d", wbg="#e8f5ec", l="#b91c1c", lbg="#fdecec",
         tag="#eef0f4", font="-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif")


def ea(text, href, color=None):
    if not href:
        return esc(text)
    return f'<a href="{esc(href)}" target="_blank" style="color:{color or E["link"]};text-decoration:none">{esc(text)}</a>'


def e_tag(text):
    return (f'<span style="background:{E["tag"]};color:{E["dim"]};border-radius:4px;padding:1px 6px;'
            f'font-size:12px;white-space:nowrap">{esc(text)}</span>')


def e_row(label, body):
    """One line inside a card: fixed-width gray label on the left, content on the right."""
    return (f'<tr><td valign="top" style="width:64px;padding:3px 8px 3px 0;color:{E["dim"]};font-size:13px;'
            f'white-space:nowrap">{label}</td><td valign="top" style="padding:3px 0;font-size:14px;'
            f'color:{E["fg"]};line-height:1.4">{body}</td></tr>')


def e_game(g, kind):
    if kind == "next":
        text = f"{g['date']}{' ' + g['time'] if g['time'] else ''} {g['where']} {g['opp']}"
        body = ea(text, g["link"], E["fg"])
        if g["tv"]:
            body += f' <span style="color:{E["dim"]};font-size:13px">· {esc(g["tv"])}</span>'
        label = "Next"
    else:
        res = g["result"] or "LIVE"
        fg, bg = {"W": (E["w"], E["wbg"]), "L": (E["l"], E["lbg"])}.get(res, (E["dim"], E["tag"]))
        badge = (f'<span style="background:{bg};color:{fg};font-weight:700;border-radius:4px;'
                 f'padding:1px 6px;white-space:nowrap">{res} {esc(g["score"])}</span>')
        body = badge + " " + ea(f"{g['where']} {g['opp']}", g["link"], E["fg"])
        if g["detail"]:
            body += f' <span style="color:{E["dim"]};font-size:13px">({esc(g["detail"])})</span>'
        label = g["date"]
    extras = [x for x in (g["note"], g["series"]) if x]
    if extras:
        body += " " + e_tag(" · ".join(extras))
    if kind != "next" and g.get("recap"):
        body += f'<div style="color:{E["dim"]};font-size:13px;margin-top:2px">{esc(g["recap"])}</div>'
    return e_row(label, body)


def e_card(title_html, meta, rows):
    meta_html = f'<div style="color:{E["dim"]};font-size:13px;margin-top:1px">{esc(meta)}</div>' if meta else ""
    inner = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:6px">'
             f'{"".join(rows)}</table>') if rows else ""
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{E["card"]};'
            f'border:1px solid {E["line"]};border-radius:8px;margin:0 0 10px;border-collapse:separate">'
            f'<tr><td style="padding:11px 14px;font-family:{E["font"]}">'
            f'<div style="font-size:16px;font-weight:700;color:{E["fg"]}">{title_html}</div>{meta_html}{inner}'
            f'</td></tr></table>')


def e_heading(text):
    return (f'<div style="font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;'
            f'color:{E["dim"]};margin:20px 0 8px">{esc(text)}</div>')


def e_team(c, by):
    rows = [e_game(g, "live") for g in c["live"]]
    rows += [e_game(g, "result") for g in reversed(c["results"])]
    if c["older"]:
        rows.append(e_game(c["older"], "result"))
    if c["next"] and c["active"]:
        rows.append(e_game(c["next"], "next"))
    note = (c["standing"] or {}).get("note")
    if note:
        rows.append(e_row("", f'<span style="color:{E["dim"]};font-size:13px">{esc(note)}</span>'))
    for n in c["headlines"]:
        rows.append(e_row("News", ea(n["title"], n["link"])))
    if c["active"]:
        rows.append(e_row("", f'<span style="font-size:13px">{ea("Standings →", c["standings_url"])}</span>'))
    return e_card(ea(card_title(c, by), c["team_url"], E["fg"]), " · ".join(card_meta(c, by)), rows)


def e_lines(sec):
    rows = []
    for l in sec["lines"]:
        tag = " · ".join(x for x in (l["label"], l["series"]) if x)
        body = ea(l["text"], l["link"], E["fg"]) + (" " + e_tag(tag) if tag else "")
        if l["recap"]:
            body += f'<div style="color:{E["dim"]};font-size:13px;margin-top:2px">{esc(l["recap"])}</div>'
        rows.append(f'<tr><td style="padding:3px 0;font-size:14px;color:{E["fg"]};line-height:1.4">{body}</td></tr>')
    if sec["extra"]:
        rows.append(f'<tr><td style="padding:3px 0;font-size:13px;color:{E["dim"]}">+{sec["extra"]} more — '
                    f'{ea("full scoreboard", sec["url"])}</td></tr>')
    return e_card(ea(sec["name"], sec["url"], E["fg"]), sec.get("starts", ""), rows)


def render_email(b):
    d = b["generated"]
    by = b["organize_by"]
    body = [f'<div style="font-size:22px;font-weight:700;color:{E["fg"]}">Sports Briefing</div>'
            f'<div style="font-size:13px;color:{E["dim"]};margin-top:2px">{d:%A, %B} {d.day}, {d.year}</div>']
    for t, cards in team_sections(b, by):
        body.append(e_heading(t) + "".join(e_team(c, by) for c in cards))
    if b["playoffs"]:
        body.append(e_heading("Playoffs") + "".join(e_lines(s) for s in b["playoffs"]))
    if b["events"]:
        body.append(e_heading("Major events") + "".join(e_lines(s) for s in b["events"]))
    if b["more"]:
        body.append(e_heading("More headlines"))
        for label, items in b["more"].items():
            rows = [f'<tr><td style="padding:3px 0;font-size:14px;line-height:1.4">{ea(n["title"], n["link"])}</td></tr>'
                    for n in items]
            body.append(e_card(esc(label), "", rows))
    foot = []
    quiet = [c["name"] for c in b["cards"] if not c["active"] and not c["headlines"] and not c["failed"]]
    if quiet:
        foot.append("Off-season / idle: " + esc(", ".join(quiet)) + ".")
    bad = b["issues"] + [f"{c['name']} (no data)" for c in b["cards"] if c["failed"]]
    if bad:
        foot.append("<b>Could not load today:</b> " + esc(", ".join(dict.fromkeys(bad))) + ".")
    if b["llm_note"]:
        foot.append(esc(b["llm_note"]))
    foot.append(f'Scores, standings and rankings from the {ea("ESPN", "https://www.espn.com")} data feed, '
                "rendered by script. Headlines are original titles; an AI model only chose which to show.")
    body.append(f'<div style="border-top:1px solid {E["line"]};margin-top:18px;padding-top:10px;font-size:12px;'
                f'color:{E["dim"]};line-height:1.5">' + "<br>".join(foot) + "</div>")
    return ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="color-scheme" content="light only"></head>'
            f'<body style="margin:0;padding:0;background:{E["bg"]}">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{E["bg"]}">'
            f'<tr><td align="center" style="padding:16px 10px">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:640px">'
            f'<tr><td style="font-family:{E["font"]};color:{E["fg"]}">' + "".join(body)
            + '</td></tr></table></td></tr></table></body></html>')


def render_md(b):
    d = b["generated"]
    md = [f"# Sports Briefing — {d:%a, %b} {d.day}, {d.year}", ""]

    def link(text, href):
        return f"[{text}]({href})" if href else text

    def game(g, kind):
        if kind == "next":
            s = f"Next: {g['date']} {g['time']} {g['where']} {g['opp']}".replace("  ", " ")
            s = link(s, g["link"])
        else:
            s = f"{g['date']}: " + link(f"**{g['result'] or 'LIVE'} {g['score']}** {g['where']} {g['opp']}", g["link"])
            if g["detail"]:
                s += f" ({g['detail']})"
            if g.get("recap"):
                s += f" — {g['recap']}"
        extras = [x for x in (g["note"], g["series"]) if x]
        return "  - " + s + (f" _[{' · '.join(extras)}]_" if extras else "")

    by = b["organize_by"]
    for grp, cards in team_sections(b, by):
        md.append(f"## {grp}")
        for c in cards:
            md.append(f"- **{link(card_title(c, by), c['team_url'])}** {' · '.join(card_meta(c, by))}".rstrip())
            md += [game(g, "live") for g in c["live"]] + [game(g, "result") for g in reversed(c["results"])]
            if c["older"]:
                md.append(game(c["older"], "result"))
            if c["next"] and c["active"]:
                md.append(game(c["next"], "next"))
            md += [f"  - ▸ {link(n['title'], n['link'])}" for n in c["headlines"]]
        md.append("")
    for title, secs in (("Playoffs", b["playoffs"]), ("Major events", b["events"])):
        if secs:
            md.append(f"## {title}")
            for s in secs:
                md.append(f"- **{link(s['name'], s['url'])}** {s.get('starts', '')}".rstrip())
                for l in s["lines"]:
                    tag = " · ".join(x for x in (l["label"], l["series"]) if x)
                    md.append("  - " + link(l["text"], l["link"]) + (f" _[{tag}]_" if tag else ""))
            md.append("")
    if b["more"]:
        md.append("## More headlines")
        for label, items in b["more"].items():
            md.append(f"- **{label}**")
            md += [f"  - ▸ {link(n['title'], n['link'])}" for n in items]
        md.append("")
    bad = b["issues"] + [f"{c['name']} (no data)" for c in b["cards"] if c["failed"]]
    if bad:
        md.append("Could not load today: " + ", ".join(dict.fromkeys(bad)) + ".")
    if b["llm_note"]:
        md.append(b["llm_note"])
    return "\n".join(md) + "\n"


def send_email(cfg, subject, html_body, text_body):
    """Settings come from SPORTS_BRIEF_* environment variables first, then config.json."""
    import smtplib
    from email.message import EmailMessage
    e, env = cfg.get("email", {}), os.environ
    user = env.get("SPORTS_BRIEF_SMTP_USER") or e.get("username")
    to = env.get("SPORTS_BRIEF_EMAIL_TO") or e.get("to") or user
    host = env.get("SPORTS_BRIEF_SMTP_HOST") or e.get("smtp_host", "smtp.gmail.com")
    port = int(env.get("SPORTS_BRIEF_SMTP_PORT") or e.get("smtp_port", 587))
    pw = env.get(e.get("password_env", "SPORTS_BRIEF_SMTP_PASSWORD"))
    if not (user and pw):
        sys.exit("Email requested, but the SMTP username or password is not set.")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        s.login(user, pw)
        s.send_message(msg)


def main():
    ap = argparse.ArgumentParser(description="Build the daily sports briefing.")
    ap.add_argument("--no-llm", action="store_true", help="skip the AI headline filter")
    ap.add_argument("--open", action="store_true", help="open the briefing in your browser")
    ap.add_argument("--email", action="store_true", help="email it (needs the email block in config.json)")
    ap.add_argument("--by", choices=list(VIEWS),
                    help="organize teams by location, league or sport (overrides organize_by in config.json)")
    args = ap.parse_args()
    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    if args.by:
        cfg["organize_by"] = args.by
    if cfg.get("organize_by", "location") not in VIEWS:
        sys.exit(f'config.json: organize_by must be one of {", ".join(VIEWS)} (got "{cfg["organize_by"]}").')

    b = build(cfg, use_llm=not args.no_llm)
    page, md = render_html(b), render_md(b)
    email_page = render_email(b)
    out = HERE / "output"
    (out / "archive").mkdir(parents=True, exist_ok=True)
    (out / "briefing.html").write_text(page, encoding="utf-8")
    (out / "briefing.md").write_text(md, encoding="utf-8")
    (out / "archive" / f"{NOW:%Y-%m-%d}.html").write_text(page, encoding="utf-8")
    (out / "email.html").write_text(email_page, encoding="utf-8")   # preview of the emailed version
    if args.email or cfg.get("email", {}).get("enabled"):
        send_email(cfg, f"Sports Briefing — {NOW:%a} {NOW.month}/{NOW.day}", email_page, md)
    if args.open:
        webbrowser.open((out / "briefing.html").as_uri())
    if sys.stdout:                      # None when launched windowless by Task Scheduler
        sys.stdout.reconfigure(encoding="utf-8")
        print(md)


if __name__ == "__main__":
    main()
