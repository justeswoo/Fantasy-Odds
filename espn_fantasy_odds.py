"""
ESPN Fantasy Basketball + Live Win Odds (Polymarket / Kalshi)

Open the page and everything loads and refreshes on its own:
  * Pulls YOUR roster + matchup score straight from your ESPN fantasy league
  * Shows today's NBA games with win-probability % and a live odds line
  * Games with your players are pinned to the top and starred
"""

import json
import unicodedata
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from espn_api.basketball import League
from nba_api.live.nba.endpoints import boxscore, scoreboard

REFRESH_SECONDS = 30  # how often the page updates itself

# Approximate fantasy scoring for the per-player live column.
# (Your official matchup score comes from ESPN itself.)
SCORING = {"points": 1.0, "reboundsTotal": 1.2, "assists": 1.5,
           "steals": 3.0, "blocks": 3.0, "turnovers": -1.0}

# ESPN team abbreviations that differ from the NBA's
ESPN_TO_NBA = {"GS": "GSW", "NY": "NYK", "SA": "SAS", "NO": "NOP",
               "UTAH": "UTA", "WSH": "WAS"}

POLYMARKET_EVENTS = "https://gamma-api.polymarket.com/events"
KALSHI_MARKETS = "https://api.elections.kalshi.com/trade-api/v2/markets"
KALSHI_NBA_SERIES = "KXNBAGAME"
HEADERS = {"User-Agent": "espn-fantasy-odds/1.0"}


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def norm(name: str) -> str:
    """'Luka Dončić' -> 'luka doncic' so ESPN and NBA.com names match."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return s.lower().replace(".", "").replace("'", "").strip()


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------
# ESPN
# ----------------------------------------------------------------------------
@st.cache_resource(ttl=120)
def get_league():
    cfg = st.secrets["espn"]
    return League(
        league_id=int(cfg["league_id"]),
        year=int(cfg["year"]),  # ESPN uses the season's END year (2026-27 -> 2027)
        espn_s2=cfg.get("espn_s2"),
        swid=cfg.get("swid"),
    )


def get_my_team(league):
    tid = int(st.secrets["espn"]["team_id"])
    return next(t for t in league.teams if t.team_id == tid)


def get_matchup(league, team_id):
    """Returns (my_team_name, my_score, opp_name, opp_score) or None."""
    try:
        for m in league.box_scores():
            if m.home_team.team_id == team_id:
                return m.home_team.team_name, m.home_score, m.away_team.team_name, m.away_score
            if m.away_team.team_id == team_id:
                return m.away_team.team_name, m.away_score, m.home_team.team_name, m.home_score
    except Exception:
        pass
    return None


# ----------------------------------------------------------------------------
# NBA live data
# ----------------------------------------------------------------------------
@st.cache_data(ttl=15)
def get_games():
    games = scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]
    return [{"gameId": g["gameId"], "status": g["gameStatusText"].strip(),
             "state": g["gameStatus"], "home": g["homeTeam"], "away": g["awayTeam"]}
            for g in games]


@st.cache_data(ttl=15)
def get_box(game_id):
    try:
        return boxscore.BoxScore(game_id).get_dict()["game"]
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Odds (returns HOME win probability 0-100, or None)
# ----------------------------------------------------------------------------
@st.cache_data(ttl=15)
def polymarket_events():
    try:
        r = requests.get(POLYMARKET_EVENTS, headers=HEADERS, timeout=10,
                         params={"tag_slug": "nba", "closed": "false", "limit": 100})
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


def polymarket_home_prob(home, away):
    h, a = home["teamName"].lower(), away["teamName"].lower()
    for ev in polymarket_events():
        title = (ev.get("title") or "").lower()
        if h not in title or a not in title:
            continue
        for m in ev.get("markets", []):
            try:
                outs = m["outcomes"]
                prices = m["outcomePrices"]
                outs = json.loads(outs) if isinstance(outs, str) else outs
                prices = json.loads(prices) if isinstance(prices, str) else prices
            except Exception:
                continue
            names = [str(o).lower() for o in outs]
            if h in names and a in names:
                return round(float(prices[names.index(h)]) * 100, 1)
    return None


@st.cache_data(ttl=15)
def kalshi_markets():
    try:
        r = requests.get(KALSHI_MARKETS, headers=HEADERS, timeout=10,
                         params={"series_ticker": KALSHI_NBA_SERIES, "status": "open", "limit": 200})
        r.raise_for_status()
        return r.json().get("markets", [])
    except Exception:
        return []


def kalshi_home_prob(home, away):
    ht, at = home["teamTricode"], away["teamTricode"]
    for m in kalshi_markets():
        t = m.get("ticker", "")
        if ht in t and at in t and t.endswith("-" + ht):
            c = _f(m.get("last_price"))
            if c:
                return round(c, 1)
            d = _f(m.get("last_price_dollars"))
            if d is not None:
                return round(d * 100, 1)
            bid, ask = _f(m.get("yes_bid")) or 0, _f(m.get("yes_ask")) or 0
            if bid and ask:
                return round((bid + ask) / 2, 1)
    return None


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------
st.set_page_config(page_title="Fantasy + Live Odds", layout="wide")
st.title("🏀 My ESPN Fantasy + Live Odds")

source = st.sidebar.radio("Odds source", ["Polymarket", "Kalshi"])
if "history" not in st.session_state:
    st.session_state.history = {}


@st.fragment(run_every=REFRESH_SECONDS)
def dashboard():
    # ---- ESPN roster + matchup ------------------------------------------
    try:
        league = get_league()
        team = get_my_team(league)
    except Exception as e:
        st.error(f"Couldn't connect to ESPN: {e}\n\nCheck your league_id, year, team_id "
                 "and (for private leagues) espn_s2 / swid in the app secrets.")
        return

    roster = {norm(p.name): p for p in team.roster}
    my_pro_teams = {ESPN_TO_NBA.get(p.proTeam, p.proTeam) for p in team.roster}

    matchup = get_matchup(league, team.team_id)
    if matchup:
        me, my_score, opp, opp_score = matchup
        c1, c2 = st.columns(2)
        c1.metric(me, f"{my_score:g}")
        c2.metric(opp, f"{opp_score:g}")
    else:
        st.caption(f"Team: {team.team_name}")

    # ---- Games -----------------------------------------------------------
    try:
        games = get_games()
    except Exception as e:
        st.error(f"Couldn't load the NBA scoreboard: {e}")
        return
    if not games:
        st.info("No NBA games today.")

    def has_mine(g):
        return g["home"]["teamTricode"] in my_pro_teams or g["away"]["teamTricode"] in my_pro_teams

    games = sorted(games, key=lambda g: (not has_mine(g), g["state"]))
    tonight_total = 0.0

    for g in games:
        home, away, gid = g["home"], g["away"], g["gameId"]
        prob = (kalshi_home_prob if source == "Kalshi" else polymarket_home_prob)(home, away)

        if prob is not None:
            hist = st.session_state.history.setdefault(gid, [])
            hist.append((datetime.now(), prob))
            hist[:] = hist[-600:]

        star = "⭐ " if has_mine(g) else ""
        with st.container(border=True):
            top = st.columns([2, 1, 1])
            top[0].subheader(f"{star}{away['teamName']} @ {home['teamName']}")
            top[0].caption(f"{g['status']}  •  {away['score']} – {home['score']}")
            if prob is None:
                top[1].metric(away["teamTricode"], "—")
                top[2].metric(home["teamTricode"], "—")
                st.caption(f"No {source} market found for this game.")
            else:
                top[1].metric(away["teamTricode"], f"{100 - prob:.1f}%")
                top[2].metric(home["teamTricode"], f"{prob:.1f}%")
                pts = st.session_state.history[gid]
                fig = go.Figure()
                for label, ys in ((home["teamTricode"], [p[1] for p in pts]),
                                  (away["teamTricode"], [100 - p[1] for p in pts])):
                    fig.add_trace(go.Scatter(x=[p[0] for p in pts], y=ys, mode="lines",
                                             name=label, line=dict(width=3, shape="hv")))
                fig.update_layout(height=230, margin=dict(l=0, r=0, t=10, b=0),
                                  yaxis=dict(range=[0, 100], ticksuffix="%"),
                                  legend=dict(orientation="h", y=1.15))
                st.plotly_chart(fig, use_container_width=True, key=f"chart-{gid}-{len(pts)}")

            # your players in this game (live once it tips off)
            if g["state"] >= 2:
                box = get_box(gid)
                rows = []
                if box:
                    for side in ("homeTeam", "awayTeam"):
                        for p in box[side]["players"]:
                            key = norm(p["name"])
                            if key in roster and p["status"] == "ACTIVE":
                                s = p["statistics"]
                                fp = sum(s.get(k, 0) * w for k, w in SCORING.items())
                                tonight_total += fp
                                rows.append({"Player": roster[key].name,
                                             "Slot": roster[key].lineupSlot,
                                             "PTS": s.get("points", 0), "REB": s.get("reboundsTotal", 0),
                                             "AST": s.get("assists", 0), "STL": s.get("steals", 0),
                                             "BLK": s.get("blocks", 0), "TO": s.get("turnovers", 0),
                                             "Fantasy Pts": round(fp, 1)})
                if rows:
                    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    st.divider()
    st.metric("Your players' fantasy points tonight (approx.)", round(tonight_total, 1))
    st.caption(f"Auto-updates every {REFRESH_SECONDS}s • last update {datetime.now():%I:%M:%S %p}")


dashboard()
