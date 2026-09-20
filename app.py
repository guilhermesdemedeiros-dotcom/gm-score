import io
import html
import math
import json
import base64
import hashlib
import os
import re
import time
import secrets
import inspect
import itertools
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from html.parser import HTMLParser
from urllib.parse import quote, urlencode
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components

# Cliente oficial do Supabase. Nesta etapa ele é usado apenas como
# infraestrutura de autenticação em modo de teste; o GM SCORE continua
# público até a ativação explícita do portal VIP em uma etapa posterior.
try:
    from supabase import create_client
except Exception:
    create_client = None

try:
    from cryptography.fernet import Fernet, InvalidToken
except Exception:
    Fernet = None
    InvalidToken = Exception

# ============================================================
# CONFIGURAÇÃO
# ============================================================
GM_BUILD = "2026-09-20-v152-daily-picks-results-fix"
GM_DAILY_PICK_RESET_DATE = date(2026, 9, 16)  # novo ciclo: Matadeira, Dica Principal e Bingo

# IDs auditados das 21 competições.
# v45: definidos no início do runtime porque a agenda pode ser executada antes
# da seção de auditoria estatística onde este mapa ficava originalmente.
GM_APIFOOTBALL_FIXED_LEAGUE_IDS = {
    "Inglaterra - Premier League": "152",
    "Espanha - La Liga": "302",
    "Itália - Serie A": "207",
    "Alemanha - Bundesliga": "175",
    "França - Ligue 1": "168",
    "Portugal - Liga Portugal": "266",
    "Holanda - Eredivisie": "244",
    "Escócia - Premiership": "279",
    "Turquia - Süper Lig": "322",
    "Brasil - Série A": "99",
    "Brasil - Série B": "75",
    "Arábia Saudita - Saudi Pro League": "328",
    "Estados Unidos - MLS": "332",
    "Argentina - Liga Profesional": "44",
    "México - Liga MX": "235",
    "Colômbia - Primera A": "120",
    "CONMEBOL Libertadores": "18",
    "CONMEBOL Sul-Americana": "385",
    "UEFA Champions League": "3",
    "UEFA Europa League": "4",
    "UEFA Conference League": "683",
}

st.set_page_config(
    page_title="GM SCORE",
    page_icon="⚽",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ============================================================
# IDENTIDADE VISUAL — TEMA ESCURO FIXO
# ============================================================
st.markdown(
    """
    <style>
    :root{color-scheme:dark;}
    html,body,.stApp,[data-testid="stAppViewContainer"],[data-testid="stMain"]{background:#0b1015!important;color:#f8fafc!important;}
    [data-testid="stHeader"]{background:rgba(11,16,21,.96)!important;}
    [data-testid="stSidebar"]{background:#0d141b!important;}
    [data-testid="stSidebar"] *{color:#f1f5f9;}
    .stTextInput input,.stTextArea textarea,.stNumberInput input,[data-baseweb="select"]>div{background:#111923!important;color:#f8fafc!important;border-color:rgba(148,163,184,.28)!important;}
    [data-testid="stForm"],div[data-testid="stExpander"]{border-color:rgba(148,163,184,.22)!important;}
    </style>
    """,
    unsafe_allow_html=True,
)

# ============================================================
# APIFOOTBALL.COM — CONEXÃO SEGURA / TESTE DE SAÚDE
# A chave fica exclusivamente em st.secrets["apifootball"]["api_key"].
# Nunca é exibida na interface nem gravada no repositório.
# ============================================================
APIFOOTBALL_BASE_URL = "https://apiv3.apifootball.com/"

def gm_apifootball_api_key():
    try:
        cfg = st.secrets["apifootball"]
        key = str(cfg.get("api_key", "") or "").strip()
        return key
    except Exception:
        return ""

@st.cache_data(ttl=300, show_spinner=False)
def gm_apifootball_request(action, **params):
    key = gm_apifootball_api_key()
    if not key:
        return None, "secret_missing"

    query = {"action": action, "APIkey": key}
    for k, v in params.items():
        if v is not None and str(v).strip() != "":
            query[k] = v

    try:
        response = requests.get(
            APIFOOTBALL_BASE_URL,
            params=query,
            timeout=(5, 15),
            headers={"User-Agent": "GM-SCORE/1.0"},
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return None, f"request_error:{type(exc).__name__}"

    # A API pode responder um objeto de erro mesmo com HTTP 200.
    if isinstance(payload, dict):
        lowered = {str(k).lower(): v for k, v in payload.items()}
        if any(k in lowered for k in ("error", "errors", "message")) and not any(
            k in payload for k in ("statistics", "match_id", "country_id", "league_id")
        ):
            return payload, "api_error"

    return payload, None

@st.cache_data(ttl=300, show_spinner=False)
def gm_apifootball_healthcheck():
    payload, err = gm_apifootball_request("get_countries")
    if err:
        return {"ok": False, "error": err, "countries": 0}
    if isinstance(payload, list):
        return {"ok": len(payload) > 0, "error": None, "countries": len(payload)}
    return {"ok": False, "error": "unexpected_payload", "countries": 0}


@lru_cache(maxsize=32768)
def _gm_api_norm_cached(text):
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _gm_api_norm(value):
    # V117: normalização é uma operação pura e extremamente recorrente em agenda,
    # aliases e auditorias. Cache local evita repetir Unicode/regex sem alterar saída.
    return _gm_api_norm_cached(str(value or ""))


# V87 — identidade visual das equipes. A APIfootball é a fonte de verdade dos
# brasões; nenhum escudo é associado manualmente por nome. Falhas visuais nunca
# interferem no motor estatístico, na agenda ou na seleção das partidas.
@st.cache_data(ttl=21600, show_spinner=False)
def gm_team_visual_catalog(competition):
    league_id = str((GM_APIFOOTBALL_FIXED_LEAGUE_IDS or {}).get(competition) or "").strip()
    if not league_id:
        return {"by_id": {}, "by_name": {}}
    payload, err = gm_apifootball_request("get_teams", league_id=league_id)
    if err or not isinstance(payload, list):
        return {"by_id": {}, "by_name": {}}
    by_id, by_name = {}, {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        tid = str(row.get("team_key") or row.get("team_id") or "").strip()
        name = str(row.get("team_name") or "").strip()
        badge = str(row.get("team_badge") or row.get("team_logo") or "").strip()
        if not badge.lower().startswith("https://"):
            badge = ""
        item = {"id": tid, "name": name, "badge": badge}
        if tid:
            by_id[tid] = item
        if name:
            by_name[_gm_api_norm(name)] = item
    return {"by_id": by_id, "by_name": by_name}


def gm_team_visual(team_name, competition=None, team_id=None):
    """Resolve ID/brasão só para exibição; nunca adivinha um escudo."""
    try:
        catalog = gm_team_visual_catalog(competition) if competition else {"by_id": {}, "by_name": {}}
        tid = str(team_id or "").strip()
        if tid and tid in catalog.get("by_id", {}):
            return catalog["by_id"][tid]
        key = _gm_api_norm(team_name)
        exact = catalog.get("by_name", {}).get(key)
        if exact:
            return exact
        # Compatibilidade controlada com abreviações já aceitas pelo próprio app.
        matches = [v for k, v in catalog.get("by_name", {}).items() if _gm_team_name_match(team_name, v.get("name"))]
        if len(matches) == 1:
            return matches[0]
    except Exception:
        pass
    return {"id": str(team_id or ""), "name": str(team_name or ""), "badge": ""}


@st.cache_data(ttl=21600, show_spinner=False)
def gm_badge_data_uri(url):
    """V94: incorpora o brasão no compartilhamento para evitar bloqueio CORS do canvas.

    É uma transformação exclusivamente visual. Falha silenciosamente e mantém o
    fallback neutro; não participa de identificação estatística nem de cálculos.
    """
    src = str(url or "").strip()
    if not src.lower().startswith("https://"):
        return ""
    try:
        response = requests.get(src, timeout=8)
        response.raise_for_status()
        content = response.content or b""
        if not content or len(content) > 2_000_000:
            return ""
        content_type = str(response.headers.get("Content-Type") or "image/png").split(";", 1)[0].strip().lower()
        if not content_type.startswith("image/"):
            content_type = "image/png"
        encoded = base64.b64encode(content).decode("ascii")
        return f"data:{content_type};base64,{encoded}"
    except Exception:
        return ""


def gm_current_match_team_id(team_name, side=None):
    """ID visual da equipe no confronto aberto; não participa dos cálculos."""
    try:
        payload = st.session_state.get("gm_games_direct_match") or {}
        if st.session_state.get("gm_analysis_origin") != "games" or not payload:
            return ""
        wanted = _gm_api_norm(team_name)
        candidates = []
        if side in ("home", "away"):
            candidates = [side]
        else:
            candidates = ["home", "away"]
        for key in candidates:
            raw = str(payload.get(key) or "").strip()
            if raw and (_gm_api_norm(raw) == wanted or _gm_team_name_match(team_name, raw)):
                return str(payload.get(f"{key}_team_id") or "").strip()
    except Exception:
        pass
    return ""



@st.cache_data(ttl=21600, show_spinner=False)
def gm_league_visual(competition):
    """Identidade visual oficial da competição; camada somente de apresentação."""
    league_id = str((GM_APIFOOTBALL_FIXED_LEAGUE_IDS or {}).get(competition) or "").strip()
    if not league_id:
        return {"id": "", "name": competition_display_name(competition), "logo": ""}
    try:
        leagues, err = gm_apifootball_all_leagues()
        if not err:
            for row in leagues or []:
                if not isinstance(row, dict):
                    continue
                rid = str(row.get("league_id") or row.get("league_key") or "").strip()
                if rid != league_id:
                    continue
                logo = str(row.get("league_logo") or row.get("league_badge") or "").strip()
                if not logo.lower().startswith("https://"):
                    logo = ""
                return {
                    "id": league_id,
                    "name": str(row.get("league_name") or competition_display_name(competition)),
                    "logo": logo,
                }
    except Exception:
        pass
    return {"id": league_id, "name": competition_display_name(competition), "logo": ""}


def gm_current_match_datetime():
    """Data/hora visual do confronto aberto pela agenda; não participa dos cálculos."""
    try:
        payload = st.session_state.get("gm_games_direct_match") or {}
        raw_date = str(payload.get("date") or "").strip()
        raw_time = str(payload.get("time") or "").strip()
        if raw_date:
            dt = datetime.strptime(raw_date[:10], "%Y-%m-%d")
            label = dt.strftime("%d/%m/%Y")
            if raw_time and raw_time != "—":
                label += f" • {raw_time[:5]}"
            return label
    except Exception:
        pass
    return ""

def gm_team_badge_html(team_name, competition=None, team_id=None, size=24, show_name=True):
    visual = gm_team_visual(team_name, competition, team_id)
    badge = str(visual.get("badge") or "")
    name = html.escape(str(team_name or ""))
    img = (f'<img src="{html.escape(badge, quote=True)}" alt="" loading="lazy" '
           f'style="width:{int(size)}px;height:{int(size)}px;object-fit:contain;flex:0 0 auto">') if badge else (
           f'<span aria-hidden="true" style="width:{int(size)}px;height:{int(size)}px;display:inline-flex;align-items:center;justify-content:center;opacity:.45">⚽</span>')
    text = f'<span>{name}</span>' if show_name else ''
    return f'<span class="gm-team-with-badge" style="display:inline-flex;align-items:center;gap:7px;min-width:0">{img}{text}</span>'

def _gm_api_stat_map(items):
    out = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        stat_type = str(item.get("type") or "").strip()
        if stat_type:
            out[stat_type] = {"home": item.get("home"), "away": item.get("away")}
    return out

def _gm_api_has_value(value):
    if value is None:
        return False
    text = str(value).strip().replace("%", "")
    return text not in ("", "-", "None", "null", "N/A")

def _gm_api_extract_event(payload):
    if isinstance(payload, list) and payload:
        return payload[0] if isinstance(payload[0], dict) else None
    if isinstance(payload, dict):
        if "match_id" in payload:
            return payload
        for value in payload.values():
            if isinstance(value, dict) and "match_id" in value:
                return value
            if isinstance(value, list) and value and isinstance(value[0], dict) and "match_id" in value[0]:
                return value[0]
    return None

@st.cache_data(ttl=900, show_spinner=False)
def gm_apifootball_team_coverage(team_a="Union Berlin", team_b="Schalke 04", limit_per_team=10):
    result = {
        "ok": False,
        "teams": {},
        "h2h": 0,
        "error": None,
        "requests_planned": 1,
        "requests_used": 0,
    }
    payload, err = gm_apifootball_request("get_H2H", firstTeam=team_a, secondTeam=team_b, timezone="America/Sao_Paulo")
    result["requests_used"] += 1
    if err or not isinstance(payload, dict):
        result["error"] = err or "h2h_unexpected_payload"
        return result

    result["h2h"] = len(payload.get("firstTeam_VS_secondTeam") or [])
    sources = [
        (team_a, payload.get("firstTeam_lastResults") or []),
        (team_b, payload.get("secondTeam_lastResults") or []),
    ]
    metric_types = {
        "Escanteios": ("Corners",),
        "Cartões amarelos": ("Yellow Cards",),
        "Cartões vermelhos": ("Red Cards",),
        "Faltas": ("Fouls",),
        "Finalizações": ("Shots Total",),
        "No alvo": ("Shots On Goal", "On Target"),
        "Impedimentos": ("Offsides",),
        "Posse": ("Ball Possession",),
    }
    half_types = {
        "Escanteios 1T": ("Corners",),
        "Finalizações 1T": ("Shots Total",),
        "No alvo 1T": ("Shots On Goal", "On Target"),
    }

    for team_name, matches in sources:
        finished = []
        seen = set()
        for match in matches:
            if not isinstance(match, dict):
                continue
            mid = str(match.get("match_id") or "").strip()
            if not mid or mid in seen:
                continue
            status = _gm_api_norm(match.get("match_status"))
            if status and status not in {"finished", "ft", "after et", "after pen"}:
                continue
            seen.add(mid)
            finished.append(match)
            if len(finished) >= int(limit_per_team):
                break

        counts = {name: 0 for name in metric_types}
        half_counts = {name: 0 for name in half_types}
        detailed = 0
        leagues = set()
        dates = []
        errors = 0

        for match in finished:
            mid = str(match.get("match_id") or "").strip()
            ev_payload, ev_err = gm_apifootball_request("get_events", match_id=mid, timezone="America/Sao_Paulo")
            result["requests_used"] += 1
            if ev_err:
                errors += 1
                continue
            event = _gm_api_extract_event(ev_payload)
            if not event:
                errors += 1
                continue

            home_name = str(event.get("match_hometeam_name") or "")
            away_name = str(event.get("match_awayteam_name") or "")
            target = _gm_api_norm(team_name)
            home_n = _gm_api_norm(home_name)
            away_n = _gm_api_norm(away_name)
            if target == home_n or (target and target in home_n) or (home_n and home_n in target):
                side = "home"
            elif target == away_n or (target and target in away_n) or (away_n and away_n in target):
                side = "away"
            else:
                continue

            stats = _gm_api_stat_map(event.get("statistics"))
            stats_1h = _gm_api_stat_map(event.get("statistics_1half"))
            if stats:
                detailed += 1
            for label, aliases in metric_types.items():
                for alias in aliases:
                    pair = stats.get(alias)
                    if pair and _gm_api_has_value(pair.get(side)):
                        counts[label] += 1
                        break
            for label, aliases in half_types.items():
                for alias in aliases:
                    pair = stats_1h.get(alias)
                    if pair and _gm_api_has_value(pair.get(side)):
                        half_counts[label] += 1
                        break
            league = str(event.get("league_name") or "").strip()
            if league:
                leagues.add(league)
            mdate = str(event.get("match_date") or "").strip()
            if mdate:
                dates.append(mdate)

        result["teams"][team_name] = {
            "matches": len(finished),
            "detailed": detailed,
            "metrics": counts,
            "half_metrics": half_counts,
            "leagues": sorted(leagues),
            "latest": max(dates) if dates else None,
            "oldest": min(dates) if dates else None,
            "errors": errors,
        }

    result["requests_planned"] = 1 + sum(v.get("matches", 0) for v in result["teams"].values())
    result["ok"] = bool(result["teams"]) and any(v.get("detailed", 0) > 0 for v in result["teams"].values())
    if not result["ok"] and not result["error"]:
        result["error"] = "no_detailed_statistics"
    return result

def gm_render_apifootball_coverage_probe():
    st.markdown("---")
    st.markdown("### 🧪 Diagnóstico APIfootball • Union Berlin × Schalke 04")
    st.caption("Teste temporário de manutenção. A chave da API nunca é exibida.")
    with st.spinner("Validando partidas históricas e estatísticas reais…"):
        probe = gm_apifootball_team_coverage("Union Berlin", "Schalke 04", 10)

    if probe.get("error") and not probe.get("teams"):
        st.error(f"A API respondeu, mas o teste histórico não pôde ser concluído: {probe.get('error')}")
        return

    st.caption(f"Chamadas usadas neste teste: {probe.get('requests_used', 0)} • H2H encontrados: {probe.get('h2h', 0)}")
    rows = []
    for team_name, data in probe.get("teams", {}).items():
        row = {
            "Equipe": team_name,
            "Jogos": data.get("matches", 0),
            "Com estatísticas": data.get("detailed", 0),
        }
        row.update(data.get("metrics", {}))
        row.update(data.get("half_metrics", {}))
        rows.append(row)
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    for team_name, data in probe.get("teams", {}).items():
        leagues = ", ".join(data.get("leagues") or []) or "—"
        st.caption(f"{team_name}: {data.get('oldest') or '—'} → {data.get('latest') or '—'} • Competições: {leagues}")
    if probe.get("ok"):
        st.success("A nova fonte está retornando estatísticas detalhadas reais para o teste.")
    else:
        st.warning("A conexão funciona, mas este teste ainda não retornou cobertura estatística suficiente.")

# ============================================================
# MODO MANUTENÇÃO GLOBAL
# Bloqueia a interface inteira para todos os usuários antes de
# autenticação, pesquisa, agenda, análises ou qualquer outra tela.
# ============================================================
GM_MAINTENANCE_MODE = False

if GM_MAINTENANCE_MODE:
    _gm_api_health = gm_apifootball_healthcheck()
    st.markdown(
        """
        <style>
        [data-testid="stSidebar"],
        [data-testid="collapsedControl"],
        [data-testid="stHeader"],
        [data-testid="stToolbar"],
        [data-testid="stDecoration"],
        #MainMenu, footer {
            display:none !important;
            visibility:hidden !important;
        }
        .stApp {
            background:
                radial-gradient(circle at 50% 18%, rgba(24, 170, 86, .14), transparent 34%),
                linear-gradient(180deg, #07110c 0%, #030806 100%);
        }
        .block-container {
            max-width:760px !important;
            min-height:100vh;
            padding:0 1.15rem !important;
            display:flex;
            align-items:center;
            justify-content:center;
        }
        .gm-maintenance {
            width:100%;
            padding:2.2rem 1.35rem 2rem;
            border:1px solid rgba(34,211,107,.34);
            border-radius:22px;
            background:rgba(5,15,10,.88);
            box-shadow:0 24px 70px rgba(0,0,0,.42);
            text-align:center;
        }
        .gm-maintenance-logo {
            font-size:2.5rem;
            font-weight:950;
            letter-spacing:-.06em;
            line-height:1;
            margin-bottom:.65rem;
        }
        .gm-maintenance-gm, .gm-maintenance-score {
            -webkit-text-stroke:3px #05070a;
            paint-order:stroke fill;
            text-shadow:0 3px 10px rgba(0,0,0,.5);
        }
        .gm-maintenance-gm { color:#fff; }
        .gm-maintenance-score { color:#22d36b; }
        .gm-maintenance-kicker {
            color:#22d36b;
            font-size:.78rem;
            font-weight:850;
            letter-spacing:.16em;
            text-transform:uppercase;
            margin-bottom:1.15rem;
        }
        .gm-maintenance h1 {
            color:#fff;
            font-size:clamp(1.55rem, 6vw, 2.25rem);
            line-height:1.12;
            margin:0 0 .9rem;
        }
        .gm-maintenance p {
            max-width:570px;
            margin:0 auto;
            color:rgba(255,255,255,.72);
            font-size:1rem;
            line-height:1.6;
        }
        .gm-maintenance-status {
            display:inline-flex;
            align-items:center;
            gap:.5rem;
            margin-top:1.4rem;
            padding:.58rem .9rem;
            border-radius:999px;
            border:1px solid rgba(34,211,107,.25);
            background:rgba(34,211,107,.08);
            color:#bff8d2;
            font-size:.82rem;
            font-weight:750;
        }
        .gm-maintenance-dot {
            width:8px;
            height:8px;
            border-radius:50%;
            background:#22d36b;
            box-shadow:0 0 12px rgba(34,211,107,.85);
        }
        @media (max-width:640px) {
            .gm-maintenance { padding:1.8rem 1rem 1.7rem; border-radius:18px; }
            .gm-maintenance-logo { font-size:2.15rem; }
        }
        </style>
        <div class="gm-maintenance">
            <div class="gm-maintenance-logo">
                <span class="gm-maintenance-gm">GM</span><span class="gm-maintenance-score"> SCORE</span>
            </div>
            <div class="gm-maintenance-kicker">Manutenção programada</div>
            <h1>Estamos aprimorando o GM SCORE</h1>
            <p>O acesso está temporariamente indisponível enquanto realizamos melhorias na plataforma e no nosso sistema de dados. Voltaremos em breve com uma experiência ainda mais completa.</p>
            <div class="gm-maintenance-status"><span class="gm-maintenance-dot"></span> Sistema temporariamente bloqueado</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if _gm_api_health.get("ok"):
        st.caption("✅ Nova fonte estatística conectada • validação de cobertura em andamento")
    else:
        st.caption("🔄 Atualização da base estatística em andamento")

    # Diagnóstico temporário acessível apenas quando a URL contém ?gm_diag=coverage.
    # Não expõe chave, secrets ou dados de usuário.
    try:
        _gm_diag = str(st.query_params.get("gm_diag", "") or "").strip().lower()
    except Exception:
        _gm_diag = ""
    if _gm_diag == "coverage" and _gm_api_health.get("ok"):
        gm_render_apifootball_coverage_probe()
    st.stop()

# V130: a marca é renderizada antes da restauração de autenticação.
# O selo persistente é aplicado após o portal, quando o perfil já está resolvido.
st.markdown(f"""
<div class="gm-brand" style="margin:0 0 1.15rem 0">
  <div style="display:flex;align-items:center;gap:.65rem;line-height:1;flex-wrap:wrap">
    <span style="font-size:2.35rem">⚽</span>
    <span class="gm-brand-title"><span class="gm-brand-gm">GM</span><span class="gm-brand-score">SCORE</span></span>
  </div>
  <div class="gm-brand-subtitle">ANÁLISE • ESTATÍSTICAS • PROBABILIDADES</div>
</div>
""", unsafe_allow_html=True)

st.markdown("""
<style>
/* Identidade visual GM SCORE: GM branco + SCORE verde, ambos com contorno preto espesso. */
.gm-brand-title {
  display:inline-flex;
  align-items:center;
  gap:.12em;
  font-size:2.35rem;
  font-weight:950;
  letter-spacing:-.055em;
  paint-order:stroke fill;
}
.gm-brand-gm, .gm-brand-score {
  -webkit-text-stroke:4px #05070a;
  paint-order:stroke fill;
  text-shadow:0 2px 0 #05070a, 0 4px 10px rgba(0,0,0,.45);
}
.gm-brand-gm { color:#ffffff !important; }
.gm-brand-score { color:#22d36b !important; }
.gm-brand-subtitle {
  margin-top:.55rem;
  font-size:.82rem;
  font-weight:700;
  letter-spacing:.08em;
  color:color-mix(in srgb, var(--text-color) 68%, transparent);
}
.gm-brand-access{display:inline-flex;align-items:center;justify-content:center;padding:.26rem .52rem;border-radius:8px;font-size:.66rem;font-weight:950;letter-spacing:.09em;border:1px solid rgba(148,163,184,.30);background:rgba(148,163,184,.08);color:#cbd5e1;transform:translateY(.05rem)}.gm-brand-access-pro{border-color:rgba(52,230,129,.55);background:rgba(52,230,129,.12);color:#34e681}.gm-brand-access-free{color:#cbd5e1}.gm-brand-access-adm{border-color:rgba(250,204,21,.55);background:rgba(250,204,21,.10);color:#fde047}.gm-plan-badge{display:none!important}.gm-plan-pro{display:none!important}.gm-plan-free{display:none!important}.gm-pro-lock{display:flex;align-items:center;gap:12px;padding:16px;margin:.7rem 0;border:1px solid rgba(52,230,129,.24);border-radius:16px;background:linear-gradient(145deg,#0d1718,#0b1118);color:#cbd5e1}.gm-pro-lock b{display:block;color:#f8fafc;margin-bottom:3px}.gm-pro-lock-icon{font-size:1.5rem}
.gm-brand-credit {
  margin-top:.25rem;
  font-size:.78rem;
  color:color-mix(in srgb, var(--text-color) 54%, transparent);
}
@media (max-width: 768px) {
  .gm-brand-title { font-size:2.15rem; }
  .gm-brand-gm, .gm-brand-score { -webkit-text-stroke:3px #05070a; }
}

/* Navegação interna: botões sempre visíveis, porém discretos. */
.st-key-gm_admin_back_top button,
.st-key-gm_admin_back_bottom button,
.st-key-gm_news_modal_close button,
.st-key-gm_install_guide_close button {
  min-height:2.45rem !important;
  border:1px solid rgba(148,163,184,.24) !important;
  background:rgba(15,23,42,.20) !important;
  color:color-mix(in srgb, var(--text-color) 78%, transparent) !important;
  font-weight:650 !important;
  box-shadow:none !important;
}
.st-key-gm_admin_back_top button:hover,
.st-key-gm_admin_back_bottom button:hover,
.st-key-gm_news_modal_close button:hover,
.st-key-gm_install_guide_close button:hover {
  border-color:rgba(34,197,94,.52) !important;
  color:var(--text-color) !important;
}
</style>
""", unsafe_allow_html=True)


# URL pública usada somente em fluxos de retorno do Supabase (ex.: confirmação de e-mail).
# Não autentica o usuário e não inicia sessão VIP automaticamente.
GM_PUBLIC_APP_URL = "https://gmscore.streamlit.app"
GM_EMAIL_CONFIRM_REDIRECT = f"{GM_PUBLIC_APP_URL}/?gm_email_confirmed=1"


# ============================================================
# AUTENTICAÇÃO GM SCORE — ETAPA 11 (MODO DE TESTE)
# ============================================================
# IMPORTANTE:
# - Nenhuma chave fica escrita neste arquivo.
# - Usamos somente Project URL + Publishable Key dos Secrets do Streamlit.
# - Não usamos Secret/Service Role Key no app.
# - O acesso ao GM SCORE ainda NÃO é bloqueado nesta etapa.
# - Tokens de login ficam apenas no st.session_state da sessão do navegador.
GM_AUTH_TEST_MODE = True
GM_AUTH_SESSION_KEYS = (
    "gm_auth_access_token",
    "gm_auth_refresh_token",
    "gm_auth_user_id",
    "gm_auth_email",
)
GM_AUTH_RESUME_PARAM = "gm_resume"
GM_AUTH_RESUME_HOURS = 24
GM_AUTH_RESUME_RENEW_SECONDS = 300  # renova a janela após atividade, no máximo a cada 5 min


def gm_auth_persistence_secret():
    """Segredo privado usado apenas para proteger a sessão persistente do navegador."""
    try:
        cfg = st.secrets.get("auth", {})
        value = str(cfg.get("persistence_secret", "") or "").strip()
    except Exception:
        value = ""
    return value if len(value) >= 32 else ""


def gm_auth_fernet():
    secret = gm_auth_persistence_secret()
    if not secret or Fernet is None:
        return None
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def gm_auth_clear_resume_marker():
    """Remove o marcador persistente e sinaliza ao navegador para apagar a cópia local."""
    try:
        if GM_AUTH_RESUME_PARAM in st.query_params:
            del st.query_params[GM_AUTH_RESUME_PARAM]
        # V139: o Nginx injeta uma ponte mínima com localStorage para que o PWA/Safari
        # consiga restaurar gm_resume mesmo quando reabre diretamente em "/". Este
        # sinal é usado em logout, sessão substituída ou marcador inválido/expirado.
        st.query_params["gm_logout"] = "1"
    except Exception:
        pass


def gm_auth_persist_current_session():
    """Grava na URL um envelope criptografado para sobreviver a F5/reabertura da mesma URL.

    O conteúdo nunca fica em texto puro e só pode ser aberto com o segredo privado
    configurado no Streamlit. O envelope expira automaticamente.
    """
    fernet = gm_auth_fernet()
    if fernet is None:
        return False
    access = str(st.session_state.get("gm_auth_access_token") or "").strip()
    refresh = str(st.session_state.get("gm_auth_refresh_token") or "").strip()
    user_id = str(st.session_state.get("gm_auth_user_id") or "").strip()
    if not access or not refresh or not user_id:
        return False
    device_token = gm_device_session_token()
    signature = hashlib.sha256(f"{access}|{refresh}|{user_id}|{device_token}".encode("utf-8")).hexdigest()
    try:
        existing = str(st.query_params.get(GM_AUTH_RESUME_PARAM, "") or "").strip()
    except Exception:
        existing = ""
    last_persisted_at = float(st.session_state.get("_gm_auth_persisted_at") or 0)
    if (
        existing
        and st.session_state.get("_gm_auth_persist_signature") == signature
        and (time.time() - last_persisted_at) < GM_AUTH_RESUME_RENEW_SECONDS
    ):
        return True
    payload = {
        "a": access,
        "r": refresh,
        "u": user_id,
        "e": str(st.session_state.get("gm_auth_email") or ""),
        "d": device_token,
        "exp": int(time.time()) + (GM_AUTH_RESUME_HOURS * 3600),
    }
    encrypted = fernet.encrypt(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
    try:
        if str(st.query_params.get(GM_AUTH_RESUME_PARAM, "") or "") != encrypted:
            st.query_params[GM_AUTH_RESUME_PARAM] = encrypted
        st.session_state["_gm_auth_persist_signature"] = signature
        st.session_state["_gm_auth_persisted_at"] = time.time()
    except Exception:
        return False
    return True


def gm_auth_try_restore_persistent_session():
    """Restaura a autenticação após F5/reabertura sem pedir senha novamente."""
    if st.session_state.get("gm_auth_access_token") and st.session_state.get("gm_auth_refresh_token"):
        return True
    try:
        encrypted = str(st.query_params.get(GM_AUTH_RESUME_PARAM, "") or "").strip()
    except Exception:
        encrypted = ""
    if not encrypted:
        return False
    fernet = gm_auth_fernet()
    if fernet is None:
        return False
    try:
        payload = json.loads(fernet.decrypt(encrypted.encode("ascii")).decode("utf-8"))
        if int(payload.get("exp") or 0) < int(time.time()):
            raise ValueError("persistent_session_expired")
        access = str(payload.get("a") or "").strip()
        refresh = str(payload.get("r") or "").strip()
        user_id = str(payload.get("u") or "").strip()
        email = str(payload.get("e") or "").strip()
        device_token = str(payload.get("d") or "").strip()
        if not access or not refresh or not user_id:
            raise ValueError("persistent_session_invalid")
        if device_token:
            st.session_state["gm_device_session_token"] = device_token
            try:
                st.query_params["gm_session"] = device_token
            except Exception:
                pass
        st.session_state["gm_auth_access_token"] = access
        st.session_state["gm_auth_refresh_token"] = refresh
        st.session_state["gm_auth_user_id"] = user_id
        st.session_state["gm_auth_email"] = email
        st.session_state.pop("gm_auth_profile", None)
        st.session_state["_gm_auth_persist_signature"] = hashlib.sha256(
            f"{access}|{refresh}|{user_id}|{device_token}".encode("utf-8")
        ).hexdigest()
        # Força uma renovação imediata após restaurar: a abertura do app conta como atividade.
        st.session_state["_gm_auth_persisted_at"] = 0.0
        client = gm_auth_client_from_session()
        if client is None:
            raise ValueError("persistent_session_rejected")
        gm_auth_persist_current_session()
        return True
    except Exception:
        gm_auth_clear_local_session()
        gm_auth_clear_resume_marker()
        return False


def gm_supabase_config():
    """Lê a configuração do Supabase sem expor credenciais na interface/log."""
    try:
        cfg = st.secrets.get("supabase", {})
        url = str(cfg.get("url", "")).strip().rstrip("/")
        key = str(cfg.get("publishable_key", "")).strip()
    except Exception:
        url, key = "", ""
    return url, key


def gm_supabase_is_configured():
    url, key = gm_supabase_config()
    return bool(url.startswith("https://") and url.endswith("supabase.co") and key)


def gm_new_supabase_client():
    """Cria um cliente NOVO por chamada, evitando compartilhar sessão entre usuários."""
    if create_client is None:
        raise RuntimeError("Biblioteca 'supabase' não instalada. Verifique requirements.txt.")
    url, key = gm_supabase_config()
    if not url or not key:
        raise RuntimeError("Secrets [supabase] ainda não estão configurados.")
    return create_client(url, key)


@st.cache_data(ttl=120, show_spinner=False)
def gm_supabase_public_probe(url):
    """Teste leve de disponibilidade do projeto, sem login e sem ler dados de clientes."""
    _, key = gm_supabase_config()
    if not url or not key:
        return False, "Configuração ausente"
    try:
        r = requests.get(
            f"{url}/auth/v1/settings",
            headers={"apikey": key},
            timeout=8,
        )
        if r.status_code == 200:
            return True, "Supabase acessível"
        return False, f"Resposta HTTP {r.status_code}"
    except Exception as exc:
        return False, f"Falha de conexão: {type(exc).__name__}"


def gm_auth_clear_local_session():
    for key in GM_AUTH_SESSION_KEYS:
        st.session_state.pop(key, None)
    st.session_state.pop("gm_auth_profile", None)
    st.session_state.pop("_gm_auth_persist_signature", None)
    st.session_state.pop("_gm_auth_persisted_at", None)


def gm_auth_store_session(access_token, refresh_token, user_id, email=""):
    """Guarda apenas os tokens da sessão atual do navegador Streamlit."""
    if not access_token or not refresh_token or not user_id:
        raise RuntimeError("O Supabase não retornou uma sessão válida.")
    st.session_state["gm_auth_access_token"] = str(access_token)
    st.session_state["gm_auth_refresh_token"] = str(refresh_token)
    st.session_state["gm_auth_user_id"] = str(user_id)
    st.session_state["gm_auth_email"] = str(email or "")
    st.session_state.pop("gm_auth_profile", None)
    # Persistência segura é opcional e só ativa quando [auth].persistence_secret existe.
    gm_auth_persist_current_session()


def gm_auth_sign_in(email, password):
    """Autentica por e-mail/senha. Usa Auth REST oficial para diagnóstico robusto."""
    url, key = gm_supabase_config()
    clean_email = str(email).strip().lower()
    raw_password = str(password)
    if not url or not key:
        raise RuntimeError("Secrets [supabase] ainda não estão configurados.")

    # Chamada direta ao endpoint oficial do Supabase Auth. Isso elimina diferenças
    # de versão do cliente Python durante o diagnóstico e preserva RLS nos acessos seguintes.
    r = requests.post(
        f"{url}/auth/v1/token?grant_type=password",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json={"email": clean_email, "password": raw_password},
        timeout=15,
    )
    try:
        payload = r.json()
    except Exception:
        payload = {}

    if r.status_code != 200:
        code = str(payload.get("error_code") or payload.get("code") or "").strip()
        message = str(payload.get("msg") or payload.get("message") or payload.get("error_description") or "").strip()
        raise RuntimeError(f"GM_AUTH_HTTP_{r.status_code}|{code}|{message}")

    user = payload.get("user") or {}
    gm_auth_store_session(
        payload.get("access_token"),
        payload.get("refresh_token"),
        user.get("id"),
        user.get("email") or clean_email,
    )
    return user


def _gm_auth_refresh_tokens_rest(refresh_token):
    """Renova tokens diretamente no Auth REST sem depender do estado interno do supabase-py."""
    url, key = gm_supabase_config()
    token = str(refresh_token or "").strip()
    if not url or not key or not token:
        return None
    try:
        r = requests.post(
            f"{url}/auth/v1/token?grant_type=refresh_token",
            headers={"apikey": key, "Content-Type": "application/json"},
            json={"refresh_token": token},
            timeout=15,
        )
        payload = r.json() if r.content else {}
        if r.status_code != 200 or not isinstance(payload, dict):
            return None
        access = str(payload.get("access_token") or "").strip()
        refresh = str(payload.get("refresh_token") or token).strip()
        user = payload.get("user") or {}
        if not access:
            return None
        gm_auth_store_session(
            access, refresh,
            user.get("id") or st.session_state.get("gm_auth_user_id"),
            user.get("email") or st.session_state.get("gm_auth_email"),
        )
        return access, refresh
    except Exception:
        return None


def _gm_jwt_still_valid(access_token, leeway_seconds=30):
    """Checagem local apenas para permitir usar um JWT ainda válido se o refresh falhar."""
    try:
        parts = str(access_token or "").split(".")
        if len(parts) != 3:
            return False
        raw = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(raw.encode()).decode("utf-8"))
        return int(payload.get("exp") or 0) > int(time.time()) + int(leeway_seconds)
    except Exception:
        return False


def gm_auth_client_from_session():
    """Restaura a sessão Supabase com recuperação segura de token para ações VIP/admin."""
    access = st.session_state.get("gm_auth_access_token")
    refresh = st.session_state.get("gm_auth_refresh_token")
    if not access or not refresh:
        return None
    client = gm_new_supabase_client()
    try:
        result = client.auth.set_session(access, refresh)
        session = getattr(result, "session", None)
        if session is not None:
            st.session_state["gm_auth_access_token"] = session.access_token
            st.session_state["gm_auth_refresh_token"] = session.refresh_token
            gm_auth_persist_current_session()
        return client
    except Exception:
        # v31: uma falha transitória do set_session não deve destruir imediatamente
        # a sessão administrativa. Primeiro tentamos renovar pelo endpoint oficial.
        renewed = _gm_auth_refresh_tokens_rest(refresh)
        if renewed:
            access2, refresh2 = renewed
            client2 = gm_new_supabase_client()
            try:
                result = client2.auth.set_session(access2, refresh2)
                session = getattr(result, "session", None)
                if session is not None:
                    st.session_state["gm_auth_access_token"] = session.access_token
                    st.session_state["gm_auth_refresh_token"] = session.refresh_token
                    gm_auth_persist_current_session()
                return client2
            except Exception:
                access = access2
        # Se o JWT atual ainda é válido, PostgREST/RPC pode usá-lo diretamente.
        # Isso mantém publicação de novidades/admin disponível durante falhas de refresh.
        if _gm_jwt_still_valid(access):
            try:
                client3 = gm_new_supabase_client()
                client3.postgrest.auth(str(access))
                return client3
            except Exception:
                pass
        gm_auth_clear_local_session()
        return None


def gm_auth_get_profile(force=False):
    """Lê somente o próprio gm_users; o RLS do Supabase faz a proteção no servidor."""
    if not force and isinstance(st.session_state.get("gm_auth_profile"), dict):
        return st.session_state["gm_auth_profile"]
    client = gm_auth_client_from_session()
    user_id = st.session_state.get("gm_auth_user_id")
    if client is None or not user_id:
        return None
    result = (
        client.table("gm_users")
        .select("id,nome,email,role,vip_status,payment_status,vip_until,blocked,terms_accepted,terms_accepted_at,created_at")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    rows = getattr(result, "data", None) or []
    profile = rows[0] if rows else None
    if isinstance(profile, dict):
        st.session_state["gm_auth_profile"] = profile
    return profile


def gm_auth_access_state(profile=None):
    """Classifica a autorização sem ainda bloquear a interface nesta etapa."""
    profile = profile or gm_auth_get_profile()
    if not profile:
        return "anonymous"
    if bool(profile.get("blocked")) or profile.get("vip_status") == "blocked":
        return "blocked"
    if profile.get("role") == "admin":
        return "admin"
    if profile.get("vip_status") != "active":
        return str(profile.get("vip_status") or "pending")
    vip_until = profile.get("vip_until")
    if vip_until:
        try:
            expiry = pd.to_datetime(vip_until, utc=True)
            if expiry < pd.Timestamp.now(tz="UTC"):
                return "expired"
        except Exception:
            return "pending"
    return "vip"


def gm_pro_suspended(profile=None, force=False):
    """Suspensão administrativa do acesso PRO, independente da validade/pagamento."""
    profile = profile or gm_auth_get_profile()
    if not profile or profile.get("role") == "admin":
        return False
    now = time.time()
    cached_at = float(st.session_state.get("gm_pro_suspended_checked_at") or 0)
    if not force and "gm_pro_suspended" in st.session_state and (now - cached_at) < 60:
        return bool(st.session_state.get("gm_pro_suspended"))
    try:
        data = gm_admin_rpc("gm_my_pro_suspension")
        if isinstance(data, list):
            value = bool(data[0]) if data else False
        elif isinstance(data, dict):
            value = bool(data.get("pro_suspended"))
        else:
            value = bool(data)
        st.session_state["gm_pro_suspended"] = value
        st.session_state["gm_pro_suspended_checked_at"] = now
        return value
    except Exception:
        # Compatibilidade antes da migração SQL: nunca derruba login nem acesso existente.
        return False


def gm_product_tier(profile=None):
    """Camada comercial Free/Pro preservando vip_status/vip_until do banco."""
    state = gm_auth_access_state(profile)
    if state == "admin":
        return "pro"
    if state == "vip" and not gm_pro_suspended(profile):
        return "pro"
    if state in {"blocked", "anonymous"}:
        return state
    return "free"


def gm_is_pro(profile=None):
    return gm_product_tier(profile) == "pro"


def gm_access_capabilities(profile=None):
    """Matriz única Free/Pro usada pelas telas do cliente.

    V132: navegação e informação pública permanecem abertas no Free; somente a
    inteligência produzida pelo GM SCORE e as seleções atuais exigem Pro.
    Centralizar esta regra evita que uma tela trate VIP/Free de forma diferente
    das demais sem alterar os campos legados de autenticação/pagamento.
    """
    tier = gm_product_tier(profile)
    pro = tier == "pro"
    return {
        "tier": tier,
        "public_navigation": tier not in {"anonymous", "blocked"},
        "games": tier not in {"anonymous", "blocked"},
        "news": tier not in {"anonymous", "blocked"},
        "past_results": tier not in {"anonymous", "blocked"},
        "analysis_intelligence": pro,
        "current_daily_picks": pro,
        "current_admin_bets": pro,
    }


def gm_render_plan_badge(profile=None):
    profile = profile or gm_auth_get_profile()
    tier = gm_product_tier(profile)
    if tier not in {"free", "pro"}:
        return
    is_admin = str((profile or {}).get("role") or "").lower() == "admin"
    label = "ADM" if is_admin else ("PRO" if tier == "pro" else "FREE")
    cls = "gm-plan-pro" if tier == "pro" else "gm-plan-free"
    st.markdown(f'<div class="gm-plan-badge {cls}">GM SCORE · {label}</div>', unsafe_allow_html=True)


def gm_render_pro_lock(title="Conteúdo exclusivo GM SCORE Pro", message=None, key="gm_pro_unlock"):
    """Paywall server-side: não renderiza dados estatísticos/seleções PRO no Free."""
    message = message or "Assine o GM SCORE Pro para liberar análises, probabilidades, projeções e seleções atuais."
    lock_html = f'<div class="gm-pro-lock"><div class="gm-pro-lock-icon">🔒</div><div><b>{html.escape(title)}</b><div>{html.escape(message)}</div></div></div>'
    st.markdown(lock_html, unsafe_allow_html=True)
    with st.expander("Desbloquear GM SCORE Pro", expanded=False):
        gm_render_payment_plans(title="⭐ Escolha seu plano GM SCORE Pro", compact=True)


def gm_device_session_token():
    """
    Identificador opaco da sessão/dispositivo.

    O token NÃO é credencial de login. Ele também é mantido no parâmetro
    ``gm_session`` da URL para sobreviver a recarregamentos do Streamlit.
    Assim, um F5 não cria por engano uma segunda sessão no Supabase.
    """
    token = str(st.session_state.get("gm_device_session_token") or "").strip()

    if not token:
        try:
            token = str(st.query_params.get("gm_session", "") or "").strip()
        except Exception:
            token = ""

    # Aceita somente o formato/porte esperado de um token opaco.
    if not token or len(token) < 32 or len(token) > 128:
        token = secrets.token_urlsafe(32)

    st.session_state["gm_device_session_token"] = token
    try:
        if str(st.query_params.get("gm_session", "") or "") != token:
            st.query_params["gm_session"] = token
    except Exception:
        pass
    return token


def gm_clear_device_session_marker():
    """Remove o marcador persistente somente em logout explícito."""
    st.session_state.pop("gm_device_session_token", None)
    st.session_state.pop("gm_device_session_started", None)
    try:
        if "gm_session" in st.query_params:
            del st.query_params["gm_session"]
    except Exception:
        pass


def gm_session_rpc(function_name, params=None):
    client = gm_auth_client_from_session()
    if client is None:
        raise RuntimeError("Sessão autenticada indisponível.")
    result = client.rpc(function_name, params or {}).execute()
    return getattr(result, "data", None)


def gm_session_result(data):
    """Normaliza retorno TABLE do Postgres para (ok, reason)."""
    row = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
    ok = bool(row.get("allowed")) if "allowed" in row else bool(row.get("valid"))
    return ok, str(row.get("reason") or "unknown")


def gm_start_or_validate_session():
    """V122: valida a trava VIP sem fazer uma RPC em cada transição de página.

    O primeiro acesso continua fazendo takeover no servidor. Depois disso, uma
    validação aprovada é reutilizada por até 90 s; o fragmento de heartbeat
    continua sendo a autoridade periódica para detectar troca de dispositivo.
    """
    token = gm_device_session_token()
    started = bool(st.session_state.get("gm_device_session_started"))
    if started:
        last_check = st.session_state.get("gm_session_last_validation_monotonic")
        if isinstance(last_check, (int, float)) and (time.monotonic() - float(last_check)) < 90:
            return True, "cached_validation"
    # Regra v29: o acesso mais recente assume a conta. Em vez de bloquear o novo
    # acesso, o Supabase troca o token ativo; os acessos anteriores caem na próxima
    # validação/heartbeat.
    fn = "gm_validate_session" if started else "gm_takeover_session"
    data = gm_session_rpc(fn, {"p_session_token": token})
    ok, reason = gm_session_result(data)
    if ok:
        st.session_state["gm_device_session_started"] = True
        st.session_state["gm_session_last_validation_monotonic"] = time.monotonic()
    return ok, reason


def gm_drop_replaced_local_session():
    """Derruba somente este navegador quando outro acesso assumiu a conta.

    Não chama gm_end_session nem sign_out remoto, portanto não interfere na sessão
    mais nova que acabou de assumir a conta.
    """
    gm_auth_clear_local_session()
    gm_auth_clear_resume_marker()
    gm_clear_device_session_marker()
    st.session_state["gm_session_replaced_notice"] = True


def gm_session_heartbeat_tick():
    """
    Mantém last_seen_at atualizado enquanto a área VIP permanece aberta.

    A função só valida a sessão já iniciada; nunca chama gm_start_session e,
    portanto, nunca toma a sessão de outro aparelho. Se o Supabase indicar que
    esta aba perdeu a sessão, força um rerun completo para que a tela normal de
    conflito/bloqueio seja exibida pelo portal.
    """
    if not st.session_state.get("gm_device_session_started"):
        return

    if not st.session_state.get("gm_auth_access_token"):
        return

    # Um rerun normal do app acabou de validar a sessão. Evita uma segunda RPC
    # imediata quando o fragmento é montado.
    last_check = st.session_state.get("gm_session_last_validation_monotonic")
    if isinstance(last_check, (int, float)):
        if (time.monotonic() - float(last_check)) < 90:
            return

    token = str(st.session_state.get("gm_device_session_token") or "").strip()
    if not token:
        return

    try:
        data = gm_session_rpc("gm_validate_session", {"p_session_token": token})
        ok, reason = gm_session_result(data)
    except Exception:
        # Uma falha transitória de rede não deve expulsar o cliente.
        return

    if ok:
        st.session_state["gm_session_last_validation_monotonic"] = time.monotonic()
        return

    st.session_state["gm_session_heartbeat_reason"] = str(reason or "unknown")
    if str(reason or "") in {"another_session_active", "session_mismatch"}:
        gm_drop_replaced_local_session()
    st.rerun()


# Streamlit atual oferece fragments com rerun periódico sem recarregar o app
# inteiro. Mantemos um fallback para que uma versão antiga do Streamlit não
# derrube o GM SCORE; nesse caso, os reruns normais ainda validam a sessão.
if hasattr(st, "fragment"):
    @st.fragment(run_every="2m")
    def gm_render_session_heartbeat():
        gm_session_heartbeat_tick()
else:
    def gm_render_session_heartbeat():
        return


def gm_end_current_session():
    token = st.session_state.get("gm_device_session_token")
    if token and st.session_state.get("gm_auth_access_token"):
        try:
            gm_session_rpc("gm_end_session", {"p_session_token": token})
        except Exception:
            pass
    st.session_state.pop("gm_device_session_started", None)


def gm_auth_sign_out():
    # Libera primeiro a trava de sessão no banco. Se este navegador não for o
    # dono da sessão ativa, o RPC não altera a sessão legítima do outro aparelho.
    gm_end_current_session()
    client = gm_auth_client_from_session()
    if client is not None:
        try:
            client.auth.sign_out()
        except Exception:
            pass
    gm_auth_clear_local_session()
    gm_auth_clear_resume_marker()
    gm_clear_device_session_marker()


def gm_accept_terms():
    """Registra o aceite da política de uso pelo próprio cliente via RLS."""
    client = gm_auth_client_from_session()
    user_id = st.session_state.get("gm_auth_user_id")
    if client is None or not user_id:
        raise RuntimeError("Sessão autenticada indisponível.")

    now_utc = pd.Timestamp.now(tz="UTC").isoformat()
    result = (
        client.table("gm_users")
        .update({
            "terms_accepted": True,
            "terms_accepted_at": now_utc,
            "last_seen_at": now_utc,
            "updated_at": now_utc,
        })
        .eq("id", user_id)
        .execute()
    )
    rows = getattr(result, "data", None) or []
    st.session_state.pop("gm_auth_profile", None)
    if not rows:
        # Dependendo da configuração do PostgREST, UPDATE pode não devolver linhas.
        # A confirmação definitiva é feita relendo o perfil protegido por RLS.
        profile = gm_auth_get_profile(force=True)
        if not profile or not bool(profile.get("terms_accepted")):
            raise RuntimeError("Não foi possível registrar o aceite dos termos.")
    return True


def gm_auth_sign_up(nome, email, password):
    """Cria conta pelo Auth REST oficial e preserva confirmação por e-mail.

    O retorno/erro é estruturado para a UI distinguir rate limit, cadastro
    desabilitado, e-mail inválido, usuário existente e indisponibilidade.
    Nenhuma credencial privilegiada é usada: somente a Publishable Key.
    """
    url, key = gm_supabase_config()
    clean_name = str(nome).strip()
    clean_email = str(email).strip().lower()
    raw_password = str(password)
    if not url or not key:
        raise RuntimeError("GM_SIGNUP_CONFIG|supabase_not_configured|Configuração do Supabase ausente.")

    try:
        r = requests.post(
            f"{url}/auth/v1/signup",
            params={"redirect_to": GM_EMAIL_CONFIRM_REDIRECT},
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={
                "email": clean_email,
                "password": raw_password,
                "data": {"nome": clean_name, "terms_accepted": True},
            },
            timeout=15,
        )
    except requests.Timeout as exc:
        raise RuntimeError("GM_SIGNUP_NETWORK|timeout|Tempo esgotado ao conectar ao serviço de cadastro.") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"GM_SIGNUP_NETWORK|{type(exc).__name__}|Falha de conexão com o serviço de cadastro.") from exc

    try:
        payload = r.json() if r.content else {}
    except Exception:
        payload = {}

    if r.status_code not in (200, 201):
        code = str(payload.get("error_code") or payload.get("code") or payload.get("error") or "").strip()
        message = str(
            payload.get("msg")
            or payload.get("message")
            or payload.get("error_description")
            or payload.get("error")
            or ""
        ).strip()
        raise RuntimeError(f"GM_SIGNUP_HTTP_{r.status_code}|{code}|{message}")

    user = payload.get("user") if isinstance(payload, dict) else None
    if user is None and isinstance(payload, dict) and payload.get("id"):
        user = payload
    if not isinstance(user, dict) or not str(user.get("id") or "").strip():
        raise RuntimeError("GM_SIGNUP_RESPONSE|invalid_response|O serviço não retornou um usuário válido.")

    # Quando confirmação de e-mail está ativa, o Supabase pode devolver resposta
    # ofuscada para um e-mail já existente. identities=[] é o sinal seguro para
    # não anunciar falsamente que uma nova conta foi criada.
    identities = user.get("identities")
    if isinstance(identities, list) and len(identities) == 0:
        raise RuntimeError("GM_SIGNUP_HTTP_422|user_already_exists|User already registered")

    return payload


def gm_admin_rpc(function_name, params=None):
    """Executa uma função administrativa usando a sessão autenticada do administrador."""
    client = gm_auth_client_from_session()
    if client is None:
        raise RuntimeError("Sessão administrativa indisponível.")
    result = client.rpc(function_name, params or {}).execute()
    return getattr(result, "data", None)


def gm_admin_list_users():
    rows = gm_admin_rpc("gm_admin_list_users") or []
    return [row for row in rows if isinstance(row, dict)]


def gm_admin_pro_suspensions():
    """Mapa de suspensões PRO para o painel, carregado em uma única RPC."""
    try:
        rows = gm_admin_rpc("gm_admin_list_pro_suspensions") or []
        return {str(r.get("user_id")): bool(r.get("pro_suspended")) for r in rows if isinstance(r, dict)}
    except Exception:
        return {}


def gm_admin_set_pro_suspension(user_id, suspended):
    return gm_admin_rpc("gm_admin_set_pro_suspension", {"p_user_id": str(user_id), "p_suspended": bool(suspended)})


def gm_daily_pick_delete_history(row):
    """Exclui uma dica encerrada específica; exige ADM no RPC do banco."""
    return gm_daily_pick_rpc("gm_admin_delete_daily_pick", {
        "p_pick_date": str(row.get("pick_date") or ""),
        "p_pick_kind": str(row.get("pick_kind") or "dica"),
        "p_status": str(row.get("status") or ""),
        "p_total_odd": _gm_daily_num(row.get("total_odd")),
        "p_legs": row.get("legs") or [],
    })


# ============================================================
# CENTRAL DE NOVIDADES
# ============================================================
GM_NEWS_CATEGORIES = {
    "novidade": "🆕 Novidade",
    "melhoria": "✨ Melhoria",
    "vip": "⭐ VIP",
    "seguranca": "🔐 Segurança",
    "manutencao": "🛠️ Manutenção",
    "importante": "📣 Importante",
}


def gm_news_rpc(function_name, params=None):
    """Executa RPCs da Central de Novidades com a sessão autenticada atual."""
    client = gm_auth_client_from_session()
    if client is None:
        raise RuntimeError("Sessão autenticada indisponível.")
    result = client.rpc(function_name, params or {}).execute()
    return getattr(result, "data", None)


def gm_list_news():
    rows = gm_news_rpc("gm_list_news") or []
    return [row for row in rows if isinstance(row, dict)]


def gm_onesignal_push(title, message, launch_url="https://gmscore.com.br"):
    """Envia Web Push para todos os dispositivos inscritos no OneSignal.

    Falhas de push nunca desfazem uma publicação já concluída no GM SCORE.
    As credenciais permanecem somente em st.secrets no servidor.
    """
    try:
        app_id = str(st.secrets.get("ONESIGNAL_APP_ID", "") or "").strip()
        api_key = str(st.secrets.get("ONESIGNAL_API_KEY", "") or "").strip()
    except Exception:
        return False
    if not app_id or not api_key:
        return False

    payload = {
        "app_id": app_id,
        "included_segments": ["Total Subscriptions"],
        "headings": {"en": str(title or "GM SCORE")[:120]},
        "contents": {"en": str(message or "Há uma nova atualização disponível.")[:500]},
        "url": str(launch_url or "https://gmscore.com.br"),
    }
    try:
        response = requests.post(
            "https://api.onesignal.com/notifications",
            headers={
                "Authorization": f"Key {api_key}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=payload,
            timeout=8,
        )
        return 200 <= int(response.status_code) < 300
    except Exception:
        return False


def gm_publish_system_news(title, message, category="novidade", featured=True, push_title=None, push_message=None):
    """Publica na Central de Novidades e dispara o push externo correspondente."""
    data = gm_admin_rpc("gm_admin_publish_news", {
        "p_title": str(title or "").strip(),
        "p_message": str(message or "").strip(),
        "p_category": str(category or "novidade"),
        "p_is_featured": bool(featured),
    })
    gm_invalidate_unread_news_cache()
    gm_onesignal_push(
        push_title if push_title is not None else title,
        push_message if push_message is not None else message,
    )
    return data


def gm_daily_pick_publish_news(kind, opt):
    labels = {"matadeira": "Matadeira", "dica": "Dica do Dia", "bingo": "Bingo"}
    label = labels.get(str(kind or "dica"), "Dica do Dia")
    legs = list((opt or {}).get("legs") or [])
    odd = _gm_daily_num((opt or {}).get("total_odd")) or 0.0
    count = len(legs)
    detail = f"{count} seleção(ões) · odd {odd:.2f}" if count else f"odd {odd:.2f}"
    return gm_publish_system_news(
        f"💡 Nova {label} disponível",
        f"Uma nova {label} foi publicada · {detail}. Confira em Dicas do Dia.",
        category="novidade",
        featured=True,
        push_title="💡 Novas Dicas do Dia disponíveis",
        push_message="Há uma nova publicação no GM SCORE. Abra o app para conferir.",
    )


def gm_invalidate_unread_news_cache():
    st.session_state.pop("_gm_unread_news_count_cache", None)
    st.session_state.pop("_gm_unread_news_count_tick", None)


def gm_unread_news_count():
    # V117: a navegação é renderizada em todo rerun. Evita um RPC ao Supabase a
    # cada clique/widget mantendo uma janela curta de 120 s; ações de leitura
    # invalidam explicitamente o valor para o badge continuar imediato.
    now = time.monotonic()
    tick = st.session_state.get("_gm_unread_news_count_tick")
    if isinstance(tick, (int, float)) and now - float(tick) < 120:
        try:
            return max(0, int(st.session_state.get("_gm_unread_news_count_cache") or 0))
        except Exception:
            pass
    data = gm_news_rpc("gm_unread_news_count")
    if isinstance(data, list):
        data = data[0] if data else 0
    try:
        count = max(0, int(data or 0))
    except Exception:
        count = 0
    st.session_state["_gm_unread_news_count_cache"] = count
    st.session_state["_gm_unread_news_count_tick"] = now
    return count


def gm_news_format_datetime(value):
    if not value:
        return "—"
    try:
        ts = pd.to_datetime(value, utc=True)
        return ts.tz_convert("America/Sao_Paulo").strftime("%d/%m/%Y às %H:%M")
    except Exception:
        return str(value)


def gm_render_top_news_bell(unread_count):
    """Sino flutuante da Central de Novidades, com badge vermelho de não lidas."""
    try:
        unread = max(0, int(unread_count or 0))
    except Exception:
        unread = 0

    badge_text = "99+" if unread > 99 else str(unread)
    if unread > 0:
        badge_css = f"""
        .st-key-gm_top_news_floating::after {{
            content: "{badge_text}";
            position: absolute;
            top: -5px;
            right: -5px;
            min-width: 22px;
            height: 22px;
            padding: 0 5px;
            border-radius: 999px;
            background: #ff3347;
            color: #ffffff;
            border: 2px solid #0b0f14;
            font-size: 12px;
            font-weight: 800;
            line-height: 18px;
            text-align: center;
            box-sizing: border-box;
            pointer-events: none;
        }}
        """
    else:
        badge_css = ".st-key-gm_top_news_floating::after { display: none; }"

    st.markdown(
        f"""
        <style>
        .st-key-gm_top_news_floating {{
            position: fixed;
            top: 5.4rem;
            right: 1rem;
            z-index: 999999;
            width: 52px !important;
        }}
        .st-key-gm_top_news_floating button {{
            width: 52px !important;
            height: 52px !important;
            min-height: 52px !important;
            padding: 0 !important;
            border-radius: 50% !important;
            border: 1px solid rgba(255,255,255,.20) !important;
            background: #171c24 !important;
            box-shadow: 0 8px 24px rgba(0,0,0,.28) !important;
            font-size: 23px !important;
        }}
        .st-key-gm_top_news_floating button:hover {{
            border-color: #22c55e !important;
        }}
        {badge_css}
        @media (max-width: 768px) {{
            .st-key-gm_top_news_floating {{
                top: 5.2rem;
                right: .85rem;
            }}
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )

    with st.container(key="gm_top_news_floating"):
        if st.button(
            "🔔",
            key="gm_top_news_button",
            help=(f"{unread} novidade(s) não lida(s)" if unread else "Novidades do GM SCORE"),
        ):
            st.session_state["gm_news_open"] = True
            st.rerun()


def gm_render_news_center():
    """Central compacta de novidades em formato de lista, otimizada para celular."""
    try:
        rows = gm_list_news()
    except Exception:
        rows = []

    hidden = set(st.session_state.get("gm_news_hidden_session", []))

    # Sincroniza itens que já foram ocultados nesta sessão, mas ainda constam como
    # não lidos no Supabase. Isso evita o sino exibir um contador maior do que a
    # quantidade de novidades realmente visíveis para o usuário.
    hidden_unread_ids = [
        str(r.get("id") or "")
        for r in rows
        if str(r.get("id") or "") in hidden and not bool(r.get("is_read"))
    ]
    hidden_unread_ids = [news_id for news_id in hidden_unread_ids if news_id]
    if hidden_unread_ids:
        sync_ok = True
        for news_id in hidden_unread_ids:
            try:
                gm_news_rpc("gm_mark_news_read", {"p_news_id": news_id})
                gm_invalidate_unread_news_cache()
            except Exception:
                sync_ok = False
                break
        if sync_ok:
            st.rerun()

    visible_rows = [r for r in rows if str(r.get("id") or "") not in hidden]
    unread = sum(1 for row in visible_rows if not bool(row.get("is_read")))

    @st.dialog("🔔 Novidades", width="large")
    def _gm_news_dialog():
        st.markdown(
            """
            <style>
            div[data-testid="stDialog"] div[data-testid="stVerticalBlock"] { gap: .48rem; }
            div[data-testid="stDialog"] div[data-testid="stHorizontalBlock"] { flex-wrap:nowrap !important; gap:.5rem !important; }
            div[data-testid="stDialog"] div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] { min-width:0 !important; width:50% !important; flex:1 1 50% !important; }
            div[data-testid="stDialog"] div[data-testid="stHorizontalBlock"] button { min-height:2.65rem; }
            .gm-news-head { margin:-.15rem 0 .35rem 0; color:#9ca3af; font-size:.88rem; line-height:1.35; }
            .gm-news-card { border:1px solid rgba(148,163,184,.20); background:rgba(20,25,34,.72); border-radius:14px; padding:.72rem .82rem .62rem .82rem; margin:.15rem 0 .22rem 0; }
            .gm-news-card.unread { border-left:4px solid #22c55e; }
            .gm-news-card.read { opacity:.78; }
            .gm-news-row { display:flex; align-items:flex-start; gap:.65rem; }
            .gm-news-icon { font-size:1.25rem; line-height:1.3; width:1.45rem; flex:0 0 1.45rem; }
            .gm-news-body { min-width:0; flex:1; }
            .gm-news-title { color:#f8fafc; font-size:1rem; font-weight:780; line-height:1.22; margin:0; }
            .gm-news-msg { color:#aeb6c2; font-size:.84rem; line-height:1.32; margin:.22rem 0 0 0; }
            .gm-news-meta { color:#7f8997; font-size:.72rem; line-height:1.2; margin-top:.34rem; }
            .gm-news-new { color:#34d399; font-weight:800; }
            @media (max-width:768px) { .gm-news-card { border-radius:12px; padding:.62rem .68rem .54rem .68rem; } .gm-news-title { font-size:.96rem; } .gm-news-msg { font-size:.81rem; } }
            </style>
            """,
            unsafe_allow_html=True,
        )
        st.markdown(
            f'<div class="gm-news-head">Atualizações do GM SCORE'
            + (f' • <span class="gm-news-new">{unread} nova{"s" if unread != 1 else ""}</span>' if unread else ' • tudo lido')
            + '</div>',
            unsafe_allow_html=True,
        )

        if not visible_rows:
            st.info("Nenhuma novidade para exibir nesta sessão.")
        else:
            icon_by_category = {"vip": "⭐", "melhoria": "📊", "novidade": "🎁", "seguranca": "🛡️", "manutencao": "🔧", "importante": "📣"}
            label_by_category = {"vip": "VIP", "melhoria": "MELHORIA", "novidade": "NOVIDADE", "seguranca": "SEGURANÇA", "manutencao": "ATUALIZAÇÃO", "importante": "IMPORTANTE"}

            for row in visible_rows:
                news_id = str(row.get("id") or "")
                title = str(row.get("title") or "Novidade")
                message = " ".join(str(row.get("message") or "").split())
                if len(message) > 112:
                    message = message[:109].rstrip() + "…"
                category = str(row.get("category") or "novidade")
                is_read = bool(row.get("is_read"))
                featured = bool(row.get("is_featured"))
                icon = icon_by_category.get(category, "🔔")
                label = label_by_category.get(category, "NOVIDADE")
                state_class = "read" if is_read else "unread"
                state_label = "Lida" if is_read else "Nova"
                featured_label = " • Destaque" if featured else ""
                safe_title = html.escape(title)
                safe_message = html.escape(message)
                safe_meta = html.escape(f"{label} • {state_label}{featured_label} • {gm_news_format_datetime(row.get('published_at'))}")

                st.markdown(
                    f"""<div class="gm-news-card {state_class}">
                        <div class="gm-news-row"><div class="gm-news-icon">{icon}</div><div class="gm-news-body">
                        <div class="gm-news-title">{safe_title}</div><div class="gm-news-msg">{safe_message}</div>
                        <div class="gm-news-meta">{safe_meta}</div></div></div></div>""",
                    unsafe_allow_html=True,
                )

                action_left, action_right = st.columns(2, gap="small")
                with action_left:
                    if not is_read and news_id:
                        if st.button("👁 Ler", use_container_width=True, key=f"gm_news_modal_read_{news_id}"):
                            try:
                                gm_news_rpc("gm_mark_news_read", {"p_news_id": news_id})
                                gm_invalidate_unread_news_cache()
                                st.rerun()
                            except Exception:
                                st.error("Não foi possível marcar como lida agora.")
                    else:
                        st.button("✓ Lida", use_container_width=True, key=f"gm_news_modal_read_done_{news_id}", disabled=True)
                with action_right:
                    if news_id and st.button("🗑 Apagar", use_container_width=True, key=f"gm_news_modal_hide_{news_id}", help="Remove esta novidade da sua lista atual"):
                        try:
                            # Ao apagar uma novidade ainda não lida, marcamos como lida
                            # antes de ocultar. Assim o contador do sino permanece
                            # sincronizado com o que o usuário realmente consegue ver.
                            if not is_read:
                                gm_news_rpc("gm_mark_news_read", {"p_news_id": news_id})
                                gm_invalidate_unread_news_cache()
                            hidden_now = set(st.session_state.get("gm_news_hidden_session", []))
                            hidden_now.add(news_id)
                            st.session_state["gm_news_hidden_session"] = list(hidden_now)
                            st.rerun()
                        except Exception:
                            st.error("Não foi possível apagar esta novidade agora.")

        if visible_rows and unread:
            if st.button("✓ Marcar todas como lidas", use_container_width=True, key="gm_news_modal_mark_all"):
                try:
                    gm_news_rpc("gm_mark_all_news_read")
                    gm_invalidate_unread_news_cache()
                    st.rerun()
                except Exception:
                    st.error("Não foi possível atualizar as leituras agora.")

        if st.button("Fechar", use_container_width=True, key="gm_news_modal_close"):
            st.session_state["gm_news_open"] = False
            st.rerun()

    _gm_news_dialog()

def gm_render_admin_news_manager():
    """Publicação e gestão de novidades. A autorização real continua nas RPCs do Supabase."""
    st.markdown("### 🔔 Central de Novidades")
    st.caption("Publique somente mudanças relevantes para a experiência dos clientes.")

    with st.expander("➕ Publicar nova novidade", expanded=False):
        with st.form("gm_admin_news_publish_form", clear_on_submit=True):
            title = st.text_input("Título", max_chars=140)
            message = st.text_area("Mensagem", height=130, max_chars=2000)
            category = st.selectbox(
                "Categoria",
                list(GM_NEWS_CATEGORIES.keys()),
                format_func=lambda x: GM_NEWS_CATEGORIES.get(x, x),
            )
            featured = st.checkbox("Destacar esta novidade")
            submitted = st.form_submit_button("📣 Publicar novidade", type="primary", use_container_width=True)
        if submitted:
            if not title.strip() or not message.strip():
                st.warning("Informe título e mensagem.")
            else:
                try:
                    gm_publish_system_news(
                        title.strip(),
                        message.strip(),
                        category=category,
                        featured=bool(featured),
                    )
                    st.success("Novidade publicada com sucesso.")
                    st.rerun()
                except Exception as exc:
                    st.error("Não foi possível publicar a novidade.")
                    st.caption(str(exc))

    try:
        news_rows = gm_admin_rpc("gm_admin_list_news") or []
    except Exception as exc:
        st.error("Não foi possível carregar as novidades administrativas.")
        st.caption(str(exc))
        return

    if not news_rows:
        st.info("Nenhuma novidade cadastrada.")
        return

    st.caption(f"{len(news_rows)} publicação(ões) cadastrada(s).")
    for row in news_rows:
        news_id = str(row.get("id") or "")
        title_now = str(row.get("title") or "Novidade")
        active_now = bool(row.get("is_active"))
        status = "🟢 Ativa" if active_now else "⚪ Inativa"
        with st.expander(f"{status} · {title_now}", expanded=False):
            st.caption(f"{GM_NEWS_CATEGORIES.get(str(row.get('category') or 'novidade'), '🆕 Novidade')} • {gm_news_format_datetime(row.get('published_at'))}")
            with st.form(f"gm_admin_news_edit_{news_id}"):
                edit_title = st.text_input("Título", value=title_now, key=f"gm_admin_news_title_{news_id}")
                edit_message = st.text_area("Mensagem", value=str(row.get("message") or ""), height=130, key=f"gm_admin_news_message_{news_id}")
                categories = list(GM_NEWS_CATEGORIES.keys())
                current_category = str(row.get("category") or "novidade")
                category_index = categories.index(current_category) if current_category in categories else 0
                edit_category = st.selectbox("Categoria", categories, index=category_index, format_func=lambda x: GM_NEWS_CATEGORIES.get(x, x), key=f"gm_admin_news_category_{news_id}")
                edit_featured = st.checkbox("Destacar", value=bool(row.get("is_featured")), key=f"gm_admin_news_featured_{news_id}")
                save = st.form_submit_button("💾 Salvar alterações", use_container_width=True)
            if save:
                if not edit_title.strip() or not edit_message.strip():
                    st.warning("Título e mensagem são obrigatórios.")
                else:
                    try:
                        gm_admin_rpc("gm_admin_update_news", {
                            "p_news_id": news_id,
                            "p_title": edit_title.strip(),
                            "p_message": edit_message.strip(),
                            "p_category": edit_category,
                            "p_is_featured": bool(edit_featured),
                        })
                        st.success("Novidade atualizada.")
                        st.rerun()
                    except Exception as exc:
                        st.error("Não foi possível atualizar a novidade.")
                        st.caption(str(exc))

            a1, a2 = st.columns(2)
            with a1:
                toggle_label = "⏸ Desativar" if active_now else "▶️ Ativar"
                if st.button(toggle_label, use_container_width=True, key=f"gm_admin_news_toggle_{news_id}"):
                    try:
                        gm_admin_rpc("gm_admin_set_news_active", {"p_news_id": news_id, "p_is_active": not active_now})
                        st.rerun()
                    except Exception as exc:
                        st.error("Não foi possível alterar o status da novidade.")
                        st.caption(str(exc))
            with a2:
                confirm_delete = st.checkbox("Confirmar exclusão", key=f"gm_admin_news_confirm_delete_{news_id}")
                if st.button("🗑️ Excluir", use_container_width=True, disabled=not confirm_delete, key=f"gm_admin_news_delete_{news_id}"):
                    try:
                        gm_admin_rpc("gm_admin_delete_news", {"p_news_id": news_id})
                        st.success("Novidade excluída.")
                        st.rerun()
                    except Exception as exc:
                        st.error("Não foi possível excluir a novidade.")
                        st.caption(str(exc))



# ============================================================
# AVALIAÇÕES PÚBLICAS — CLIENTES VIP + RESPOSTA OFICIAL
# ============================================================
def gm_reviews_rpc(function_name, params=None):
    """Executa RPCs de avaliações com a sessão autenticada atual."""
    client = gm_auth_client_from_session()
    if client is None:
        raise RuntimeError("Sessão autenticada indisponível.")
    result = client.rpc(function_name, params or {}).execute()
    return getattr(result, "data", None)


@st.cache_data(ttl=60, show_spinner=False)
def gm_public_reviews():
    """Lista somente avaliações públicas; nunca expõe e-mail."""
    try:
        client = gm_new_supabase_client()
        result = client.rpc("gm_list_public_reviews").execute()
        rows = getattr(result, "data", None) or []
        return [row for row in rows if isinstance(row, dict)]
    except Exception:
        return []


def _gm_review_stars(rating):
    try:
        value = max(1, min(5, int(rating)))
    except Exception:
        value = 0
    return "⭐" * value + "☆" * max(0, 5 - value)


def gm_render_public_reviews():
    """Bloco público de avaliações reais cadastradas por clientes VIP."""
    rows = gm_public_reviews()
    st.markdown("### ⭐ Avaliações de clientes")
    st.caption("Avaliações publicadas por usuários identificados do GM SCORE. Respostas da equipe aparecem junto ao comentário.")

    if not rows:
        st.info("As primeiras avaliações de clientes aparecerão aqui.")
        return

    ratings = []
    for row in rows:
        try:
            ratings.append(float(row.get("rating") or 0))
        except Exception:
            pass
    avg = sum(ratings) / len(ratings) if ratings else 0.0
    st.markdown(
        f"""
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:.35rem 0 .8rem">
          <div style="font-size:1.7rem;font-weight:950;color:#f8fafc">{avg:.1f}/5</div>
          <div style="font-size:1.05rem;color:#facc15">{_gm_review_stars(round(avg))}</div>
          <div style="font-size:.82rem;color:#94a3b8">{len(rows)} avaliação(ões) pública(s)</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    for row in rows[:12]:
        name = _gm_safe_html(row.get("reviewer_name") or "Cliente GM SCORE")
        comment = _gm_safe_html(row.get("comment") or "")
        reply = str(row.get("admin_reply") or "").strip()
        stars = _gm_review_stars(row.get("rating"))
        when = gm_admin_format_datetime(row.get("created_at"))
        reply_html = ""
        if reply:
            reply_html = (
                '<div style="margin-top:10px;padding:10px 11px;border-left:3px solid #22c55e;'
                'background:rgba(34,197,94,.08);border-radius:8px"><b style="color:#86efac">'
                'Resposta GM SCORE</b><div style="margin-top:4px;color:#dbe4ea">'
                + _gm_safe_html(reply)
                + '</div></div>'
            )
        st.markdown(
            f"""
            <div style="border:1px solid rgba(148,163,184,.20);border-radius:15px;padding:13px 14px;margin:.55rem 0;background:rgba(15,23,42,.48)">
              <div style="display:flex;justify-content:space-between;gap:8px;align-items:center;flex-wrap:wrap">
                <b style="color:#f8fafc">{name}</b><span style="color:#facc15;letter-spacing:.03em">{stars}</span>
              </div>
              <div style="font-size:.72rem;color:#64748b;margin-top:2px">{_gm_safe_html(when)}</div>
              <div style="margin-top:8px;line-height:1.5;color:#dbe4ea">{comment}</div>
              {reply_html}
            </div>
            """,
            unsafe_allow_html=True,
        )


def gm_render_review_form(profile):
    """Permite uma avaliação por usuário; nova submissão atualiza a anterior."""
    if not profile or profile.get("role") == "admin":
        return
    try:
        mine = gm_reviews_rpc("gm_my_review") or []
        current = mine[0] if isinstance(mine, list) and mine else (mine if isinstance(mine, dict) else {})
    except Exception:
        current = {}

    try:
        current_rating = max(1, min(5, int(current.get("rating") or 5)))
    except Exception:
        current_rating = 5
    current_comment = str(current.get("comment") or "")
    options = [1, 2, 3, 4, 5]
    with st.form("gm_review_form", clear_on_submit=False):
        st.markdown("### ⭐ Avalie o GM SCORE")
        st.caption("Sua avaliação será pública com o nome cadastrado na sua conta. Seu e-mail nunca é exibido.")
        rating = st.radio(
            "Sua nota",
            options,
            index=options.index(current_rating),
            horizontal=True,
            format_func=lambda n: "⭐" * n,
        )
        comment = st.text_area(
            "Comentário, reclamação ou dica",
            value=current_comment,
            max_chars=1200,
            height=120,
            placeholder="Conte como está sendo sua experiência com o GM SCORE.",
        )
        submitted = st.form_submit_button("Publicar avaliação", type="primary", use_container_width=True)
    if submitted:
        clean = str(comment or "").strip()
        if len(clean) < 3:
            st.warning("Escreva um comentário com pelo menos 3 caracteres.")
            return
        try:
            gm_reviews_rpc("gm_submit_review", {"p_rating": int(rating), "p_comment": clean})
            gm_public_reviews.clear()
            st.success("✅ Avaliação publicada. Obrigado pelo feedback.")
            st.session_state["gm_reviews_open"] = False
            st.rerun()
        except Exception:
            st.error("Não foi possível publicar a avaliação agora. Tente novamente.")


def gm_render_review_dialog(profile):
    if not st.session_state.get("gm_reviews_open"):
        return
    if hasattr(st, "dialog"):
        @st.dialog("⭐ Avaliar GM SCORE", width="large")
        def _gm_review_dialog():
            gm_render_review_form(profile)
            if st.button("Fechar", use_container_width=True, key="gm_review_close"):
                st.session_state["gm_reviews_open"] = False
                st.rerun()
        _gm_review_dialog()
    else:
        with st.expander("⭐ Avaliar GM SCORE", expanded=True):
            gm_render_review_form(profile)
            if st.button("Fechar", use_container_width=True, key="gm_review_close_fallback"):
                st.session_state["gm_reviews_open"] = False
                st.rerun()


def gm_render_admin_reviews_manager():
    st.markdown("### ⭐ Avaliações de clientes")
    st.caption("Responda avaliações, reclamações e sugestões. A resposta aparece publicamente junto à avaliação.")
    try:
        rows = gm_admin_rpc("gm_admin_list_reviews") or []
    except Exception:
        st.error("Não foi possível carregar as avaliações administrativas.")
        return
    rows = [row for row in rows if isinstance(row, dict)]
    if not rows:
        st.info("Nenhuma avaliação recebida ainda.")
        return

    for row in rows[:50]:
        rid = row.get("review_id")
        name = str(row.get("reviewer_name") or "Cliente GM SCORE")
        rating = row.get("rating")
        public = bool(row.get("is_public", True))
        st.markdown(f"**{name}** • {_gm_review_stars(rating)}")
        st.caption(gm_admin_format_datetime(row.get("created_at")))
        st.write(str(row.get("comment") or ""))
        with st.form(f"gm_admin_review_reply_{rid}"):
            reply = st.text_area(
                "Resposta oficial",
                value=str(row.get("admin_reply") or ""),
                max_chars=1200,
                height=90,
                key=f"gm_admin_review_reply_text_{rid}",
            )
            save = st.form_submit_button("💬 Salvar resposta", use_container_width=True)
        if save:
            try:
                gm_admin_rpc("gm_admin_reply_review", {"p_review_id": int(rid), "p_reply": str(reply or "").strip()})
                gm_public_reviews.clear()
                st.success("Resposta salva.")
                st.rerun()
            except Exception:
                st.error("Não foi possível salvar a resposta.")
        toggle_label = "🙈 Ocultar do público" if public else "👁 Tornar pública"
        if st.button(toggle_label, use_container_width=True, key=f"gm_admin_review_public_{rid}"):
            try:
                gm_admin_rpc("gm_admin_set_review_public", {"p_review_id": int(rid), "p_is_public": not public})
                gm_public_reviews.clear()
                st.rerun()
            except Exception:
                st.error("Não foi possível alterar a visibilidade.")
        st.markdown("---")


def gm_admin_format_datetime(value):
    if not value:
        return "—"
    try:
        ts = pd.to_datetime(value, utc=True)
        return ts.tz_convert("America/Sao_Paulo").strftime("%d/%m/%Y %H:%M")
    except Exception:
        return str(value)


def gm_admin_status_label(row):
    if bool(row.get("blocked")) or row.get("vip_status") == "blocked":
        return "⛔ Bloqueado"
    if bool(row.get("pro_suspended")):
        return "⏸️ PRO suspenso · FREE"
    status = str(row.get("vip_status") or "pending")
    if status == "active":
        return "🟢 VIP ativo"
    if status == "expired":
        return "⌛ Expirado"
    return "⏳ Pendente"


def _gm_daily_valid_direct_bet_url(value):
    """Valida o link direto informado pelo ADM sem vincular a uma casa específica."""
    value = str(value or "").strip()
    if not value:
        return None
    try:
        from urllib.parse import urlparse
        parsed = urlparse(value)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            return None
        return value
    except Exception:
        return None


def gm_daily_pick_admin_bet_link(kind, idx, key_prefix):
    """Um único link HTTPS opcional, definido pelo ADM para a dica aprovada."""
    st.markdown("**🔗 Link direto da aposta (opcional)**")
    value = st.text_input(
        "Link da aposta",
        key=f"{key_prefix}_direct_bet_url_{kind}_{idx}",
        placeholder="https://...",
        help="Cole o link direto da aposta na casa que você escolher. O cliente verá apenas o botão oficial do GM SCORE.",
        label_visibility="collapsed",
    ).strip()
    if not value:
        return None, False
    valid = _gm_daily_valid_direct_bet_url(value)
    if not valid:
        st.warning("Informe um link HTTPS válido para a aposta.")
        return None, True
    st.caption("O cliente verá: 🎯 Ir para a aposta · GM SCORE")
    return valid, False


def _gm_daily_row_direct_bet_url(row):
    """Lê o link atual e mantém compatibilidade com dicas publicadas no formato V74/V76."""
    if not isinstance(row, dict):
        return None
    direct = _gm_daily_valid_direct_bet_url(row.get("bookmaker_url"))
    if direct:
        return direct
    meta = row.get("model_meta") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    if isinstance(meta, dict):
        direct = _gm_daily_valid_direct_bet_url(meta.get("direct_bet_url"))
        if direct:
            return direct
        # Compatibilidade com o antigo array bet_links: usa o primeiro link válido.
        for item in meta.get("bet_links") or []:
            if isinstance(item, dict):
                direct = _gm_daily_valid_direct_bet_url(item.get("url"))
                if direct:
                    return direct
    return None


def _gm_daily_pick_option_signature(opt):
    """V127: hash estável da bet exata; independe de sessão/cache e pode ser persistido no Supabase."""
    if not isinstance(opt, dict):
        return ""
    legs = []
    for leg in (opt.get("legs") or []):
        if not isinstance(leg, dict):
            continue
        legs.append({
            "match_id": str(leg.get("match_id") or "").strip(),
            "market_code": str(leg.get("market_code") or "").strip(),
            "market": str(leg.get("market") or "").strip(),
        })
    payload = {
        "pick_kind": str(opt.get("pick_kind") or "").strip(),
        "legs": sorted(legs, key=lambda x: (x["match_id"], x["market_code"], x["market"])),
        "total_odd": round(float(_gm_daily_num(opt.get("total_odd")) or 0.0), 4),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def _gm_daily_pick_discard_key(day):
    return f"gm_daily_discarded_{day.isoformat()}"

def _gm_daily_pick_load_discarded(day, force=False):
    """Carrega uma vez por sessão os descartes persistentes do dia."""
    key = _gm_daily_pick_discard_key(day)
    if force or key not in st.session_state:
        try:
            data = gm_daily_pick_rpc("gm_admin_daily_pick_discarded", {"p_pick_date": day.isoformat()}) or []
            st.session_state[key] = {str(x.get("option_signature") or "") for x in data if isinstance(x, dict) and x.get("option_signature")}
        except Exception:
            st.session_state.setdefault(key, set())
    return set(st.session_state.get(key, set()) or set())

def _gm_daily_pick_is_discarded(opt, day):
    return _gm_daily_pick_option_signature(opt) in _gm_daily_pick_load_discarded(day)

def _gm_daily_pick_discard(opt, day):
    """V127: descarte permanente. A mesma bet não volta após refresh, logout ou restart."""
    sig = _gm_daily_pick_option_signature(opt)
    if not sig:
        return False
    gm_daily_pick_rpc("gm_admin_discard_daily_pick_option", {
        "p_pick_date": day.isoformat(),
        "p_pick_kind": str(opt.get("pick_kind") or "dica"),
        "p_option_signature": sig,
        "p_option_payload": opt,
    })
    key = _gm_daily_pick_discard_key(day)
    discarded = _gm_daily_pick_load_discarded(day)
    discarded.add(sig)
    st.session_state[key] = discarded
    return True

def _gm_daily_pick_remove_cached_option(cache_key, kind, opt):
    """Remove só o card tratado; mantém odds/probabilidades já preparadas para resposta instantânea."""
    prepared = st.session_state.get(cache_key)
    if not isinstance(prepared, dict):
        return
    options = prepared.get("options") or {}
    sig = _gm_daily_pick_option_signature(opt)
    options[kind] = [x for x in (options.get(kind) or []) if _gm_daily_pick_option_signature(x) != sig]
    prepared["options"] = options
    st.session_state[cache_key] = prepared


def gm_render_admin_daily_pick_approval():
    """Área privada do ADM para avaliar e publicar as Dicas do Dia."""
    st.markdown("### 💡 Aprovação de dicas")
    st.caption("O motor prepara alternativas privadas. Só a opção aprovada é publicada para os clientes e entra no histórico oficial.")
    try:
        rows = gm_daily_pick_recent(100)
    except Exception:
        st.warning("As Dicas do Dia ainda não estão ativas nesta instalação.")
        return

    today = datetime.now(BRASILIA_TZ).date()
    def published_for(kind):
        return [row for row in rows if str(row.get("pick_date") or "") == today.isoformat() and str(row.get("pick_kind") or "dica") == kind]

    try:
        gm_daily_pick_settle_pending(limit=100)
    except Exception:
        pass

    cache_key = f"gm_daily_admin_options_{today.isoformat()}"
    _has_prepared = isinstance(st.session_state.get(cache_key), dict) and bool(st.session_state.get(cache_key))

    a1, a2 = st.columns(2)
    _load_label = "🔄 Carregar mais opções" if _has_prepared else "💡 Preparar opções"
    if a1.button(_load_label, use_container_width=True, key="gm_admin_daily_refresh_candidates"):
        try:
            if not _has_prepared:
                with st.spinner("Preparando opções para avaliação do ADM..."):
                    # V145: a Central abre imediatamente. O cálculo pesado só começa
                    # por ação explícita do ADM, evitando travar navegação/reruns gerais.
                    st.session_state[cache_key] = gm_daily_pick_prepare_admin_options(force_refresh=False, per_kind=1)
            else:
                with st.spinner("Montando mais opções com os dados já carregados..."):
                    _cached_prepared = st.session_state.get(cache_key) or {}
                    _expanded = gm_daily_pick_expand_cached_admin_options(_cached_prepared, per_kind=5)
                    if _expanded.get("ok"):
                        st.session_state[cache_key] = _expanded
                    else:
                        # Só usa a fonte novamente se a sessão realmente não possuir
                        # a base preparada; nunca força atualização de odds neste botão.
                        st.session_state[cache_key] = gm_daily_pick_prepare_admin_options(force_refresh=False, per_kind=1)
            st.rerun()
        except Exception as exc:
            st.error("Não foi possível preparar as opções agora.")
            st.caption(type(exc).__name__)
    if a2.button("✅ Conferir resultados", use_container_width=True, key="gm_admin_daily_settle_now"):
        try:
            result = gm_daily_pick_settle_pending(limit=100)
            if result.get("errors"):
                st.error("Erro ao gravar resultado. Confirme a RPC v3 do V152 no Supabase.")
                st.caption(" • ".join(result.get("errors")[:3]))
            else:
                st.success(f"Conferência concluída: {result.get('settled',0)} atualizada(s) · {result.get('pending',0)} ainda pendente(s).")
            st.rerun()
        except Exception as exc:
            st.error("Não foi possível conferir os resultados agora.")
            st.caption(type(exc).__name__)

    prepared = st.session_state.get(cache_key) or {}
    if not prepared:
        st.info("Toque em **💡 Preparar opções** para calcular as alternativas do dia. A navegação permanece livre até você iniciar o processamento.")
        return
    if not prepared.get("ok"):
        reason = str(prepared.get("reason") or "indisponivel")
        meta = prepared.get("meta") or {}
        st.info("As opções privadas ainda não puderam ser preparadas.")
        if reason == "source_error":
            st.warning("A fonte oficial de probabilidades/odds não respondeu corretamente. Use Atualizar opções; o sistema não publica dica sem dados reais.")
            if meta.get("error"):
                st.caption(f"Diagnóstico da fonte: {meta.get('error')}")
        else:
            st.caption(f"Diagnóstico: {reason}")
        return

    option_map = prepared.get("options") or {}
    meta = prepared.get("meta") or {}
    total_options = sum(len(v or []) for v in option_map.values())
    if total_options == 0:
        st.warning("Nenhuma aprovação foi formada nesta atualização sem violar os critérios oficiais.")
        st.caption(
            "Fonte: "
            f"{int(meta.get('fixtures') or 0)} partidas oficiais · "
            f"{int(meta.get('predictions') or 0)} previsões · "
            f"{int(meta.get('odds') or 0)} linhas de odds · "
            f"{int(meta.get('candidates') or 0)} mercados candidatos. "
            f"Busca direta recuperou {int(meta.get('direct_prediction_hits') or 0)} previsão(ões) e "
            f"{int(meta.get('direct_odds_hits') or 0)} conjunto(s) de odds. "
            f"Excluídos: {int(meta.get('excluded_past') or 0)} por horário/status e "
            f"{int(meta.get('excluded_unknown_time') or 0)} sem horário confiável. "
            "O piso de 75% permanece inalterado."
        )
    for kind in ("matadeira", "dica", "bingo"):
        label = GM_DAILY_PICK_PROFILES[kind]["label"]
        published_count = len(published_for(kind))
        opts = [o for o in (option_map.get(kind) or []) if not _gm_daily_pick_is_discarded(o, today)]
        suffix = f" · {published_count} publicada(s) hoje" if published_count else ""
        with st.expander(f"{label} — {len(opts)} opção(ões) para avaliar{suffix}", expanded=(kind == "matadeira")):
            if not opts:
                st.caption("Nenhuma alternativa atingiu os filtros mínimos nesta atualização.")
            for idx, opt in enumerate(opts, 1):
                legs = _gm_daily_sort_legs(opt.get("legs") or [])
                total_odd = _gm_daily_num(opt.get("total_odd")) or 0.0
                model_prob = _gm_daily_num(opt.get("model_probability")) or 0.0
                st.markdown(f"**Opção {idx} · {_gm_daily_bet_type_label(opt.get('bet_type'), len(legs))} · odd {total_odd:.2f}**")
                st.caption(f"Probabilidade combinada estimada: {model_prob:.1f}% · todas as pernas ≥ 75%")
                for leg in legs:
                    st.markdown(f"- **{leg.get('market')}** @ {(_gm_daily_num(leg.get('odd')) or 0):.2f} · {(_gm_daily_num(leg.get('probability')) or 0):.0f}% · {leg.get('home')} × {leg.get('away')} · {_gm_daily_time_label(leg.get('time'))}")
                direct_bet_url, bet_link_invalid = gm_daily_pick_admin_bet_link(kind, idx, "gm_admin")
                approve_col, discard_col = st.columns([2, 1])
                if approve_col.button("✅ Aprovar e publicar", use_container_width=True, type="primary", key=f"gm_admin_approve_{kind}_{idx}", disabled=bet_link_invalid):
                    try:
                        publish_opt = dict(opt)
                        publish_opt["direct_bet_url"] = direct_bet_url
                        with st.spinner("Publicando..."):
                            result = gm_daily_pick_publish_selected(publish_opt)
                        if result.get("ok"):
                            _gm_daily_pick_remove_cached_option(cache_key, kind, opt)
                            news_ok = True
                            try:
                                gm_daily_pick_publish_news(kind, publish_opt)
                            except Exception:
                                news_ok = False
                            st.toast("Dica publicada com sucesso.", icon="✅")
                            if not news_ok:
                                st.warning("A dica foi publicada, mas a Novidade automática não pôde ser criada.")
                            st.rerun()
                        else:
                            st.warning("Esta alternativa não pôde ser publicada pelos critérios de segurança.")
                    except Exception as exc:
                        st.error("Falha ao publicar a dica aprovada.")
                        st.caption(type(exc).__name__)
                if discard_col.button("✕ Descartar", use_container_width=True, key=f"gm_admin_discard_{kind}_{idx}"):
                    _gm_daily_pick_discard(opt, today)
                    _gm_daily_pick_remove_cached_option(cache_key, kind, opt)
                    st.toast("Opção descartada permanentemente.", icon="🗑️")
                    st.rerun()
                if idx < len(opts):
                    st.divider()

    st.markdown("### 🗂️ Histórico publicado")
    st.caption("Dicas encerradas ficam visíveis ao público até você decidir excluí-las. Green e Red podem ser removidos individualmente.")
    settled_rows = [r for r in rows if str(r.get("status") or "") in {"green", "red", "void"}]
    settled_rows.sort(key=lambda r: str(r.get("pick_date") or ""), reverse=True)
    if not settled_rows:
        st.info("Ainda não há Dicas do Dia encerradas no histórico.")
    else:
        for hidx, row in enumerate(settled_rows[:60], 1):
            kind = str(row.get("pick_kind") or "dica")
            label = GM_DAILY_PICK_PROFILES.get(kind, {}).get("label", kind.title())
            day = str(row.get("pick_date") or "—")
            odd = _gm_daily_num(row.get("total_odd")) or 0.0
            status = _gm_daily_status_badge(row.get("status"))
            with st.expander(f"{status} · {label} · {day} · odd {odd:.2f}", expanded=False):
                for leg in _gm_daily_sort_legs(row.get("legs") or []):
                    st.caption(f"⚽ {leg.get('home')} × {leg.get('away')} · {leg.get('market')} @ {(_gm_daily_num(leg.get('odd')) or 0):.2f}")
                st.warning("Excluir remove esta dica do histórico público e das métricas que usam o histórico publicado.")
                if st.button("🗑️ Excluir do histórico", use_container_width=True, key=f"gm_admin_delete_daily_history_{hidx}_{day}_{kind}"):
                    try:
                        gm_daily_pick_delete_history(row)
                        st.success("Dica excluída do histórico.")
                        st.rerun()
                    except Exception as exc:
                        st.error("Não foi possível excluir esta dica do histórico.")
                        st.caption(str(exc))



def gm_render_admin_vip_manager():
    st.markdown("### 👥 Gestão de clientes VIP")

    try:
        rows = gm_admin_list_users()
    except Exception as exc:
        st.error("Não foi possível carregar os clientes do painel administrativo.")
        st.caption(f"Detalhe técnico: {type(exc).__name__}")
        return

    suspension_map = gm_admin_pro_suspensions()
    for _row in rows:
        if isinstance(_row, dict):
            _row["pro_suspended"] = bool(suspension_map.get(str(_row.get("id") or ""), False))
    clients = [r for r in rows if r.get("role") == "client"]
    pending = sum(1 for r in clients if r.get("vip_status") == "pending" and not r.get("blocked"))
    active = sum(1 for r in clients if r.get("vip_status") == "active" and not r.get("blocked"))
    expired = sum(1 for r in clients if r.get("vip_status") == "expired" and not r.get("blocked"))
    blocked = sum(1 for r in clients if r.get("blocked") or r.get("vip_status") == "blocked")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Pendentes", pending)
    m2.metric("VIP ativos", active)
    m3.metric("Expirados", expired)
    m4.metric("Bloqueados", blocked)

    if not clients:
        st.info("Nenhum cliente cadastrado ainda.")
        return

    status_filter = st.selectbox(
        "Filtrar clientes",
        ["Todos", "Pendentes", "PRO ativos", "PRO suspensos", "Free / Expirados", "Bloqueados"],
        key="gm_admin_status_filter",
    )
    search = st.text_input("Buscar por nome ou e-mail", key="gm_admin_search").strip().lower()

    def include_row(row):
        if search:
            hay = f"{row.get('nome','')} {row.get('email','')}".lower()
            if search not in hay:
                return False
        blocked_now = bool(row.get("blocked")) or row.get("vip_status") == "blocked"
        status = row.get("vip_status")
        if status_filter == "Pendentes" and not (status == "pending" and not blocked_now):
            return False
        suspended_now = bool(row.get("pro_suspended"))
        if status_filter == "PRO ativos" and not (status == "active" and not blocked_now and not suspended_now):
            return False
        if status_filter == "PRO suspensos" and not (suspended_now and not blocked_now):
            return False
        if status_filter == "Free / Expirados" and not ((status != "active" or suspended_now) and not blocked_now):
            return False
        if status_filter == "Bloqueados" and not blocked_now:
            return False
        return True

    filtered = [r for r in clients if include_row(r)]
    if not filtered:
        st.info("Nenhum cliente corresponde ao filtro selecionado.")
        return

    st.markdown(f"### Clientes ({len(filtered)})")
    for row in filtered:
        uid = str(row.get("id") or "")
        nome = str(row.get("nome") or "Cliente").strip()
        email = str(row.get("email") or "").strip()
        status_label = gm_admin_status_label(row)
        payment = str(row.get("payment_status") or "pending")
        vip_until = gm_admin_format_datetime(row.get("vip_until"))
        created = gm_admin_format_datetime(row.get("created_at"))
        approved = gm_admin_format_datetime(row.get("approved_at"))

        with st.expander(f"{status_label} · {nome} · {email}", expanded=(row.get("vip_status") == "pending")):
            suspended_now = bool(row.get("pro_suspended"))
            blocked_now = bool(row.get("blocked")) or row.get("vip_status") == "blocked"

            # V128: resumo compacto do perfil. As ações menos frequentes ficam
            # agrupadas para reduzir altura e facilitar a leitura no desktop/mobile.
            info1, info2, info3 = st.columns(3)
            info1.caption(f"Plano · {('FREE · suspenso' if suspended_now else status_label)}")
            info2.caption(f"Pagamento · {payment}")
            info3.caption(f"Validade · {vip_until}")
            st.caption(f"Cadastro {created} · Aprovação {approved}")

            days = st.selectbox(
                "Período do acesso",
                [30, 90, 180, 365],
                format_func=lambda d: f"{d} dias",
                key=f"gm_admin_days_{uid}",
            )

            a1, a2, a3 = st.columns(3)
            with a1:
                if row.get("vip_status") == "pending":
                    if st.button("✅ Aprovar", use_container_width=True, key=f"gm_admin_approve_{uid}"):
                        try:
                            gm_admin_rpc("gm_admin_approve_user", {"p_user_id": uid, "p_days": int(days)})
                            st.success("Cliente aprovado com sucesso.")
                            st.rerun()
                        except Exception as exc:
                            st.error("Não foi possível aprovar o cliente.")
                            st.caption(str(exc))
                else:
                    if st.button("➕ Renovar", use_container_width=True, key=f"gm_admin_renew_{uid}"):
                        try:
                            gm_admin_rpc("gm_admin_renew_user", {"p_user_id": uid, "p_days": int(days)})
                            st.success("PRO renovado com sucesso.")
                            st.rerun()
                        except Exception as exc:
                            st.error("Não foi possível renovar o PRO.")
                            st.caption(str(exc))
            with a2:
                pro_label = "▶️ Reativar PRO" if suspended_now else "⏸️ Suspender PRO"
                if st.button(pro_label, use_container_width=True, key=f"gm_admin_pro_toggle_{uid}"):
                    try:
                        gm_admin_set_pro_suspension(uid, not suspended_now)
                        st.success("Acesso PRO reativado." if suspended_now else "Acesso PRO suspenso; a conta agora navega como FREE.")
                        st.rerun()
                    except Exception as exc:
                        st.error("Não foi possível alterar o acesso PRO.")
                        st.caption(str(exc))
            with a3:
                action_label = "🔓 Desbloquear" if blocked_now else "⛔ Bloquear"
                if st.button(action_label, use_container_width=True, key=f"gm_admin_blocktoggle_{uid}"):
                    try:
                        fn = "gm_admin_unblock_user" if blocked_now else "gm_admin_block_user"
                        gm_admin_rpc(fn, {"p_user_id": uid})
                        st.success("Status do cliente atualizado.")
                        st.rerun()
                    except Exception as exc:
                        st.error("Não foi possível alterar o bloqueio.")
                        st.caption(str(exc))

            if suspended_now:
                st.caption("⏸️ PRO suspenso pelo ADM · login e validade original preservados.")

            with st.expander("Mais ações", expanded=False):
                courtesy_days = st.selectbox(
                    "Cortesia",
                    [30, 90, 180],
                    format_func=lambda d: {30: "30 dias · 1 mês", 90: "90 dias · 3 meses", 180: "180 dias · 6 meses"}[d],
                    key=f"gm_admin_courtesy_days_{uid}",
                )
                m1, m2, m3 = st.columns(3)
                with m1:
                    if st.button("🎁 Liberar cortesia", use_container_width=True, key=f"gm_admin_courtesy_{uid}"):
                        try:
                            gm_admin_rpc("gm_admin_grant_courtesy", {"p_user_id": uid, "p_days": int(courtesy_days)})
                            st.success(f"Cortesia de {courtesy_days} dias liberada.")
                            st.rerun()
                        except Exception as exc:
                            st.error("Não foi possível liberar a cortesia.")
                            st.caption(str(exc))
                with m2:
                    if payment != "paid":
                        if st.button("💳 Confirmar pagamento", use_container_width=True, key=f"gm_admin_paid_{uid}"):
                            try:
                                gm_admin_rpc("gm_admin_set_payment", {"p_user_id": uid, "p_status": "paid"})
                                st.success("Pagamento atualizado.")
                                st.rerun()
                            except Exception as exc:
                                st.error("Não foi possível atualizar o pagamento.")
                                st.caption(str(exc))
                    else:
                        st.caption("💳 Pagamento confirmado")
                with m3:
                    if st.button("🔄 Liberar sessão", use_container_width=True, key=f"gm_admin_reset_session_{uid}"):
                        try:
                            gm_admin_rpc("gm_admin_reset_session", {"p_user_id": uid})
                            st.success("Sessão liberada.")
                            st.rerun()
                        except Exception as exc:
                            st.error("Não foi possível liberar a sessão do cliente.")
                            st.caption(str(exc))




# ============================================================
# NAVEGAÇÃO DESKTOP — sidebar deve controlar a mesma view do app
# ============================================================
def gm_desktop_navigate(view):
    """V103: navegação lateral desktop sem interferir na navegação mobile."""
    st.session_state["gm_main_view"] = str(view)
    # Se o ADM estiver na Central, qualquer item normal da sidebar volta ao app.
    st.session_state["gm_admin_panel_open"] = False
    # gm_view é usado por links da navegação mobile. Consumido uma vez, não pode
    # sobrescrever cliques posteriores da sidebar em cada rerun.
    try:
        if "gm_view" in st.query_params:
            del st.query_params["gm_view"]
    except Exception:
        pass


# ============================================================
# APOSTAS DO ADM — feed manual, separado das Dicas do Dia
# ============================================================
def gm_admin_bets_client():
    client = gm_auth_client_from_session()
    if client is None:
        raise RuntimeError("Sessão autenticada indisponível.")
    return client


def gm_admin_bets_list(active_only=False, limit=100):
    # V150: cache curto por sessão; evita SELECT repetido em cada rerun da Home.
    _key = f"{bool(active_only)}:{int(limit)}"
    _now = time.time()
    _all = st.session_state.setdefault("_gm_admin_bets_cache", {})
    _cached = _all.get(_key) if isinstance(_all, dict) else None
    if isinstance(_cached, dict) and (_now - float(_cached.get("at") or 0)) < 30:
        return list(_cached.get("rows") or [])
    client = gm_admin_bets_client()
    query = client.table("gm_admin_bets").select("*").order("created_at", desc=True).limit(int(limit))
    if active_only:
        query = query.eq("is_active", True)
    result = query.execute()
    rows = [row for row in (getattr(result, "data", None) or []) if isinstance(row, dict)]
    _all[_key] = {"at": _now, "rows": rows}
    return rows


def gm_admin_bet_is_current(row):
    if not bool((row or {}).get("is_active")):
        return False
    valid_until = (row or {}).get("valid_until")
    if not valid_until:
        return True
    try:
        expiry = pd.to_datetime(valid_until, utc=True, errors="coerce")
        if pd.isna(expiry):
            return True
        return expiry > pd.Timestamp.now(tz="UTC")
    except Exception:
        return True


def gm_admin_bet_format_time(value):
    return gm_news_format_datetime(value)


def gm_admin_bet_publish(title, description, odd, bet_url, valid_until=None):
    client = gm_admin_bets_client()
    payload = {
        "title": str(title).strip(),
        "description": str(description).strip(),
        "odd": float(odd),
        "bet_url": str(bet_url).strip(),
        "is_active": True,
    }
    if valid_until:
        payload["valid_until"] = valid_until
    result = client.table("gm_admin_bets").insert(payload).execute()
    return getattr(result, "data", None)


def gm_admin_bet_update(bet_id, payload):
    client = gm_admin_bets_client()
    data = dict(payload or {})
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    result = client.table("gm_admin_bets").update(data).eq("id", str(bet_id)).execute()
    return getattr(result, "data", None)


def gm_admin_bet_upload_result_image(bet_id, uploaded_file):
    """Envia comprovante HD opcional para o bucket público gm-admin-bet-results."""
    if uploaded_file is None:
        return None
    client = gm_admin_bets_client()
    raw = uploaded_file.getvalue()
    if not raw:
        return None
    if len(raw) > 12 * 1024 * 1024:
        raise ValueError("A imagem deve ter no máximo 12 MB.")
    content_type = str(getattr(uploaded_file, "type", "") or "image/jpeg")
    allowed = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
    if content_type not in allowed:
        raise ValueError("Use uma imagem JPG, PNG ou WEBP.")
    ext = allowed[content_type]
    path = f"{str(bet_id)}/{int(time.time())}-{secrets.token_hex(4)}.{ext}"
    bucket = client.storage.from_("gm-admin-bet-results")
    bucket.upload(path, raw, {"content-type": content_type, "upsert": "false"})
    public = bucket.get_public_url(path)
    if isinstance(public, dict):
        public = public.get("publicUrl") or public.get("public_url") or public.get("data", {}).get("publicUrl")
    return str(public or "").strip()


def gm_admin_bet_result_label(status):
    return {"green": "✅ GREEN", "red": "❌ RED", "void": "↩️ ANULADA", "pending": "⏳ Pendente"}.get(str(status or "pending"), "⏳ Pendente")


def gm_admin_bet_delete(bet_id):
    client = gm_admin_bets_client()
    result = client.table("gm_admin_bets").delete().eq("id", str(bet_id)).execute()
    return getattr(result, "data", None)


def gm_render_admin_bets_manager():
    st.markdown("### ⭐ Apostas do ADM")
    st.caption("Publicações manuais independentes das Dicas do Dia. Não entram no desempenho nem no aprendizado estatístico do GM SCORE.")

    with st.expander("➕ Publicar Aposta do ADM", expanded=True):
        with st.form("gm_admin_bet_publish_form", clear_on_submit=True):
            title = st.text_input("Título curto", max_chars=120, placeholder="Ex.: Especial da tarde")
            description = st.text_area("Aposta / descrição", max_chars=600, height=100, placeholder="Ex.: Mais de 1.5 gols no jogo...")
            odd = st.number_input("Odd", min_value=1.01, max_value=1000.0, value=1.50, step=0.01, format="%.2f")
            bet_url = st.text_input("🔗 Link direto da aposta", placeholder="https://...")
            use_expiry = st.checkbox("Definir horário limite")
            expiry_date = st.date_input("Data limite", value=datetime.now(BRASILIA_TZ).date(), disabled=not use_expiry)
            expiry_time = st.time_input("Horário limite", value=datetime.now(BRASILIA_TZ).replace(hour=23, minute=59, second=0, microsecond=0).time(), disabled=not use_expiry)
            submitted = st.form_submit_button("⭐ Publicar Aposta do ADM", type="primary", use_container_width=True)
        if submitted:
            clean_url = str(bet_url or "").strip()
            if not str(title or "").strip() or not str(description or "").strip():
                st.warning("Informe título e descrição da aposta.")
            elif not clean_url.lower().startswith("https://"):
                st.warning("Informe um link HTTPS válido para a aposta.")
            else:
                valid_until = None
                if use_expiry:
                    local_dt = datetime.combine(expiry_date, expiry_time).replace(tzinfo=BRASILIA_TZ)
                    if local_dt <= datetime.now(BRASILIA_TZ):
                        st.warning("O horário limite precisa estar no futuro.")
                        st.stop()
                    valid_until = local_dt.astimezone(timezone.utc).isoformat()
                try:
                    gm_admin_bet_publish(title, description, odd, clean_url, valid_until)
                    news_ok = True
                    try:
                        gm_publish_system_news(
                            "⭐ Nova Aposta do ADM disponível",
                            f"{str(title).strip()} · Odd {float(odd):.2f}. Confira a publicação na página Início.",
                            category="novidade",
                            featured=True,
                            push_title="⭐ Nova Aposta do ADM disponível",
                            push_message="Há uma nova publicação no GM SCORE. Abra o app para conferir.",
                        )
                    except Exception:
                        news_ok = False
                    st.success("Aposta do ADM publicada com sucesso.")
                    if not news_ok:
                        st.warning("A aposta foi publicada, mas a Novidade automática não pôde ser criada. Você pode publicá-la manualmente na aba Novidades.")
                    st.rerun()
                except Exception as exc:
                    st.error("Não foi possível publicar a Aposta do ADM.")
                    st.caption(str(exc))

    try:
        rows = gm_admin_bets_list(active_only=False, limit=100)
    except Exception as exc:
        st.error("Não foi possível carregar as Apostas do ADM.")
        st.caption(str(exc))
        return
    if not rows:
        st.info("Nenhuma Aposta do ADM publicada ainda.")
        return

    st.markdown("#### Publicações")
    for row in rows:
        bet_id = str(row.get("id") or "")
        active = gm_admin_bet_is_current(row)
        raw_active = bool(row.get("is_active"))
        status = "🟢 Ativa" if active else ("⏱ Encerrada" if raw_active else "⚪ Inativa")
        title_now = str(row.get("title") or "Aposta do ADM")
        with st.expander(f"{status} · {title_now} · Odd {float(row.get('odd') or 0):.2f}", expanded=False):
            st.write(str(row.get("description") or ""))
            st.caption(f"Publicada em {gm_admin_bet_format_time(row.get('created_at'))}" + (f" • Limite: {gm_admin_bet_format_time(row.get('valid_until'))}" if row.get("valid_until") else ""))
            current_result = str(row.get("result_status") or "pending")
            st.markdown(f"**Resultado:** {gm_admin_bet_result_label(current_result)}")
            result_choice = st.selectbox(
                "Finalizar como",
                options=["pending", "green", "red", "void"],
                format_func=lambda x: gm_admin_bet_result_label(x),
                index=["pending", "green", "red", "void"].index(current_result) if current_result in {"pending", "green", "red", "void"} else 0,
                key=f"gm_admin_bet_result_{bet_id}",
            )
            result_image = st.file_uploader(
                "📷 Comprovante do resultado (opcional · HD)",
                type=["jpg", "jpeg", "png", "webp"],
                accept_multiple_files=False,
                key=f"gm_admin_bet_result_image_{bet_id}",
                help="Opcional. JPG, PNG ou WEBP, até 12 MB.",
            )
            if row.get("result_image_url"):
                st.image(str(row.get("result_image_url")), caption="Comprovante publicado", use_container_width=True)
            if st.button("💾 Salvar resultado", use_container_width=True, type="primary", key=f"gm_admin_bet_save_result_{bet_id}"):
                try:
                    image_url = str(row.get("result_image_url") or "").strip() or None
                    if result_image is not None:
                        image_url = gm_admin_bet_upload_result_image(bet_id, result_image)
                    payload = {
                        "result_status": result_choice,
                        "result_image_url": image_url,
                        "settled_at": datetime.now(timezone.utc).isoformat() if result_choice != "pending" else None,
                    }
                    # Uma publicação finalizada deixa o feed ativo e passa ao histórico.
                    if result_choice != "pending":
                        payload["is_active"] = False
                    gm_admin_bet_update(bet_id, payload)
                    st.success("Resultado salvo no histórico.")
                    st.rerun()
                except Exception as exc:
                    st.error("Não foi possível salvar o resultado. Confirme se a migração V103 foi aplicada no Supabase.")
                    st.caption(str(exc))
            c1, c2 = st.columns(2)
            with c1:
                if st.button("⏸ Encerrar" if raw_active else "▶️ Reativar", use_container_width=True, key=f"gm_admin_bet_toggle_{bet_id}"):
                    try:
                        gm_admin_bet_update(bet_id, {"is_active": not raw_active})
                        st.rerun()
                    except Exception as exc:
                        st.error("Não foi possível alterar a publicação."); st.caption(str(exc))
            with c2:
                confirm = st.checkbox("Confirmar exclusão", key=f"gm_admin_bet_delete_confirm_{bet_id}")
                if st.button("🗑 Excluir", use_container_width=True, disabled=not confirm, key=f"gm_admin_bet_delete_{bet_id}"):
                    try:
                        gm_admin_bet_delete(bet_id); st.rerun()
                    except Exception as exc:
                        st.error("Não foi possível excluir a publicação."); st.caption(str(exc))


def gm_render_admin_bets_home():
    # Card compacto na Home; ADM e VIP veem a mesma apresentação.
    try:
        active_rows = [r for r in gm_admin_bets_list(active_only=True, limit=30) if gm_admin_bet_is_current(r)]
        all_visible_rows = gm_admin_bets_list(active_only=False, limit=100)
    except Exception:
        return

    # V106: o histórico é independente do feed ativo. Uma aposta finalizada fica
    # is_active=False, mas continua visível aos clientes pela política RLS V106.
    history_rows = [r for r in all_visible_rows if str(r.get("result_status") or "pending") in {"green", "red", "void"}]
    try:
        _profile = gm_auth_get_profile()
    except Exception:
        _profile = None
    _is_pro = gm_is_pro(_profile)

    st.markdown("### ⭐ Apostas do ADM")
    if not active_rows:
        st.caption("Ainda não temos Apostas do ADM para hoje.")
    elif not _is_pro:
        gm_render_pro_lock("Apostas do ADM disponíveis", "As seleções atuais do ADM são exclusivas do GM SCORE Pro.", key="gm_home_adm_pro")

    if active_rows and _is_pro:
        show_all = bool(st.session_state.get("gm_admin_bets_show_all"))
        visible = active_rows if show_all else active_rows[:3]
        st.caption("Seleções manuais publicadas pela administração do GM SCORE.")
        st.markdown('''<style>
        .gm-adm-bet-card{background:linear-gradient(145deg,rgba(13,24,23,.97),rgba(9,15,20,.98));border:1px solid rgba(52,230,129,.27);border-left:3px solid #34e681;border-radius:14px;padding:11px 12px 9px;margin:.42rem 0 .18rem}
        .gm-adm-bet-top{display:flex;align-items:center;justify-content:space-between;gap:8px}.gm-adm-bet-title{font-weight:900;color:#f8fafc;font-size:.94rem;line-height:1.2}.gm-adm-bet-odd{white-space:nowrap;color:#34e681;font-weight:950;font-size:.86rem;border:1px solid rgba(52,230,129,.28);border-radius:999px;padding:3px 8px;background:rgba(52,230,129,.08)}
        .gm-adm-bet-desc{color:#cbd5df;font-size:.80rem;line-height:1.35;margin-top:5px}.gm-adm-bet-meta{color:#7f8997;font-size:.67rem;margin-top:7px}.gm-adm-bet-note{color:#7f8997;font-size:.64rem;margin-top:4px}
        div[class*="st-key-gm_adm_bet_link_"] a{min-height:2.25rem!important;border-radius:10px!important;border:1px solid rgba(52,230,129,.45)!important;background:rgba(52,230,129,.08)!important;color:#eafff3!important;font-size:.79rem!important;font-weight:850!important}
        </style>''', unsafe_allow_html=True)
        for row in visible:
            bet_id = str(row.get("id") or "")
            try: odd_text = f"{float(row.get('odd') or 0):.2f}"
            except Exception: odd_text = str(row.get("odd") or "—")
            expiry = f" • até {gm_admin_bet_format_time(row.get('valid_until'))}" if row.get("valid_until") else ""
            st.markdown(f'''<div class="gm-adm-bet-card"><div class="gm-adm-bet-top"><div class="gm-adm-bet-title">⭐ {html.escape(str(row.get('title') or 'Aposta do ADM'))}</div><div class="gm-adm-bet-odd">ODD {html.escape(odd_text)}</div></div><div class="gm-adm-bet-desc">{html.escape(str(row.get('description') or ''))}</div><div class="gm-adm-bet-meta">Publicada {html.escape(gm_admin_bet_format_time(row.get('created_at')))}{html.escape(expiry)}</div><div class="gm-adm-bet-note">Seleção manual do administrador · não integra o histórico estatístico das Dicas do Dia.</div></div>''', unsafe_allow_html=True)
            url = str(row.get("bet_url") or "").strip()
            if url.lower().startswith("https://"):
                st.link_button("🎯 Ir para a aposta · GM SCORE  ›", url, use_container_width=True, key=f"gm_adm_bet_link_{bet_id}")
        if len(active_rows) > 3:
            label = "Mostrar somente as mais recentes" if show_all else f"Ver todas ({len(active_rows)})"
            if st.button(label, use_container_width=True, key="gm_admin_bets_show_all_btn"):
                st.session_state["gm_admin_bets_show_all"] = not show_all; st.rerun()

    # Botão discreto sempre que houver ao menos um resultado publicado, inclusive
    # quando não existir nenhuma aposta ativa no momento.
    if history_rows:
        if st.button("Ver resultados ›", use_container_width=False, key="gm_admin_bets_results_page_btn"):
            st.session_state["gm_main_view"] = "admin_results"
            st.rerun()


def gm_render_admin_bets_results_page():
    """V104: página exclusiva, para clientes e ADM, com resultados publicados manualmente."""
    st.markdown("## 📋 Resultados do ADM")
    st.caption("Histórico das Apostas do ADM que já foram finalizadas e publicadas.")
    if st.button("‹ Voltar ao Início", use_container_width=False, key="gm_admin_results_back_home"):
        st.session_state["gm_main_view"] = "analysis"
        st.rerun()
    try:
        rows = [r for r in gm_admin_bets_list(active_only=False, limit=100) if str(r.get("result_status") or "pending") in {"green", "red", "void"}]
    except Exception:
        rows = []
    if not rows:
        st.info("Ainda não há resultados publicados pelo ADM.")
        return
    for row in rows:
        title = html.escape(str(row.get("title") or "Aposta do ADM"))
        try:
            odd_text = f"{float(row.get('odd') or 0):.2f}"
        except Exception:
            odd_text = "—"
        status = gm_admin_bet_result_label(row.get("result_status"))
        st.markdown(f"### {status} · {title}")
        st.markdown(f"**ODD {html.escape(odd_text)}**")
        if row.get("description"):
            st.write(str(row.get("description")))
        st.caption("Finalizada " + gm_admin_bet_format_time(row.get("settled_at") or row.get("updated_at") or row.get("created_at")))
        if row.get("result_image_url"):
            st.image(str(row.get("result_image_url")), caption="Comprovante do resultado", use_container_width=True)
        st.markdown("---")


def gm_render_admin_panel(profile):
    """Painel administrativo organizado por função. Validações continuam no Supabase."""
    if not profile or profile.get("role") != "admin":
        st.error("Acesso administrativo não autorizado.")
        return

    # V141: Central compacta; ferramentas administrativas ficam somente aqui.
    # V140: st.tabs executa o conteúdo de TODAS as abas em cada rerun, inclusive
    # diagnósticos pesados do sistema. O seletor abaixo mantém a mesma separação
    # administrativa, mas executa somente a seção escolhida.
    _admin_sections = [
        "⚽ Jogos / Sistema",
        "💡 Dicas do Dia",
        "⭐ Apostas do ADM",
        "📰 Novidades",
        "⭐ Avaliações",
        "👥 Clientes / VIP",
    ]
    _admin_section = st.radio(
        "Área administrativa",
        _admin_sections,
        horizontal=True,
        key="gm_admin_active_section",
        label_visibility="collapsed",
    )
    if _admin_section == "⚽ Jogos / Sistema":
        with st.expander("🧭 Verificação da versão carregada", expanded=False):
            try:
                _gm_runtime_file = os.path.basename(os.path.abspath(__file__))
            except Exception:
                _gm_runtime_file = "app.py"
            try:
                _gm_runtime_mtime = datetime.fromtimestamp(os.path.getmtime(__file__), tz=timezone.utc).astimezone(ZoneInfo("America/Sao_Paulo"))
                _gm_runtime_mtime_txt = _gm_runtime_mtime.strftime("%d/%m/%Y %H:%M:%S")
            except Exception:
                _gm_runtime_mtime_txt = "indisponível"
            st.markdown(f"**Build carregada:** `{GM_BUILD}`")
            st.caption(f"Arquivo em execução: `{_gm_runtime_file}` • modificado: {_gm_runtime_mtime_txt} (Brasília)")
        gm_render_auth_test_console()
        st.markdown("### ⚽ Jogos e operação do sistema")
        st.caption("Controles administrativos de atualização e diagnóstico. A experiência normal de Jogos permanece idêntica à do cliente VIP.")
        gm_admin_fixture_date = st.date_input(
            "Data para reconstruir",
            value=datetime.now(BRASILIA_TZ).date(),
            key="gm_admin_fixture_rebuild_date",
            help="A atualização reconstrói somente a data escolhida e reaplica todas as validações da agenda.",
        )
        c1, c2 = st.columns(2)
        with c1:
            if st.button("🔄 Atualizar partidas", use_container_width=True, type="primary", key="gm_admin_games_refresh"):
                with st.spinner("Reconstruindo a agenda e validando as partidas..."):
                    for _fn_name in (
                        "load_apifootball_prediction_fixtures_for_date",
                        "load_apifootball_competition_fixtures_for_date",
                        "load_apifootball_all_competitions_fixtures_for_date",
                        "load_apifootball_fixtures_for_date",
                        "load_sofascore_fixtures_for_date",
                        "load_espn_fixtures_for_date",
                        "load_thesportsdb_fixtures_for_date",
                        "load_fixtures_for_date",
                        "gm_games_prepared_fixtures",
                    ):
                        _fn = globals().get(_fn_name)
                        try:
                            if _fn is not None and hasattr(_fn, "clear"):
                                _fn.clear()
                        except Exception:
                            pass
                    try:
                        _official = load_apifootball_all_competitions_fixtures_for_date(gm_admin_fixture_date) or []
                    except Exception:
                        _official = []
                    try:
                        _aggregated = load_fixtures_for_date(gm_admin_fixture_date) or []
                    except Exception:
                        _aggregated = []
                    try:
                        _final = gm_games_prepared_fixtures(gm_admin_fixture_date) or []
                    except Exception:
                        _final = []
                    _wrong_date = sum(1 for _f in _aggregated if not fixture_matches_selected_date(_f, gm_admin_fixture_date))
                    _invalid = sum(1 for _f in _aggregated if not valid_daily_fixture(_f))
                    _roster_rejected = sum(1 for _f in _aggregated if valid_daily_fixture(_f) and fixture_matches_selected_date(_f, gm_admin_fixture_date) and not gm_fixture_matches_official_league_roster(_f))
                    _competitions = len({str((_f or {}).get("competition") or "") for _f in _final if (_f or {}).get("competition")})
                    st.session_state["gm_admin_fixture_last_report"] = {
                        "date": gm_admin_fixture_date.isoformat(),
                        "official": len(_official),
                        "aggregated": len(_aggregated),
                        "final": len(_final),
                        "wrong_date": _wrong_date,
                        "invalid": _invalid,
                        "roster_rejected": _roster_rejected,
                        "competitions": _competitions,
                        "updated_at": datetime.now(BRASILIA_TZ).strftime("%d/%m/%Y %H:%M:%S"),
                    }
                st.success(f"Agenda de {gm_admin_fixture_date.strftime('%d/%m/%Y')} reconstruída e validada.")
        with c2:
            if st.button("🔄 Sincronizar resultados", use_container_width=True, key="gm_admin_results_sync"):
                try:
                    _sync = gm_calibration_auto_settle(limit=30, force=True)
                    st.success(f"Sincronização concluída: {_sync.get('settled', 0)} resultado(s) atualizado(s).")
                except Exception as exc:
                    st.warning("Não foi possível concluir a sincronização agora.")
                    st.caption(f"Detalhe: {type(exc).__name__}")
        _fixture_report = st.session_state.get("gm_admin_fixture_last_report")
        if isinstance(_fixture_report, dict):
            st.markdown("#### Relatório da última atualização")
            r1, r2, r3, r4 = st.columns(4)
            r1.metric("APIfootball oficial", int(_fixture_report.get("official", 0)))
            r2.metric("Registros agregados", int(_fixture_report.get("aggregated", 0)))
            r3.metric("Agenda válida", int(_fixture_report.get("final", 0)))
            r4.metric("Competições", int(_fixture_report.get("competitions", 0)))
            _discarded = int(_fixture_report.get("wrong_date", 0)) + int(_fixture_report.get("invalid", 0)) + int(_fixture_report.get("roster_rejected", 0))
            st.caption(
                f"Validação: {_fixture_report.get('wrong_date', 0)} fora da data · "
                f"{_fixture_report.get('invalid', 0)} inválidos · "
                f"{_fixture_report.get('roster_rejected', 0)} incompatíveis com a competição · "
                f"{_discarded} descarte(s) detectado(s) · atualizado em {_fixture_report.get('updated_at', '—')}."
            )
            if int(_fixture_report.get("official", 0)) == 0 and int(_fixture_report.get("final", 0)) > 0:
                st.warning("A fonte oficial não retornou partidas nesta atualização. A agenda exibida dependeu das fontes complementares e merece conferência.")
            elif int(_fixture_report.get("final", 0)) == 0:
                st.info("Nenhuma partida válida permaneceu para a data após as validações.")
            else:
                st.success("A agenda final foi reconstruída com as barreiras de data, competição, identidade e deduplicação ativas.")

        st.markdown("#### Diagnóstico das competições")
        gm_render_apifootball_league_audit()
        gm_render_apifootball_stat_audit()
        gm_render_calibration_dashboard()
    elif _admin_section == "💡 Dicas do Dia":
        gm_render_admin_daily_pick_approval()
    elif _admin_section == "⭐ Apostas do ADM":
        gm_render_admin_bets_manager()
    elif _admin_section == "📰 Novidades":
        gm_render_admin_news_manager()
    elif _admin_section == "⭐ Avaliações":
        gm_render_admin_reviews_manager()
    elif _admin_section == "👥 Clientes / VIP":
        gm_render_admin_vip_manager()

    st.markdown("---")
    if st.button("← Voltar", use_container_width=True, key="gm_admin_back_bottom", help="Retornar ao GM SCORE"):
        st.session_state["gm_admin_panel_open"] = False
        st.rerun()



def gm_render_auth_test_console():
    """Console invisível no uso normal. Abra o app com ?auth_test=1 para testar."""
    if not GM_AUTH_TEST_MODE:
        return
    try:
        enabled = str(st.query_params.get("auth_test", "0")).lower() in {"1", "true", "sim", "yes"}
    except Exception:
        enabled = False
    if not enabled:
        return

    with st.expander("🔐 Diagnóstico de autenticação — ETAPA 12", expanded=True):
        st.caption("Modo técnico de teste. O conteúdo atual do GM SCORE continua liberado.")
        if not gm_supabase_is_configured():
            st.error("Secrets do Supabase não foram encontrados ou estão incompletos.")
            return
        url, _ = gm_supabase_config()
        ok, detail = gm_supabase_public_probe(url)
        if ok:
            st.success("Conexão Streamlit ↔ Supabase funcionando.")
        else:
            st.error(f"Não foi possível validar a conexão: {detail}")
        if create_client is None:
            st.error("Pacote supabase ainda não foi carregado no ambiente.")
        else:
            st.caption("Cliente Supabase carregado. Nenhuma Secret/Service Role Key é usada pelo app.")

        if st.session_state.get("gm_auth_user_id"):
            profile = None
            try:
                profile = gm_auth_get_profile(force=True)
            except Exception as exc:
                st.warning(f"Sessão encontrada, mas o perfil não pôde ser consultado ({type(exc).__name__}).")
            if profile:
                state = gm_auth_access_state(profile)
                labels = {
                    "admin": "🛠️ Administrador",
                    "vip": "⭐ VIP ativo",
                    "pending": "⏳ Aguardando aprovação",
                    "expired": "⌛ VIP expirado",
                    "blocked": "⛔ Bloqueado",
                    "anonymous": "👤 Não autenticado",
                }
                st.success(f"Login realizado com sucesso · {labels.get(state, state)}")
                st.write(f"**Perfil:** `{profile.get('role', 'client')}`")
                st.write(f"**VIP:** `{profile.get('vip_status', 'pending')}`")
                st.write(f"**Pagamento:** `{profile.get('payment_status', 'pending')}`")
                st.write(f"**Bloqueado:** `{'sim' if profile.get('blocked') else 'não'}`")
                full_access = state in {"admin", "vip"}
                st.write(f"**Acesso completo GM SCORE:** {'✅ Sim' if full_access else '❌ Não'}")
            else:
                st.warning("O login existe, mas não foi encontrado um perfil correspondente em gm_users.")
            if st.button("🚪 Sair da sessão de teste", key="gm_auth_test_logout", use_container_width=True):
                gm_auth_sign_out()
                st.rerun()
        else:
            st.markdown("#### 🔐 Testar login GM SCORE")
            st.caption("Use primeiro o cliente de teste criado no Supabase. Não envie a senha por mensagem.")
            with st.form("gm_auth_test_login_form", clear_on_submit=False):
                email = st.text_input("E-mail", key="gm_auth_test_email", autocomplete="email")
                password = st.text_input("Senha", type="password", key="gm_auth_test_password", autocomplete="current-password")
                submitted = st.form_submit_button("Entrar", use_container_width=True)
            if submitted:
                if not str(email).strip() or not str(password):
                    st.warning("Informe e-mail e senha.")
                else:
                    try:
                        gm_auth_sign_in(email, password)
                        profile = gm_auth_get_profile(force=True)
                        if not profile:
                            gm_auth_sign_out()
                            st.error("Login autenticado, mas o perfil gm_users não foi encontrado. A sessão foi encerrada por segurança.")
                        else:
                            st.rerun()
                    except Exception as exc:
                        # Diagnóstico seguro: mostra categoria/status, nunca senha, token ou chave.
                        raw = str(exc)
                        msg = raw.lower()
                        if raw.startswith("GM_AUTH_HTTP_"):
                            parts = raw.split("|", 2)
                            status = parts[0].replace("GM_AUTH_HTTP_", "")
                            code = parts[1] if len(parts) > 1 else ""
                            detail = parts[2] if len(parts) > 2 else ""
                            detail_l = detail.lower()
                            if "invalid login credentials" in detail_l or "invalid_credentials" in code.lower():
                                st.error("O Supabase recusou as credenciais deste usuário.")
                                st.caption(f"Diagnóstico Auth: HTTP {status} · `{code or 'credenciais recusadas'}`. Se o painel mostra Last signed in, redefina a senha deste usuário de teste e tente novamente.")
                            elif "email not confirmed" in detail_l or "email_not_confirmed" in code.lower():
                                st.error("O Supabase informou que este e-mail ainda não foi confirmado.")
                                st.caption(f"Diagnóstico Auth: HTTP {status} · `{code or 'email_not_confirmed'}`")
                            elif status in {"401", "403"}:
                                st.error("O projeto respondeu, mas recusou a autenticação.")
                                st.caption(f"Diagnóstico Auth: HTTP {status} · `{code or 'auth_rejected'}`")
                            else:
                                st.error("O Supabase respondeu ao login, mas não criou a sessão.")
                                st.caption(f"Diagnóstico Auth: HTTP {status} · `{code or 'auth_error'}`")
                        elif "email not confirmed" in msg:
                            st.error("Este e-mail ainda não foi confirmado.")
                        else:
                            st.error("Falha técnica durante o login.")
                            st.caption(f"Diagnóstico local: `{type(exc).__name__}`")

# Guia de instalação: mantém o app intacto e ensina o cliente a criar um
# atalho do GM SCORE na tela inicial do iPhone/iPad ou Android.
@st.dialog("📲 GM SCORE no seu celular")
def render_install_guide():
    st.markdown(
        "Adicione o **GM SCORE à tela inicial** para abrir o site diretamente pelo ícone, "
        "como um aplicativo. Não é necessário baixar nada pela App Store ou Google Play."
    )
    ios_tab, android_tab = st.tabs(["🍎 iPhone / iPad", "🤖 Android"])
    with ios_tab:
        st.markdown(
            """
**Pelo Safari:**

1. Abra o GM SCORE no **Safari**.
2. Toque no botão **Compartilhar** (quadrado com uma seta para cima).
3. Role as opções e toque em **Adicionar à Tela de Início**.
4. Confirme o nome **GM SCORE** e toque em **Adicionar**.
5. O ícone ficará na Tela de Início. Nas próximas vezes, toque nele para abrir o GM SCORE.

> Se a opção não aparecer, confirme que o endereço foi aberto no Safari e procure **Editar Ações** no menu de compartilhamento.
"""
        )
    with android_tab:
        st.markdown(
            """
**Pelo Google Chrome:**

1. Abra o GM SCORE no **Chrome**.
2. Toque no menu **⋮** no canto superior direito.
3. Toque em **Adicionar à tela inicial** ou **Instalar app**, conforme a opção exibida no aparelho.
4. Confirme **GM SCORE** e toque em **Adicionar / Instalar**.
5. O ícone ficará na tela inicial do celular para acesso rápido.

> Os nomes das opções podem variar um pouco conforme a versão do Android e do Chrome.
"""
        )
    st.caption("💡 O acesso continua usando a versão mais recente do GM SCORE publicada na internet; não é preciso reinstalar quando o site for atualizado.")
    if st.button("Fechar", use_container_width=True, key="gm_install_guide_close"):
        st.rerun()


# ============================================================
# PORTAL PÚBLICO / VIP GM SCORE — ETAPA 13
# ============================================================
def gm_auth_test_enabled():
    try:
        return str(st.query_params.get("auth_test", "0")).lower() in {"1", "true", "sim", "yes"}
    except Exception:
        return False


# Catálogo visual dos planos VIP. Os preços efetivos são definidos no PostgreSQL
# por gm_create_order(); o Streamlit envia somente plano + forma de pagamento.
GM_VIP_PLANS = [
    {
        "key": "mensal", "plan_code": "vip_30", "title": "1 mês", "badge": "🔥 PREÇO PROMOCIONAL",
        "pix_price": "R$ 15,15", "monthly": "R$ 15,15/mês",
        "saving": "Plano de entrada", "card_price": "", "card_text": "",
    },
    {
        "key": "trimestral", "plan_code": "vip_90", "title": "3 meses", "badge": "🔥 PREÇO PROMOCIONAL",
        "pix_price": "R$ 36,36", "monthly": "R$ 12,12/mês no Pix",
        "saving": "Economize R$ 9,09 (20%) no Pix", "card_price": "R$ 41,54",
        "card_text": "Cartão em até 2x • total R$ 41,54",
    },
    {
        "key": "semestral", "plan_code": "vip_180", "title": "6 meses", "badge": "⭐ MELHOR CUSTO-BENEFÍCIO",
        "pix_price": "R$ 60,60", "monthly": "R$ 10,10/mês no Pix",
        "saving": "Economize R$ 30,30 (33,3%) no Pix", "card_price": "R$ 70,24",
        "card_text": "Cartão em até 3x • total R$ 70,24",
    },
]


def gm_create_checkout(plan_code, payment_method):
    """Cria um checkout individual no backend. Preço e dias nunca vêm do navegador."""
    access_token = str(st.session_state.get("gm_auth_access_token") or "").strip()
    if not access_token:
        raise RuntimeError("LOGIN_REQUIRED")
    url, anon_key = gm_supabase_config()
    if not url or not anon_key:
        raise RuntimeError("SUPABASE_NOT_CONFIGURED")

    response = requests.post(
        f"{url}/functions/v1/gm-create-checkout",
        headers={
            "apikey": anon_key,
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json={"plan_code": plan_code, "payment_method": payment_method},
        timeout=25,
    )
    try:
        payload = response.json()
    except Exception:
        payload = {}
    if response.status_code != 200 or not payload.get("ok"):
        error = str(payload.get("error") or f"HTTP_{response.status_code}")
        message = str(payload.get("message") or "").strip()
        raise RuntimeError(f"{error}|{message}")
    checkout_url = str(payload.get("checkout_url") or "").strip()
    if not checkout_url.startswith("https://"):
        raise RuntimeError("INVALID_CHECKOUT_URL")
    return payload


def gm_checkout_button(plan, payment_method, label, primary=False):
    """Botão que cria pedido único e, em seguida, oferece o Checkout Pro oficial."""
    key = f"gm_checkout_{plan['plan_code']}_{payment_method}"
    checkout_state_key = f"{key}_result"

    if st.button(label, key=key, type="primary" if primary else "secondary", use_container_width=True):
        try:
            with st.spinner("Criando checkout seguro no Mercado Pago..."):
                checkout = gm_create_checkout(plan["plan_code"], payment_method)
            st.session_state[checkout_state_key] = checkout
        except Exception as exc:
            raw = str(exc)
            low = raw.lower()
            if "login_required" in low or "not_authenticated" in low or "invalid_session" in low:
                st.warning("Entre na sua conta GM SCORE para gerar o pagamento.")
            elif "mercadopago_not_configured" in low:
                st.warning("O checkout automático do Mercado Pago ainda está sendo finalizado. Tente novamente em breve ou fale com o suporte.")
            elif "monthly_plan_pix_only" in low:
                st.warning("O plano de 1 mês está disponível somente no Pix.")
            else:
                st.error("Não foi possível criar o checkout agora. Nenhuma liberação VIP foi realizada.")

    checkout = st.session_state.get(checkout_state_key)
    if isinstance(checkout, dict) and checkout.get("checkout_url"):
        amount = checkout.get("amount")
        try:
            amount_text = f"R$ {float(amount):.2f}".replace(".", ",")
        except Exception:
            amount_text = "valor confirmado no checkout"
        st.success(f"Checkout criado com segurança • {amount_text}")
        st.link_button(
            "↗️ Abrir Mercado Pago",
            checkout["checkout_url"],
            type="primary",
            use_container_width=True,
        )


def gm_render_payment_plans(title="🔥 Planos VIP — preços promocionais", compact=False):
    """Exibe o catálogo; usuários logados geram um Checkout Pro individual por pedido."""
    st.markdown(f"### {title}")
    logged_in = bool(st.session_state.get("gm_auth_access_token"))
    if logged_in:
        st.caption("Escolha o plano e a forma de pagamento. O GM SCORE cria um pedido individual e abre o ambiente seguro do Mercado Pago.")
    else:
        st.caption("Confira os planos. Para pagar, crie sua conta ou entre no GM SCORE; o checkout é individual e vinculado ao seu cadastro.")

    cols = st.columns(3)
    for col, plan in zip(cols, GM_VIP_PLANS):
        with col:
            st.markdown(f"**{plan['badge']}**")
            st.markdown(f"#### ⭐ VIP {plan['title']}")
            st.markdown(f"### {plan['pix_price']} no Pix")
            st.caption(plan['monthly'])
            if plan['key'] != 'mensal':
                st.success(plan['saving'])
            else:
                st.info(plan['saving'])

            if logged_in:
                gm_checkout_button(plan, "pix", "⚡ Pagar com Pix", primary=True)
                if plan.get("card_price"):
                    gm_checkout_button(plan, "card", "💳 Pagar com cartão")
                    st.caption(plan['card_text'])
                else:
                    st.caption("Pagamento mensal: somente Pix.")
            else:
                st.button("🔐 Entre para pagar", key=f"gm_login_needed_{plan['plan_code']}", disabled=True, use_container_width=True)
                if plan.get("card_price"):
                    st.caption(plan['card_text'])
                else:
                    st.caption("Pagamento mensal: somente Pix.")

    if not compact:
        if logged_in:
            st.caption("🔐 O valor e a validade são definidos no servidor. A ativação automática ocorre somente após confirmação válida do pagamento pelo Mercado Pago.")
        else:
            st.caption("🔐 Nenhum pagamento é iniciado sem uma conta autenticada no GM SCORE.")


def gm_payment_url():
    """Compatibilidade legada: pagamentos novos usam gm-create-checkout."""
    return ""


GM_PUBLIC_COMPETITIONS = [
    "🏴󠁧󠁢󠁥󠁮󠁧󠁿 Inglaterra - Premier League",
    "🇪🇸 Espanha - La Liga",
    "🇮🇹 Itália - Serie A",
    "🇩🇪 Alemanha - Bundesliga",
    "🇫🇷 França - Ligue 1",
    "🇵🇹 Portugal - Liga Portugal",
    "🇳🇱 Holanda - Eredivisie",
    "🏴󠁧󠁢󠁳󠁣󠁴󠁿 Escócia - Premiership",
    "🇹🇷 Turquia - Süper Lig",
    "🇧🇷 Brasil - Série A",
    "🇧🇷 Brasil - Série B",
    "🇸🇦 Arábia Saudita - Saudi Pro League",
    "🇺🇸 Estados Unidos - MLS",
    "🇦🇷 Argentina - Liga Profesional",
    "🇲🇽 México - Liga MX",
    "🇨🇴 Colômbia - Primera A",
    "🏆 CONMEBOL Libertadores",
    "🏆 CONMEBOL Sul-Americana",
    "🏆 UEFA Champions League",
    "🏆 UEFA Europa League",
    "🏆 UEFA Conference League",
]


def _gm_safe_html(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def gm_render_vip_showcase(compact=False):
    """Vitrine pública compacta e fiel ao que é entregue dentro do VIP."""
    st.markdown("### 🚀 O que você recebe dentro do GM SCORE VIP")
    st.caption("Recursos reais do aplicativo. As análises são geradas a partir dos dados disponíveis para a partida selecionada.")

    st.markdown(
        """
        <style>
        .gm-vip-hero{position:relative;overflow:hidden;border:1px solid rgba(34,197,94,.38);border-radius:22px;padding:18px;
          background:radial-gradient(circle at 84% 14%,rgba(74,222,128,.16),transparent 28%),linear-gradient(145deg,rgba(22,128,58,.20),rgba(15,23,42,.94));
          box-shadow:0 16px 42px rgba(0,0,0,.20);margin:.35rem 0 .85rem;color:#f8fafc}
        .gm-vip-kicker{font-size:.68rem;font-weight:850;letter-spacing:.13em;color:#86efac;text-transform:uppercase}
        .gm-vip-title{font-size:1.48rem;font-weight:900;margin:.34rem 0 .2rem;letter-spacing:-.025em}
        .gm-vip-sub{font-size:.84rem;color:#cbd5e1;margin-bottom:.75rem;max-width:720px;line-height:1.45}
        .gm-delivery-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:10px}
        .gm-delivery{background:rgba(15,23,42,.72);border:1px solid rgba(148,163,184,.20);border-radius:13px;padding:10px}
        .gm-delivery b{display:block;font-size:.82rem;color:#f8fafc;margin-bottom:3px}.gm-delivery span{font-size:.71rem;color:#94a3b8;line-height:1.3}
        .gm-trust-row{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}.gm-chip{display:inline-block;border:1px solid rgba(74,222,128,.30);background:rgba(22,163,74,.12);color:#bbf7d0;border-radius:999px;padding:4px 8px;font-size:.71rem;font-weight:750}
        .gm-confidence{display:grid;grid-template-columns:repeat(3,1fr);gap:7px;margin:.55rem 0 .65rem}.gm-confidence>div{border:1px solid rgba(148,163,184,.20);border-radius:12px;padding:9px 10px;background:rgba(30,41,59,.14);font-size:.75rem;line-height:1.3}.gm-confidence b{display:block;margin-bottom:2px}
        .gm-extra-row{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:7px;margin:.55rem 0 .9rem}.gm-extra{border:1px solid rgba(148,163,184,.20);border-radius:12px;padding:9px 10px;background:rgba(30,41,59,.12);font-size:.76rem;line-height:1.3}.gm-extra b{display:block;margin-bottom:2px}.gm-extra span{opacity:.72;font-size:.70rem}
        .gm-comp-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px;margin-top:8px}.gm-comp{border:1px solid rgba(148,163,184,.20);border-radius:11px;padding:8px 9px;background:rgba(30,41,59,.14);font-size:.77rem;font-weight:650;line-height:1.25}
        @media(max-width:700px){.gm-delivery-grid{grid-template-columns:1fr 1fr}.gm-extra-row{grid-template-columns:1fr 1fr}.gm-comp-grid{grid-template-columns:1fr 1fr}.gm-vip-title{font-size:1.30rem}}
        @media(max-width:420px){.gm-delivery span{display:none}.gm-delivery{padding:9px}.gm-confidence{grid-template-columns:1fr}.gm-confidence>div{padding:8px 10px}.gm-extra span{display:none}.gm-comp{font-size:.73rem;padding:7px 8px}}
        </style>
        <div class="gm-vip-hero">
          <div class="gm-vip-kicker">Painel VIP • análise da partida selecionada</div>
          <div class="gm-vip-title">⚽ Escolha o confronto. O GM SCORE organiza a leitura.</div>
          <div class="gm-vip-sub">Histórico e métricas disponíveis são organizados em projeções e mercados, sempre com o nível de amostra correspondente.</div>
          <div class="gm-delivery-grid">
            <div class="gm-delivery"><b>⚽ Resultado e gols</b><span>1X2, dupla chance, gols por tempo/equipe e linhas suportadas.</span></div>
            <div class="gm-delivery"><b>🚩 Escanteios</b><span>Partida, tempos e equipe quando houver cobertura.</span></div>
            <div class="gm-delivery"><b>🟨 Cartões</b><span>Total, por equipe e mercados de ambas receberem.</span></div>
            <div class="gm-delivery"><b>🥅 Finalizações</b><span>Total, no alvo e por equipe conforme a fonte.</span></div>
            <div class="gm-delivery"><b>⭐ Oportunidades</b><span>Destaques somente em mercados habilitados e consolidados.</span></div>
            <div class="gm-delivery"><b>📤 Compartilhar em HD</b><span>Arte da análise pronta para compartilhar pelo celular.</span></div>
          </div>
          <div class="gm-trust-row"><span class="gm-chip">Estimativas estatísticas</span><span class="gm-chip">Amostra por métrica</span><span class="gm-chip">Odds justas quando aplicável</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown("#### 📊 Transparência da análise")
    st.markdown(
        """
        <div class="gm-confidence">
          <div><b>🟢 Conclusivo</b>Amostra consolidada.</div>
          <div><b>🟡 Cautela</b>Amostra curta, exibida com aviso.</div>
          <div><b>⚪ Inconclusivo</b>Sem base suficiente; nenhuma estimativa é forçada.</div>
        </div>
        <div class="gm-extra-row">
          <div class="gm-extra"><b>📅 Agenda</b><span>Competição, data e confronto.</span></div>
          <div class="gm-extra"><b>📈 Contexto</b><span>Forma, histórico e métricas.</span></div>
          <div class="gm-extra"><b>💰 Odds justas</b><span>Quando aplicável ao mercado.</span></div>
          <div class="gm-extra"><b>📰 Notícias</b><span>Atualizações dentro da conta.</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.caption("Probabilidades são estimativas estatísticas, não promessa de acerto. Mercados e métricas variam conforme a cobertura real da partida.")

    st.markdown("### 🌍 21 competições disponíveis")
    st.caption("Cobertura atual do GM SCORE; a disponibilidade de métricas pode variar por competição e partida.")
    competition_html = ''.join(f'<div class="gm-comp">{_gm_safe_html(item)}</div>' for item in GM_PUBLIC_COMPETITIONS)
    st.markdown(f'<div class="gm-comp-grid">{competition_html}</div>', unsafe_allow_html=True)

    if not compact:
        st.caption("⭐ No VIP, cada análise é gerada para a partida selecionada com os dados disponíveis naquele confronto.")

def gm_render_public_intro():
    st.markdown(
        """
        <style>
        .gm-public-hero{position:relative;overflow:hidden;border:1px solid rgba(34,197,94,.42);border-radius:24px;padding:24px 22px 20px;margin:.15rem 0 1rem;
          background:
            radial-gradient(circle at 80% 18%,rgba(74,222,128,.21),transparent 27%),
            linear-gradient(115deg,rgba(6,78,59,.96),rgba(15,23,42,.96) 58%,rgba(2,44,34,.94));
          box-shadow:0 20px 55px rgba(0,0,0,.24);color:#f8fafc}
        .gm-public-hero:before{content:"";position:absolute;inset:0;opacity:.15;pointer-events:none;
          background:linear-gradient(90deg,transparent 49.5%,rgba(255,255,255,.45) 50%,transparent 50.5%),radial-gradient(circle at 50% 50%,transparent 0 61px,rgba(255,255,255,.40) 62px 63px,transparent 64px)}
        .gm-public-ball{position:absolute;right:22px;top:18px;font-size:4.8rem;opacity:.12;filter:grayscale(1)}
        .gm-public-kicker{position:relative;font-size:.72rem;font-weight:850;letter-spacing:.14em;text-transform:uppercase;color:#86efac}
        .gm-public-title{position:relative;font-size:clamp(1.85rem,5.5vw,3.15rem);line-height:1.02;font-weight:950;letter-spacing:-.045em;max-width:760px;margin:.48rem 0 .65rem}
        .gm-public-title span{color:#4ade80}.gm-public-copy{position:relative;max-width:690px;color:#dbeafe;font-size:.95rem;line-height:1.55;margin-bottom:1rem}
        .gm-public-pills{position:relative;display:flex;gap:7px;flex-wrap:wrap;margin-top:12px}
        .gm-public-pill{border:1px solid rgba(134,239,172,.25);background:rgba(15,23,42,.42);padding:6px 9px;border-radius:999px;font-size:.76rem;font-weight:750;color:#dcfce7}
        .gm-feature-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:9px;margin:.85rem 0 1rem}
        .gm-feature-mini{border:1px solid rgba(148,163,184,.20);border-radius:15px;padding:12px;background:rgba(30,41,59,.10)}
        .gm-feature-mini b{display:block;font-size:.86rem;margin-bottom:3px}.gm-feature-mini span{font-size:.75rem;opacity:.70;line-height:1.35}
        .gm-access-cta{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:.85rem 0 1.35rem}
        .gm-access-cta a{display:flex;align-items:center;justify-content:center;text-decoration:none!important;border-radius:13px;padding:12px 10px;font-weight:850;border:1px solid rgba(34,197,94,.55)}
        .gm-login-cta{background:#16803a;color:white!important;box-shadow:0 9px 24px rgba(22,128,58,.20)}
        .gm-signup-cta{background:rgba(22,128,58,.08);color:inherit!important}
        @media(max-width:760px){.gm-feature-grid{grid-template-columns:1fr 1fr}.gm-public-hero{padding:21px 17px 18px}.gm-public-ball{font-size:3.8rem;right:12px;top:14px}}
        @media(max-width:520px){.gm-access-cta{grid-template-columns:1fr}.gm-feature-grid{grid-template-columns:1fr 1fr}}
        </style>
        <section class="gm-public-hero">
          <div class="gm-public-ball">⚽</div>
          <div class="gm-public-kicker">GM SCORE VIP • análise pré-jogo</div>
          <div class="gm-public-title">Dados da partida organizados para uma leitura <span>mais objetiva.</span></div>
          <div class="gm-public-copy">Escolha o confronto e veja projeções, probabilidades estimadas e métricas disponíveis em um único painel. Quando a amostra não é suficiente, o próprio GM SCORE informa.</div>
          <div class="gm-public-pills">
            <span class="gm-public-pill">⚽ Resultado e gols</span>
            <span class="gm-public-pill">🚩 Escanteios</span>
            <span class="gm-public-pill">🟨 Cartões</span>
            <span class="gm-public-pill">🥅 Finalizações</span>
            <span class="gm-public-pill">⭐ Oportunidades</span>
          </div>
        </section>
        <div class="gm-feature-grid">
          <div class="gm-feature-mini"><b>📅 Agenda</b><span>Escolha competição e confronto.</span></div>
          <div class="gm-feature-mini"><b>📊 Dados</b><span>Histórico e métricas disponíveis.</span></div>
          <div class="gm-feature-mini"><b>🎯 Probabilidades</b><span>Estimativas para os principais mercados.</span></div>
          <div class="gm-feature-mini"><b>📈 Contexto</b><span>Forma, força e leitura da partida.</span></div>
        </div>
        <div class="gm-access-cta">
          <a class="gm-login-cta" href="#gm-acesso">🔐 Já sou cliente — Entrar</a>
          <a class="gm-signup-cta" href="#gm-acesso">⭐ Quero ser VIP — Criar conta</a>
        </div>
        """,
        unsafe_allow_html=True,
    )

    gm_render_vip_showcase(compact=False)

    st.markdown("---")
    gm_render_public_reviews()

    st.markdown("---")
    gm_render_payment_plans()

    st.markdown("### 🔐 Como liberar seu acesso")
    st.markdown(
        """
        <div style="border:1px solid rgba(148,163,184,.22);border-radius:16px;padding:14px 16px;background:rgba(30,41,59,.14);line-height:1.55">
          <div><b>1.</b> Crie sua conta GM SCORE e confirme o e-mail.</div>
          <div><b>2.</b> Entre na conta e escolha seu plano VIP.</div>
          <div><b>3.</b> Faça o pagamento pelo Mercado Pago.</div>
          <div><b>4.</b> Após a confirmação válida, o VIP é ativado automaticamente.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _gm_display_1x2_percentages(probs):
    """Arredonda 1X2 para inteiros preservando soma visual exata de 100%."""
    if not probs:
        return {"home": 0, "draw": 0, "away": 0}
    keys = ("home", "draw", "away")
    vals = []
    for key in keys:
        try:
            vals.append(max(float(probs.get(key, 0.0) or 0.0), 0.0))
        except Exception:
            vals.append(0.0)
    total = sum(vals)
    if total <= 0:
        return {"home": 0, "draw": 0, "away": 0}
    scaled = [v * 100.0 / total for v in vals]
    base = [int(math.floor(v)) for v in scaled]
    missing = 100 - sum(base)
    order = sorted(range(3), key=lambda i: (scaled[i] - base[i], scaled[i]), reverse=True)
    for i in order[:max(missing, 0)]:
        base[i] += 1
    return dict(zip(keys, base))


def gm_render_match_hero(team_a, team_b, league_name, season_text, probs=None, updated_until=None, sample=None):
    """Cabeçalho visual da partida real, sem alterar nenhum cálculo do modelo."""
    team_a_html = _gm_safe_html(team_a)
    team_b_html = _gm_safe_html(team_b)
    team_a_visual = gm_team_badge_html(team_a, league_name, team_id=gm_current_match_team_id(team_a, "home"), size=38)
    team_b_visual = gm_team_badge_html(team_b, league_name, team_id=gm_current_match_team_id(team_b, "away"), size=38)
    league_html = _gm_safe_html(league_name)
    league_visual = gm_league_visual(league_name)
    league_logo = str(league_visual.get("logo") or "")
    league_logo_html = (f'<img src="{html.escape(league_logo, quote=True)}" alt="" loading="lazy" style="width:30px;height:30px;object-fit:contain">') if league_logo else ""
    league_title = _gm_safe_html(league_name)
    match_datetime = gm_current_match_datetime()
    season_html = _gm_safe_html(season_text)
    meta = f"{season_html}"
    if match_datetime:
        meta += f" • {_gm_safe_html(match_datetime)}"
    if updated_until is not None and not pd.isna(updated_until):
        try:
            meta += f" • dados até {pd.Timestamp(updated_until):%d/%m/%Y}"
        except Exception:
            pass
    if sample is not None:
        meta += f" • amostra mínima: {int(sample)} jogo(s)"

    if probs:
        _display_1x2 = _gm_display_1x2_percentages(probs)
        home = _display_1x2["home"]
        draw = _display_1x2["draw"]
        away = _display_1x2["away"]
        fair_home = 100.0 / max(min(float(probs.get("home", 0.0)), 99.5), 0.5)
        fair_draw = 100.0 / max(min(float(probs.get("draw", 0.0)), 99.5), 0.5)
        fair_away = 100.0 / max(min(float(probs.get("away", 0.0)), 99.5), 0.5)
        probability_html = f"""
          <div class="gm-real-probgrid">
            <div class="gm-real-probbox"><span>Vitória casa</span><b>{home}%</b><small>Odd justa {fair_home:.2f}</small></div>
            <div class="gm-real-probbox"><span>Empate</span><b>{draw}%</b><small>Odd justa {fair_draw:.2f}</small></div>
            <div class="gm-real-probbox"><span>Vitória fora</span><b>{away}%</b><small>Odd justa {fair_away:.2f}</small></div>
          </div>
          <div class="gm-real-note">Probabilidades estimadas pelo modelo GM SCORE para a partida selecionada — não representam garantia de resultado.</div>
        """
    else:
        probability_html = '<div class="gm-real-note">A análise estatística será exibida conforme a disponibilidade e qualidade dos dados da competição.</div>'

    st.markdown(
        f"""
        <style>
        .gm-real-match{{position:relative;overflow:hidden;border:1px solid rgba(34,197,94,.40);border-radius:22px;padding:20px;margin:.65rem 0 1rem;
          background:radial-gradient(circle at 84% 10%,rgba(74,222,128,.16),transparent 27%),linear-gradient(145deg,rgba(22,128,58,.20),rgba(15,23,42,.94));box-shadow:0 15px 42px rgba(0,0,0,.19);color:#f8fafc}}
        .gm-real-kicker{{font-size:.69rem;text-transform:uppercase;letter-spacing:.13em;font-weight:850;color:#86efac}}
        .gm-real-title{{font-size:clamp(1.35rem,4.5vw,2rem);font-weight:950;letter-spacing:-.035em;margin:.38rem 0 .22rem;line-height:1.12}}
        .gm-real-meta{{font-size:.76rem;color:#94a3b8;line-height:1.4}}
        .gm-real-probgrid{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:15px}}
        .gm-real-probbox{{background:rgba(15,23,42,.76);border:1px solid rgba(148,163,184,.20);border-radius:14px;padding:11px;text-align:center}}
        .gm-real-probbox span{{display:block;font-size:.67rem;color:#94a3b8;text-transform:uppercase;letter-spacing:.05em}}.gm-real-probbox b{{display:block;font-size:1.32rem;margin-top:3px;color:#fff}}.gm-real-probbox small{{display:block;font-size:.64rem;color:#94a3b8;margin-top:2px;font-weight:600}}
        .gm-real-note{{font-size:.72rem;color:#94a3b8;margin-top:10px;line-height:1.4}}
        @media(max-width:520px){{.gm-real-match{{padding:17px 14px}}.gm-real-probbox{{padding:9px 5px}}.gm-real-probbox b{{font-size:1.12rem}}}}
        </style>
        <section class="gm-real-match">
          <div class="gm-real-kicker">Partida carregada • análise VIP</div>
          <div class="gm-match-identity" style="display:grid;grid-template-columns:minmax(0,1fr) minmax(150px,.82fr) minmax(0,1fr);align-items:center;gap:8px;margin:.65rem 0 .35rem">
            <div style="display:flex;justify-content:center;align-items:center;min-width:0">{team_a_visual}</div>
            <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;min-width:0;text-align:center">
              <div style="height:38px;display:flex;align-items:center;justify-content:center">{league_logo_html}</div>
              <div style="width:100%;font-size:.74rem;font-weight:900;color:#cbd5e1;line-height:1.15;margin-top:3px;text-align:center;white-space:normal;overflow-wrap:anywhere">{league_title}</div>
              <div style="font-size:1.38rem;font-weight:950;color:#24e58b;line-height:1;margin-top:7px">×</div>
              <div style="font-size:.69rem;font-weight:750;color:#94a3b8;line-height:1.2;margin-top:7px">{_gm_safe_html(match_datetime) if match_datetime else ''}</div>
            </div>
            <div style="display:flex;justify-content:center;align-items:center;min-width:0">{team_b_visual}</div>
          </div>
          <div class="gm-real-meta" style="text-align:center">{season_html}{(' • dados até ' + pd.Timestamp(updated_until).strftime('%d/%m/%Y')) if updated_until is not None and not pd.isna(updated_until) else ''}{(' • amostra mínima: ' + str(int(sample)) + ' jogo(s)') if sample is not None else ''}</div>
          {probability_html}
        </section>
        """,
        unsafe_allow_html=True,
    )


def gm_render_login_form(form_key="gm_public_login"):
    with st.form(form_key, clear_on_submit=False):
        email = st.text_input("E-mail", key=f"{form_key}_email", autocomplete="email")
        password = st.text_input("Senha", type="password", key=f"{form_key}_password", autocomplete="current-password")
        submitted = st.form_submit_button("🔐 Entrar no GM SCORE", type="primary", use_container_width=True)
    if submitted:
        if not str(email).strip() or not str(password):
            st.warning("Informe seu e-mail e sua senha.")
            return
        try:
            gm_auth_sign_in(email, password)
            profile = gm_auth_get_profile(force=True)
            if not profile:
                gm_auth_sign_out()
                st.error("Sua conta foi autenticada, mas o perfil GM SCORE não foi encontrado. Fale com o suporte.")
                return
            st.rerun()
        except Exception as exc:
            raw = str(exc)
            low = raw.lower()
            if "invalid login credentials" in low or "invalid_credentials" in low:
                st.error("E-mail ou senha incorretos.")
            elif "email not confirmed" in low or "email_not_confirmed" in low:
                st.error("Confirme seu e-mail antes de entrar.")
            else:
                st.error("Não foi possível entrar agora. Confira os dados e tente novamente.")


def gm_render_signup_form():
    st.markdown("### ⭐ Criar conta VIP")
    st.caption("Crie sua conta e confirme o e-mail. Depois, entre no GM SCORE para gerar seu checkout individual do Mercado Pago.")
    with st.form("gm_public_signup", clear_on_submit=False):
        nome = st.text_input("Nome", autocomplete="name")
        email = st.text_input("E-mail", key="gm_signup_email", autocomplete="email")
        password = st.text_input("Senha", type="password", key="gm_signup_password", autocomplete="new-password")
        confirm = st.text_input("Confirmar senha", type="password", key="gm_signup_confirm", autocomplete="new-password")
        st.markdown(
            "**⚠️ Política de uso da conta VIP**  \n"
            "O acesso GM SCORE VIP é individual. O uso simultâneo da mesma conta em mais de um "
            "dispositivo não é permitido. Compartilhamento de acesso ou tentativas recorrentes de "
            "uso simultâneo poderão resultar em bloqueio. Para uso simultâneo por mais pessoas, "
            "é necessário contratar acessos adicionais, cada um com seu próprio login."
        )
        accepted = st.checkbox("Li e concordo com a Política de Uso da Conta VIP.")
        submitted = st.form_submit_button("Criar minha conta", type="primary", use_container_width=True)

    if submitted:
        clean_name = str(nome).strip()
        clean_email = str(email).strip().lower()
        if not clean_name or not clean_email or not password or not confirm:
            st.warning("Preencha todos os campos.")
            return
        if len(str(password)) < 8:
            st.warning("A senha deve ter pelo menos 8 caracteres.")
            return
        if password != confirm:
            st.warning("As senhas não coincidem.")
            return
        if not accepted:
            st.warning("Para criar a conta, é necessário aceitar a Política de Uso da Conta VIP.")
            return
        try:
            # O aceite também segue como metadata do cadastro. A tabela gm_users continua
            # protegida pelo RLS e os campos VIP não são controlados pelo cliente.
            result = gm_auth_sign_up(clean_name, clean_email, password)
            st.session_state["gm_signup_completed"] = True
            st.success("✅ Conta criada com sucesso.")
            st.info("📧 Confira seu e-mail e confirme o cadastro antes de tentar entrar.")
            st.markdown("---")
            gm_render_payment_plans(title="💳 Escolha seu plano VIP", compact=True)
            st.info("Após confirmar o e-mail, entre na conta para gerar o checkout individual. Pagamentos válidos serão processados automaticamente.")
            st.link_button("✈️ Falar com o suporte no Telegram", "https://t.me/suport_gm", use_container_width=True)
        except Exception as exc:
            raw = str(exc)
            text = raw.lower()
            if "already registered" in text or "user_already_exists" in text or "email_exists" in text:
                st.warning("Já existe uma conta com este e-mail. Use a opção Entrar.")
            elif (
                "over_email_send_rate_limit" in text
                or "email_rate_limit" in text
                or "rate limit" in text
                or "too many requests" in text
                or "gm_signup_http_429" in text
            ):
                st.warning(
                    "O serviço de confirmação por e-mail atingiu o limite temporário de envios. "
                    "Aguarde alguns minutos e tente novamente uma única vez."
                )
            elif "signup_disabled" in text or "signups not allowed" in text or "signup is disabled" in text:
                st.error("O cadastro de novas contas está temporariamente desabilitado. Fale com o suporte.")
            elif (
                "email_address_invalid" in text
                or "invalid email" in text
                or "email is invalid" in text
                or "unable to validate email address" in text
            ):
                st.warning("O e-mail informado não foi aceito. Confira o endereço e tente novamente.")
            elif "email_address_not_authorized" in text or "email not authorized" in text:
                st.warning("Este e-mail não está autorizado para cadastro. Use outro endereço ou fale com o suporte.")
            elif "weak_password" in text or "password should be" in text or "password is too weak" in text:
                st.warning("A senha não atende aos requisitos de segurança. Use uma senha mais forte.")
            elif "gm_signup_network" in text or "timeout" in text or "connection" in text:
                st.error("Não foi possível conectar ao serviço de cadastro agora. Tente novamente em instantes.")
            elif "gm_signup_config" in text:
                st.error("O cadastro está temporariamente indisponível por configuração do sistema. Fale com o suporte.")
            else:
                # Mensagem pública continua segura: não expõe resposta bruta, tokens ou configuração.
                st.error("Não foi possível criar a conta agora. Tente novamente em alguns minutos ou fale com o suporte.")


def gm_render_terms_acceptance(profile):
    """Exige aceite explícito de contas antigas antes de qualquer acesso do cliente."""
    nome = str((profile or {}).get("nome") or "Cliente").strip()
    st.markdown("## 📄 Política de Uso do GM SCORE VIP")
    st.write(
        f"Olá, **{nome}**. Antes de continuar, precisamos registrar o seu aceite da política de uso atual do GM SCORE."
    )
    st.info(
        "O acesso VIP é **individual** e permite **1 sessão ativa por conta**. "
        "Não é permitido compartilhar o login ou utilizar a mesma conta simultaneamente em mais de um dispositivo. "
        "Para acessos simultâneos de outras pessoas, é necessário contratar contas adicionais."
    )
    st.markdown(
        "**Ao aceitar, você confirma que:**\n\n"
        "- a conta será utilizada individualmente;\n"
        "- o compartilhamento de login não é permitido;\n"
        "- tentativas recorrentes de uso simultâneo podem resultar em bloqueio;\n"
        "- a validade do VIP depende do período contratado e do status do pagamento."
    )
    accepted = st.checkbox(
        "Li e concordo com a Política de Uso da Conta VIP.",
        key="gm_existing_terms_checkbox",
    )
    if st.button(
        "✅ Aceitar e continuar",
        type="primary",
        use_container_width=True,
        disabled=not accepted,
        key="gm_existing_terms_accept_button",
    ):
        try:
            gm_accept_terms()
            st.success("✅ Aceite registrado com sucesso.")
            st.rerun()
        except Exception:
            st.error("Não foi possível registrar o aceite agora. Tente novamente ou fale com o suporte.")

    st.link_button("✈️ Suporte pelo Telegram", "https://t.me/suport_gm", use_container_width=True)
    if st.button("🚪 Sair da conta", use_container_width=True, key="gm_terms_logout"):
        gm_auth_sign_out()
        st.rerun()


def gm_render_waiting_access(profile, state):
    nome = str(profile.get("nome") or "Cliente").strip()
    if state == "blocked":
        st.error("⛔ **Acesso bloqueado**")
        st.write(f"Olá, **{nome}**. Esta conta está bloqueada. Entre em contato com o suporte para verificar a situação do acesso.")
    elif state == "expired":
        st.warning("⌛ **Seu acesso VIP expirou**")
        st.write(f"Olá, **{nome}**. Escolha abaixo o período da renovação. Após a confirmação válida do pagamento pelo Mercado Pago, a renovação será processada automaticamente.")
        gm_render_payment_plans(title="💳 Renovar GM SCORE VIP", compact=True)
    else:
        st.info("⏳ **Acesso VIP aguardando liberação**")
        st.write(
            f"Olá, **{nome}**. Sua conta foi criada corretamente. Escolha um plano e gere seu checkout individual do Mercado Pago. "
            "Assim que o pagamento aprovado for validado pelo GM SCORE, o acesso VIP será liberado automaticamente."
        )
        gm_render_payment_plans(title="💳 Escolha seu plano VIP", compact=True)

    # Enquanto aguarda a aprovação, o cliente continua vendo a vitrine do que receberá no VIP.
    if state not in {"blocked", "expired"}:
        st.markdown("---")
        gm_render_vip_showcase(compact=True)

    st.link_button("✈️ Suporte pelo Telegram", "https://t.me/suport_gm", use_container_width=True)
    if st.button("🚪 Sair da conta", use_container_width=True, key="gm_wait_logout"):
        gm_auth_sign_out()
        st.rerun()


def gm_render_session_conflict(profile, reason):
    nome = str((profile or {}).get("nome") or "Cliente").strip()
    st.error("🔒 Esta conta já está em uso em outro dispositivo ou navegador.")
    st.write(
        f"Olá, **{nome}**. O GM SCORE permite **1 sessão ativa por conta VIP**. "
        "Encerre a sessão no dispositivo anterior ou peça ao administrador para liberar o acesso."
    )
    st.caption("Isso protege a conta contra compartilhamento e uso simultâneo não autorizado.")
    st.link_button("✈️ Falar com o suporte no Telegram", "https://t.me/suport_gm", use_container_width=True)
    if st.button("🚪 Sair desta tentativa de acesso", use_container_width=True, key="gm_session_conflict_logout"):
        gm_auth_sign_out()
        st.rerun()


def gm_render_email_confirmation_notice():
    """Mostra uma confirmação pública sem criar/loginar uma sessão VIP."""
    try:
        confirmed = str(st.query_params.get("gm_email_confirmed", "") or "").strip().lower() in {"1", "true", "sim", "yes"}
    except Exception:
        confirmed = False

    if not confirmed:
        return False

    st.markdown(
        """
        <div style="max-width:720px;margin:8vh auto 1.5rem auto;padding:2rem 1.4rem;
                    border:1px solid rgba(36,229,139,.35);border-radius:24px;
                    background:linear-gradient(145deg,#07100f,#0b1716);text-align:center;
                    box-shadow:0 18px 55px rgba(0,0,0,.28)">
          <div style="font-size:3rem;line-height:1">✅</div>
          <div style="font-size:1.8rem;font-weight:900;margin-top:.7rem;color:#f8fafc">E-mail confirmado com sucesso</div>
          <div style="font-size:1rem;line-height:1.6;margin-top:.8rem;color:#b9c6c3">
            Sua conta GM SCORE foi validada. Você já pode fechar esta aba e retornar ao aplicativo para entrar com seu e-mail e senha.
          </div>
          <div style="margin-top:1rem;font-size:.9rem;color:#7f918d">
            Esta página não inicia uma nova sessão VIP e não interfere em um acesso que já esteja aberto em outra aba ou dispositivo.
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.link_button("🔐 Voltar para o GM SCORE", GM_PUBLIC_APP_URL, use_container_width=True)
    st.caption("Se você já estava usando o GM SCORE em outra aba, pode simplesmente fechar esta página e continuar por lá.")
    return True


def gm_render_public_portal():
    """Retorna True somente quando o usuário pode acessar o app completo."""
    user_id = st.session_state.get("gm_auth_user_id")
    profile = None
    if user_id:
        try:
            # V122: o perfil já fica em session_state após a primeira leitura.
            # Não consultar gm_users novamente em cada clique/transição.
            profile = gm_auth_get_profile(force=False)
        except Exception:
            profile = None
    if profile:
        # Contas antigas precisam registrar um aceite explícito antes de continuar.
        # Administradores ficam isentos desta etapa para evitar travar a gestão do sistema.
        if profile.get("role") != "admin" and not bool(profile.get("terms_accepted")):
            gm_render_terms_acceptance(profile)
            return False

        state = gm_auth_access_state(profile)
        if state not in {"blocked", "anonymous"}:
            # A regra de 1 sessão ativa é exclusiva das contas VIP de clientes.
            # O administrador precisa conseguir entrar de qualquer navegador/dispositivo
            # para liberar sessões, bloquear contas e prestar suporte, inclusive quando
            # um cliente fica preso no controle de sessão.
            if state == "vip":
                try:
                    session_ok, session_reason = gm_start_or_validate_session()
                except Exception:
                    session_ok, session_reason = False, "session_service_error"

                if not session_ok:
                    if session_reason in {"another_session_active", "session_mismatch"}:
                        # Este navegador foi substituído por um acesso mais recente.
                        # Cai localmente sem bloquear o novo acesso e sem criar ping-pong
                        # de restauração automática.
                        gm_drop_replaced_local_session()
                        st.rerun()
                    elif session_reason == "blocked":
                        gm_render_waiting_access(profile, "blocked")
                    elif session_reason in {"vip_expired", "vip_not_active"}:
                        gm_render_waiting_access(profile, "expired")
                    else:
                        st.error("Não foi possível validar a sessão segura do GM SCORE agora.")
                        st.caption("Tente novamente. Se o problema continuar, fale com o suporte.")
                        if st.button("🚪 Sair", use_container_width=True, key="gm_session_error_logout"):
                            gm_auth_sign_out()
                            st.rerun()
                    return False

                # Heartbeat leve: enquanto a área VIP estiver realmente aberta,
                # revalida o mesmo token a cada ~2 minutos. Isso mantém
                # last_seen_at recente e permite que a janela ampliada de 60 minutos no
                # Supabase diferencie uma sessão ativa de uma aba abandonada.
                gm_render_session_heartbeat()

            with st.sidebar:
                st.markdown("### 👤 Minha conta")
                st.caption(str(profile.get("nome") or profile.get("email") or "GM SCORE"))
                tier = gm_product_tier(profile)
                st.success("🛠️ GM SCORE ADM" if state == "admin" else ("⭐ GM SCORE PRO" if tier == "pro" else "○ GM SCORE FREE"))

                # Renovação simples para clientes que já estão com o VIP ativo.
                # Reutiliza exatamente o checkout individual já existente:
                # pedido no servidor -> Mercado Pago -> webhook -> extensão do VIP.
                if state == "vip":
                    # Mostra ao próprio cliente a validade atual do VIP.
                    vip_until_raw = profile.get("vip_until")
                    if vip_until_raw:
                        try:
                            vip_until_dt = datetime.fromisoformat(
                                str(vip_until_raw).replace("Z", "+00:00")
                            )
                            now_vip = datetime.now(vip_until_dt.tzinfo)
                            remaining_seconds = (
                                vip_until_dt - now_vip
                            ).total_seconds()
                            remaining_days = max(
                                0,
                                math.ceil(
                                    remaining_seconds / 86400
                                ),
                            )

                            st.caption(
                                "Vencimento: "
                                f"{vip_until_dt.astimezone(ZoneInfo('America/Sao_Paulo')).strftime('%d/%m/%Y às %H:%M')}"
                            )

                            if remaining_days > 1:
                                st.info(
                                    f"⏳ {remaining_days} dias restantes"
                                )
                            elif remaining_days == 1:
                                st.warning(
                                    "⏳ 1 dia restante"
                                )
                            else:
                                st.warning(
                                    "⏳ Vencimento hoje"
                                )
                        except Exception:
                            # Se houver algum formato inesperado de data,
                            # não interfere no acesso VIP nem no checkout.
                            pass

                    renewal_open = bool(
                        st.session_state.get("gm_sidebar_renewal_open", False)
                    )

                    if st.button(
                        "💳 Renovar Pro",
                        use_container_width=True,
                        key="gm_sidebar_renew_vip",
                    ):
                        st.session_state["gm_sidebar_renewal_open"] = not renewal_open
                        st.rerun()

                    if st.session_state.get("gm_sidebar_renewal_open", False):
                        st.markdown("#### 👑 Renovação de planos Pro")
                        st.caption("Escolha o período e mantenha seu acesso completo ao GM SCORE.")

                        # Apresentação compacta e comercial dos planos. Mantém exatamente
                        # os mesmos plan_code, preços efetivos e botões de checkout existentes.
                        for plan in GM_VIP_PLANS:
                            is_monthly = plan.get("plan_code") == "vip_30"
                            is_semester = plan.get("plan_code") == "vip_180"

                            if is_monthly:
                                top_line = "🔥 PROMOÇÃO ESPECIAL • 1 MÊS"
                            elif is_semester:
                                top_line = "⭐ MELHOR CUSTO-BENEFÍCIO • 6 MESES"
                            else:
                                top_line = "🔥 PREÇO PROMOCIONAL • 3 MESES"

                            saving = str(plan.get("saving") or "").strip()
                            monthly = str(plan.get("monthly") or "").replace(" no Pix", "")

                            st.markdown(
                                f"""
<div style="margin:.65rem 0 .35rem 0;padding:.85rem .9rem;border:1px solid rgba(46,204,113,.38);border-radius:16px;background:linear-gradient(135deg,rgba(22,128,58,.16),rgba(17,24,39,.20));">
  <div style="font-size:.78rem;font-weight:900;letter-spacing:.035em;color:#ffcf4a;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{html.escape(top_line)}</div>
  <div style="display:flex;align-items:flex-end;justify-content:space-between;gap:.5rem;margin-top:.55rem;">
    <div style="font-size:1.65rem;font-weight:900;line-height:1;color:#f8fafc;">VIP {html.escape(str(plan['title']).upper())}</div>
    <div style="text-align:right;">
      <div style="font-size:1.55rem;font-weight:950;line-height:1;color:#2ee67d;">{html.escape(str(plan['pix_price']))}</div>
      <div style="margin-top:.22rem;font-size:.78rem;color:#cbd5e1;">no Pix • {html.escape(monthly)}</div>
    </div>
  </div>
  <div style="margin-top:.55rem;font-size:.76rem;font-weight:700;color:#a7f3d0;">{html.escape(saving)}</div>
</div>
                                """,
                                unsafe_allow_html=True,
                            )

                            gm_checkout_button(
                                plan,
                                "pix",
                                "⚡ Renovar agora com Pix",
                                primary=True,
                            )

                            if plan.get("card_price"):
                                gm_checkout_button(
                                    plan,
                                    "card",
                                    "💳 Renovar com cartão",
                                )
                                st.caption(plan["card_text"])
                            else:
                                st.caption("🔒 Plano mensal disponível somente no Pix.")

                        st.caption(
                            "🔐 O novo prazo é acrescentado ao seu VIP atual somente após "
                            "a confirmação válida do pagamento pelo Mercado Pago."
                        )

                if gm_product_tier(profile) == "free":
                    st.caption("Conta ativa · plano FREE")
                    if st.button("⭐ Desbloquear GM SCORE Pro", use_container_width=True, key="gm_sidebar_upgrade_pro"):
                        st.session_state["gm_sidebar_free_upgrade_open"] = not bool(st.session_state.get("gm_sidebar_free_upgrade_open"))
                        st.rerun()
                    if st.session_state.get("gm_sidebar_free_upgrade_open"):
                        gm_render_payment_plans(title="GM SCORE Pro", compact=True)

                st.markdown("### 🧭 Navegação")
                nav1, nav2 = st.columns(2)
                with nav1:
                    if st.button("🏠 Início", use_container_width=True, key="gm_sidebar_nav_analysis"):
                        gm_desktop_navigate("analysis")
                        st.rerun()
                with nav2:
                    if st.button("⚽ Jogos", use_container_width=True, key="gm_sidebar_nav_games"):
                        gm_desktop_navigate("games")
                        st.rerun()
                nav3, nav4 = st.columns(2)
                with nav3:
                    if st.button("💡 Dicas do Dia", use_container_width=True, key="gm_sidebar_nav_daily_pick"):
                        gm_desktop_navigate("daily_pick")
                        st.rerun()
                with nav4:
                    if st.button("📰 Novidades", use_container_width=True, key="gm_sidebar_nav_news"):
                        gm_desktop_navigate("news")
                        st.rerun()
                if st.button("👤 Minha Conta", use_container_width=True, key="gm_sidebar_nav_account"):
                    gm_desktop_navigate("account")
                    st.rerun()

                if state == "vip":
                    if st.button("⭐ Avaliar GM SCORE", use_container_width=True, key="gm_sidebar_review"):
                        st.session_state["gm_reviews_open"] = True
                        st.rerun()
                if st.button("🚪 Sair", use_container_width=True, key="gm_sidebar_logout"):
                    gm_auth_sign_out()
                    st.session_state.pop("gm_admin_panel_open", None)
                    st.session_state.pop("gm_news_open", None)
                    st.session_state.pop("gm_reviews_open", None)
                    st.session_state.pop("gm_main_view", None)
                    st.rerun()
                st.markdown("---")
            return True
        gm_render_waiting_access(profile, state)
        st.markdown("---")
        if st.button("📲 Como instalar o GM SCORE no celular", use_container_width=True):
            render_install_guide()
        st.link_button("✈️ Suporte pelo Telegram", "https://t.me/suport_gm", use_container_width=True)
        return False

    # Se existiam tokens inválidos/expirados, limpa a sessão antes de mostrar o portal.
    if user_id and not profile:
        gm_auth_clear_local_session()

    gm_render_public_intro()
    if st.session_state.pop("gm_session_replaced_notice", False):
        st.info("Este acesso foi encerrado porque a conta entrou no GM SCORE em outro acesso mais recente.")
    # Destino dos botões de acesso exibidos no topo da página pública.
    st.markdown('<div id="gm-acesso"></div>', unsafe_allow_html=True)
    st.markdown("## 🔐 Acesse sua conta ou entre para o VIP")
    st.caption("Já é cliente? Entre com seu e-mail e senha. Novo por aqui? Crie sua conta VIP.")
    login_tab, signup_tab = st.tabs(["🔐 Entrar", "⭐ Criar conta VIP"])
    with login_tab:
        gm_render_login_form()
    with signup_tab:
        gm_render_signup_form()

    st.markdown("---")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("📲 Como instalar o GM SCORE no celular", use_container_width=True, key="gm_public_install"):
            render_install_guide()
    with c2:
        st.link_button("✈️ Suporte pelo Telegram", "https://t.me/suport_gm", use_container_width=True)
    st.caption("As análises do GM SCORE são estimativas estatísticas e não garantem resultados. Aposte com responsabilidade.")
    return False


# Retorno da confirmação de e-mail: exibe apenas a mensagem pública.
# Não troca tokens, não autentica automaticamente e não inicia a trava de sessão VIP.
if gm_render_email_confirmation_notice():
    st.stop()

# Restaura silenciosamente a sessão após F5/reabertura da mesma URL quando o
# segredo privado de persistência está configurado no Streamlit.
gm_auth_try_restore_persistent_session()

# Portal de acesso. Usuários anônimos, pendentes, expirados ou bloqueados param aqui.
# VIPs e administradores seguem para o mesmo aplicativo completo já existente.
if not gm_render_public_portal():
    st.stop()

# Aviso aparece somente dentro da área completa. O diagnóstico técnico só é exibido
# para administrador autenticado, evitando qualquer rota de bypass do acesso VIP.
try:
    _gm_profile_after_gate = gm_auth_get_profile()
except Exception:
    _gm_profile_after_gate = None

# V130: resolve FREE/PRO/ADM depois do restore/login e aplica o selo na marca já renderizada.
# Assim o indicador não desaparece após F5 ou reabertura da sessão persistida.
if _gm_profile_after_gate:
    _gm_runtime_tier = gm_product_tier(_gm_profile_after_gate)
    _gm_runtime_is_admin = str((_gm_profile_after_gate or {}).get("role") or "").lower() == "admin"
    _gm_runtime_badge = "ADM" if _gm_runtime_is_admin else ("PRO" if _gm_runtime_tier == "pro" else "FREE")
    _gm_runtime_badge_css = (
        "border-color:rgba(52,230,129,.55);background:rgba(52,230,129,.12);color:#34e681;"
        if _gm_runtime_tier == "pro"
        else "border-color:rgba(148,163,184,.30);background:rgba(148,163,184,.08);color:#cbd5e1;"
    )
    st.markdown(
        f"""<style>
        .gm-brand-title::after{{
            content:"{_gm_runtime_badge}";
            display:inline-flex;align-items:center;justify-content:center;
            margin-left:.62rem;padding:.26rem .52rem;border-radius:8px;
            font-size:.66rem;font-weight:950;letter-spacing:.09em;
            border:1px solid;vertical-align:middle;transform:translateY(-.18rem);
            -webkit-text-stroke:0;paint-order:normal;{_gm_runtime_badge_css}
        }}
        </style>""",
        unsafe_allow_html=True,
    )


# V66: as novidades ficam concentradas na aba própria da navegação.
# O sino flutuante deixou de ser renderizado; RPCs, leitura e publicações permanecem intactos.
if _gm_profile_after_gate and st.session_state.get("gm_news_open"):
    gm_render_news_center()

if _gm_profile_after_gate and st.session_state.get("gm_reviews_open"):
    gm_render_review_dialog(_gm_profile_after_gate)

# V75: a Central Administrativa é renderizada somente após todas as funções e constantes do app estarem definidas.
# Isso evita dependências prematuras (ex.: BRASILIA_TZ e funções das Dicas do Dia).


# V141: diagnósticos administrativos ficam somente na Central Administrativa.

# Competições com estatísticas detalhadas em CSV público.
EUROPE_LEAGUES = {
    "Inglaterra - Premier League": "E0",
    "Espanha - La Liga": "SP1",
    "Itália - Serie A": "I1",
    "Alemanha - Bundesliga": "D1",
    "França - Ligue 1": "F1",
    "Portugal - Liga Portugal": "P1",
    "Holanda - Eredivisie": "N1",
    "Escócia - Premiership": "SC0",
    "Turquia - Süper Lig": "T1",
}

COMPETITIONS = {
    **{name: {"kind": "football_data", "code": code, "season": "europe"}
       for name, code in EUROPE_LEAGUES.items()},
    "Brasil - Série A": {"kind": "hybrid_extra", "id": "br1", "extra_code": "BRA", "season": "calendar"},
    "Brasil - Série B": {"kind": "open_results", "id": "br2", "season": "calendar"},
    "Arábia Saudita - Saudi Pro League": {"kind": "open_results", "id": "saudi", "season": "europe"},
    "Estados Unidos - MLS": {"kind": "hybrid_extra", "id": "mls", "extra_code": "USA", "season": "calendar"},
    "Argentina - Liga Profesional": {"kind": "hybrid_extra", "id": "argentina", "extra_code": "ARG", "season": "calendar"},
    "México - Liga MX": {"kind": "hybrid_extra", "id": "mexico", "extra_code": "MEX", "season": "calendar"},
    "Colômbia - Primera A": {"kind": "open_results", "id": "colombia", "season": "calendar"},
    "CONMEBOL Libertadores": {"kind": "open_results", "id": "libertadores", "season": "calendar"},
    "CONMEBOL Sul-Americana": {"kind": "open_results", "id": "sudamericana", "season": "calendar"},
    "UEFA Champions League": {"kind": "open_results", "id": "champions", "season": "europe"},
    "UEFA Europa League": {"kind": "open_results", "id": "europa", "season": "europe"},
    "UEFA Conference League": {"kind": "open_results", "id": "conference", "season": "europe"},
}

FD_STATS = {
    "Finalizações": ("HS", "AS"),
    "Chutes no alvo": ("HST", "AST"),
    "Escanteios": ("HC", "AC"),
    "Faltas": ("HF", "AF"),
    "Amarelos": ("HY", "AY"),
    "Vermelhos": ("HR", "AR"),
}

# Quantidade mínima de clubes esperada na competição vigente.
# Serve para impedir que uma fonte parcial seja aceita silenciosamente.
MIN_TEAMS = {
    "Inglaterra - Premier League": 20, "Espanha - La Liga": 20,
    "Itália - Serie A": 20, "Alemanha - Bundesliga": 18,
    "França - Ligue 1": 18, "Portugal - Liga Portugal": 18,
    "Holanda - Eredivisie": 18, "Escócia - Premiership": 12,
    "Turquia - Süper Lig": 18, "Brasil - Série A": 20,
    "Brasil - Série B": 20, "Arábia Saudita - Saudi Pro League": 18,
    "Estados Unidos - MLS": 30, "Argentina - Liga Profesional": 30,
    "México - Liga MX": 18, "Colômbia - Primera A": 20,
    "CONMEBOL Libertadores": 32, "CONMEBOL Sul-Americana": 32,
    "UEFA Champions League": 36, "UEFA Europa League": 36,
    "UEFA Conference League": 36,
}


# Participantes oficiais/conferidos da temporada vigente para competições cujas
# fontes de resultados podem trazer apenas os clubes que já entraram em campo.
# Estes elencos COMPLETAM a lista de seleção; estatísticas continuam vindo das
# fontes reais e nunca são inventadas para um clube sem amostra.
CURRENT_TEAM_ROSTERS = {
    "Brasil - Série B": [
        "América-MG", "Athletic-MG", "Atlético-GO", "Avaí", "Botafogo-SP",
        "Ceará", "CRB", "Criciúma", "Cuiabá", "Fortaleza", "Goiás",
        "Juventude", "Londrina", "Náutico", "Novorizontino", "Operário-PR",
        "Ponte Preta", "São Bernardo", "Sport", "Vila Nova",
    ],
    "Estados Unidos - MLS": [
        "Atlanta United", "Austin FC", "Charlotte FC", "Chicago Fire FC",
        "FC Cincinnati", "Colorado Rapids", "Columbus Crew", "D.C. United",
        "FC Dallas", "Houston Dynamo FC", "Inter Miami CF", "LA Galaxy",
        "Los Angeles FC", "Minnesota United FC", "CF Montréal", "Nashville SC",
        "New England Revolution", "New York City FC", "Orlando City SC",
        "Philadelphia Union", "Portland Timbers", "Real Salt Lake",
        "Red Bull New York", "San Diego FC", "San Jose Earthquakes",
        "Seattle Sounders FC", "Sporting Kansas City", "St. Louis CITY SC",
        "Toronto FC", "Vancouver Whitecaps FC",
    ],
    "Argentina - Liga Profesional": [
        "Aldosivi", "Argentinos Juniors", "Atlético Tucumán", "Banfield",
        "Barracas Central", "Belgrano", "Boca Juniors", "Central Córdoba",
        "Defensa y Justicia", "Deportivo Riestra", "Estudiantes",
        "Estudiantes de Río Cuarto", "Gimnasia La Plata", "Gimnasia de Mendoza",
        "Huracán", "Independiente", "Independiente Rivadavia", "Instituto",
        "Lanús", "Newell's Old Boys", "Platense", "Racing Club", "River Plate",
        "Rosario Central", "San Lorenzo", "Sarmiento", "Talleres", "Tigre",
        "Unión", "Vélez Sarsfield",
    ],
    "México - Liga MX": [
        "América", "Atlas", "Atlante", "Atlético de San Luis", "Cruz Azul",
        "FC Juárez", "Guadalajara", "León", "Monterrey", "Necaxa", "Pachuca",
        "Puebla", "Pumas UNAM", "Querétaro", "Santos Laguna", "Tigres UANL",
        "Tijuana", "Toluca",
    ],
    "Colômbia - Primera A": [
        "Águilas Doradas", "Alianza FC", "América de Cali", "Atlético Bucaramanga",
        "Atlético Nacional", "Boyacá Chicó", "Cúcuta Deportivo", "Deportivo Cali",
        "Deportivo Pasto", "Deportivo Pereira", "Deportes Tolima", "Fortaleza CEIF",
        "Independiente Medellín", "Independiente Santa Fe", "Internacional de Bogotá",
        "Jaguares de Córdoba", "Junior", "Llaneros", "Millonarios", "Once Caldas",
    ],
    "CONMEBOL Libertadores": [
        # Quartas de final 2026 + participantes que podem não aparecer ainda
        # na fonte de resultados usada pelo seletor.
        "Fluminense", "Platense", "Palmeiras", "LDU Quito",
        "Estudiantes", "Corinthians", "Independiente del Valle", "Flamengo",
    ],
    "UEFA Champions League": [
        "AEK Athens", "Arsenal", "Aston Villa", "Atlético de Madrid", "Barcelona",
        "Bayern München", "Bodø/Glimt", "Borussia Dortmund", "Club Brugge", "Como",
        "Fenerbahçe", "Feyenoord", "Galatasaray", "Inter", "LASK", "Leipzig",
        "Lens", "Lille", "Liverpool", "Manchester City", "Manchester United",
        "Napoli", "Paris Saint-Germain", "Porto", "PSV", "Real Betis", "Real Madrid",
        "Roma", "Sabah", "Shakhtar Donetsk", "Slavia Praha", "Slovan Bratislava",
        "Sporting CP", "Stuttgart", "Viking", "Villarreal",
    ],
    "UEFA Europa League": [
        "Anderlecht", "Ararat-Armenia", "AZ Alkmaar", "Benfica", "Beşiktaş",
        "Bournemouth", "Celje", "Celtic", "Crystal Palace", "Celta", "Ferencváros",
        "GNK Dinamo", "Hapoel Beer-Sheva", "Hoffenheim", "Jagiellonia", "Juventus",
        "Lech Poznań", "Leverkusen", "Levski Sofia", "Lillestrøm", "Lyon", "Marseille",
        "Milan", "N.E.C.", "OFI Crete", "Olympiacos", "Omonia", "Real Sociedad",
        "Rennes", "Salzburg", "Sparta Praha", "Sturm Graz", "Sunderland", "Torreense",
        "Union SG", "Viktoria Plzeň",
    ],
}

# Competições em que o seletor deve conter SOMENTE o elenco principal oficial.
# Evita U19/Sub-19, Youth League, reservas, feminino e clubes de fases paralelas.
STRICT_OFFICIAL_ROSTERS = {"UEFA Champions League"}
SECONDARY_TEAM_RE = re.compile(
    r"(?:\bu\s*[- ]?\d{2}\b|\bsub\s*[- ]?\d{2}\b|\bunder\s*[- ]?\d{2}\b|"
    # Não bloqueie a palavra "Junior/Juniors" isoladamente: ela faz parte de
    # nomes oficiais de clubes profissionais (Boca Juniors, Argentinos Juniors,
    # Junior FC etc.). Categorias de base continuam bloqueadas por Uxx/Sub-xx,
    # Youth, Academy, reservas, feminino e demais marcadores inequívocos.
    r"\byouth\b|\bacadem(?:y|ia)\b|\breserv(?:e|es|as?)\b|"
    r"\bb\s*team\b|\bteam\s*b\b|\bfemin(?:ino|ina|ine)?\b|\bwomen(?:'s)?\b|"
    r"\bfemenin(?:o|a)\b|\bfrauen\b|\bfemminile\b|\b(?:ii|iii)\s*$)",
    re.IGNORECASE,
)

SECONDARY_COMPETITION_RE = re.compile(
    r"(?:\bu\s*[- ]?\d{2}\b|\bsub\s*[- ]?\d{2}\b|\bunder\s*[- ]?\d{2}\b|"
    r"\byouth\b|\bacadem(?:y|ia)\b|\bjunior(?:es|s)?\b|\breserv(?:e|es|as?)\b|"
    r"\bpremier\s+league\s+2\b|\bprofessional\s+development\s+league\b|"
    r"\bwomen(?:'s)?\b|\bfemin(?:ino|ina|ine)?\b|\bfemenin(?:o|a)\b|\bfrauen\b|\bfemminile\b)",
    re.IGNORECASE,
)

def is_main_senior_team_name(name):
    text = str(name or "").strip()
    if not text:
        return False
    return SECONDARY_TEAM_RE.search(text) is None

COMPETITION_ICONS = {
    "Inglaterra - Premier League": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "Espanha - La Liga": "🇪🇸",
    "Itália - Serie A": "🇮🇹", "Alemanha - Bundesliga": "🇩🇪",
    "França - Ligue 1": "🇫🇷", "Portugal - Liga Portugal": "🇵🇹",
    "Holanda - Eredivisie": "🇳🇱", "Escócia - Premiership": "🏴󠁧󠁢󠁳󠁣󠁴󠁿",
    "Turquia - Süper Lig": "🇹🇷", "Brasil - Série A": "🇧🇷",
    "Brasil - Série B": "🇧🇷", "Arábia Saudita - Saudi Pro League": "🇸🇦",
    "Estados Unidos - MLS": "🇺🇸", "Argentina - Liga Profesional": "🇦🇷",
    "México - Liga MX": "🇲🇽", "Colômbia - Primera A": "🇨🇴",
    "CONMEBOL Libertadores": "🏆", "CONMEBOL Sul-Americana": "🏆",
    "UEFA Champions League": "🏆", "UEFA Europa League": "🏆",
    "UEFA Conference League": "🏆",
}

# Força relativa aproximada do nível competitivo da liga doméstica.
# O valor não é uma "nota absoluta"; serve somente para traduzir desempenho
# doméstico para partidas entre clubes de campeonatos diferentes.
LEAGUE_STRENGTH = {
    "Inglaterra - Premier League": 1.20, "Espanha - La Liga": 1.15,
    "Itália - Serie A": 1.10, "Alemanha - Bundesliga": 1.09,
    "França - Ligue 1": 1.04, "Portugal - Liga Portugal": 0.96,
    "Holanda - Eredivisie": 0.98, "Turquia - Süper Lig": 0.94,
    "Escócia - Premiership": 0.90, "Brasil - Série A": 1.00,
    "Brasil - Série B": 0.88, "Argentina - Liga Profesional": 0.96,
    "México - Liga MX": 0.94, "Colômbia - Primera A": 0.90,
    "Estados Unidos - MLS": 0.92, "Arábia Saudita - Saudi Pro League": 0.93,
}

# Baselines conservadores de torneio. Gols são recalculados dinamicamente com
# edições anteriores quando o OpenFootball estiver disponível. Escanteios e
# cartões funcionam como regressão à média quando a competição atual ainda não
# possui amostra suficiente.
COMPETITION_PRIORS = {
    "UEFA Champions League": {"Gols": 2.85, "Escanteios": 9.70, "Cartões": 4.50},
    "UEFA Europa League": {"Gols": 2.75, "Escanteios": 9.60, "Cartões": 4.70},
    "UEFA Conference League": {"Gols": 2.80, "Escanteios": 9.55, "Cartões": 4.65},
    "CONMEBOL Libertadores": {"Gols": 2.45, "Escanteios": 9.40, "Cartões": 5.10},
    "CONMEBOL Sul-Americana": {"Gols": 2.40, "Escanteios": 9.35, "Cartões": 5.20},
}


# Confrontos históricos verificados usados apenas como complemento quando as
# bases públicas de resultados não alcançam edições antigas ou outra competição
# continental. Não existe favoritismo manual: são somente placares/resultados
# históricos, combinados com os demais sinais do modelo.
VERIFIED_H2H_MATCHES = {
    frozenset({"Porto", "Manchester City"}): [
        {"home": "Porto", "away": "Manchester City", "hg": 1, "ag": 2, "date": "2012-02-16", "source": "UEFA"},
        {"home": "Manchester City", "away": "Porto", "hg": 4, "ag": 0, "date": "2012-02-22", "source": "UEFA"},
        {"home": "Manchester City", "away": "Porto", "hg": 3, "ag": 1, "date": "2020-10-21", "source": "UEFA"},
        {"home": "Porto", "away": "Manchester City", "hg": 0, "ag": 0, "date": "2020-12-01", "source": "UEFA"},
    ],
    frozenset({"Real Madrid", "Inter"}): [
        {"home": "Real Madrid", "away": "Inter", "hg": 3, "ag": 2, "date": "2020-11-03", "source": "UEFA"},
        {"home": "Inter", "away": "Real Madrid", "hg": 0, "ag": 2, "date": "2020-11-25", "source": "UEFA"},
        {"home": "Inter", "away": "Real Madrid", "hg": 0, "ag": 1, "date": "2021-09-15", "source": "UEFA"},
        {"home": "Real Madrid", "away": "Inter", "hg": 2, "ag": 0, "date": "2021-12-07", "source": "UEFA"},
    ],
}

CONTEXTUAL_COMPETITIONS = set(COMPETITION_PRIORS)

def competition_display_name(name):
    return f"{COMPETITION_ICONS.get(name, '🏆')} {name}"

def ensure_team_coverage(df, competition_name, tolerance=0, strict=False):
    """Registra cobertura sem invalidar dados atuais legítimos.

    Arquivos de resultados, especialmente no início da temporada, naturalmente
    contêm apenas clubes que já disputaram partidas. Portanto cobertura de clubes
    é um sinal para complementar a lista, e não motivo para derrubar a competição.
    """
    expected = MIN_TEAMS.get(competition_name)
    if not expected or df is None:
        return df
    if isinstance(df, pd.DataFrame) and "Time" in df.columns:
        count = int(df["Time"].dropna().astype(str).nunique())
    elif isinstance(df, pd.DataFrame) and {"HomeTeam", "AwayTeam"}.issubset(df.columns):
        count = len(set(df["HomeTeam"].dropna().astype(str)) | set(df["AwayTeam"].dropna().astype(str)))
    else:
        return df
    try:
        df.attrs["team_coverage"] = {"found": count, "expected": expected, "complete": count >= expected - tolerance}
    except Exception:
        pass
    if strict and count < expected - tolerance:
        raise RuntimeError(f"fonte incompleta: {count}/{expected} equipes encontradas")
    return df

DISPLAY_METRICS = [
    "Jogos",
    "Gols pró",
    "Gols contra",
    "Escanteios",
    "Amarelos",
    "Vermelhos",
    "Faltas",
    "Finalizações",
    "Chutes no alvo",
    "Posse (%)",
    "Impedimentos",
    "Passes",
    "Precisão passes (%)",
]


BRASILIA_TZ = ZoneInfo("America/Sao_Paulo")

COMPETITION_TIMEZONES = {
    "Inglaterra - Premier League": "Europe/London",
    "Espanha - La Liga": "Europe/Madrid",
    "Itália - Serie A": "Europe/Rome",
    "Alemanha - Bundesliga": "Europe/Berlin",
    "França - Ligue 1": "Europe/Paris",
    "Portugal - Liga Portugal": "Europe/Lisbon",
    "Holanda - Eredivisie": "Europe/Amsterdam",
    "Escócia - Premiership": "Europe/London",
    "Turquia - Süper Lig": "Europe/Istanbul",
    "Brasil - Série A": "America/Sao_Paulo",
    "Brasil - Série B": "America/Sao_Paulo",
    "Arábia Saudita - Saudi Pro League": "Asia/Riyadh",
    "Estados Unidos - MLS": "America/New_York",
    "Argentina - Liga Profesional": "America/Argentina/Buenos_Aires",
    "México - Liga MX": "America/Mexico_City",
    "Colômbia - Primera A": "America/Bogota",
    "CONMEBOL Libertadores": "America/Sao_Paulo",
    "CONMEBOL Sul-Americana": "America/Sao_Paulo",
    "UEFA Champions League": "Europe/Paris",
    "UEFA Europa League": "Europe/Paris",
    "UEFA Conference League": "Europe/Paris",
}

def fixture_time_brasilia(raw_time, competition, fixture_date=None, source_tz=None):
    """Converte HH:MM da fonte para horário de Brasília; preserva status como FT/AO VIVO."""
    if raw_time is None:
        return "", None
    txt = str(raw_time).strip()
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", txt)
    if not m:
        return txt, None
    if fixture_date is None:
        fixture_date = datetime.now(BRASILIA_TZ).date()
    if isinstance(fixture_date, pd.Timestamp):
        fixture_date = fixture_date.date()
    tz_name = source_tz or COMPETITION_TIMEZONES.get(competition, "UTC")
    try:
        src = datetime(fixture_date.year, fixture_date.month, fixture_date.day, int(m.group(1)), int(m.group(2)), tzinfo=ZoneInfo(tz_name))
        brt = src.astimezone(BRASILIA_TZ)
        return brt.strftime("%H:%M"), brt.date()
    except Exception:
        return txt, fixture_date

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Accept": "text/csv,text/plain,application/json,*/*",
}


def season_label(year, season_type):
    if season_type == "europe":
        return f"{year}/{str(year + 1)[-2:]}"
    return str(year)


def season_options(season_type):
    now = datetime.now()
    if season_type == "europe":
        current = now.year if now.month >= 7 else now.year - 1
    else:
        current = now.year
    return list(range(current, 2015, -1))


def current_season_year(season_type):
    """Retorna exclusivamente a temporada vigente; nunca recua silenciosamente."""
    now = datetime.now()
    if season_type == "europe":
        return now.year if now.month >= 7 else now.year - 1
    return now.year


def season_code(year):
    return f"{year % 100:02d}{(year + 1) % 100:02d}"


def request_first(urls, timeout=30):
    """Tenta múltiplas URLs e retentativas sem depender de serviços protegidos por 403."""
    errors = []
    for url in urls:
        for attempt in range(3):
            try:
                r = requests.get(url, headers=HEADERS, timeout=timeout)
                if r.status_code == 200 and r.content:
                    return r
                errors.append(f"HTTP {r.status_code}")
                if r.status_code not in (429, 500, 502, 503, 504):
                    break
            except requests.RequestException as exc:
                errors.append(str(exc))
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(errors[-1] if errors else "fonte indisponível")


def read_csv_bytes(content):
    last = None
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        for sep in (",", ";"):
            try:
                df = pd.read_csv(io.BytesIO(content), encoding=enc, sep=sep)
                if len(df.columns) > 1:
                    return df
            except Exception as exc:
                last = exc
    raise RuntimeError(f"não foi possível ler o arquivo: {last}")


@lru_cache(maxsize=16384)
def _clean_col_cached(text):
    text = text.strip().lower()
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def clean_col(text):
    # V117: chaves de widgets e colunas repetem os mesmos nomes em cada rerun.
    return _clean_col_cached(str(text))



# ============================================================
# ODDS PÚBLICAS - calibração de mercado sem API key
# ============================================================
# O GM SCORE não depende de chave privada. As odds são apenas uma referência
# de mercado e nunca substituem o modelo estatístico. A fonte principal abaixo
# é o OddsPortal, que publica comparações 1X2 em páginas públicas. Como páginas
# públicas podem mudar de estrutura ou bloquear automação, qualquer falha é
# tratada silenciosamente e a análise continua 100% estatística.
ODDSPORTAL_PATHS = {
    "Inglaterra - Premier League": ["england/premier-league"],
    "Espanha - La Liga": ["spain/laliga", "spain/la-liga"],
    "Itália - Serie A": ["italy/serie-a"],
    "Alemanha - Bundesliga": ["germany/bundesliga"],
    "França - Ligue 1": ["france/ligue-1"],
    "Portugal - Liga Portugal": ["portugal/liga-portugal", "portugal/primeira-liga"],
    "Holanda - Eredivisie": ["netherlands/eredivisie"],
    "Escócia - Premiership": ["scotland/premiership"],
    "Turquia - Süper Lig": ["turkey/super-lig"],
    "Brasil - Série A": ["brazil/serie-a-betano", "brazil/serie-a"],
    "Brasil - Série B": ["brazil/serie-b"],
    "Arábia Saudita - Saudi Pro League": ["saudi-arabia/saudi-professional-league"],
    "Estados Unidos - MLS": ["usa/mls"],
    "Argentina - Liga Profesional": ["argentina/liga-profesional"],
    "México - Liga MX": ["mexico/liga-mx"],
    "Colômbia - Primera A": ["colombia/primera-a"],
    "CONMEBOL Libertadores": ["south-america/copa-libertadores", "south-america/copa-libertadores-betano"],
    "CONMEBOL Sul-Americana": ["south-america/copa-sudamericana"],
    "UEFA Champions League": ["europe/champions-league"],
    "UEFA Europa League": ["europe/europa-league"],
    "UEFA Conference League": ["europe/conference-league"],
}


def _odds_team_key(name):
    txt = clean_col(name).replace("_", " ")
    stop = {
        "fc", "cf", "afc", "sc", "ac", "ec", "club", "clube", "de", "do", "da",
        "the", "football", "futbol", "futebol", "calcio", "fbpa", "sad", "sa"
    }
    toks = [t for t in txt.split() if t not in stop]
    return " ".join(toks)


def _team_similarity(a, b):
    aa, bb = _odds_team_key(a), _odds_team_key(b)
    if not aa or not bb:
        return 0.0
    if aa == bb:
        return 1.0
    if aa in bb or bb in aa:
        return 0.92
    sa, sb = set(aa.split()), set(bb.split())
    return len(sa & sb) / max(len(sa | sb), 1)


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0
    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        elif tag in ("div", "tr", "li", "br", "p", "td", "span"):
            self.parts.append("\n")
    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.skip:
            self.skip -= 1
        elif tag in ("div", "tr", "li", "p", "td"):
            self.parts.append("\n")
    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)
    def text(self):
        return re.sub(r"[ \t]+", " ", "".join(self.parts))


def _extract_visible_text(html):
    parser = _VisibleText()
    try:
        parser.feed(html)
        return parser.text()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html or "")


def _public_odds_from_text(text, home, away):
    """Localiza o confronto no texto visível e extrai o 1X2 mais próximo.
    O filtro exige odds decimais plausíveis e usa os nomes apenas como âncora.
    """
    if not text:
        return None
    lines = [re.sub(r"\s+", " ", x).strip() for x in text.splitlines()]
    lines = [x for x in lines if x]
    hk, ak = _odds_team_key(home), _odds_team_key(away)
    best = None
    for i in range(len(lines)):
        chunk = " ".join(lines[max(0, i-3):min(len(lines), i+8)])
        ck = _odds_team_key(chunk)
        # A mesma janela precisa conter pistas fortes das duas equipes.
        sh = 1.0 if hk and hk in ck else max((_team_similarity(home, x) for x in lines[max(0,i-3):min(len(lines),i+8)]), default=0)
        sa = 1.0 if ak and ak in ck else max((_team_similarity(away, x) for x in lines[max(0,i-3):min(len(lines),i+8)]), default=0)
        if sh < 0.72 or sa < 0.72:
            continue
        vals = []
        for raw in re.findall(r"(?<!\d)(\d{1,2}[.,]\d{2})(?!\d)", chunk):
            try:
                v = float(raw.replace(",", "."))
                if 1.01 <= v <= 30.0:
                    vals.append(v)
            except Exception:
                pass
        # Procura três odds consecutivas plausíveis. Evita horários como 18.30
        # exigindo que a soma implícita seja compatível com um mercado 1X2.
        for j in range(max(0, len(vals)-8), len(vals)-2):
            trio = vals[j:j+3]
            if len(trio) < 3:
                continue
            inv = sum(1/x for x in trio)
            if 0.88 <= inv <= 1.35:
                score = sh + sa - abs(inv-1.06)*0.5
                cand = (score, trio)
                if best is None or cand[0] > best[0]:
                    best = cand
    if not best:
        return None
    h, d, a = best[1]
    inv = [1/h, 1/d, 1/a]
    z = sum(inv)
    return {
        "home_odd": h, "draw_odd": d, "away_odd": a,
        "home_prob": inv[0]/z*100, "draw_prob": inv[1]/z*100, "away_prob": inv[2]/z*100,
        "overround": (z-1)*100,
    }


@st.cache_data(ttl=300, show_spinner=False)
def find_sofascore_event(home, away, day_iso=None, search_days=2):
    """Localiza o evento EXATO no calendário público do SofaScore.

    A busca é feita por nomes das duas equipes e respeita mandante/visitante.
    Isso evita o problema de capturar três números pertencentes a outro jogo da
    mesma página — a causa das odds incorretas que apareciam anteriormente.
    """
    base_day = date.fromisoformat(day_iso) if day_iso else datetime.now(BRASILIA_TZ).date()
    wanted_h, wanted_a = str(home or ''), str(away or '')
    candidates = []
    # Hoje primeiro; depois dias próximos para partidas carregadas antes/depois.
    offsets = [0]
    for n in range(1, max(0, int(search_days)) + 1):
        offsets.extend([-n, n])
    for off in offsets:
        d = base_day + timedelta(days=off)
        url = f"https://api.sofascore.com/api/v1/sport/football/scheduled-events/{d.isoformat()}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code != 200:
                continue
            payload = r.json()
            for ev in payload.get("events", []):
                eh = str((ev.get("homeTeam") or {}).get("name") or "")
                ea = str((ev.get("awayTeam") or {}).get("name") or "")
                # Para recuperação estatística usamos a resolução tolerante,
                # capaz de casar Athletico-PR/Athletico Paranaense, FC/prefixos etc.
                # Também exclui explicitamente feminino/base/reserva.
                hobj, aobj = ev.get("homeTeam") or {}, ev.get("awayTeam") or {}
                if str(hobj.get("gender") or "M").upper() not in ("M", "MALE"):
                    continue
                if str(aobj.get("gender") or "M").upper() not in ("M", "MALE"):
                    continue
                if SECONDARY_TEAM_RE.search(eh) or SECONDARY_TEAM_RE.search(ea):
                    continue
                sh = _gm_recovery_team_similarity(wanted_h, eh)
                sa = _gm_recovery_team_similarity(wanted_a, ea)
                if sh >= 0.72 and sa >= 0.72:
                    candidates.append((sh + sa, ev, d.isoformat()))
        except Exception:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    score, ev, event_day = candidates[0]
    # Exige correspondência forte das duas pontas para não trocar equipes homônimas.
    if score < 1.55:
        return None
    return {
        "event_id": ev.get("id"),
        "date": event_day,
        "home": (ev.get("homeTeam") or {}).get("name"),
        "away": (ev.get("awayTeam") or {}).get("name"),
        "home_id": (ev.get("homeTeam") or {}).get("id"),
        "away_id": (ev.get("awayTeam") or {}).get("id"),
        "start_timestamp": ev.get("startTimestamp"),
        "status": (ev.get("status") or {}).get("type") or (ev.get("status") or {}).get("description"),
        "tournament": ((ev.get("tournament") or {}).get("uniqueTournament") or {}).get("name") or (ev.get("tournament") or {}).get("name"),
    }


def _decimal_odd(value):
    try:
        if value is None:
            return None
        if isinstance(value, str) and '/' in value:
            a, b = value.split('/', 1)
            return 1.0 + float(a) / float(b)
        v = float(str(value).replace(',', '.'))
        return v if 1.01 <= v <= 50.0 else None
    except Exception:
        return None


def _extract_sofascore_1x2(payload):
    """Extrai SOMENTE o mercado 1X2 de tempo regulamentar do SofaScore.

    O endpoint também devolve 1X2 do 1º tempo e outros mercados com escolhas
    1/X/2. Antes o código varria todos eles e podia selecionar a tríade errada
    apenas porque a margem parecia plausível. Agora o mercado precisa estar
    explicitamente identificado como Full time/tempo regulamentar (ou marketId 1
    com period Full-time), evitando trocar a odd do jogo por outro 1X2.
    """
    found = []

    def is_full_time_1x2(obj):
        if not isinstance(obj, dict):
            return False
        name = str(obj.get("marketName") or obj.get("name") or "").strip().lower()
        group = str(obj.get("group") or "").strip().lower()
        period = str(obj.get("period") or "").strip().lower()
        mid = obj.get("marketId", obj.get("id"))
        full_names = {"full time", "full-time", "match result", "resultado final", "1x2"}
        full_periods = {"full time", "full-time", "ft", "match"}
        # SofaScore tradicional: marketId=1 / marketName='Full time'.
        if str(mid) == "1" and (not period or period in full_periods):
            return True
        if name in full_names and (not period or period in full_periods):
            return True
        if group == "1x2" and period in full_periods:
            return True
        return False

    def walk(obj):
        if isinstance(obj, dict):
            choices = obj.get("choices")
            if isinstance(choices, list) and is_full_time_1x2(obj):
                vals = {}
                for ch in choices:
                    if not isinstance(ch, dict):
                        continue
                    cname = str(ch.get("name") or ch.get("choice") or ch.get("label") or "").strip().upper()
                    if cname not in {"1", "X", "2"}:
                        continue
                    odd = None
                    # O SofaScore oficial normalmente entrega fractionalValue.
                    # Suportamos também estruturas com decimalValue/value.decimal.
                    nested_value = ch.get("value") if isinstance(ch.get("value"), dict) else {}
                    candidates = [
                        ch.get("decimalValue"), ch.get("decimal"), nested_value.get("decimal"),
                        ch.get("fractionalValue"), ch.get("odds"),
                    ]
                    for raw in candidates:
                        odd = _decimal_odd(raw)
                        if odd is not None:
                            break
                    if odd is not None:
                        vals[cname] = odd
                if set(vals) == {"1", "X", "2"}:
                    h, d, a = vals["1"], vals["X"], vals["2"]
                    z = 1/h + 1/d + 1/a
                    # Margem plausível para 1X2 pré-jogo. Fora disso, rejeita.
                    if 0.96 <= z <= 1.22:
                        found.append((abs(z - 1.055), h, d, a))
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(payload)
    if not found:
        return None
    _, h, d, a = sorted(found)[0]
    inv = [1/h, 1/d, 1/a]
    z = sum(inv)
    return {
        "home_odd": h, "draw_odd": d, "away_odd": a,
        "home_prob": inv[0]/z*100, "draw_prob": inv[1]/z*100, "away_prob": inv[2]/z*100,
        "overround": (z-1)*100,
    }


@st.cache_data(ttl=300, show_spinner=False)
def fetch_sofascore_h2h(home, away):
    """H2H público e dinâmico para o confronto carregado, sem chave de API."""
    event = find_sofascore_event(home, away)
    if not event or not event.get("event_id"):
        return []
    url = f"https://api.sofascore.com/api/v1/event/{event['event_id']}/h2h/events"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            return []
        payload = r.json()
        rows = []
        for ev in payload.get("events", []):
            status = str((ev.get("status") or {}).get("type") or '').lower()
            hs, aas = ev.get("homeScore") or {}, ev.get("awayScore") or {}
            hg = hs.get("normaltime", hs.get("current"))
            ag = aas.get("normaltime", aas.get("current"))
            if status not in {"finished", "ended"} or hg is None or ag is None:
                continue
            ts = ev.get("startTimestamp")
            dt = datetime.fromtimestamp(ts, BRASILIA_TZ).date().isoformat() if ts else ""
            rows.append({
                "home": (ev.get("homeTeam") or {}).get("name"),
                "away": (ev.get("awayTeam") or {}).get("name"),
                "hg": float(hg), "ag": float(ag), "date": dt,
                "source": "SofaScore H2H",
            })
        rows.sort(key=lambda x: x.get("date") or "")
        return rows[-10:]
    except Exception:
        return []


@st.cache_data(ttl=180, show_spinner=False)
def fetch_public_market_odds(home, away, league_name):
    """Busca 1X2 público ligado ao ID EXATO do evento, sem chave.

    OddsPortal por varredura de texto foi removido como fonte automática porque
    páginas agregadoras podem colocar várias odds próximas e gerar associação ao
    jogo errado. Se o evento/mercado exato não puder ser confirmado, retornamos
    indisponível em vez de mostrar uma cotação incorreta.
    """
    event = find_sofascore_event(home, away)
    if not event or not event.get("event_id"):
        return {"error": "evento público exato não localizado"}
    eid = event["event_id"]
    urls = [
        f"https://api.sofascore.com/api/v1/event/{eid}/odds/1/all",
        f"https://www.sofascore.com/api/v1/event/{eid}/odds/1/all",
    ]
    for url in urls:
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code != 200:
                continue
            parsed = _extract_sofascore_1x2(r.json())
            if parsed:
                parsed.update({
                    "source": "SofaScore (mercado público)",
                    "source_url": url,
                    "event_id": eid,
                    "event_home": event.get("home"),
                    "event_away": event.get("away"),
                    "updated_at": datetime.now(BRASILIA_TZ).isoformat(timespec="minutes"),
                })
                return parsed
        except Exception:
            continue
    return {"error": "mercado 1X2 exato indisponível para este evento"}


def adaptive_market_weight(model_probs, moneyline, base_weight):
    """Usa o mercado como âncora de realidade quando há inversão severa.

    A odd nunca vira 100% do modelo. Porém, se o consenso 1X2 aponta um favorito
    claro e o GM SCORE o coloca atrás por margem grande, aumentamos a calibração
    somente na probabilidade FINAL exibida. A odd justa/valor continua vindo da
    probabilidade independente, preservada em model_home/model_draw/model_away.
    """
    if not model_probs or not moneyline:
        return float(base_weight)
    m = [float(model_probs[k]) for k in ("home", "draw", "away")]
    q = [float(moneyline[f"{k}_prob"]) for k in ("home", "draw", "away")]
    fav = max(range(3), key=lambda i: q[i])
    second_market = sorted(q, reverse=True)[1]
    market_gap = q[fav] - second_market
    model_rank = sorted(range(3), key=lambda i: m[i], reverse=True)
    inversion = model_rank[0] != fav
    deficit = q[fav] - m[fav]

    # Favorito de mercado bem definido + modelo invertido: forte guardrail.
    if inversion and market_gap >= 12 and deficit >= 18:
        return 0.84
    if inversion and market_gap >= 8 and deficit >= 12:
        return 0.72
    if inversion and deficit >= 10:
        return max(float(base_weight), 0.58)
    return float(base_weight)


def calibrate_result_with_market(model_probs, moneyline, market_weight):
    """Calibra o 1X2 com o mercado público sem transformar a odd no modelo.

    Além da mistura ponderada, há um guardrail de coerência: quando o mercado
    sem margem tem um favorito realmente claro (>=48% e >=12 p.p. sobre o
    segundo), uma amostra estatística curta não pode terminar apontando o lado
    oposto como favorito. Isso protege confrontos interligas como Real Madrid ×
    Inter e Porto × Manchester City sem simplesmente copiar as cotações.
    """
    if not model_probs or not moneyline:
        return model_probs
    w = max(0.0, min(float(market_weight), 0.90))
    out = dict(model_probs)
    out["model_home"] = float(model_probs["home"])
    out["model_draw"] = float(model_probs["draw"])
    out["model_away"] = float(model_probs["away"])
    keys = ("home", "draw", "away")
    market = [float(moneyline[f"{k}_prob"]) for k in keys]
    independent = [out[f"model_{k}"] for k in keys]
    final = [independent[i]*(1-w) + market[i]*w for i in range(3)]

    mfav = max(range(3), key=lambda i: market[i])
    second = sorted(market, reverse=True)[1]
    market_gap = market[mfav] - second
    ffav = max(range(3), key=lambda i: final[i])

    # Consenso público forte é tratado como validação externa de realidade, não
    # como palpite. Quando o mercado sem margem tem favorito >=48% e vantagem
    # >=12 p.p., o GM pode discordar da intensidade, mas não inverter a equipe
    # favorita por ruído de amostra, H2H antigo ou comparação entre ligas.
    # O piso fica alguns pontos abaixo do mercado para preservar independência.
    if market[mfav] >= 48.0 and market_gap >= 12.0:
        floor = max(46.0, market[mfav] - 6.0)
        if final[mfav] < floor or ffav != mfav:
            target = floor
            rest_idx = [i for i in range(3) if i != mfav]
            rest_total = max(sum(final[i] for i in rest_idx), 1e-9)
            remaining = 100.0 - target
            final[mfav] = target
            for i in rest_idx:
                final[i] = remaining * final[i] / rest_total
            out["market_guardrail"] = True

    total = sum(final)
    final = [v / total * 100.0 for v in final]
    for i, k in enumerate(keys):
        out[k] = final[i]
    out["market_calibrated"] = True
    out["market_weight"] = w
    return out


def value_reading(model_probability_pct, current_odd):
    p = max(min(float(model_probability_pct)/100.0, .995), .005)
    fair = 1.0/p
    edge = p*float(current_odd) - 1.0
    if edge >= 0.05:
        status, color = "🟢 Valor", "#16a34a"
    elif edge <= -0.05:
        status, color = "🔴 Sem valor", "#dc2626"
    else:
        status, color = "🟡 Neutra", "#b7791f"
    return {"fair_odd": fair, "edge": edge*100, "status": status, "color": color}


def render_market_value_panel(team_a, team_b, probs, moneyline):
    if not moneyline or not probs:
        return
    st.markdown("### 💹 Mercado × odd justa GM SCORE")
    st.caption("Odd justa = probabilidade final do GM SCORE. As cotações públicas são usadas como referência e validação do favoritismo, sem substituir a análise estatística.")
    # A leitura de valor precisa ser coerente com a chance final mostrada acima.
    # Mantemos a probabilidade independente apenas para diagnóstico interno.
    model = {
        "home": float(probs.get("home")),
        "draw": float(probs.get("draw")),
        "away": float(probs.get("away")),
    }
    independent = {
        "home": probs.get("model_home"),
        "draw": probs.get("model_draw"),
        "away": probs.get("model_away"),
    }
    labels = [("home", f"🏠 {team_a}"), ("draw", "🤝 Empate"), ("away", f"✈️ {team_b}")]
    cols = st.columns(3)
    for col, (key, label) in zip(cols, labels):
        odd = moneyline[f"{key}_odd"]
        marketp = moneyline[f"{key}_prob"]
        vr = value_reading(model[key], odd)
        with col:
            st.markdown(f"**{label}**")
            st.markdown(f"Mercado: **{odd:.2f}**")
            st.caption(f"Mercado sem margem: {marketp:.1f}% · GM final: {model[key]:.1f}%")
            st.markdown(f'<span style="font-weight:700;color:{vr["color"]}">{vr["status"]}</span> · justa **{vr["fair_odd"]:.2f}** · edge **{vr["edge"]:+.1f}%**', unsafe_allow_html=True)
    st.caption(f"📌 Avaliação: cruzamos desempenho atual, força global, nível da liga e produção ofensiva/defensiva com a referência pública de mercado ({moneyline.get('source','mercado público')}). A odd justa usa exatamente a probabilidade final mostrada pelo GM SCORE.")

def to_num(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, str):
        value = value.replace("%", "").replace(",", ".").strip()
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


HIDDEN_OPP_METRICS = [
    "Finalizações contra", "Chutes no alvo contra",
    "Escanteios 1T", "Escanteios 2T",
    "Finalizações 1T", "Chutes no alvo 1T",
    "Gols 1T", "Gols 2T",
]


def empty_team(name):
    return {
        "Time": name,
        "Jogos": 0,
        "Gols pró": 0.0,
        "Gols contra": 0.0,
        **{m: 0.0 for m in DISPLAY_METRICS if m not in ("Jogos", "Gols pró", "Gols contra")},
        **{f"_n_{m}": 0 for m in DISPLAY_METRICS if m not in ("Jogos", "Gols pró", "Gols contra")},
        **{m: 0.0 for m in HIDDEN_OPP_METRICS},
        **{f"_n_{m}": 0 for m in HIDDEN_OPP_METRICS},
    }


def add_metric(team, metric, value):
    value = to_num(value)
    if value is not None:
        team[metric] += value
        team[f"_n_{metric}"] += 1


def finish_averages(acc):
    rows = []
    for item in acc.values():
        games = item["Jogos"]
        if not games:
            continue
        row = {"Time": item["Time"], "Jogos": games}
        row["Gols pró"] = round(item["Gols pró"] / games, 2)
        row["Gols contra"] = round(item["Gols contra"] / games, 2)
        for metric in DISPLAY_METRICS:
            if metric in ("Jogos", "Gols pró", "Gols contra"):
                continue
            n = item.get(f"_n_{metric}", 0)
            row[metric] = round(item[metric] / n, 2) if n else None
            # Cobertura fica disponível internamente para o motor de confiança,
            # sem aparecer na tabela do cliente.
            row[f"_n_{metric}"] = int(n)
        for metric in HIDDEN_OPP_METRICS:
            n = item.get(f"_n_{metric}", 0)
            row[metric] = round(item[metric] / n, 2) if n else None
            row[f"_n_{metric}"] = int(n)
        rows.append(row)
    return pd.DataFrame(rows).sort_values("Time").reset_index(drop=True) if rows else pd.DataFrame()


# ============================================================
# EUROPA - FOOTBALL-DATA
# ============================================================
@st.cache_data(ttl=21600, show_spinner=False)
def load_football_data(code, year):
    sc = season_code(year)
    r = request_first([
        f"https://www.football-data.co.uk/mmz4281/{sc}/{code}.csv",
        f"https://football-data.co.uk/mmz4281/{sc}/{code}.csv",
    ])
    df = read_csv_bytes(r.content)
    required = {"HomeTeam", "AwayTeam", "FTHG", "FTAG"}
    if not required.issubset(df.columns):
        raise RuntimeError("arquivo sem resultados reconhecíveis")

    for c in ["FTHG", "FTAG", "HTHG", "HTAG", *{x for pair in FD_STATS.values() for x in pair}]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna(subset=["HomeTeam", "AwayTeam", "FTHG", "FTAG"]).copy()
    if "Date" in df.columns:
        parsed_dates = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")
        df.attrs["updated_until"] = parsed_dates.max() if parsed_dates.notna().any() else None
    else:
        df.attrs["updated_until"] = None
    return df


def averages_football_data(df, last_games_per_team=0):
    teams = sorted(set(df["HomeTeam"]).union(df["AwayTeam"]))
    acc = {t: empty_team(t) for t in teams}

    # Limite EXATO por equipe. A implementação anterior criava a união dos
    # últimos N índices de todos os clubes e podia acabar usando mais de N jogos
    # para um time. Aqui cada lado só recebe a partida se ela pertence à sua
    # própria janela recente.
    allowed = None
    if last_games_per_team:
        allowed = {}
        for team in teams:
            mask = (df["HomeTeam"] == team) | (df["AwayTeam"] == team)
            allowed[team] = set(df[mask].tail(last_games_per_team).index.tolist())

    for idx, g in df.iterrows():
        home, away = g["HomeTeam"], g["AwayTeam"]
        hg, ag = float(g["FTHG"]), float(g["FTAG"])
        H, A = acc[home], acc[away]
        use_h = allowed is None or idx in allowed[home]
        use_a = allowed is None or idx in allowed[away]
        if use_h:
            H["Jogos"] += 1
            H["Gols pró"] += hg; H["Gols contra"] += ag
            for metric, (hc, ac) in FD_STATS.items():
                if hc in df.columns: add_metric(H, metric, g.get(hc))
            # Para projeção ofensiva real, também guardamos quanto o time CEDEU
            # ao adversário. Isso evita tratar duas médias ofensivas iguais como
            # se implicassem uma divisão 50/50 do confronto futuro.
            if "AS" in df.columns: add_metric(H, "Finalizações contra", g.get("AS"))
            if "AST" in df.columns: add_metric(H, "Chutes no alvo contra", g.get("AST"))
        if use_a:
            A["Jogos"] += 1
            A["Gols pró"] += ag; A["Gols contra"] += hg
            for metric, (hc, ac) in FD_STATS.items():
                if ac in df.columns: add_metric(A, metric, g.get(ac))
            if "HS" in df.columns: add_metric(A, "Finalizações contra", g.get("HS"))
            if "HST" in df.columns: add_metric(A, "Chutes no alvo contra", g.get("HST"))
    out = finish_averages(acc)
    out.attrs["updated_until"] = df.attrs.get("updated_until")
    raw_matches = []
    for _, g in df.iterrows():
        dt = None
        if "Date" in df.columns:
            parsed = pd.to_datetime(g.get("Date"), dayfirst=True, errors="coerce")
            dt = None if pd.isna(parsed) else parsed
        raw_matches.append({
            "home": str(g["HomeTeam"]), "away": str(g["AwayTeam"]),
            "hg": float(g["FTHG"]), "ag": float(g["FTAG"]), "date": dt,
            "ht_hg": None if "HTHG" not in df.columns or pd.isna(g.get("HTHG")) else float(g.get("HTHG")),
            "ht_ag": None if "HTAG" not in df.columns or pd.isna(g.get("HTAG")) else float(g.get("HTAG")),
        })
    out.attrs["matches"] = raw_matches
    return out


# ============================================================
# BRASILEIRÃO SÉRIE A - DATASET ESTÁTICO CONSOLIDADO
# ============================================================
BR_MATCH_URLS = [
    "https://raw.githubusercontent.com/leeofernandes1980/brasileirao-dataset/main/campeonato-brasileiro-full.csv",
    "https://github.com/leeofernandes1980/brasileirao-dataset/raw/refs/heads/main/campeonato-brasileiro-full.csv",
]
BR_STATS_URLS = [
    "https://raw.githubusercontent.com/leeofernandes1980/brasileirao-dataset/main/campeonato-brasileiro-estatisticas-full.csv",
    "https://github.com/leeofernandes1980/brasileirao-dataset/raw/refs/heads/main/campeonato-brasileiro-estatisticas-full.csv",
]

@st.cache_data(ttl=21600, show_spinner=False)
def load_brasileirao_dataset(year):
    matches = read_csv_bytes(request_first(BR_MATCH_URLS).content)
    stats = read_csv_bytes(request_first(BR_STATS_URLS).content)

    matches.columns = [clean_col(c) for c in matches.columns]
    stats.columns = [clean_col(c) for c in stats.columns]

    # Localiza nomes de colunas tolerando pequenas mudanças no dataset.
    id_col = next((c for c in ["id", "partida_id"] if c in matches.columns), None)
    date_col = next((c for c in ["data", "date"] if c in matches.columns), None)
    home_col = next((c for c in ["mandante", "home_team"] if c in matches.columns), None)
    away_col = next((c for c in ["visitante", "away_team"] if c in matches.columns), None)
    hg_col = next((c for c in ["mandante_placar", "gols_mandante", "home_goals"] if c in matches.columns), None)
    ag_col = next((c for c in ["visitante_placar", "gols_visitante", "away_goals"] if c in matches.columns), None)
    sid_col = next((c for c in ["partida_id", "id"] if c in stats.columns), None)
    club_col = next((c for c in ["clube", "time", "team"] if c in stats.columns), None)

    if not all([id_col, date_col, home_col, away_col, hg_col, ag_col, sid_col, club_col]):
        raise RuntimeError("estrutura do dataset brasileiro mudou")

    matches["_date"] = pd.to_datetime(matches[date_col], dayfirst=True, errors="coerce")
    matches = matches[matches["_date"].dt.year == year].copy()
    matches[hg_col] = pd.to_numeric(matches[hg_col], errors="coerce")
    matches[ag_col] = pd.to_numeric(matches[ag_col], errors="coerce")
    matches = matches.dropna(subset=[home_col, away_col, hg_col, ag_col])
    if matches.empty:
        raise RuntimeError("sem partidas disponíveis para esta temporada")

    ids = set(matches[id_col].astype(str))
    stats["_id"] = stats[sid_col].astype(str)
    stats = stats[stats["_id"].isin(ids)].copy()

    mapping = {
        "Chutes": "Finalizações",
        "Chutes a gol": "Chutes no alvo",
        "Posse de bola": "Posse (%)",
        "Passes": "Passes",
        "precisao_passes": "Precisão passes (%)",
        "Faltas": "Faltas",
        "cartao_amarelo": "Amarelos",
        "cartao_vermelho": "Vermelhos",
        "Impedimentos": "Impedimentos",
        "Escanteios": "Escanteios",
    }
    mapping = {clean_col(k): v for k, v in mapping.items()}

    teams = sorted(set(matches[home_col]).union(matches[away_col]))
    acc = {t: empty_team(t) for t in teams}

    for _, g in matches.iterrows():
        h, a = str(g[home_col]), str(g[away_col])
        hg, ag = float(g[hg_col]), float(g[ag_col])
        H, A = acc[h], acc[a]
        H["Jogos"] += 1; A["Jogos"] += 1
        H["Gols pró"] += hg; H["Gols contra"] += ag
        A["Gols pró"] += ag; A["Gols contra"] += hg

    for _, s in stats.iterrows():
        club = str(s[club_col])
        if club not in acc:
            continue
        for raw, metric in mapping.items():
            if raw in stats.columns:
                add_metric(acc[club], metric, s.get(raw))

    out = finish_averages(acc)
    out.attrs["updated_until"] = matches["_date"].max() if matches["_date"].notna().any() else None
    out.attrs["matches"] = [
        {"home": str(g[home_col]), "away": str(g[away_col]),
         "hg": float(g[hg_col]), "ag": float(g[ag_col]), "date": g["_date"]}
        for _, g in matches.iterrows()
    ]
    return out


# ============================================================
# COMPETIÇÕES ABERTAS - RESULTADOS (SEM INVENTAR STATS AUSENTES)
# ============================================================
def score_ft(score):
    if isinstance(score, list) and len(score) >= 2:
        return score[0], score[1]
    if isinstance(score, dict):
        ft = score.get("ft")
        if isinstance(ft, list) and len(ft) >= 2:
            return ft[0], ft[1]
    return None, None

@st.cache_data(ttl=21600, show_spinner=False)
def load_open_json(urls):
    r = request_first(urls)
    try:
        return r.json()
    except ValueError as exc:
        raise RuntimeError("JSON público inválido") from exc


def averages_open_matches(matches):
    acc = {}
    match_dates = []
    standardized = []
    for m in matches:
        h = m.get("team1") or m.get("home")
        a = m.get("team2") or m.get("away")
        hg, ag = score_ft(m.get("score"))
        if not h or not a or hg is None or ag is None:
            continue
        try:
            hg, ag = float(hg), float(ag)
        except (TypeError, ValueError):
            continue

        raw_date = m.get("date") or m.get("datetime") or m.get("played_at")
        if raw_date:
            dt = pd.to_datetime(raw_date, errors="coerce", dayfirst=True)
            if not pd.isna(dt):
                match_dates.append(dt)

        standardized.append({"home": str(h), "away": str(a), "hg": hg, "ag": ag, "date": dt if raw_date and not pd.isna(dt) else None})
        acc.setdefault(h, empty_team(h)); acc.setdefault(a, empty_team(a))
        H, A = acc[h], acc[a]
        H["Jogos"] += 1; A["Jogos"] += 1
        H["Gols pró"] += hg; H["Gols contra"] += ag
        A["Gols pró"] += ag; A["Gols contra"] += hg

    out = finish_averages(acc)
    out.attrs["updated_until"] = max(match_dates) if match_dates else None
    out.attrs["matches"] = standardized
    return out



@st.cache_data(ttl=21600, show_spinner=False)
def load_extra_football_data(code, year):
    """Carrega ligas extras do Football-Data (arquivo único com várias temporadas).
    Usa nomes de colunas flexíveis e mantém apenas partidas do ano vigente.
    """
    urls = [
        f"https://www.football-data.co.uk/new/{code}.csv",
        f"https://www.football-data.co.uk/{code.lower()}/new/{code}.csv",
    ]
    r = request_first(urls)
    df = read_csv_bytes(r.content)
    if df.empty:
        raise RuntimeError("arquivo sem partidas")

    # Normaliza os nomes usados historicamente pelas ligas extras.
    aliases = {
        "Home": "HomeTeam", "Home Team": "HomeTeam", "HomeTeam": "HomeTeam",
        "Away": "AwayTeam", "Away Team": "AwayTeam", "AwayTeam": "AwayTeam",
        "HG": "FTHG", "Home Goals": "FTHG", "FTHG": "FTHG",
        "AG": "FTAG", "Away Goals": "FTAG", "FTAG": "FTAG",
    }
    rename = {c: aliases[c] for c in df.columns if c in aliases}
    df = df.rename(columns=rename)
    required = {"HomeTeam", "AwayTeam", "FTHG", "FTAG"}
    if not required.issubset(df.columns):
        raise RuntimeError("formato de dados não reconhecido")

    date_col = next((c for c in ["Date", "DATE", "MatchDate"] if c in df.columns), None)
    if date_col:
        dt = pd.to_datetime(df[date_col], dayfirst=True, errors="coerce")
        current = df[dt.dt.year == year].copy()
        current["Date"] = dt[dt.dt.year == year]
    else:
        current = df.copy()

    current["FTHG"] = pd.to_numeric(current["FTHG"], errors="coerce")
    current["FTAG"] = pd.to_numeric(current["FTAG"], errors="coerce")
    current = current.dropna(subset=["HomeTeam", "AwayTeam", "FTHG", "FTAG"])
    if current.empty:
        raise RuntimeError(f"sem partidas de {year} nesta fonte")
    return current

def parse_openfootball_txt(text):
    matches = []
    # Formato mais comum: Time A v Time B 2-1 ...
    pattern = re.compile(r"^(?:\s*\d{1,2}:\d{2}\s+)?(.+?)\s+v\s+(.+?)\s+(\d+)\s*-\s*(\d+)(?:\s|$)")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "=", "▪")) or "[cancelled]" in line:
            continue
        m = pattern.search(line)
        if m:
            matches.append({
                "team1": m.group(1).strip(),
                "team2": m.group(2).strip(),
                "score": {"ft": [int(m.group(3)), int(m.group(4))]},
            })
    return matches


# ============================================================
# FALLBACK DE TEMPORADA ATUAL - LIVESCORE.MOBI
# ============================================================
# Algumas competições não existem no football.json de 2026/2026-27. Em vez
# de recuar para uma temporada antiga, usamos páginas públicas da competição
# vigente para recuperar a lista de equipes e os totais atuais de gols.
LIVESCORE_SEASON_URLS = {
    # Fallback de cobertura para TODAS as ligas domésticas. O Football-Data
    # continua prioritário onde oferece estatísticas detalhadas; estas páginas
    # entram apenas quando a fonte principal falhar ou vier incompleta.
    "Inglaterra - Premier League": ["https://www.livescore.mobi/football/england/premier-league/"],
    "Espanha - La Liga": ["https://www.livescore.mobi/football/spain/laliga/"],
    "Itália - Serie A": ["https://www.livescore.mobi/football/italy/serie-a/"],
    "Alemanha - Bundesliga": ["https://www.livescore.mobi/football/germany/bundesliga/"],
    "França - Ligue 1": ["https://www.livescore.mobi/football/france/ligue-1/"],
    "Portugal - Liga Portugal": ["https://www.livescore.mobi/football/portugal/primeira-liga/"],
    "Holanda - Eredivisie": ["https://www.livescore.mobi/football/holland/eredivisie/"],
    "Escócia - Premiership": ["https://www.livescore.mobi/football/scotland/scotland-premiership/"],
    "Turquia - Süper Lig": ["https://www.livescore.mobi/football/turkey/super-lig/"],
    "Brasil - Série A": ["https://www.livescore.mobi/football/brazil/serie-a/"],
    "CONMEBOL Libertadores": ["https://www.livescore.mobi/football/copa-libertadores/"],
    "Brasil - Série B": [
        "https://www.cbf.com.br/futebol-brasileiro/tabelas/campeonato-brasileiro/serie-b/2026",
        "https://www.livescore.mobi/football/brazil/serie-b/",
    ],
    "Arábia Saudita - Saudi Pro League": [
        "https://www.livescore.mobi/football/saudi-arabia/saudi-professional-league/",
        "https://www.livescore.mobi/football/saudi-arabia/pro-league/",
    ],
    "Estados Unidos - MLS": [
        "https://www.livescore.mobi/football/usa/major-league-soccer/",
    ],
    "Argentina - Liga Profesional": [
        "https://www.livescore.mobi/football/argentina/liga-profesional-clausura/",
        "https://www.livescore.mobi/football/argentina/liga-profesional/",
    ],
    "México - Liga MX": [
        "https://www.foxsports.com/soccer/liga-mx/standings",
        "https://www.livescore.mobi/football/mexico/liga-mx-apertura/",
        "https://www.livescore.mobi/football/mexico/liga-mx/",
    ],
    "Colômbia - Primera A": [
        "https://www.colombia.com/futbol/liga-colombiana/tabla-de-posiciones",
        "https://www.livescore.mobi/football/colombia/primera-a-clausura/",
        "https://www.livescore.mobi/football/colombia/primera-a/",
    ],
    "CONMEBOL Sul-Americana": [
        "https://www.livescore.mobi/football/copa-sudamericana/",
    ],
    "UEFA Champions League": [
        "https://www.livescore.mobi/football/champions-league/league-stage/",
        "https://www.livescore.mobi/football/champions-league/qualification/",
    ],
    "UEFA Europa League": [
        "https://www.livescore.mobi/football/europa-league/league-stage/",
        "https://www.livescore.mobi/football/europa-league/qualification/",
    ],
    "UEFA Conference League": [
        "https://www.livescore.mobi/football/conference-league/league-stage/",
        "https://www.livescore.mobi/football/conference-league/qualification/",
    ],
}


class _HTMLTableParser(HTMLParser):
    """Parser mínimo de tabelas HTML, sem dependências extras."""
    def __init__(self):
        super().__init__()
        self.tables = []
        self._table = None
        self._row = None
        self._cell = None
        self._cell_parts = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("th", "td") and self._row is not None:
            self._cell = tag
            self._cell_parts = []

    def handle_data(self, data):
        if self._cell is not None:
            txt = re.sub(r"\s+", " ", str(data)).strip()
            if txt:
                self._cell_parts.append(txt)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("th", "td") and self._cell is not None:
            self._row.append(" ".join(self._cell_parts).strip())
            self._cell = None
            self._cell_parts = []
        elif tag == "tr" and self._row is not None:
            if self._row:
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None


class _MatchAnchorParser(HTMLParser):
    """Captura links de jogos concluídos exibidos na página da competição."""
    def __init__(self):
        super().__init__()
        self.links = []
        self._href = None
        self._parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self._href = dict(attrs).get("href")
            self._parts = []

    def handle_data(self, data):
        if self._href is not None:
            txt = re.sub(r"\s+", " ", str(data)).strip()
            if txt:
                self._parts.append(txt)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._parts).strip()))
            self._href = None
            self._parts = []


def _clean_standings_team(text):
    """Remove rótulos de classificação que podem vir junto ao nome do clube."""
    txt = re.sub(r"\s+", " ", str(text or "")).strip()
    txt = re.sub(r"^\d+\s*", "", txt)
    phrases = [
        "Championship play-off (additional spot(s) for Copa Libertadores)",
        "Qualification to 1/8 finals", "Qualification to knockout stage",
        "Championship play-off", "Promotion play-off", "Promotion", "Relegation",
        "Next stage", "Copa Libertadores", "CONCACAF Champions League",
    ]
    changed = True
    while changed:
        changed = False
        for phrase in phrases:
            if txt.lower().startswith(phrase.lower()):
                txt = txt[len(phrase):].strip(" -–|:")
                changed = True
    return txt.strip()


def _standings_rows_from_html(html):
    parser = _HTMLTableParser()
    parser.feed(html)
    rows_out = []
    for table in parser.tables:
        header_idx = None
        header = None
        for i, row in enumerate(table[:5]):
            low_cells = [str(x).strip().lower() for x in row]
            joined = " | ".join(low_cells)
            has_team = any(("team name" in x) or x in {"team", "club", "equipo", "equipe", "classificação", "clasificacion"} for x in low_cells)
            has_played = any(("played" in x) or x in {"p", "j", "pj"} or "jogos" in x or "partidos jugados" in x for x in low_cells)
            has_for = any(("goals for" in x) or x in {"f", "gf", "gp"} or "gols pr" in x or "goles a favor" in x for x in low_cells)
            has_against = any(("goals against" in x) or x in {"a", "ga", "gc"} or "gols contr" in x or "goles en contra" in x for x in low_cells)
            if has_team and has_played and has_for and has_against:
                header_idx, header = i, row
                break
        if header_idx is None:
            continue

        def find_col(tokens):
            for idx, name in enumerate(header):
                low = name.lower()
                if any(tok in low for tok in tokens):
                    return idx
            return None

        team_i = find_col(["team name", "team", "club", "equipo", "equipe", "classificação", "clasificacion"])
        played_i = find_col(["p played", "played", "partidos jugados", "jogos", "pj", "j"])
        gf_i = find_col(["f goals for", "goals for", "goles a favor", "gols pr", "gf", "gp"])
        ga_i = find_col(["a goals against", "goals against", "goles en contra", "gols contr", "ga", "gc"])
        pts_i = find_col(["pts points", "points", "pontos", "pts"])
        yc_i = find_col(["cartões amarelos", "cartoes amarelos", "yellow cards", "ca"])
        rc_i = find_col(["cartões vermelhos", "cartoes vermelhos", "red cards", "cv"])
        if None in (team_i, played_i, gf_i, ga_i):
            continue

        for row in table[header_idx + 1:]:
            if max(team_i, played_i, gf_i, ga_i) >= len(row):
                continue
            team = _clean_standings_team(row[team_i])
            games = to_num(row[played_i])
            gf = to_num(row[gf_i])
            ga = to_num(row[ga_i])
            pts = to_num(row[pts_i]) if pts_i is not None and pts_i < len(row) else None
            yc = to_num(row[yc_i]) if yc_i is not None and yc_i < len(row) else None
            rc = to_num(row[rc_i]) if rc_i is not None and rc_i < len(row) else None
            if not team or games is None or gf is None or ga is None:
                continue
            rows_out.append({"team": team, "games": int(games), "gf": float(gf), "ga": float(ga), "pts": pts, "yc": yc, "rc": rc})
    return rows_out


def _completed_matches_from_html(html):
    parser = _MatchAnchorParser()
    parser.feed(html)
    matches = []
    seen = set()
    for href, label in parser.links:
        if not href or "-vs-" not in href:
            continue
        # Só partidas com placar concluído/registrado.
        sm = re.search(r"(\d+)\s*-\s*(\d+)", label or "")
        if not sm:
            continue
        hm = re.search(r"/([^/]+)-vs-([^/]+)/\d+/?", href)
        if not hm:
            continue
        home = _pretty_slug_team(hm.group(1))
        away = _pretty_slug_team(hm.group(2))
        hg, ag = int(sm.group(1)), int(sm.group(2))
        key = (fixture_team_key(home), fixture_team_key(away), hg, ag)
        if key in seen:
            continue
        seen.add(key)
        matches.append({"team1": home, "team2": away, "score": {"ft": [hg, ag]}})
    return matches


@st.cache_data(ttl=3600, show_spinner=False)
def load_livescore_season(competition_name, year):
    urls = LIVESCORE_SEASON_URLS.get(competition_name, [])
    if not urls:
        raise RuntimeError("sem fonte alternativa configurada")
    # Algumas competições são divididas em grupos/conferências/fases e uma única
    # URL pode trazer só parte dos clubes. Lemos todas as páginas configuradas e
    # unimos as tabelas antes de validar a cobertura.
    html_pages = []
    errors = []
    for url in urls:
        try:
            html_pages.append(request_first([url], timeout=25).text)
        except Exception as exc:
            errors.append(str(exc))
    if not html_pages:
        raise RuntimeError("fontes alternativas indisponíveis: " + " | ".join(errors[-2:]))

    standings = []
    for html in html_pages:
        standings.extend(_standings_rows_from_html(html))
    if standings:
        merged = {}
        for item in standings:
            key = fixture_team_key(item["team"])
            # Argentina e outros torneios podem exibir duas tabelas/grupos. Mantemos
            # a linha com maior número de partidas quando houver repetição.
            if key not in merged or item["games"] > merged[key]["games"]:
                merged[key] = item

        rows = []
        for item in merged.values():
            games = item["games"]
            rows.append({
                "Time": item["team"],
                "Jogos": games,
                "Gols pró": round(item["gf"] / games, 2) if games else None,
                "Gols contra": round(item["ga"] / games, 2) if games else None,
                "Amarelos": round(float(item.get("yc")) / games, 2) if games and item.get("yc") is not None else None,
                "Vermelhos": round(float(item.get("rc")) / games, 2) if games and item.get("rc") is not None else None,
                **{m: None for m in DISPLAY_METRICS if m not in ("Jogos", "Gols pró", "Gols contra", "Amarelos", "Vermelhos")},
            })
        out = pd.DataFrame(rows).sort_values("Time").reset_index(drop=True)
        out.attrs["updated_until"] = None
        out.attrs["matches"] = []
        out.attrs["season_source"] = "current_standings"
        has_cards = any((x.get("yc") is not None or x.get("rc") is not None) for x in merged.values())
        out.attrs["metric_coverage"] = {
            "Gols": True, "Escanteios": False, "Cartões": has_cards,
            "Faltas": False, "Finalizações": False, "Chutes no alvo": False,
        }
        if len(out) >= 2:
            ensure_team_coverage(out, competition_name)
            return out

    # Torneios mata-mata podem não ter classificação geral. Nesse caso usamos
    # apenas resultados que a página atual realmente fornece, sem inventar zeros.
    matches = []
    for html in html_pages:
        matches.extend(_completed_matches_from_html(html))
    if matches:
        # averages_open_matches naturalmente consolida os clubes; a validação abaixo
        # evita aceitar uma fase/página que represente apenas parte da competição.
        out = averages_open_matches(matches)
        out.attrs["updated_until"] = None
        out.attrs["season_source"] = "current_results"
        if len(out) >= 2:
            ensure_team_coverage(out, competition_name)
            return out

    raise RuntimeError("a página atual da competição ainda não trouxe equipes/resultados suficientes")

@st.cache_data(ttl=21600, show_spinner=False)
def load_open_results(comp_id, year, season_type):
    if comp_id in {"br1", "br2", "mls", "saudi", "argentina", "mexico", "colombia"}:
        if comp_id == "br1":
            candidates = [(str(year), "br.1.json")]
        elif comp_id == "br2":
            candidates = [(str(year), "br.2.json")]
        elif comp_id == "mls":
            candidates = [(str(year), "mls.json"), (str(year), "us.1.json"), (str(year), "usa.1.json")]
        elif comp_id == "argentina":
            candidates = [(str(year), "ar.1.json"), (str(year), "arg.1.json"), (str(year), "ar-liga.json")]
        elif comp_id == "mexico":
            candidates = [(str(year), "mx.1.json"), (str(year), "mex.1.json"), (str(year), "liga-mx.json")]
        elif comp_id == "colombia":
            candidates = [(str(year), "co.1.json"), (str(year), "col.1.json"), (str(year), "colombia.1.json")]
        else:
            # Saudi Pro League segue calendário europeu. Mantemos vários nomes
            # públicos possíveis para tolerar mudanças de nomenclatura do dataset.
            folder = f"{year}-{str(year + 1)[-2:]}"
            candidates = [
                (folder, "sa.1.json"),
                (folder, "ksa.1.json"),
                (folder, "saudi.1.json"),
                (folder, "saudi-pro-league.json"),
            ]

        urls = []
        for folder, filename in candidates:
            urls.extend([
                f"https://raw.githubusercontent.com/openfootball/football.json/master/{folder}/{filename}",
                f"https://github.com/openfootball/football.json/raw/refs/heads/master/{folder}/{filename}",
            ])
        data = load_open_json(urls)
        matches = data.get("matches", [])
        if not matches:
            raise RuntimeError("sem partidas disponíveis para a temporada atual")
        return averages_open_matches(matches)

    if comp_id == "libertadores":
        urls = [
            f"https://raw.githubusercontent.com/openfootball/south-america/master/copa-libertadores/{year}_copal.txt",
            f"https://github.com/openfootball/south-america/raw/refs/heads/master/copa-libertadores/{year}_copal.txt",
        ]
    elif comp_id == "sudamericana":
        urls = [
            f"https://raw.githubusercontent.com/openfootball/south-america/master/copa-sudamericana/{year}_copas.txt",
            f"https://raw.githubusercontent.com/openfootball/south-america/master/copa-sudamericana/{year}_copa_sudamericana.txt",
            f"https://github.com/openfootball/south-america/raw/refs/heads/master/copa-sudamericana/{year}_copas.txt",
        ]
    else:
        yy = str(year + 1)[-2:]
        folder = f"{year}-{yy}"
        if comp_id == "champions":
            names = ["cl.txt", "champions.txt"]
        elif comp_id == "europa":
            names = ["el.txt", "europa.txt", "europa-league.txt"]
        else:
            names = ["conf.txt", "conference.txt", "conference-league.txt", "ecl.txt"]
        urls = []
        for name in names:
            urls.extend([
                f"https://raw.githubusercontent.com/openfootball/champions-league/master/{folder}/{name}",
                f"https://raw.githubusercontent.com/openfootball/champions-league/master/{year}/{name}",
                f"https://github.com/openfootball/champions-league/raw/refs/heads/master/{folder}/{name}",
            ])

    text = request_first(urls).text
    matches = parse_openfootball_txt(text)
    if not matches:
        raise RuntimeError("sem resultados estruturados disponíveis para esta temporada")
    return averages_open_matches(matches)


# ============================================================
# MODELO CONTEXTUAL ENTRE COMPETIÇÕES
# ============================================================
def _norm_team(value):
    return clean_col(value or "")


def _find_team_in_df(team, df):
    if df is None or not isinstance(df, pd.DataFrame) or "Time" not in df.columns:
        return None
    wanted = _norm_team(team)
    exact = df[df["Time"].astype(str).map(_norm_team) == wanted]
    if not exact.empty:
        return exact.iloc[0]
    resolved = resolve_team_name(team, df["Time"].dropna().astype(str).tolist()) if "resolve_team_name" in globals() else None
    if resolved:
        hit = df[df["Time"] == resolved]
        if not hit.empty:
            return hit.iloc[0]
    # Tolerância para sufixos FC/CF e variantes de nomes.
    for _, row in df.iterrows():
        a, b = wanted, _norm_team(row.get("Time"))
        if a and b and (a in b or b in a) and min(len(a), len(b)) >= 5:
            return row
    return None


@st.cache_data(ttl=21600, show_spinner=False)
def _domestic_dataset(competition_name, recent_games=10):
    cfg = COMPETITIONS.get(competition_name)
    if not cfg:
        raise RuntimeError("competição doméstica desconhecida")
    year = current_season_year(cfg["season"])
    if cfg["kind"] == "football_data":
        raw = load_football_data(cfg["code"], year)
        return averages_football_data(raw, recent_games)
    if cfg["kind"] == "hybrid_extra":
        try:
            raw = load_extra_football_data(cfg["extra_code"], year)
            return averages_football_data(raw, recent_games)
        except Exception:
            pass
        try:
            return load_open_results(cfg["id"], year, cfg["season"])
        except Exception:
            return load_livescore_season(competition_name, year)
    if cfg["kind"] == "open_results":
        try:
            return load_open_results(cfg["id"], year, cfg["season"])
        except Exception:
            return load_livescore_season(competition_name, year)
    raise RuntimeError("sem fonte doméstica")


def _candidate_domestic_competitions(team):
    # Se o clube está em um elenco doméstico conhecido, evita varrer todas as ligas.
    hinted = []
    for comp, roster in CURRENT_TEAM_ROSTERS.items():
        if comp in CONTEXTUAL_COMPETITIONS:
            continue
        if any(_norm_team(team) == _norm_team(x) for x in roster):
            hinted.append(comp)
    # Para clubes europeus, os nove campeonatos com CSV detalhado são baratos e
    # ficam em cache depois da primeira consulta.
    european = list(EUROPE_LEAGUES.keys())
    others = [
        "Brasil - Série A", "Brasil - Série B", "Argentina - Liga Profesional",
        "México - Liga MX", "Colômbia - Primera A", "Estados Unidos - MLS",
        "Arábia Saudita - Saudi Pro League",
    ]
    ordered = hinted + european + others
    seen = set(); out = []
    for comp in ordered:
        if comp not in seen and comp in COMPETITIONS:
            seen.add(comp); out.append(comp)
    return out


CLUBELO_NAME_ALIASES = {
    "manchester_city": ["man_city", "mancity", "manchester_city"],
    "manchester_united": ["man_united", "manutd", "manchester_united"],
    "porto": ["porto", "fc_porto"],
    "paris_saint_germain": ["paris_sg", "psg", "paris_saint_germain"],
    "internazionale": ["inter", "internazionale"],
    "inter": ["inter", "internazionale"],
    "atletico_madrid": ["atletico_madrid", "atletico"],
    "bayern_munchen": ["bayern", "bayern_munich", "bayern_munchen"],
    "sporting_cp": ["sporting", "sporting_cp"],
    "psv": ["psv", "psv_eindhoven"],
    "platense": ["platense", "club_atletico_platense"],
}

@st.cache_data(ttl=21600, show_spinner=False)
def load_clubelo_snapshot(day_iso=None):
    """Rating global de clubes sem chave de API; falha silenciosamente se indisponível."""
    if day_iso is None:
        day_iso = datetime.now(ZoneInfo("America/Sao_Paulo")).date().isoformat()
    urls = [f"http://api.clubelo.com/{day_iso}", f"https://api.clubelo.com/{day_iso}"]
    last = None
    for url in urls:
        try:
            r = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0 GM-SCORE/1.0"})
            last = r.status_code
            if r.status_code != 200 or not r.text.lstrip().startswith("Rank,Club"):
                continue
            df = pd.read_csv(io.StringIO(r.text))
            if "Club" not in df.columns or "Elo" not in df.columns:
                continue
            return df
        except Exception:
            continue
    return pd.DataFrame()


def _clubelo_team_key(name):
    return _norm_team(name).replace(" ", "_")


def clubelo_rating(team):
    """Retorna Elo atual e ranking. Usa correspondência tolerante de nomes."""
    try:
        snap = load_clubelo_snapshot()
        if snap is None or snap.empty:
            return None
        wanted = _clubelo_team_key(team)
        candidates = {wanted}
        for a in CLUBELO_NAME_ALIASES.get(wanted, []):
            candidates.add(_clubelo_team_key(a))
        # também remove termos institucionais comuns
        stripped = re.sub(r'_(fc|cf|ac|club|clube)$', '', wanted)
        candidates.add(stripped)
        best = None
        for _, row in snap.iterrows():
            key = _clubelo_team_key(row.get("Club", ""))
            score = 0
            if key in candidates:
                score = 100
            elif any(c and (c in key or key in c) and min(len(c), len(key)) >= 5 for c in candidates):
                score = 70
            if score and (best is None or score > best[0]):
                best = (score, row)
        if best is None:
            return None
        row = best[1]
        elo = float(row.get("Elo"))
        rank = row.get("Rank")
        return {"elo": elo, "rank": None if pd.isna(rank) else int(float(rank)), "club": str(row.get("Club", team))}
    except Exception:
        return None


@st.cache_data(ttl=21600, show_spinner=False)
def load_team_domestic_profile(team, recent_games=10):
    for comp in _candidate_domestic_competitions(team):
        try:
            data = _domestic_dataset(comp, recent_games)
            row = _find_team_in_df(team, data)
            if row is None:
                continue
            matches = data.attrs.get("matches", [])
            canonical = str(row.get("Time", team))
            season_strength, gd = _season_strength(canonical, matches) if matches else (0.5, 0.0)
            form, form_gd = _team_form(canonical, matches, min(6, recent_games)) if matches else (season_strength, gd)
            games = int(float(row.get("Jogos", 0) or 0))
            elo_info = clubelo_rating(canonical) or clubelo_rating(team)
            return {
                "team": canonical,
                "competition": comp,
                "league_strength": LEAGUE_STRENGTH.get(comp, 0.95),
                "row": row.to_dict(),
                "matches": matches,
                "games": games,
                "season_strength": season_strength,
                "form": form,
                "goal_diff": gd,
                "form_goal_diff": form_gd,
                "elo": (elo_info or {}).get("elo"),
                "elo_rank": (elo_info or {}).get("rank"),
            }
        except Exception:
            continue
    return None




def _gm_source_metric_count(row, metric, fallback_games=0):
    """Cobertura real disponível para uma métrica, sem confundir valor com amostra."""
    if row is None:
        return 0
    try:
        raw = row.get(f"_n_{metric}", None)
        if raw is not None and not pd.isna(raw):
            n = int(float(raw))
            if n > 0:
                return n
    except Exception:
        pass
    try:
        val = row.get(metric, None)
        if val is not None and not pd.isna(val):
            games = int(float(row.get("Jogos", fallback_games) or fallback_games or 0))
            return max(games, 0)
    except Exception:
        pass
    return 0


def _gm_sofascore_finished(ev):
    status = ev.get("status") or {}
    raw = " ".join(str(status.get(k) or "") for k in ("type", "description", "code")).lower()
    return any(x in raw for x in ("finished", "after extra time", "after penalties", "ended", "final"))


def _gm_sofascore_score(ev, side):
    score = ev.get("homeScore" if side == "home" else "awayScore") or {}
    for key in ("normaltime", "current", "display"):
        val = score.get(key)
        try:
            if val is not None:
                return float(val)
        except Exception:
            pass
    return None


def _gm_sofascore_stat_map(name):
    key = clean_col(name or "")
    mapping = {
        "total_shots": "Finalizações",
        "totalshots": "Finalizações",
        "shots": "Finalizações",
        "shot_attempts": "Finalizações",
        "total_attempts": "Finalizações",
        "goal_attempts": "Finalizações",
        "attempts": "Finalizações",
        "shots_on_target": "Chutes no alvo",
        "shotsontarget": "Chutes no alvo",
        "shots_on_goal": "Chutes no alvo",
        "on_target": "Chutes no alvo",
        "corner_kicks": "Escanteios",
        "cornerkicks": "Escanteios",
        "corner_kick": "Escanteios",
        "corner": "Escanteios",
        "corners": "Escanteios",
        "corner_kicks_total": "Escanteios",
        "yellow_cards": "Amarelos",
        "yellowcards": "Amarelos",
        "yellow_card": "Amarelos",
        "yellow": "Amarelos",
        "yellowcards": "Amarelos",
        "yellow_cards_total": "Amarelos",
        "red_cards": "Vermelhos",
        "redcards": "Vermelhos",
        "red_card": "Vermelhos",
        "red": "Vermelhos",
        "redcards": "Vermelhos",
        "red_cards_total": "Vermelhos",
        "fouls": "Faltas",
        "fouls_committed": "Faltas",
        "foulscommitted": "Faltas",
        "total_fouls": "Faltas",
        "offsides": "Impedimentos",
        "offside": "Impedimentos",
        "ball_possession": "Posse (%)",
        "ballpossession": "Posse (%)",
        "possession": "Posse (%)",
        "possession_pct": "Posse (%)",
    }
    return mapping.get(key)


def _gm_sofascore_stat_items(node):
    """Percorre envelopes de estatísticas tolerando pequenas mudanças de schema.

    O SofaScore já publicou itens em ``statisticsItems`` e também em envelopes
    intermediários diferentes. A v7 não assume uma única profundidade: qualquer
    dicionário com nome da estatística + valores home/away vira candidato.
    """
    if isinstance(node, dict):
        label = (node.get("name") or node.get("label") or node.get("title")
                 or node.get("statisticsType") or node.get("key"))
        has_values = any(k in node for k in ("home", "away", "homeValue", "awayValue"))
        if label and has_values:
            yield node
        for value in node.values():
            if isinstance(value, (dict, list)):
                yield from _gm_sofascore_stat_items(value)
    elif isinstance(node, list):
        for value in node:
            if isinstance(value, (dict, list)):
                yield from _gm_sofascore_stat_items(value)


def _gm_extract_sofascore_event_stats(payload):
    """Extrai estatísticas agregadas do jogo sem depender de um único schema."""
    out = {}
    periods = payload.get("statistics", []) if isinstance(payload, dict) else []
    chosen = []
    for period in periods if isinstance(periods, list) else []:
        if not isinstance(period, dict):
            continue
        p = str(period.get("period") or "").upper()
        if p in ("ALL", "FULL", "MATCH", "FT", ""):
            chosen.append(period)
    # Se o endpoint não separar períodos como esperado, percorre o payload todo.
    search_nodes = chosen or ([payload] if isinstance(payload, dict) else [])
    for node in search_nodes:
        for item in _gm_sofascore_stat_items(node):
            metric = _gm_sofascore_stat_map(
                item.get("name") or item.get("label") or item.get("title")
                or item.get("statisticsType") or item.get("key")
            )
            if not metric or metric in out:
                continue
            hv = to_num(item.get("home"))
            av = to_num(item.get("away"))
            if hv is None:
                hv = to_num(item.get("homeValue"))
            if av is None:
                av = to_num(item.get("awayValue"))
            if hv is None and av is None:
                continue
            out[metric] = (hv, av)
    return out

def _gm_extract_sofascore_period_stats(payload):
    """Extrai métricas separadas por tempo quando a fonte realmente as publica."""
    out = {"1T": {}, "2T": {}}
    periods = payload.get("statistics", []) if isinstance(payload, dict) else []
    for period in periods if isinstance(periods, list) else []:
        if not isinstance(period, dict):
            continue
        raw = str(period.get("period") or "").upper()
        if raw in ("1ST", "FIRST", "FIRST_HALF", "1H", "FIRSTHALF"):
            bucket = "1T"
        elif raw in ("2ND", "SECOND", "SECOND_HALF", "2H", "SECONDHALF"):
            bucket = "2T"
        else:
            continue
        for item in _gm_sofascore_stat_items(period):
            metric = _gm_sofascore_stat_map(
                item.get("name") or item.get("label") or item.get("title")
                or item.get("statisticsType") or item.get("key")
            )
            if not metric or metric in out[bucket]:
                continue
            hv = to_num(item.get("home"))
            av = to_num(item.get("away"))
            if hv is None:
                hv = to_num(item.get("homeValue"))
            if av is None:
                av = to_num(item.get("awayValue"))
            if hv is None and av is None:
                continue
            out[bucket][metric] = (hv, av)
    return out

def _gm_extract_sofascore_cards_from_incidents(payload):
    """Conta cartões por equipe pelos incidentes quando o bloco de estatísticas omite cartões."""
    counts = {"home_y": 0, "away_y": 0, "home_r": 0, "away_r": 0}
    found = False
    for inc in (payload or {}).get("incidents", []) or []:
        if not isinstance(inc, dict):
            continue
        itype = str(inc.get("incidentType") or inc.get("type") or "").lower()
        cls = str(inc.get("incidentClass") or inc.get("class") or "").lower()
        if "card" not in itype and cls not in ("yellow", "red", "yellowred", "yellow_red", "secondyellow"):
            continue
        is_home = inc.get("isHome")
        if is_home is None:
            side = str(inc.get("homeAway") or "").lower()
            is_home = side == "home" if side in ("home", "away") else None
        if is_home is None:
            continue
        found = True
        prefix = "home" if bool(is_home) else "away"
        if cls in ("red", "yellowred", "yellow_red", "secondyellow", "second_yellow"):
            counts[f"{prefix}_r"] += 1
        else:
            counts[f"{prefix}_y"] += 1
    return counts if found else None


def _gm_recovery_team_key(name):
    """Normalização tolerante usada SOMENTE para resolver IDs estatísticos.

    Não altera o nome exibido e não é usada para odds. Remove siglas estaduais
    brasileiras (ex.: Athletico-PR) e pequenas variações ortográficas que
    impediam a recuperação de equipes como Athletico Paranaense.
    """
    key = _odds_team_key(name)
    toks = key.split()
    if len(toks) >= 2 and re.fullmatch(r"[a-z]{2}", toks[-1] or ""):
        toks = toks[:-1]
    key = " ".join(toks)
    key = re.sub(r"\bathletico\b", "atletico", key)
    key = re.sub(r"\bparanaense\b", "paranaense", key)
    return re.sub(r"\s+", " ", key).strip()


def _gm_recovery_team_similarity(a, b):
    aa, bb = _gm_recovery_team_key(a), _gm_recovery_team_key(b)
    if not aa or not bb:
        return 0.0
    if aa == bb:
        return 1.0
    if aa in bb or bb in aa:
        return 0.94
    sa, sb = set(aa.split()), set(bb.split())
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    # Dá mais peso quando todos os tokens do nome curto estão contidos no longo.
    contain = inter / max(min(len(sa), len(sb)), 1)
    jacc = inter / max(len(sa | sb), 1)
    return max(jacc, contain * 0.92)


def _gm_team_search_queries(team):
    raw = str(team or "").strip()
    if not raw:
        return []
    candidates = [raw]
    # Remove somente sufixos estaduais explícitos: Clube-PR, Clube SP etc.
    stripped = re.sub(r"\s*[-/]?\s*[A-Z]{2}$", "", raw).strip()
    if stripped and stripped.lower() != raw.lower():
        candidates.append(stripped)
    key = _gm_recovery_team_key(raw)
    if key:
        candidates.append(key)
    out = []
    seen = set()
    for q in candidates:
        k = clean_col(q)
        if q and k not in seen:
            seen.add(k); out.append(q)
    return out


@st.cache_data(ttl=21600, show_spinner=False)
def _gm_sofascore_team_search_id(team):
    """Resolve equipe profissional masculina no SofaScore com nome tolerante.

    v7 corrige um defeito importante: clubes podem ter equipes masculina,
    feminina, base ou reserva com praticamente o mesmo nome. A similaridade do
    texto sozinha não basta; candidatos incompatíveis agora são descartados.
    """
    best_global = None
    for query in _gm_team_search_queries(team):
        q = quote(query)
        urls = [
            f"https://api.sofascore.com/api/v1/search/all?q={q}",
            f"https://www.sofascore.com/api/v1/search/all?q={q}",
        ]
        for url in urls:
            try:
                r = requests.get(url, headers=HEADERS, timeout=10)
                if r.status_code != 200:
                    continue
                data = r.json()
                for item in data.get("results", []) or []:
                    entity = item.get("entity") if isinstance(item, dict) else None
                    if not isinstance(entity, dict):
                        continue
                    sport = entity.get("sport") or {}
                    sport_slug = str(sport.get("slug") or "").lower() if isinstance(sport, dict) else ""
                    etype = str(entity.get("type") or item.get("type") or "").lower()
                    if sport_slug and sport_slug != "football":
                        continue
                    if etype and etype not in ("team", "club"):
                        continue
                    # Fundamental: não confundir principal masculina com feminina/base/reserva.
                    gender = str(entity.get("gender") or "").upper()
                    if gender and gender not in ("M", "MALE"):
                        continue
                    nm_blob = " ".join(str(entity.get(k) or "") for k in ("name", "shortName", "slug"))
                    if SECONDARY_TEAM_RE.search(nm_blob):
                        continue
                    name = str(entity.get("name") or "")
                    tid = entity.get("id")
                    sim = _gm_recovery_team_similarity(team, name)
                    if tid and sim >= 0.72 and (best_global is None or sim > best_global[0]):
                        best_global = (sim, tid, name)
                if best_global and best_global[0] >= 0.94:
                    return {"id": best_global[1], "name": best_global[2], "similarity": best_global[0]}
            except Exception:
                continue
    if best_global:
        return {"id": best_global[1], "name": best_global[2], "similarity": best_global[0]}
    return None


@st.cache_data(ttl=21600, show_spinner=False)
def load_sofascore_recent_profile(team_id, team_name, recent_games=12):
    """Recupera histórico recente detalhado de uma equipe no SofaScore.

    v5 procura mais fundo antes de desistir. A coleta só para quando alcança a
    janela desejada de resultados E uma cobertura razoável das métricas-chave,
    ou quando esgota as páginas disponíveis. Cartões também têm fallback pelos
    incidentes do jogo, pois nem todo campeonato os publica no bloco statistics.
    """
    try:
        team_id = int(team_id)
    except Exception:
        return None

    target_games = max(8, min(int(recent_games or 12), 18))
    events = []
    seen_event_ids = set()
    # Mais páginas ajudam ligas/camadas onde parte dos jogos não possui stats.
    for page in range(0, 4):
        payload = None
        for url in (
            f"https://api.sofascore.com/api/v1/team/{team_id}/events/last/{page}",
            f"https://www.sofascore.com/api/v1/team/{team_id}/events/last/{page}",
        ):
            try:
                r = requests.get(url, headers=HEADERS, timeout=10)
                if r.status_code == 200:
                    payload = r.json(); break
            except Exception:
                continue
        if not payload:
            continue
        for ev in payload.get("events", []) or []:
            eid = ev.get("id") if isinstance(ev, dict) else None
            if eid and eid not in seen_event_ids:
                seen_event_ids.add(eid); events.append(ev)
        if len(events) >= max(36, target_games * 3):
            break

    acc = empty_team(str(team_name or team_id))
    detailed_games = 0
    used_events = 0
    used_ids = []

    def key_coverage(metric):
        return int(acc.get(f"_n_{metric}", 0) or 0)

    for ev in events:
        if not isinstance(ev, dict) or not _gm_sofascore_finished(ev):
            continue
        hteam = ev.get("homeTeam") or {}
        ateam = ev.get("awayTeam") or {}
        if str(hteam.get("gender") or "M").upper() not in ("M", "MALE"):
            continue
        if str(ateam.get("gender") or "M").upper() not in ("M", "MALE"):
            continue
        if SECONDARY_TEAM_RE.search(str(hteam.get("name") or "")) or SECONDARY_TEAM_RE.search(str(ateam.get("name") or "")):
            continue
        home_id = hteam.get("id")
        away_id = ateam.get("id")
        if team_id not in (home_id, away_id):
            continue
        side = "home" if team_id == home_id else "away"
        opp_side = "away" if side == "home" else "home"
        gf, ga = _gm_sofascore_score(ev, side), _gm_sofascore_score(ev, opp_side)
        if gf is not None and ga is not None:
            acc["Jogos"] += 1
            acc["Gols pró"] += gf
            acc["Gols contra"] += ga
            used_events += 1
            used_ids.append(ev.get("id"))

        eid = ev.get("id")
        stat_payload = None
        if eid:
            for url in (
                f"https://api.sofascore.com/api/v1/event/{eid}/statistics",
                f"https://www.sofascore.com/api/v1/event/{eid}/statistics",
            ):
                try:
                    rr = requests.get(url, headers=HEADERS, timeout=9)
                    if rr.status_code == 200:
                        stat_payload = rr.json(); break
                except Exception:
                    continue

        stats = _gm_extract_sofascore_event_stats(stat_payload or {})
        period_stats = _gm_extract_sofascore_period_stats(stat_payload or {})
        if stats:
            detailed_games += 1
            idx = 0 if side == "home" else 1
            opp_idx = 1 - idx
            for metric, pair in stats.items():
                own = pair[idx]
                if metric in DISPLAY_METRICS:
                    add_metric(acc, metric, own)
                if metric == "Finalizações":
                    add_metric(acc, "Finalizações contra", pair[opp_idx])
                elif metric == "Chutes no alvo":
                    add_metric(acc, "Chutes no alvo contra", pair[opp_idx])
            # Separação de escanteios por tempo só entra quando publicada de fato.
            for bucket, hidden_metric in (("1T", "Escanteios 1T"), ("2T", "Escanteios 2T")):
                pair = (period_stats.get(bucket) or {}).get("Escanteios")
                if pair:
                    add_metric(acc, hidden_metric, pair[idx])

        # Fallback de cartões: muitos campeonatos têm incidentes mesmo quando
        # statistics não lista amarelos/vermelhos.
        if eid and (key_coverage("Amarelos") < target_games or key_coverage("Vermelhos") < max(3, target_games // 2)):
            incident_payload = None
            for url in (
                f"https://api.sofascore.com/api/v1/event/{eid}/incidents",
                f"https://www.sofascore.com/api/v1/event/{eid}/incidents",
            ):
                try:
                    rr = requests.get(url, headers=HEADERS, timeout=8)
                    if rr.status_code == 200:
                        incident_payload = rr.json(); break
                except Exception:
                    continue
            cards = _gm_extract_sofascore_cards_from_incidents(incident_payload or {})
            if cards:
                # Só usa incidentes para a métrica que NÃO veio no statistics,
                # evitando contar o mesmo jogo duas vezes.
                idx_home = side == "home"
                y = cards["home_y"] if idx_home else cards["away_y"]
                r = cards["home_r"] if idx_home else cards["away_r"]
                if "Amarelos" not in stats:
                    add_metric(acc, "Amarelos", y)
                if "Vermelhos" not in stats:
                    add_metric(acc, "Vermelhos", r)

        # Não encerra apenas por quantidade de placares. Antes exige cobertura
        # das métricas que sustentam os principais mercados.
        key_ns = [key_coverage(m) for m in ("Escanteios", "Amarelos", "Finalizações", "Chutes no alvo")]
        enough_detail = sum(n >= min(8, target_games) for n in key_ns) >= 3
        if used_events >= target_games and enough_detail:
            break

    if acc.get("Jogos", 0) <= 0:
        return None
    outdf = finish_averages({str(team_id): acc})
    if outdf is None or outdf.empty:
        return None
    row = outdf.iloc[0].to_dict()
    row["_n_Gols pró"] = int(acc.get("Jogos", 0))
    row["_n_Gols contra"] = int(acc.get("Jogos", 0))
    coverage = {
        m: int(row.get(f"_n_{m}", 0) or 0)
        for m in ("Escanteios", "Amarelos", "Vermelhos", "Faltas", "Finalizações", "Chutes no alvo", "Escanteios 1T", "Escanteios 2T")
    }
    return {
        "row": row,
        "games": int(acc.get("Jogos", 0)),
        "detailed_games": int(detailed_games),
        "coverage": coverage,
        "event_ids": [x for x in used_ids if x],
        "source": "SofaScore · histórico detalhado + incidentes",
    }


def _gm_blend_recovery_metric(current_value, current_n, recovery_value, recovery_n, prefer_recovery=False):
    """Combina coberturas reais sem somar partidas potencialmente sobrepostas.

    Quando ``prefer_recovery`` é True, a recuperação é uma fonte primária
    validada (APIfootball) e, com N>=8, seu valor e seu próprio N passam a
    comandar a métrica. Para fontes complementares continua valendo a regra
    conservadora: só elevam a amostra quando realmente possuem cobertura maior.
    """
    if recovery_value is None or recovery_n < 3:
        return current_value, current_n, False
    if prefer_recovery and int(recovery_n) >= 8:
        return float(recovery_value), int(recovery_n), True
    if current_value is None or current_n <= 0:
        return float(recovery_value), int(recovery_n), True
    if int(recovery_n) <= int(current_n):
        return float(current_value), int(current_n), False
    cw = max(min(float(current_n), 10.0), 1.0)
    rw = max(min(float(recovery_n), 12.0), 1.0)
    value = (float(current_value) * cw + float(recovery_value) * rw) / (cw + rw)
    return value, max(int(current_n), int(recovery_n)), True


def _gm_pair_metric_sample(a, b, metrics, fallback=0):
    counts = []
    for row in (a, b):
        for metric in metrics:
            n = _gm_source_metric_count(row, metric, fallback)
            if n <= 0:
                return 0
            counts.append(n)
    return min(counts) if counts else int(fallback or 0)


@st.cache_data(ttl=21600, show_spinner=False)
def load_competition_history_context(competition_name, current_year, lookback=5):
    cfg = COMPETITIONS.get(competition_name, {})
    comp_id = cfg.get("id")
    season_type = cfg.get("season")
    if not comp_id or competition_name not in CONTEXTUAL_COMPETITIONS:
        return {"matches": [], "goal_avg": None, "seasons": 0}
    all_matches = []
    seasons = 0
    # Edições anteriores: úteis para H2H e média de gols do próprio torneio.
    for offset in range(1, lookback + 1):
        y = current_year - offset
        try:
            hist = load_open_results(comp_id, y, season_type)
            ms = hist.attrs.get("matches", [])
            if ms:
                all_matches.extend(ms)
                seasons += 1
        except Exception:
            continue
    goals = [float(m.get("hg", 0)) + float(m.get("ag", 0)) for m in all_matches if m.get("hg") is not None and m.get("ag") is not None]
    return {
        "matches": all_matches,
        "goal_avg": (sum(goals) / len(goals)) if goals else None,
        "seasons": seasons,
    }


def _blend_metric(current_value, current_games, domestic_value, prior_value, league_strength=1.0, metric=""):
    cv = None if current_value is None or pd.isna(current_value) else float(current_value)
    dv = None if domestic_value is None or pd.isna(domestic_value) else float(domestic_value)
    pv = None if prior_value is None or pd.isna(prior_value) else float(prior_value)
    # Dados do torneio passam a dominar gradualmente; com 0 jogos, a base é
    # doméstica + histórico da competição.
    w_current = min(max(float(current_games), 0.0) / 8.0, 0.78)
    # Traduz desempenho doméstico pelo nível da liga, mas com ajuste pequeno.
    strength_adj = max(0.90, min(1.10, 1.0 + (float(league_strength) - 1.0) * 0.55))
    if metric in ("Gols pró", "Finalizações", "Chutes no alvo", "Escanteios") and dv is not None:
        dv *= strength_adj
    elif metric == "Gols contra" and dv is not None:
        dv /= strength_adj
    vals = []
    if cv is not None:
        vals.append((cv, w_current))
    remaining = 1.0 - sum(w for _, w in vals)
    if dv is not None:
        wd = remaining * (0.72 if pv is not None else 1.0)
        vals.append((dv, wd)); remaining -= wd
    if pv is not None and remaining > 0:
        vals.append((pv, remaining))
    if not vals:
        return None
    den = sum(w for _, w in vals)
    return sum(v*w for v,w in vals) / den if den else None




# ============================================================
# DATA RECOVERY v6 - segunda fonte pública (ESPN)
# ============================================================
# A recuperação ESPN é usada apenas quando a cobertura detalhada da fonte
# principal é insuficiente. Não altera odds, pagamentos, autenticação ou agenda.


def _gm_espn_stat_map(name):
    key = clean_col(name or "")
    mapping = {
        "shots": "Finalizações",
        "sh": "Finalizações",
        "total_shots": "Finalizações",
        "totalshots": "Finalizações",
        "shots_total": "Finalizações",
        "shot_attempts": "Finalizações",
        "shotattempts": "Finalizações",
        "total_attempts": "Finalizações",
        "totalattempts": "Finalizações",
        "shots_on_target": "Chutes no alvo",
        "shotsontarget": "Chutes no alvo",
        "shots_on_goal": "Chutes no alvo",
        "shotsongoal": "Chutes no alvo",
        "sog": "Chutes no alvo",
        "corner_kicks": "Escanteios",
        "cornerkicks": "Escanteios",
        "corners": "Escanteios",
        "ck": "Escanteios",
        "yellow_cards": "Amarelos",
        "yellowcards": "Amarelos",
        "yc": "Amarelos",
        "red_cards": "Vermelhos",
        "redcards": "Vermelhos",
        "rc": "Vermelhos",
        "fouls_committed": "Faltas",
        "foulscommitted": "Faltas",
        "fouls": "Faltas",
        "fc": "Faltas",
        "offsides": "Impedimentos",
        "offside": "Impedimentos",
        "off": "Impedimentos",
        "possession": "Posse (%)",
        "possession_pct": "Posse (%)",
        "possessionpct": "Posse (%)",
        "possession_percentage": "Posse (%)",
        "possessionpercentage": "Posse (%)",
        "poss": "Posse (%)",
    }
    return mapping.get(key)


def _gm_espn_stat_value(item):
    if not isinstance(item, dict):
        return None
    for key in ("value", "displayValue"):
        value = item.get(key)
        n = to_num(value)
        if n is not None:
            return n
    return None


def _gm_espn_team_objects(payload):
    """Extrai objetos de equipe dos diferentes envelopes usados pela ESPN."""
    out = []
    if not isinstance(payload, dict):
        return out
    # /teams normalmente vem dentro de sports -> leagues -> teams.
    for sport in payload.get("sports", []) or []:
        for league in (sport or {}).get("leagues", []) or []:
            for entry in (league or {}).get("teams", []) or []:
                team = (entry or {}).get("team") if isinstance(entry, dict) else None
                if isinstance(team, dict):
                    out.append(team)
    # Alguns endpoints retornam teams diretamente.
    for entry in payload.get("teams", []) or []:
        if isinstance(entry, dict):
            team = entry.get("team") if isinstance(entry.get("team"), dict) else entry
            if isinstance(team, dict):
                out.append(team)
    return out


@st.cache_data(ttl=21600, show_spinner=False)
def _gm_espn_team_id(league_slug, team_name):
    if not league_slug or not team_name:
        return None
    try:
        data = _espn_get_json(
            f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league_slug}/teams?limit=500",
            timeout=12,
        )
    except Exception:
        return None
    best = None
    for team in _gm_espn_team_objects(data):
        tid = team.get("id")
        names = [team.get("displayName"), team.get("shortDisplayName"), team.get("name"), team.get("abbreviation")]
        for name in names:
            if not tid or not name:
                continue
            sim = _gm_recovery_team_similarity(team_name, name)
            if sim >= 0.70 and (best is None or sim > best[0]):
                best = (sim, str(tid), str(team.get("displayName") or name))
    if best:
        return {"id": best[1], "name": best[2], "similarity": best[0]}
    return None


def _gm_espn_event_finished(event):
    if not isinstance(event, dict):
        return False
    status = event.get("status") or {}
    stype = status.get("type") if isinstance(status, dict) else {}
    if not isinstance(stype, dict):
        stype = {}
    if stype.get("completed") is True:
        return True
    raw = " ".join(str(x or "") for x in (
        stype.get("state"), stype.get("name"), stype.get("description"),
        stype.get("shortDetail"), status.get("displayClock") if isinstance(status, dict) else "",
    )).lower()
    return any(x in raw for x in ("post", "final", "full time", "ft", "completed"))


def _gm_espn_event_team_side(event, team_id, team_name=None):
    comps = event.get("competitions", []) if isinstance(event, dict) else []
    if not comps:
        return None, None, None
    competitors = (comps[0] or {}).get("competitors", []) or []
    own = opp = None
    for comp in competitors:
        team = (comp or {}).get("team") or {}
        tid = str(team.get("id") or "")
        nm = team.get("displayName") or team.get("shortDisplayName") or team.get("name") or ""
        if (team_id and tid == str(team_id)) or (team_name and _gm_recovery_team_similarity(team_name, nm) >= 0.82):
            own = comp
        else:
            if opp is None:
                opp = comp
    if not own:
        return None, None, None
    side = own.get("homeAway")
    # Resolve adversário de forma explícita depois que achou a própria equipe.
    for comp in competitors:
        if comp is own:
            continue
        opp = comp
        break
    return side, own, opp


def _gm_espn_competitor_score(comp):
    if not isinstance(comp, dict):
        return None
    score = comp.get("score")
    if isinstance(score, dict):
        for key in ("value", "displayValue"):
            n = to_num(score.get(key))
            if n is not None:
                return n
    return to_num(score)


def _gm_extract_espn_summary_stats(payload, team_id=None, team_name=None):
    """Retorna métricas da equipe e do adversário a partir do boxscore ESPN."""
    box = (payload or {}).get("boxscore") or {}
    teams = box.get("teams", []) if isinstance(box, dict) else []
    own_stats = opp_stats = None
    for entry in teams or []:
        team = (entry or {}).get("team") or {}
        tid = str(team.get("id") or "")
        nm = team.get("displayName") or team.get("shortDisplayName") or team.get("name") or ""
        mapped = {}
        for stat in (entry or {}).get("statistics", []) or []:
            label = stat.get("name") or stat.get("label") or stat.get("abbreviation") or stat.get("displayName")
            metric = _gm_espn_stat_map(label)
            if not metric:
                continue
            value = _gm_espn_stat_value(stat)
            if value is not None:
                mapped[metric] = value
        if (team_id and tid == str(team_id)) or (team_name and _gm_recovery_team_similarity(team_name, nm) >= 0.82):
            own_stats = mapped
        else:
            if opp_stats is None:
                opp_stats = mapped
    return own_stats or {}, opp_stats or {}


@st.cache_data(ttl=21600, show_spinner=False)
def load_espn_recent_profile(league_slug, team_name, recent_games=12):
    """Recupera histórico detalhado ESPN como segunda fonte independente.

    A ESPN é consultada somente para complementar cobertura. Cada métrica mantém
    a quantidade real de jogos em que foi encontrada; ausência nunca vira zero.
    """
    found = _gm_espn_team_id(league_slug, team_name)
    if not found:
        return None
    team_id = found.get("id")
    try:
        schedule = _espn_get_json(
            f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league_slug}/teams/{team_id}/schedule?limit=100",
            timeout=14,
        )
    except Exception:
        return None
    events = schedule.get("events", []) if isinstance(schedule, dict) else []
    target_games = max(8, min(int(recent_games or 12), 18))
    acc = empty_team(str(team_name))
    used_ids = []
    detailed_games = 0

    # Agenda geralmente vem em ordem cronológica; priorizamos os mais recentes.
    def event_ts(ev):
        try:
            return pd.to_datetime(ev.get("date"), utc=True, errors="coerce").value
        except Exception:
            return 0
    events = sorted([e for e in events if isinstance(e, dict)], key=event_ts, reverse=True)

    for ev in events:
        if not _gm_espn_event_finished(ev):
            continue
        side, own_comp, opp_comp = _gm_espn_event_team_side(ev, team_id, team_name)
        if own_comp is None:
            continue
        gf = _gm_espn_competitor_score(own_comp)
        ga = _gm_espn_competitor_score(opp_comp)
        if gf is not None and ga is not None:
            acc["Jogos"] += 1
            acc["Gols pró"] += gf
            acc["Gols contra"] += ga

        eid = ev.get("id")
        if not eid:
            continue
        try:
            summary = _espn_get_json(
                f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league_slug}/summary?event={eid}",
                timeout=11,
            )
        except Exception:
            summary = None
        own_stats, opp_stats = _gm_extract_espn_summary_stats(summary or {}, team_id, team_name)
        if own_stats:
            detailed_games += 1
            for metric, value in own_stats.items():
                if metric in DISPLAY_METRICS:
                    add_metric(acc, metric, value)
            if own_stats.get("Finalizações") is not None and opp_stats.get("Finalizações") is not None:
                add_metric(acc, "Finalizações contra", opp_stats.get("Finalizações"))
            if own_stats.get("Chutes no alvo") is not None and opp_stats.get("Chutes no alvo") is not None:
                add_metric(acc, "Chutes no alvo contra", opp_stats.get("Chutes no alvo"))
        used_ids.append(str(eid))

        key_ns = [int(acc.get(f"_n_{m}", 0) or 0) for m in ("Escanteios", "Amarelos", "Finalizações", "Chutes no alvo")]
        enough_detail = sum(n >= min(8, target_games) for n in key_ns) >= 3
        if int(acc.get("Jogos", 0) or 0) >= target_games and enough_detail:
            break
        if int(acc.get("Jogos", 0) or 0) >= max(target_games * 2, 24):
            break

    if int(acc.get("Jogos", 0) or 0) <= 0:
        return None
    outdf = finish_averages({str(team_id): acc})
    if outdf is None or outdf.empty:
        return None
    row = outdf.iloc[0].to_dict()
    row["_n_Gols pró"] = int(acc.get("Jogos", 0))
    row["_n_Gols contra"] = int(acc.get("Jogos", 0))
    coverage = {m: int(row.get(f"_n_{m}", 0) or 0) for m in (
        "Escanteios", "Amarelos", "Vermelhos", "Faltas", "Finalizações", "Chutes no alvo",
        "Finalizações contra", "Chutes no alvo contra",
    )}
    return {
        "row": row,
        "games": int(acc.get("Jogos", 0)),
        "detailed_games": int(detailed_games),
        "coverage": coverage,
        "event_ids": used_ids,
        "source": "ESPN · boxscore histórico",
        "team_id": str(team_id),
    }


def _gm_merge_recovery_profiles(*profiles):
    """Escolhe a melhor cobertura real por métrica sem somar amostras.

    APIfootball é a fonte primária validada. Quando ela possui N>=8 para uma
    métrica, mantém prioridade mesmo que outra fonte tenha alguns jogos a mais.
    Se a cobertura da APIfootball for curta, a melhor fonte complementar assume
    somente aquela métrica. Isso é especialmente importante para Arábia Saudita,
    México, Colômbia e Europa League, onde a auditoria encontrou lacunas pontuais.
    """
    profiles = [p for p in profiles if isinstance(p, dict) and isinstance(p.get("row"), dict)]
    if not profiles:
        return None
    metrics = [
        "Gols pró", "Gols contra", "Escanteios", "Amarelos", "Vermelhos", "Faltas",
        "Finalizações", "Chutes no alvo", "Impedimentos", "Posse (%)",
        "Finalizações contra", "Chutes no alvo contra", "Escanteios 1T", "Escanteios 2T",
        "Finalizações 1T", "Chutes no alvo 1T", "Gols 1T", "Gols 2T",
    ]
    merged = {"Time": profiles[0]["row"].get("Time")}
    winners = {}
    for metric in metrics:
        valid = []
        api_candidates = []
        for prof in profiles:
            row = prof["row"]
            n = _gm_source_metric_count(row, metric, prof.get("games", 0))
            value = row.get(metric)
            if value is None or pd.isna(value) or n <= 0:
                continue
            candidate = (int(n), float(value), prof.get("source") or "fonte complementar")
            valid.append(candidate)
            if str(candidate[2]).startswith("APIfootball"):
                api_candidates.append(candidate)
        best = None
        if api_candidates:
            api_best = max(api_candidates, key=lambda x: x[0])
            if api_best[0] >= 8:
                best = api_best
        if best is None and valid:
            best = max(valid, key=lambda x: x[0])
        if best:
            merged[metric] = best[1]
            merged[f"_n_{metric}"] = best[0]
            winners[metric] = best[2]
        else:
            merged[metric] = None
            merged[f"_n_{metric}"] = 0
    games = max(int(p.get("games", 0) or 0) for p in profiles)
    merged["Jogos"] = games
    merged_sources = sorted(set(winners.values()))
    return {
        "row": merged,
        "games": games,
        "detailed_games": max(int(p.get("detailed_games", 0) or 0) for p in profiles),
        "coverage": {m: int(merged.get(f"_n_{m}", 0) or 0) for m in metrics},
        "source": " + ".join(merged_sources) if merged_sources else "recuperação multifuente",
        "metric_sources": winners,
        "profiles": profiles,
    }


def _gm_recovery_diagnostic(profile):
    if not profile:
        return {"games": 0, "detailed": 0, "coverage": {}, "source": None}
    return {
        "games": int(profile.get("games", 0) or 0),
        "detailed": int(profile.get("detailed_games", 0) or 0),
        "coverage": dict(profile.get("coverage") or {}),
        "source": profile.get("source"),
    }


def _gm_competition_metric_fallback(competition_df, metric):
    """Fallback estatístico da própria competição, nunca um número decorativo.

    Usa somente equipes que realmente possuem a métrica. Serve para impedir que
    uma falha pontual de recuperação transforme todo o mercado em Inconclusivo.
    A origem fica marcada como contexto da competição e a confiança é limitada.
    """
    if not isinstance(competition_df, pd.DataFrame) or metric not in competition_df.columns:
        return None
    vals, ns = [], []
    for _, row in competition_df.iterrows():
        try:
            v = row.get(metric)
            if v is None or pd.isna(v):
                continue
            n = _gm_source_metric_count(row, metric, 0)
            if n < 3:
                continue
            vals.append(float(v)); ns.append(int(n))
        except Exception:
            continue
    if len(vals) < 6:
        return None
    # Mediana é menos sensível a um clube extremo que a média simples.
    value = float(pd.Series(vals).median())
    effective_n = min(7, max(3, int(pd.Series(ns).median())))
    return {"value": value, "n": effective_n, "teams": len(vals), "source": "contexto real da competição"}


def _gm_apply_competition_fallback(row, competition_df):
    """Completa apenas métricas ausentes; jamais sobrescreve dado da equipe."""
    if row is None:
        out = {}
    elif isinstance(row, pd.Series):
        out = row.to_dict()
    elif isinstance(row, dict):
        out = dict(row)
    else:
        try:
            out = dict(row)
        except Exception:
            out = {}
    used = []
    for metric in ("Escanteios", "Amarelos", "Vermelhos", "Faltas", "Finalizações", "Chutes no alvo", "Impedimentos", "Posse (%)"):
        try:
            current = out.get(metric)
            n = _gm_source_metric_count(out, metric, 0)
        except Exception:
            current, n = None, 0
        if current is not None and not pd.isna(current) and n >= 3:
            continue
        fb = _gm_competition_metric_fallback(competition_df, metric)
        if not fb:
            continue
        out[metric] = fb["value"]
        out[f"_n_{metric}"] = fb["n"]
        out[f"_gm_fallback_{metric}"] = True
        used.append(metric)
    if used:
        out["_gm_competition_fallback_used"] = True
        out["_gm_competition_fallback_metrics"] = ", ".join(used)
    return pd.Series(out)



# v11 — recuperação histórica estatística para ligas Football-Data.
# A temporada recém-iniciada pode ter só 1–3 jogos; nesses casos, depender apenas
# do CSV vigente fazia clubes com histórico abundante aparecerem como Inconclusivo.
# Esta camada procura as últimas partidas reais do clube também nas temporadas
# anteriores e, quando aplicável, na segunda divisão do mesmo país (promovidos).
_GM_FD_RECOVERY_CODES = {
    "Inglaterra - Premier League": ["E0", "E1"],
    "Espanha - La Liga": ["SP1", "SP2"],
    "Itália - Serie A": ["I1", "I2"],
    "Alemanha - Bundesliga": ["D1", "D2"],
    "França - Ligue 1": ["F1", "F2"],
    "Portugal - Liga Portugal": ["P1"],
    "Holanda - Eredivisie": ["N1"],
    "Escócia - Premiership": ["SC0", "SC1"],
    "Turquia - Süper Lig": ["T1"],
}

@st.cache_data(ttl=21600, show_spinner=False)
def load_fd_historical_team_profile(competition_name, team_name, recent_games=12):
    codes = _GM_FD_RECOVERY_CODES.get(competition_name) or []
    if not codes:
        return None
    current_year = current_season_year("europe")
    target = max(8, min(int(recent_games or 12), 18))
    candidates = []
    # Atual + 3 temporadas anteriores: suficiente para promovidos/rebaixados sem
    # misturar histórico muito antigo quando já há amostra recente disponível.
    for year in range(current_year, current_year - 4, -1):
        for code in codes:
            try:
                df = load_football_data(code, year).copy()
            except Exception:
                continue
            if df is None or df.empty:
                continue
            for idx, g in df.iterrows():
                home, away = str(g.get("HomeTeam") or ""), str(g.get("AwayTeam") or "")
                sh = _gm_recovery_team_similarity(team_name, home)
                sa = _gm_recovery_team_similarity(team_name, away)
                if max(sh, sa) < 0.78:
                    continue
                side = "home" if sh >= sa else "away"
                dt = pd.to_datetime(g.get("Date"), dayfirst=True, errors="coerce") if "Date" in df.columns else pd.NaT
                candidates.append((dt, year, code, side, g.to_dict()))
    if not candidates:
        return None

    # Remove duplicatas entre arquivos/rotas e usa as partidas mais recentes.
    candidates.sort(key=lambda x: (pd.Timestamp.min if pd.isna(x[0]) else x[0]), reverse=True)
    seen, chosen = set(), []
    for dt, year, code, side, g in candidates:
        home, away = str(g.get("HomeTeam") or ""), str(g.get("AwayTeam") or "")
        dkey = "" if pd.isna(dt) else str(pd.Timestamp(dt).date())
        key = (dkey, clean_col(home), clean_col(away), g.get("FTHG"), g.get("FTAG"))
        if key in seen:
            continue
        seen.add(key); chosen.append((dt, year, code, side, g))
        if len(chosen) >= target:
            break

    acc = empty_team(str(team_name))
    for dt, year, code, side, g in chosen:
        if side == "home":
            gf, ga = to_num(g.get("FTHG")), to_num(g.get("FTAG"))
            own_idx = 0
        else:
            gf, ga = to_num(g.get("FTAG")), to_num(g.get("FTHG"))
            own_idx = 1
        if gf is None or ga is None:
            continue
        acc["Jogos"] += 1; acc["Gols pró"] += gf; acc["Gols contra"] += ga
        for metric, (hc, ac) in FD_STATS.items():
            col = hc if own_idx == 0 else ac
            if col in g:
                add_metric(acc, metric, g.get(col))
        # Volume cedido ao adversário para projeção ataque × defesa.
        if own_idx == 0:
            add_metric(acc, "Finalizações contra", g.get("AS"))
            add_metric(acc, "Chutes no alvo contra", g.get("AST"))
        else:
            add_metric(acc, "Finalizações contra", g.get("HS"))
            add_metric(acc, "Chutes no alvo contra", g.get("HST"))

    if acc.get("Jogos", 0) <= 0:
        return None
    outdf = finish_averages({str(team_name): acc})
    if outdf.empty:
        return None
    row = outdf.iloc[0].to_dict()
    row["_n_Gols pró"] = int(acc["Jogos"]); row["_n_Gols contra"] = int(acc["Jogos"])
    coverage = {m: int(row.get(f"_n_{m}", 0) or 0) for m in (
        "Escanteios", "Amarelos", "Vermelhos", "Faltas", "Finalizações", "Chutes no alvo",
        "Finalizações contra", "Chutes no alvo contra")}
    return {
        "row": row, "games": int(acc["Jogos"]), "detailed_games": int(acc["Jogos"]),
        "coverage": coverage, "source": "Football-Data · histórico multitemporada",
    }

@st.cache_data(ttl=21600, show_spinner=False)
def gm_apifootball_pair_profiles(team_a, team_b, competition_name=None, limit_per_team=20, lookback_days=520):
    """Fonte estatística primária do GM SCORE via APIfootball.

    A resolução usa primeiro o ``league_id`` fixo auditado + ``get_teams`` e os
    aliases confirmados. H2H fica apenas como fallback para clubes cuja lista da
    liga esteja temporariamente incompleta. A busca prioriza partidas da liga
    selecionada e completa a janela com outros jogos oficiais do clube quando
    necessário, sem duplicar eventos nem transformar ausências em zero.
    """
    league_id = None
    try:
        league_id = GM_APIFOOTBALL_FIXED_LEAGUE_IDS.get(competition_name)
    except Exception:
        league_id = None

    def team_variants(name):
        # Resolução bidirecional: funciona tanto quando o GM SCORE já recebeu o
        # nome novo e a API ainda usa o antigo, quanto no sentido inverso. Isso
        # protege as 21 competições contra rebrandings e variações de cadastro
        # sem trocar a identidade exibida ao cliente.
        raw_name = str(name or "")
        vals = [raw_name]
        target_norm = _gm_api_norm(raw_name)
        try:
            vals.extend(GM_APIFOOTBALL_TEAM_ALIASES.get(raw_name, []) or [])
            for canonical, aliases in (GM_APIFOOTBALL_TEAM_ALIASES or {}).items():
                group = [str(canonical)] + [str(x) for x in (aliases or [])]
                norms = {_gm_api_norm(x) for x in group if _gm_api_norm(x)}
                if target_norm and target_norm in norms:
                    vals.extend(group)
        except Exception:
            pass
        out, seen = [], set()
        for value in vals:
            norm = _gm_api_norm(value)
            if norm and norm not in seen:
                seen.add(norm); out.append(norm)
        return out

    def resolve_from_league(name):
        if not league_id:
            return None
        teams, terr = gm_apifootball_league_teams(league_id)
        if terr or not isinstance(teams, list):
            return None
        targets = team_variants(name)
        best = None
        for item in teams:
            if not isinstance(item, dict):
                continue
            api_name = str(item.get("team_name") or "")
            tid = str(item.get("team_key") or item.get("team_id") or "").strip()
            if not tid:
                continue
            nn = _gm_api_norm(api_name)
            score = 0
            # O mesmo resolvedor auditado das 21 ligas passa a ser a regra central.
            # Isso cobre aliases oficiais, acentos, FC/CF/SC e variações de nome.
            if _gm_team_name_match(name, api_name):
                score = 100 if nn in targets else 92
            else:
                for target in targets:
                    if nn == target:
                        score = max(score, 100)
                    elif target and min(len(target), len(nn)) >= 5 and (target in nn or nn in target):
                        score = max(score, 82)
                    else:
                        sa, sb = set(target.split()), set(nn.split())
                        if sa and sb:
                            overlap = len(sa & sb) / max(len(sa | sb), 1)
                            if overlap >= 0.72:
                                score = max(score, int(80 * overlap))
            if best is None or score > best[0]:
                best = (score, tid)
        return best[1] if best and best[0] >= 65 else None

    # H2H é fallback de resolução, não fonte primária de IDs.
    h2h_payload = None
    def resolve_from_h2h(name):
        nonlocal h2h_payload
        if h2h_payload is None:
            h2h_payload, _ = gm_apifootball_request(
                "get_H2H", firstTeam=team_a, secondTeam=team_b,
                timezone="America/Sao_Paulo",
            )
        if not isinstance(h2h_payload, dict):
            return None
        all_seed = []
        for key in ("firstTeam_VS_secondTeam", "firstTeam_lastResults", "secondTeam_lastResults"):
            all_seed.extend([x for x in (h2h_payload.get(key) or []) if isinstance(x, dict)])
        targets = team_variants(name)
        best = None
        for ev in all_seed:
            for side in ("home", "away"):
                nm = str(ev.get(f"match_{side}team_name") or "")
                tid = str(ev.get(f"match_{side}team_id") or "").strip()
                nn = _gm_api_norm(nm)
                if not tid or not nn:
                    continue
                score = max((100 if nn == t else 82 if (t in nn or nn in t) else 0) for t in targets) if targets else 0
                if score and (best is None or score > best[0]):
                    best = (score, tid)
        return best[1] if best else None

    end_date = date.today()
    start_date = end_date - timedelta(days=int(lookback_days))
    date_params = {"from": start_date.isoformat(), "to": end_date.isoformat(), "timezone": "America/Sao_Paulo"}

    stat_aliases = {
        "Escanteios": ("Corners", "Corner Kicks"),
        "Amarelos": ("Yellow Cards",),
        "Vermelhos": ("Red Cards",),
        "Faltas": ("Fouls",),
        "Finalizações": ("Shots Total", "Goal Attempts", "Total Shots"),
        "Chutes no alvo": ("Shots On Goal", "Shots on Goal", "On Target"),
        "Impedimentos": ("Offsides",),
        "Posse (%)": ("Ball Possession",),
        "Passes": ("Passes Total", "Total Passes"),
        "Precisão passes (%)": ("Passes Accurate Percentage", "Pass Accuracy"),
    }

    def stat_value(stats, aliases, side):
        for alias in aliases:
            pair = stats.get(alias)
            if pair and _gm_api_has_value(pair.get(side)):
                return pair.get(side)
        return None

    def is_finished(ev):
        return _gm_api_norm(ev.get("match_status")) in {"finished", "after et", "after pen", "ft"}

    def belongs_to(ev, targets, team_id=None):
        # Depois de resolver o clube, IDs oficiais são a fonte de verdade.
        # Nome fica apenas como fallback para respostas antigas/incompletas da API.
        tid = str(team_id or "").strip()
        if tid:
            hid = str(ev.get("match_hometeam_id") or "").strip()
            aid = str(ev.get("match_awayteam_id") or "").strip()
            if hid or aid:
                return tid in {hid, aid}
        hn = str(ev.get("match_hometeam_name") or "")
        an = str(ev.get("match_awayteam_name") or "")
        return any(_gm_team_name_match(t, hn) or _gm_team_name_match(t, an) for t in targets)

    def official_event(ev):
        league = _gm_api_norm(ev.get("league_name"))
        return "friendly" not in league and "friendlies" not in league

    def halftime_score_from_event(ev):
        """Recupera o placar do intervalo; usa eventos de gol quando o campo HT veio vazio."""
        hhs = to_num(ev.get("match_hometeam_halftime_score"))
        has = to_num(ev.get("match_awayteam_halftime_score"))
        if hhs is not None and has is not None:
            return hhs, has
        goals = ev.get("goalscorer") or []
        if not isinstance(goals, list) or not goals:
            return None, None
        home_ht = away_ht = 0
        found = False
        for goal in goals:
            if not isinstance(goal, dict):
                continue
            tm = str(goal.get("time") or goal.get("score_info_time") or "")
            mt = re.search(r"(\d+)", tm)
            if not mt:
                continue
            minute = int(mt.group(1))
            # Acréscimos do 1T costumam vir como 45+N; o primeiro número basta.
            if minute > 45:
                continue
            hs = str(goal.get("home_scorer") or "").strip()
            aws = str(goal.get("away_scorer") or "").strip()
            if hs:
                home_ht += 1; found = True
            if aws:
                away_ht += 1; found = True
        return (home_ht, away_ht) if found else (None, None)

    def profile(team_name, team_id):
        if not team_id:
            return None
        targets = team_variants(team_name)
        pooled = []
        # 1) competição selecionada: fonte preferencial e semanticamente correta.
        if league_id:
            events, ev_err = gm_apifootball_request("get_events", team_id=team_id, league_id=league_id, **date_params)
            if not ev_err and isinstance(events, list):
                pooled.extend(events)
        # 2) se a temporada/fase ainda estiver curta, completa com partidas oficiais
        # recentes do mesmo clube (copas/divisão anterior), preservando IDs reais.
        league_finished = [x for x in pooled if isinstance(x, dict) and is_finished(x) and belongs_to(x, targets, team_id)]
        if len(league_finished) < int(limit_per_team):
            events, ev_err = gm_apifootball_request("get_events", team_id=team_id, **date_params)
            if not ev_err and isinstance(events, list):
                pooled.extend(events)

        # v31: get_H2H também devolve os últimos resultados de cada equipe. Essa
        # rota é especialmente útil no começo da temporada e para promovidos, quando
        # o get_events da liga atual ainda possui poucas partidas. Não contamos
        # duplicatas: match_id continua sendo a chave de deduplicação abaixo.
        if len(league_finished) < int(limit_per_team):
            nonlocal h2h_payload
            if h2h_payload is None:
                h2h_payload, _ = gm_apifootball_request(
                    "get_H2H", firstTeam=team_a, secondTeam=team_b,
                    timezone="America/Sao_Paulo",
                )
            if isinstance(h2h_payload, dict):
                for hkey in ("firstTeam_lastResults", "secondTeam_lastResults", "firstTeam_VS_secondTeam"):
                    extra = h2h_payload.get(hkey) or []
                    if isinstance(extra, list):
                        pooled.extend([x for x in extra if isinstance(x, dict)])

        seen, finished = set(), []
        for ev in pooled:
            if not isinstance(ev, dict) or not is_finished(ev) or not belongs_to(ev, targets, team_id):
                continue
            mid = str(ev.get("match_id") or "").strip()
            if mid and mid in seen:
                continue
            if mid:
                seen.add(mid)
            item = dict(ev)
            item["_gm_official"] = official_event(item)
            item["_gm_selected_league"] = str(item.get("league_id") or "") == str(league_id or "")
            finished.append(item)
        finished.sort(key=lambda x: (1 if x.get("_gm_selected_league") else 0, str(x.get("match_date") or "")), reverse=True)
        official = [x for x in finished if x.get("_gm_official")]
        chosen = official[:int(limit_per_team)]
        if len(chosen) < min(8, int(limit_per_team)):
            used = {str(x.get("match_id") or "") for x in chosen}
            chosen += [x for x in finished if str(x.get("match_id") or "") not in used][:int(limit_per_team)-len(chosen)]

        acc = empty_team(team_name)
        detailed = 0
        used_ids = []
        for ev in chosen:
            hid = str(ev.get("match_hometeam_id") or "").strip()
            aid = str(ev.get("match_awayteam_id") or "").strip()
            tid = str(team_id or "").strip()
            if tid and hid == tid:
                side = "home"
            elif tid and aid == tid:
                side = "away"
            else:
                hn = str(ev.get("match_hometeam_name") or "")
                side = "home" if any(_gm_team_name_match(t, hn) for t in targets) else "away"
            opp = "away" if side == "home" else "home"
            hs = to_num(ev.get("match_hometeam_ft_score") or ev.get("match_hometeam_score"))
            aas = to_num(ev.get("match_awayteam_ft_score") or ev.get("match_awayteam_score"))
            if hs is not None and aas is not None:
                acc["Jogos"] += 1
                gf = hs if side == "home" else aas
                ga = aas if side == "home" else hs
                acc["Gols pró"] += gf
                acc["Gols contra"] += ga
            stats = _gm_api_stat_map(ev.get("statistics"))
            half = _gm_api_stat_map(ev.get("statistics_1half"))
            if stats:
                detailed += 1
            for metric, aliases in stat_aliases.items():
                add_metric(acc, metric, stat_value(stats, aliases, side))
            add_metric(acc, "Finalizações contra", stat_value(stats, stat_aliases["Finalizações"], opp))
            add_metric(acc, "Chutes no alvo contra", stat_value(stats, stat_aliases["Chutes no alvo"], opp))

            c1 = stat_value(half, stat_aliases["Escanteios"], side)
            add_metric(acc, "Escanteios 1T", c1)
            ctot = stat_value(stats, stat_aliases["Escanteios"], side)
            nct, nc1 = to_num(ctot), to_num(c1)
            if nct is not None and nc1 is not None and nct >= nc1:
                add_metric(acc, "Escanteios 2T", nct - nc1)

            add_metric(acc, "Finalizações 1T", stat_value(half, stat_aliases["Finalizações"], side))
            add_metric(acc, "Chutes no alvo 1T", stat_value(half, stat_aliases["Chutes no alvo"], side))

            hhs, has = halftime_score_from_event(ev)
            if hhs is not None and has is not None and hs is not None and aas is not None:
                own_ht = hhs if side == "home" else has
                own_ft = hs if side == "home" else aas
                add_metric(acc, "Gols 1T", own_ht)
                if own_ft >= own_ht:
                    add_metric(acc, "Gols 2T", own_ft - own_ht)

            # Cartões: eventos são fallback real quando o bloco agregado não veio.
            cards = ev.get("cards") or []
            if isinstance(cards, list):
                yc = rc = 0
                for card in cards:
                    if not isinstance(card, dict):
                        continue
                    belongs = bool(card.get("home_fault")) if side == "home" else bool(card.get("away_fault"))
                    if not belongs:
                        continue
                    ct = _gm_api_norm(card.get("card"))
                    if "yellow" in ct:
                        yc += 1
                    elif "red" in ct:
                        rc += 1
                if stat_value(stats, stat_aliases["Amarelos"], side) is None:
                    add_metric(acc, "Amarelos", yc)
                if stat_value(stats, stat_aliases["Vermelhos"], side) is None:
                    add_metric(acc, "Vermelhos", rc)
            used_ids.append(str(ev.get("match_id") or ""))

        if acc.get("Jogos", 0) <= 0:
            return None
        outdf = finish_averages({str(team_name): acc})
        if outdf.empty:
            return None
        row = outdf.iloc[0].to_dict()
        row["_n_Gols pró"] = int(acc["Jogos"]); row["_n_Gols contra"] = int(acc["Jogos"])
        metrics = ["Escanteios","Amarelos","Vermelhos","Faltas","Finalizações","Chutes no alvo","Impedimentos","Posse (%)","Passes","Precisão passes (%)","Finalizações contra","Chutes no alvo contra","Escanteios 1T","Escanteios 2T","Finalizações 1T","Chutes no alvo 1T","Gols 1T","Gols 2T"]
        return {
            "row": row, "games": int(acc["Jogos"]), "detailed_games": int(detailed),
            "coverage": {m:int(row.get(f"_n_{m}",0) or 0) for m in metrics},
            "event_ids": used_ids, "team_id": str(team_id), "league_id": str(league_id or ""),
            "source":"APIfootball · fonte primária validada",
        }

    aid = resolve_from_league(team_a) or resolve_from_h2h(team_a)
    bid = resolve_from_league(team_b) or resolve_from_h2h(team_b)
    return {team_a: profile(team_a, aid), team_b: profile(team_b, bid), "_error": None}


def contextual_analysis_rows(team_a, team_b, competition_name, competition_df, recent_games=10):
    """Recupera contexto adicional apenas onde a base principal é curta/incompleta.

    Prioridade: base da competição -> perfil doméstico -> histórico recente
    detalhado SofaScore. Cada mercado mantém sua própria cobertura; ausência de
    uma métrica nunca é convertida em zero nem em probabilidade fictícia.
    """
    base_a = competition_df[competition_df["Time"] == team_a].iloc[0].to_dict()
    base_b = competition_df[competition_df["Time"] == team_b].iloc[0].to_dict()
    ga = int(float(base_a.get("Jogos", 0) or 0)); gb = int(float(base_b.get("Jogos", 0) or 0))

    key_detail = ["Escanteios", "Amarelos", "Finalizações", "Chutes no alvo"]
    # v20: APIfootball é consultada para TODA análise VIP das 21 competições
    # auditadas. A decisão de buscar fontes adicionais passa a ser por métrica.
    detailed_gap = any(
        _gm_source_metric_count(row, metric) < 8
        for row in (base_a, base_b) for metric in key_detail
    )
    low_results = min(ga, gb) < 6
    contextual = competition_name in CONTEXTUAL_COMPETITIONS

    prof_a = load_team_domestic_profile(team_a, recent_games) if (contextual or low_results) else None
    prof_b = load_team_domestic_profile(team_b, recent_games) if (contextual or low_results) else None

    hist = load_competition_history_context(
        competition_name,
        current_season_year(COMPETITIONS[competition_name]["season"]), 5
    ) if contextual else {"matches": [], "goal_avg": None, "seasons": 0}
    priors = dict(COMPETITION_PRIORS.get(competition_name, {}))
    if hist.get("goal_avg"):
        priors["Gols"] = max(1.8, min(3.8, float(hist["goal_avg"])))

    # v20: APIfootball é a camada primária em todas as 21 ligas já auditadas.
    # SofaScore/ESPN/Football-Data só entram quando uma métrica chave da API fica
    # abaixo de N=8, preservando custo e evitando sobreposição desnecessária.
    recovery_a = recovery_b = None
    sofa_a = sofa_b = espn_a = espn_b = fd_a = fd_b = None
    api_pair = gm_apifootball_pair_profiles(
        team_a, team_b, competition_name=competition_name,
        limit_per_team=20, lookback_days=520,
    )
    api_a = api_pair.get(team_a) if isinstance(api_pair, dict) else None
    api_b = api_pair.get(team_b) if isinstance(api_pair, dict) else None

    def api_has_gap(profile):
        if not profile or not isinstance(profile.get("row"), dict):
            return True
        row = profile["row"]
        return any(_gm_source_metric_count(row, m, profile.get("games", 0)) < 8 for m in key_detail)

    need_fallback = detailed_gap or low_results or api_has_gap(api_a) or api_has_gap(api_b)
    if need_fallback:
        event = find_sofascore_event(team_a, team_b, search_days=7)
        aid = (event or {}).get("home_id")
        bid = (event or {}).get("away_id")
        if not aid:
            found = _gm_sofascore_team_search_id(team_a); aid = (found or {}).get("id")
        if not bid:
            found = _gm_sofascore_team_search_id(team_b); bid = (found or {}).get("id")
        if aid:
            sofa_a = load_sofascore_recent_profile(aid, team_a, min(max(int(recent_games), 12), 18))
        if bid:
            sofa_b = load_sofascore_recent_profile(bid, team_b, min(max(int(recent_games), 12), 18))

        league_slug = ESPN_FIXTURE_LEAGUES.get(competition_name)
        if league_slug:
            espn_a = load_espn_recent_profile(league_slug, team_a, min(max(int(recent_games), 12), 18))
            espn_b = load_espn_recent_profile(league_slug, team_b, min(max(int(recent_games), 12), 18))

        fd_a = load_fd_historical_team_profile(competition_name, team_a, min(max(int(recent_games), 12), 18))
        fd_b = load_fd_historical_team_profile(competition_name, team_b, min(max(int(recent_games), 12), 18))

    recovery_a = _gm_merge_recovery_profiles(api_a, sofa_a, espn_a, fd_a)
    recovery_b = _gm_merge_recovery_profiles(api_b, sofa_b, espn_b, fd_b)

    def build(base, prof, recovery):
        out = dict(base)
        cg = int(float(base.get("Jogos", 0) or 0))
        prow = (prof or {}).get("row", {})
        strength = (prof or {}).get("league_strength", 0.95)
        rrow = (recovery or {}).get("row", {})
        recovery_used = []

        goal_team_prior = (priors.get("Gols") / 2.0) if priors.get("Gols") else None
        corner_team_prior = (priors.get("Escanteios") / 2.0) if priors.get("Escanteios") else None
        card_team_prior = (priors.get("Cartões") / 2.0) if priors.get("Cartões") else None
        prior_map = {
            "Gols pró": goal_team_prior, "Gols contra": goal_team_prior,
            "Escanteios": corner_team_prior, "Amarelos": card_team_prior,
            "Vermelhos": 0.10 if priors.get("Cartões") else None,
            "Finalizações": None, "Chutes no alvo": None, "Faltas": None,
            "Impedimentos": None, "Posse (%)": None,
            "Finalizações contra": None, "Chutes no alvo contra": None,
            "Escanteios 1T": None, "Escanteios 2T": None,
            "Finalizações 1T": None, "Chutes no alvo 1T": None,
            "Gols 1T": None, "Gols 2T": None,
        }
        metrics = [
            "Gols pró", "Gols contra", "Escanteios", "Amarelos", "Vermelhos",
            "Faltas", "Finalizações", "Chutes no alvo", "Impedimentos", "Posse (%)",
            "Finalizações contra", "Chutes no alvo contra",
            "Escanteios 1T", "Escanteios 2T", "Finalizações 1T", "Chutes no alvo 1T",
            "Gols 1T", "Gols 2T",
        ]
        for metric in metrics:
            current_n = _gm_source_metric_count(base, metric, cg)
            domestic_value = prow.get(metric)
            blended = _blend_metric(
                base.get(metric), cg, domestic_value, prior_map.get(metric),
                strength, metric,
            )
            # A cobertura doméstica não é somada à principal: podem conter jogos
            # iguais. Ela só oferece valor contextual; a amostra é conservadora.
            domestic_n = _gm_source_metric_count(prow, metric, (prof or {}).get("games", 0)) if prow else 0
            context_n = max(current_n, domestic_n if domestic_value is not None else 0)

            rec_val = rrow.get(metric) if rrow else None
            rec_n = _gm_source_metric_count(rrow, metric, (recovery or {}).get("games", 0)) if rrow else 0
            metric_source = ((recovery or {}).get("metric_sources") or {}).get(metric, "")
            prefer_api = str(metric_source).startswith("APIfootball") and rec_n >= 8
            final_value, effective_n, used = _gm_blend_recovery_metric(
                blended, context_n, rec_val, rec_n, prefer_recovery=prefer_api
            )
            out[metric] = final_value
            if metric_source:
                out[f"_gm_source_{metric}"] = metric_source
            out[f"_n_{metric}"] = int(effective_n or 0)
            if used:
                recovery_used.append(metric)

        out["_gm_recovery_used"] = bool(recovery_used)
        out["_gm_recovery_metrics"] = ", ".join(recovery_used)
        out["_gm_recovery_source"] = (recovery or {}).get("source") if recovery_used else None
        return pd.Series(out)

    a = build(base_a, prof_a, recovery_a)
    b = build(base_b, prof_b, recovery_b)

    # v9: última barreira antes do Inconclusivo. Se a equipe ficou sem uma
    # métrica detalhada por falha de cobertura externa, usa apenas a distribuição
    # REAL da própria competição e limita a confiança a Cautela (N efetivo <= 7).
    a = _gm_apply_competition_fallback(a, competition_df)
    b = _gm_apply_competition_fallback(b, competition_df)

    public_h2h = fetch_sofascore_h2h(team_a, team_b)
    h2h_pool = (list(competition_df.attrs.get("matches", [])) +
                list(hist.get("matches", [])) + list(public_h2h or []))
    h2d = _h2h_details(team_a, team_b, h2h_pool, 10)
    hh, ah, h2n = h2d["home_share"], h2d["away_share"], h2d["games"]

    def relevance(prof):
        if not prof:
            return 0.50
        league_component = max(0.0, min(1.0, (prof["league_strength"] - 0.82) / 0.36))
        elo = prof.get("elo")
        if elo is not None:
            elo_component = max(0.0, min(1.0, (float(elo) - 1350.0) / 700.0))
            return (0.58 * elo_component + 0.20 * league_component +
                    0.14 * prof.get("season_strength", .5) + 0.08 * prof.get("form", .5))
        return 0.48 * league_component + 0.34 * prof.get("season_strength", .5) + 0.18 * prof.get("form", .5)

    ctx = {
        "competition_name": competition_name,
        "home_profile": prof_a, "away_profile": prof_b,
        "history": hist, "h2h_games": h2n, "h2h_home": hh, "h2h_away": ah,
        "h2h_home_wins": h2d.get("home_wins", 0), "h2h_away_wins": h2d.get("away_wins", 0),
        "h2h_draws": h2d.get("draws", 0),
        "home_relevance": relevance(prof_a), "away_relevance": relevance(prof_b),
        "competition_games_home": ga, "competition_games_away": gb,
        "priors": priors,
        "data_recovery_home": recovery_a,
        "data_recovery_away": recovery_b,
        "data_recovery_version": "v11-historical-stat-recovery",
        "data_recovery_debug": {
            "home": {"apifootball": _gm_recovery_diagnostic(api_a), "merged": _gm_recovery_diagnostic(recovery_a), "sofascore": _gm_recovery_diagnostic(sofa_a), "espn": _gm_recovery_diagnostic(espn_a), "football_data": _gm_recovery_diagnostic(fd_a)},
            "away": {"apifootball": _gm_recovery_diagnostic(api_b), "merged": _gm_recovery_diagnostic(recovery_b), "sofascore": _gm_recovery_diagnostic(sofa_b), "espn": _gm_recovery_diagnostic(espn_b), "football_data": _gm_recovery_diagnostic(fd_b)},
        },
    }
    return a, b, ctx


def _attack_power_from_row(row, league_strength=1.0):
    """Índice ofensivo comparável entre ligas, usando somente métricas disponíveis.

    Não é uma probabilidade. Serve para impedir que uma sequência doméstica em
    uma liga mais fraca seja tratada como equivalente ao mesmo volume ofensivo
    produzido numa liga de nível superior.
    """
    if row is None:
        return 1.0
    try:
        gf = metric_value(row, "Gols pró")
        sot = metric_value(row, "Chutes no alvo")
        shots = metric_value(row, "Finalizações")
        poss = metric_value(row, "Posse (%)")
    except Exception:
        gf = sot = shots = poss = None

    parts = []
    if gf is not None:
        parts.append((max(0.35, min(1.85, float(gf) / 1.45)), 0.50))
    if sot is not None:
        parts.append((max(0.40, min(1.75, float(sot) / 4.6)), 0.27))
    if shots is not None:
        parts.append((max(0.45, min(1.65, float(shots) / 12.5)), 0.16))
    if poss is not None:
        parts.append((max(0.70, min(1.30, float(poss) / 50.0)), 0.07))
    if not parts:
        raw = 1.0
    else:
        den = sum(w for _, w in parts)
        raw = sum(v*w for v, w in parts) / den

    # O mesmo número ofensivo vale mais quando produzido semanalmente em uma
    # competição mais forte. Limite conservador para não duplicar o efeito Elo.
    league_adj = max(0.88, min(1.14, 1.0 + (float(league_strength) - 1.0) * 0.72))
    return max(0.45, min(1.75, raw * league_adj))



@st.cache_data(ttl=21600, show_spinner=False)
def gm_historical_team_strength(team, competition_name, lookback=3):
    """Força estrutural do clube baseada em temporadas anteriores da liga.

    É um prior de longo prazo, não uma probabilidade de partida. A função usa
    somente resultados históricos reais das mesmas fontes públicas já usadas
    pelo GM SCORE. Serve principalmente no início da temporada, quando 1-5 jogos
    podem supervalorizar uma boa arrancada de um clube recém-promovido ou
    subvalorizar uma equipe historicamente dominante.
    """
    cfg = COMPETITIONS.get(str(competition_name or ""))
    if not cfg or competition_name in CONTEXTUAL_COMPETITIONS:
        return None
    try:
        current_year = current_season_year(cfg["season"])
    except Exception:
        return None

    wanted = _norm_team(team)
    if not wanted:
        return None

    seasons = []
    for offset in range(1, int(lookback) + 1):
        year = current_year - offset
        matches = []
        try:
            if cfg.get("kind") == "football_data":
                raw = load_football_data(cfg["code"], year)
                for _, g in raw.iterrows():
                    h = str(g.get("HomeTeam") or "")
                    a = str(g.get("AwayTeam") or "")
                    if wanted not in {_norm_team(h), _norm_team(a)}:
                        continue
                    hg = pd.to_numeric(g.get("FTHG"), errors="coerce")
                    ag = pd.to_numeric(g.get("FTAG"), errors="coerce")
                    if pd.isna(hg) or pd.isna(ag):
                        continue
                    matches.append({"home": h, "away": a, "hg": float(hg), "ag": float(ag)})
            else:
                hist = None
                try:
                    hist = load_open_results(cfg.get("id"), year, cfg.get("season"))
                except Exception:
                    hist = None
                if isinstance(hist, pd.DataFrame):
                    for m in list(hist.attrs.get("matches", []) or []):
                        if wanted in {_norm_team(m.get("home")), _norm_team(m.get("away"))}:
                            matches.append(m)
        except Exception:
            matches = []

        if not matches:
            continue

        pts = gf = ga = games = 0.0
        for m in matches:
            is_home = _norm_team(m.get("home")) == wanted
            tg = float(m.get("hg", 0) if is_home else m.get("ag", 0))
            ta = float(m.get("ag", 0) if is_home else m.get("hg", 0))
            games += 1.0
            gf += tg; ga += ta
            pts += 3.0 if tg > ta else 1.0 if tg == ta else 0.0
        if games <= 0:
            continue
        pts_pct = pts / (3.0 * games)
        gdpg = (gf - ga) / games
        gd_score = 0.50 + 0.24 * math.tanh(gdpg / 0.90)
        season_score = max(0.05, min(0.95, 0.72 * pts_pct + 0.28 * gd_score))
        seasons.append({"score": season_score, "games": int(games), "offset": offset})

    # Ausência recente da primeira divisão é informação estrutural útil, mas
    # recebe valor conservador para não transformar promoção em condenação.
    if not seasons:
        return {
            "score": 0.38,
            "seasons": 0,
            "presence": 0.0,
            "source": "histórico recente da liga · sem presença nas temporadas consultadas",
        }

    num = den = 0.0
    for item in seasons:
        # temporadas mais recentes valem mais, sem apagar o histórico anterior
        w = {1: 1.00, 2: 0.72, 3: 0.52}.get(int(item["offset"]), 0.40)
        num += float(item["score"]) * w
        den += w
    score = num / den if den else 0.50
    presence = min(1.0, len(seasons) / max(float(lookback), 1.0))
    # Presença contínua na elite também é sinal de estabilidade estrutural.
    score = max(0.05, min(0.95, score * 0.90 + (0.36 + 0.18 * presence) * 0.10))
    return {
        "score": score,
        "seasons": len(seasons),
        "presence": presence,
        "source": "histórico recente da liga",
    }


def global_quality_prior(home, away, df=None, ctx=None):
    """Prior 1X2 independente de odds baseado em qualidade estrutural.

    Combina ClubElo quando disponível, força da liga, potencial ofensivo,
    desempenho da temporada e forma recente. É especialmente importante em
    torneios continentais, onde 2-5 jogos do torneio não representam a força
    real dos clubes.
    """
    ctx = ctx or {}
    hp = ctx.get("home_profile") or {}
    ap = ctx.get("away_profile") or {}

    def row_from(team, profile):
        prow = profile.get("row")
        if prow:
            return pd.Series(prow)
        try:
            if df is not None and not df.empty:
                hit = df[df["Time"] == team]
                if not hit.empty:
                    return hit.iloc[0]
        except Exception:
            pass
        return None

    hr = row_from(home, hp)
    ar = row_from(away, ap)
    hls = float(hp.get("league_strength", 1.0) or 1.0)
    als = float(ap.get("league_strength", 1.0) or 1.0)
    helo = hp.get("elo")
    aelo = ap.get("elo")
    if helo is None:
        helo = (clubelo_rating(home) or {}).get("elo")
    if aelo is None:
        aelo = (clubelo_rating(away) or {}).get("elo")

    hatk = _attack_power_from_row(hr, hls)
    aatk = _attack_power_from_row(ar, als)
    hform = float(hp.get("form", 0.50) or 0.50)
    aform = float(ap.get("form", 0.50) or 0.50)
    hseason = float(hp.get("season_strength", 0.50) or 0.50)
    aseason = float(ap.get("season_strength", 0.50) or 0.50)

    # v30: força de longo prazo. No começo da temporada, duas ou três partidas
    # não podem colocar uma equipe recém-promovida no mesmo patamar de um clube
    # que vem sustentando desempenho de elite por várias temporadas. O cálculo
    # é automático e baseado em resultados históricos, nunca em lista manual.
    selected_comp = ctx.get("competition_name")
    hhist_comp = hp.get("competition") or selected_comp
    ahist_comp = ap.get("competition") or selected_comp
    hhist = gm_historical_team_strength(home, hhist_comp, 3) if hhist_comp else None
    ahist = gm_historical_team_strength(away, ahist_comp, 3) if ahist_comp else None
    hhist_score = float((hhist or {}).get("score", 0.50) or 0.50)
    ahist_score = float((ahist or {}).get("score", 0.50) or 0.50)

    # Hierarquia estrutural interligas. O nível do campeonato doméstico é um
    # componente próprio (não apenas um pequeno ajuste do Elo), porque campanhas
    # idênticas em ligas de forças muito diferentes não são equivalentes.
    home_adv = 42.0
    league_component = (hls - als) * 620.0
    attack_component = (hatk - aatk) * 150.0
    season_component = (hseason - aseason) * 75.0
    form_component = (hform - aform) * 42.0
    # Longo prazo ganha peso material, mas continua abaixo de um Elo válido.
    # Quando um clube não esteve na elite nas temporadas consultadas, o prior
    # fica conservador (0,38) em vez de assumir força média de 0,50.
    history_component = (hhist_score - ahist_score) * 700.0

    # Confronto direto histórico entra como evidência adicional quando existe.
    # O peso cresce com a quantidade de jogos, mas é limitado para não transformar
    # partidas antigas em destino inevitável. Em continentais isso ajuda a separar
    # força doméstica aparente de desempenho real contra o mesmo adversário.
    h2h_games = int(ctx.get("h2h_games", 0) or 0)
    h2h_home = float(ctx.get("h2h_home", 0.5) or 0.5)
    h2h_away = float(ctx.get("h2h_away", 0.5) or 0.5)
    h2h_reliability = min(h2h_games / 5.0, 1.0)
    h2h_component = (h2h_home - h2h_away) * 430.0 * h2h_reliability

    if helo is not None and aelo is not None:
        # Elo segue importante, mas não pode sozinho apagar liga + H2H + produção.
        elo_component = (float(helo) - float(aelo)) * 0.82
        quality_diff = (home_adv + elo_component + league_component +
                        attack_component + season_component + form_component +
                        history_component + h2h_component)
    else:
        # Sem Elo, ampliamos a tradução entre ligas mantendo os demais sinais.
        quality_diff = (home_adv + (hls - als) * 980.0 +
                        (hatk - aatk) * 175.0 +
                        (hseason - aseason) * 90.0 +
                        (hform - aform) * 52.0 +
                        (hhist_score - ahist_score) * 820.0 + h2h_component)

    quality_diff = max(-520.0, min(520.0, quality_diff))
    expected = 1.0 / (1.0 + 10.0 ** (-quality_diff / 400.0))
    closeness = 1.0 - min(abs(quality_diff) / 360.0, 1.0)
    draw = 0.225 + 0.065 * closeness
    decisive = 1.0 - draw
    home_p = decisive * expected
    away_p = decisive * (1.0 - expected)
    total = home_p + draw + away_p
    return {
        "home": home_p / total * 100.0,
        "draw": draw / total * 100.0,
        "away": away_p / total * 100.0,
        "quality_diff": quality_diff,
        "home_attack": hatk,
        "away_attack": aatk,
        "home_elo": helo,
        "away_elo": aelo,
        "home_history_strength": hhist_score,
        "away_history_strength": ahist_score,
        "home_history_seasons": int((hhist or {}).get("seasons", 0) or 0),
        "away_history_seasons": int((ahist or {}).get("seasons", 0) or 0),
    }


def calibrate_with_global_quality(model_probs, quality_prior, sample=0, contextual=False):
    """Mistura o modelo de jogo com a hierarquia estrutural dos clubes.

    Em torneios continentais com pouca amostra, qualidade global tem peso maior.
    Em ligas maduras, entra apenas como estabilizador. Odds não participam daqui.
    """
    if not model_probs or not quality_prior:
        return model_probs
    out = dict(model_probs)
    m = [float(out[k]) for k in ("home", "draw", "away")]
    q = [float(quality_prior[k]) for k in ("home", "draw", "away")]
    qfav = max(range(3), key=lambda i: q[i])
    mfav = max(range(3), key=lambda i: m[i])
    qgap = q[qfav] - sorted(q, reverse=True)[1]

    if contextual:
        # Em continentais, a amostra inicial do torneio é pequena demais para
        # superar a hierarquia interligas. Ela ganha espaço conforme os jogos chegam.
        w = max(0.48, 0.72 - min(float(sample), 8.0) * 0.030)
    else:
        # v30: a mesma proteção vale no início de ligas nacionais. Uma arrancada
        # de 1-5 partidas não pode apagar a hierarquia estrutural de longo prazo.
        # Conforme a temporada amadurece, os dados atuais voltam a dominar.
        if float(sample) <= 2:
            w = 0.58
        elif float(sample) <= 5:
            w = 0.50
        else:
            w = max(0.20, 0.36 - min(float(sample), 12.0) * 0.010)

    # Se a hierarquia estrutural aponta favorito claro e o modelo de poucos
    # jogos aponta o adversário, aumenta o guardrail.
    if qfav != mfav and qgap >= 10:
        if contextual:
            w = max(w, 0.76)
        elif float(sample) <= 5:
            w = max(w, 0.62)
        else:
            w = max(w, 0.46)
    if qgap >= 20:
        if contextual:
            w = max(w, 0.82)
        elif float(sample) <= 5:
            w = max(w, 0.68)
        else:
            w = max(w, 0.50)

    vals = [m[i] * (1.0-w) + q[i] * w for i in range(3)]

    # Guardrail independente de casas de aposta: se qualidade global + nível da
    # liga + H2H produzem favorito claro, ruído de uma amostra curta não pode
    # terminar invertendo o lado favorito. Preserva a intensidade do modelo.
    if contextual and qgap >= 10:
        vfav = max(range(3), key=lambda i: vals[i])
        if vfav != qfav:
            floor = max(38.0, q[qfav] - 5.0)
            rest = [i for i in range(3) if i != qfav]
            rest_total = max(sum(vals[i] for i in rest), 1e-9)
            remaining = 100.0 - floor
            vals[qfav] = floor
            for i in rest:
                vals[i] = remaining * vals[i] / rest_total
            out["structural_guardrail"] = True

    z = sum(vals)
    vals = [v / z * 100.0 for v in vals]
    out["home"], out["draw"], out["away"] = vals
    out["quality_calibrated"] = True
    out["quality_weight"] = w
    out["quality_diff"] = quality_prior.get("quality_diff")
    out["home_attack_index"] = quality_prior.get("home_attack")
    out["away_attack_index"] = quality_prior.get("away_attack")
    if out.get("home_elo") is None:
        out["home_elo"] = quality_prior.get("home_elo")
    if out.get("away_elo") is None:
        out["away_elo"] = quality_prior.get("away_elo")
    return out


def apply_evidence_consensus_guardrail(probs, quality_prior, ctx=None, moneyline=None):
    """Evita inversões quando múltiplas evidências independentes apontam o mesmo lado.

    Não escolhe clubes por nome. Conta sinais objetivos: H2H, diferença entre ligas,
    Elo/força global, ataque e mercado público. O mando já está embutido no prior de
    qualidade e reduz a força do visitante, mas não deve superar sozinho um consenso forte.
    """
    if not probs or not quality_prior:
        return probs
    ctx = ctx or {}
    hp, ap = ctx.get("home_profile") or {}, ctx.get("away_profile") or {}
    score_h = score_a = 0.0
    reasons_h, reasons_a = [], []

    # H2H é um sinal forte quando há pelo menos 3 confrontos.
    n = int(ctx.get("h2h_games", 0) or 0)
    hg = float(ctx.get("h2h_home", .5) or .5)
    ag = float(ctx.get("h2h_away", .5) or .5)
    if n >= 3 and abs(hg-ag) >= .20:
        w = 2.25 + min(n, 8) * .14
        if hg > ag: score_h += w; reasons_h.append("H2H")
        else: score_a += w; reasons_a.append("H2H")

    # Nível da liga: diferença material entre campeonatos nacionais.
    hls = float(hp.get("league_strength", 1.0) or 1.0)
    als = float(ap.get("league_strength", 1.0) or 1.0)
    if abs(hls-als) >= .055:
        w = min(2.0, 0.9 + abs(hls-als) * 6.0)
        if hls > als: score_h += w; reasons_h.append("liga")
        else: score_a += w; reasons_a.append("liga")

    # Elo/força global, sem o mando, para medir qualidade estrutural pura.
    he, ae = quality_prior.get("home_elo"), quality_prior.get("away_elo")
    if he is not None and ae is not None and abs(float(he)-float(ae)) >= 55:
        w = min(2.1, 0.9 + abs(float(he)-float(ae))/180.0)
        if float(he) > float(ae): score_h += w; reasons_h.append("força global")
        else: score_a += w; reasons_a.append("força global")

    # Histórico estrutural recente na elite. Útil sobretudo nas primeiras
    # rodadas, quando a forma atual ainda tem amostra pequena.
    hhist = float(quality_prior.get("home_history_strength", 0.50) or 0.50)
    ahist = float(quality_prior.get("away_history_strength", 0.50) or 0.50)
    if abs(hhist-ahist) >= .14:
        w = min(2.0, 1.05 + abs(hhist-ahist) * 3.0)
        if hhist > ahist: score_h += w; reasons_h.append("histórico estrutural")
        else: score_a += w; reasons_a.append("histórico estrutural")

    # Potencial ofensivo comparável entre ligas.
    ha = float(quality_prior.get("home_attack", 1.0) or 1.0)
    aa = float(quality_prior.get("away_attack", 1.0) or 1.0)
    if abs(ha-aa) >= .10:
        w = min(1.3, .65 + abs(ha-aa)*2.5)
        if ha > aa: score_h += w; reasons_h.append("ataque")
        else: score_a += w; reasons_a.append("ataque")

    # Mercado é validação externa, nunca sinal único para impor favorito.
    market = None
    if moneyline:
        market = [float(moneyline[f"{k}_prob"]) for k in ("home","draw","away")]
        mf = 0 if market[0] >= market[2] else 2
        side_prob = market[mf]
        opp_prob = market[2 if mf == 0 else 0]
        if side_prob >= 44 and side_prob - opp_prob >= 10:
            if mf == 0: score_h += 1.15; reasons_h.append("mercado")
            else: score_a += 1.15; reasons_a.append("mercado")

    side = None
    diff = score_h - score_a
    if diff >= 2.15 and len(set(reasons_h)) >= 2:
        side = 0
    elif diff <= -2.15 and len(set(reasons_a)) >= 2:
        side = 2
    if side is None:
        return probs

    out = dict(probs)
    vals = [float(out["home"]), float(out["draw"]), float(out["away"])]
    other = 2 if side == 0 else 0
    # Alvo baseado nos dados, não em um número por clube. Mercado e prior de
    # qualidade entram apenas se apontarem o mesmo lado do consenso.
    anchors = [float(quality_prior["home" if side == 0 else "away"])]
    if market is not None and ((side == 0 and market[0] > market[2]) or (side == 2 and market[2] > market[0])):
        anchors.append(market[side])
    h2_side = hg if side == 0 else ag
    if n >= 3:
        # Converte domínio H2H em âncora conservadora de 1X2 (não copia o share de pontos).
        anchors.append(36.0 + 22.0 * max(0.0, min(1.0, h2_side)))
    target = sum(anchors)/len(anchors)
    target = max(42.0, min(62.0, target))

    # Se o modelo está invertido, corrige com força; se já concorda, só estabiliza.
    blend = .78 if vals[side] <= vals[other] else .38
    desired = vals[side]*(1-blend) + target*blend
    if vals[side] <= vals[other]:
        desired = max(desired, vals[other] + 4.0)
    desired = min(desired, 64.0)
    remainder = 100.0 - desired
    rest = [i for i in range(3) if i != side]
    rest_total = max(sum(vals[i] for i in rest), 1e-9)
    vals[side] = desired
    for i in rest:
        vals[i] = remainder * vals[i] / rest_total
    z = sum(vals)
    vals = [v/z*100 for v in vals]
    out["home"], out["draw"], out["away"] = vals
    out["evidence_guardrail"] = True
    out["evidence_home_score"] = score_h
    out["evidence_away_score"] = score_a
    out["evidence_reasons"] = reasons_h if side == 0 else reasons_a
    return out


def contextual_victory_probabilities(home, away, a, b, ctx):
    if not ctx:
        return None
    hgf, hga = metric_value(a, "Gols pró"), metric_value(a, "Gols contra")
    agf, aga = metric_value(b, "Gols pró"), metric_value(b, "Gols contra")
    if None in (hgf, hga, agf, aga):
        return None

    hp = ctx.get("home_profile") or {}
    ap = ctx.get("away_profile") or {}
    helo, aelo = hp.get("elo"), ap.get("elo")

    # Base de gols com mando moderado. O mando vale menos que uma diferença
    # estrutural grande de qualidade entre os clubes.
    lam_h = max(0.25, ((hgf + aga) / 2.0) * 1.055)
    lam_a = max(0.22, ((agf + hga) / 2.0) / 1.055)

    rel_delta = max(-0.22, min(0.22, (ctx.get("home_relevance", .5) - ctx.get("away_relevance", .5)) * 0.44))
    lam_h *= 1 + rel_delta
    lam_a *= 1 - rel_delta

    # Quando ClubElo está disponível, ele é usado diretamente como âncora
    # interligas. +55 pontos representam aproximadamente o mando de campo.
    elo_prior = None
    if helo is not None and aelo is not None:
        elo_diff = float(helo) + 55.0 - float(aelo)
        # converte diferença Elo em expectativa relativa; cap evita extremos.
        strength_mult = max(0.72, min(1.38, math.exp(elo_diff / 700.0)))
        lam_h *= math.sqrt(strength_mult)
        lam_a /= math.sqrt(strength_mult)

        score_expect = 1.0 / (1.0 + 10.0 ** (-elo_diff / 400.0))
        draw_prior = 0.25 + 0.035 * (1.0 - min(abs(elo_diff) / 300.0, 1.0))
        decisive = 1.0 - draw_prior
        # score_expect é expectativa Elo; transformamos em uma distribuição 1X2
        # conservadora, sem usar odds de casas de aposta.
        home_share = max(0.08, min(0.92, score_expect))
        elo_prior = (decisive * home_share, draw_prior, decisive * (1.0 - home_share))

    if ctx.get("h2h_games", 0) >= 2:
        # H2H tem peso perceptível, porém limitado. A quantidade de confrontos
        # controla a confiança para evitar exagero com apenas dois jogos antigos.
        h2_rel = min(float(ctx.get("h2h_games", 0)) / 5.0, 1.0)
        h2_gap = float(ctx.get("h2h_home", .5)) - float(ctx.get("h2h_away", .5))
        h2_delta = max(-0.18, min(0.18, h2_gap * 0.24 * h2_rel))
        lam_h *= 1 + h2_delta
        lam_a *= 1 - h2_delta

    lam_h = max(.30, min(lam_h, 3.1)); lam_a = max(.28, min(lam_a, 3.0))
    ph, pd_, pa = _poisson_result_probs(lam_h, lam_a)

    # Mistura Poisson com força global. No começo do torneio, Elo/relevância
    # tem peso maior; conforme há jogos na competição, os dados atuais dominam.
    sample = min(ctx.get("competition_games_home", 0), ctx.get("competition_games_away", 0))
    if elo_prior is not None:
        elo_w = max(0.22, 0.42 - sample * 0.035)
        ph = ph * (1-elo_w) + elo_prior[0] * elo_w
        pd_ = pd_ * (1-elo_w) + elo_prior[1] * elo_w
        pa = pa * (1-elo_w) + elo_prior[2] * elo_w

    # Regressão residual; não força mais o mandante para 42% quando o visitante
    # é claramente superior em força global.
    w = min(.92, .68 + sample * .04)
    baseline = elo_prior if elo_prior is not None else (.39, .29, .32)
    ph = ph*w + baseline[0]*(1-w)
    pd_ = pd_*w + baseline[1]*(1-w)
    pa = pa*w + baseline[2]*(1-w)

    probs = [max(.06, ph), max(.075, pd_), max(.06, pa)]
    total = sum(probs); probs = [x/total for x in probs]
    return {
        "home": probs[0]*100, "draw": probs[1]*100, "away": probs[2]*100,
        "home_form": hp.get("form", .5)*100,
        "away_form": ap.get("form", .5)*100,
        "h2h_games": ctx.get("h2h_games", 0),
        "expected_home_goals": lam_h, "expected_away_goals": lam_a,
        "home_elo": helo, "away_elo": aelo,
        "contextual": True,
    }


# ============================================================
# OPORTUNIDADES ESTATÍSTICAS
# ============================================================
def poisson_cdf(k, lam):
    """P(X <= k) para Poisson, sem scipy."""
    if lam is None or pd.isna(lam) or lam < 0:
        return None
    term = math.exp(-lam)
    total = term
    for i in range(1, k + 1):
        term *= lam / i
        total += term
    return min(max(total, 0.0), 1.0)


def prob_over_half_line(lam, line):
    # Para linha n+0,5, over significa X >= n+1.
    if lam is None or pd.isna(lam):
        return None
    k = int(math.floor(line))
    cdf = poisson_cdf(k, lam)
    return None if cdf is None else 1 - cdf


def prob_under_half_line(lam, line):
    # Para linha n+0,5, under significa X <= n.
    if lam is None or pd.isna(lam):
        return None
    k = int(math.floor(line))
    return poisson_cdf(k, lam)


def metric_value(row, name):
    if name not in row.index:
        return None
    value = row[name]
    if pd.isna(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def add_candidate(items, emoji, market, probability, basis, category, direction="over"):
    if probability is None:
        return
    pct = probability * 100

    # Priorizamos mercados com sustentação razoável. Para "menos de", exigimos
    # uma confiança bem maior, porque a tela deve privilegiar linhas de "mais de".
    min_pct = 58 if direction != "under" else 82
    if min_pct <= pct <= 95:
        if pct >= 80:
            level = "🟢 Forte"
        elif pct >= 70:
            level = "🟡 Boa"
        else:
            level = "🟠 Moderada"
        items.append({
            "Mercado": f"{emoji} {market}",
            "Chance": round(pct, 1),
            "Leitura": level,
            "Base": basis,
            "Categoria": category,
            "Direcao": direction,
        })



# Perfil de gols por competição. Estes valores funcionam como âncoras de estilo
# e são usados apenas para regressão à média; os números das equipes continuam
# sendo a base principal da projeção.
GOAL_ENVIRONMENT_PRIORS = {
    "Inglaterra - Premier League": 2.95,
    "Espanha - La Liga": 2.65,
    "Itália - Serie A": 2.60,
    "Alemanha - Bundesliga": 3.15,
    "França - Ligue 1": 2.70,
    "Portugal - Liga Portugal": 2.65,
    "Holanda - Eredivisie": 3.15,
    "Escócia - Premiership": 2.75,
    "Turquia - Süper Lig": 2.80,
    "Brasil - Série A": 2.35,
    "Brasil - Série B": 2.20,
    "Arábia Saudita - Saudi Pro League": 3.00,
    "Estados Unidos - MLS": 3.05,
    "Argentina - Liga Profesional": 2.25,
    "México - Liga MX": 2.65,
    "Colômbia - Primera A": 2.30,
    "UEFA Champions League": 2.80,
    "UEFA Europa League": 2.65,
    "UEFA Conference League": 2.75,
    "CONMEBOL Libertadores": 2.30,
    "CONMEBOL Sul-Americana": 2.25,
}



def _safe_float(value):
    try:
        v = float(value)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def _completed_match_stats(matches):
    """Resume apenas partidas com placar final válido, sem inventar dados."""
    rows = []
    for m in matches or []:
        hg, ag = _safe_float(m.get("hg")), _safe_float(m.get("ag"))
        if hg is None or ag is None:
            continue
        rows.append((hg, ag))
    if not rows:
        return {
            "games": 0, "goal_avg": None, "btts": None, "over25": None,
            "home_win_rate": None, "draw_rate": None, "away_win_rate": None,
        }
    totals = [hg + ag for hg, ag in rows]
    n = len(rows)
    return {
        "games": n,
        "goal_avg": sum(totals) / n,
        "btts": sum(1 for hg, ag in rows if hg > 0 and ag > 0) / n,
        "over25": sum(1 for t in totals if t >= 3) / n,
        "home_win_rate": sum(1 for hg, ag in rows if hg > ag) / n,
        "draw_rate": sum(1 for hg, ag in rows if hg == ag) / n,
        "away_win_rate": sum(1 for hg, ag in rows if ag > hg) / n,
    }


def competition_learning_profile(competition_name, competition_df=None):
    """Camada adaptativa de contexto do GM SCORE.

    Ela não substitui nem reescreve os dados das fontes. Apenas mede a amostra
    disponível e cria uma âncora dinâmica para a competição. Conforme novos
    resultados entram nas fontes públicas, o perfil é recalculado automaticamente.
    """
    fixed_prior = GOAL_ENVIRONMENT_PRIORS.get(competition_name)
    current_matches = []
    if isinstance(competition_df, pd.DataFrame):
        current_matches = list(competition_df.attrs.get("matches", []) or [])
    cur = _completed_match_stats(current_matches)

    hist_matches = []
    if competition_name in CONTEXTUAL_COMPETITIONS:
        try:
            hist = load_competition_history_context(competition_name, int(globals().get("used_year", datetime.now().year)), lookback=5)
            hist_matches = list((hist or {}).get("matches", []) or [])
        except Exception:
            hist_matches = []
    hist = _completed_match_stats(hist_matches)

    # Histórico real ajuda a estabilizar o início da temporada/fase. O prior fixo
    # continua como último fallback e perde peso rapidamente conforme a amostra cresce.
    background = fixed_prior
    if hist.get("goal_avg") is not None:
        background = hist["goal_avg"] if fixed_prior is None else (0.72 * hist["goal_avg"] + 0.28 * fixed_prior)
    if background is None and cur.get("goal_avg") is not None:
        background = cur["goal_avg"]

    n = int(cur.get("games") or 0)
    if cur.get("goal_avg") is not None and background is not None:
        # 18 jogos equivalem a uma amostra de estabilização. Não existe salto
        # brusco: a competição aprende gradualmente com a temporada vigente.
        current_weight = max(0.0, min(0.88, n / (n + 18.0)))
        learned_goal_avg = current_weight * cur["goal_avg"] + (1.0 - current_weight) * background
    else:
        current_weight = 0.0
        learned_goal_avg = background

    # Cobertura das estatísticas detalhadas realmente disponíveis na tabela.
    detailed = ["Escanteios", "Amarelos", "Faltas", "Finalizações", "Chutes no alvo"]
    available = 0
    possible = 0
    if isinstance(competition_df, pd.DataFrame) and not competition_df.empty:
        for metric in detailed:
            if metric not in competition_df.columns:
                continue
            possible += 1
            vals = pd.to_numeric(competition_df[metric], errors="coerce")
            if vals.notna().mean() >= 0.60:
                available += 1
    metric_coverage = available / possible if possible else 0.0

    # Confiança informa robustez da BASE, não promessa de acerto de aposta.
    sample_score = min(n / 40.0, 1.0)
    history_score = min((hist.get("games") or 0) / 80.0, 1.0)
    confidence_score = 0.60 * sample_score + 0.25 * metric_coverage + 0.15 * history_score
    if confidence_score >= 0.72:
        confidence = "Alta"
    elif confidence_score >= 0.42:
        confidence = "Média"
    else:
        confidence = "Cautelosa"

    if n <= 5:
        stage = "amostra inicial"
    elif n <= 20:
        stage = "amostra em formação"
    else:
        stage = "amostra consolidada"

    return {
        "current_games": n,
        "current_goal_avg": cur.get("goal_avg"),
        "historical_games": int(hist.get("games") or 0),
        "historical_goal_avg": hist.get("goal_avg"),
        "goal_prior": learned_goal_avg,
        "current_weight": current_weight,
        "metric_coverage": metric_coverage,
        "confidence": confidence,
        "confidence_score": confidence_score,
        "stage": stage,
        "btts": cur.get("btts"),
        "over25": cur.get("over25"),
        "home_win_rate": cur.get("home_win_rate"),
        "draw_rate": cur.get("draw_rate"),
        "away_win_rate": cur.get("away_win_rate"),
    }


def apply_competition_result_learning(probs, competition_name, competition_df=None):
    """Aprendizado conservador do ambiente 1X2 da competição.

    Só entra com pelo menos 30 resultados reais da temporada. O peso começa em
    2% e cresce lentamente até no máximo 8%, preservando força das equipes, H2H,
    qualidade estrutural e mercado. Não usa uma partida isolada nem reduz os
    limiares estatísticos do restante do motor.
    """
    if not probs:
        return probs
    learning = competition_learning_profile(competition_name, competition_df)
    n = int(learning.get("current_games") or 0)
    rates = [learning.get("home_win_rate"), learning.get("draw_rate"), learning.get("away_win_rate")]
    if n < 30 or any(v is None for v in rates):
        return probs
    try:
        empirical = [max(0.0, float(v) * 100.0) for v in rates]
        z = sum(empirical)
        if z <= 0:
            return probs
        empirical = [v / z * 100.0 for v in empirical]
        base = [float(probs[k]) for k in ("home", "draw", "away")]
    except Exception:
        return probs

    # N=30 -> 2%; N=60 -> 5%; N>=90 -> 8%.
    weight = min(0.08, 0.02 + max(0, n - 30) * 0.001)
    learned = [base[i] * (1.0 - weight) + empirical[i] * weight for i in range(3)]
    total = sum(learned) or 100.0
    out = dict(probs)
    out["home"], out["draw"], out["away"] = [v / total * 100.0 for v in learned]
    out["competition_learning_active"] = True
    out["competition_learning_games"] = n
    out["competition_learning_weight"] = weight
    return out


def render_data_intelligence_status(competition_name, competition_df):
    """Calcula o perfil da base e exibe o diagnóstico somente para administrador."""
    profile = competition_learning_profile(competition_name, competition_df)
    try:
        _profile = gm_auth_get_profile()
    except Exception:
        _profile = None
    if (_profile or {}).get("role") != "admin":
        return profile

    icon = {"Alta": "🟢", "Média": "🟡", "Cautelosa": "🟠"}.get(profile["confidence"], "⚪")
    with st.expander("🧠 Qualidade e evolução da base GM SCORE", expanded=False):
        c1, c2, c3 = st.columns(3)
        c1.metric("Confiança da base", f"{icon} {profile['confidence']}")
        c2.metric("Jogos aprendidos", str(profile["current_games"]))
        c3.metric("Cobertura detalhada", f"{profile['metric_coverage'] * 100:.0f}%")
        if profile.get("goal_prior") is not None:
            st.caption(
                f"Ambiente adaptativo de gols: {profile['goal_prior']:.2f} por jogo · "
                f"{profile['stage']}. O peso da temporada atual cresce automaticamente conforme novos jogos entram na base."
            )
        if profile["confidence"] == "Cautelosa":
            st.caption("⚠️ Amostra curta ou cobertura estatística limitada: o modelo aplica regressão maior ao contexto histórico da competição para evitar conclusões extremas.")
    return profile


def competition_adjusted_goal_total(raw_total, a, b, competition_name=None, competition_df=None):
    """Ajusta gols ao ambiente da competição e à maturidade da amostra.

    No começo de copas continentais, poucos jogos não devem gerar projeções
    extremas: há regressão mais forte à média histórica do torneio. Conforme
    as equipes acumulam partidas na competição, o peso dos dados próprios cresce.
    Competições de perfil mais aberto/fechado também recebem sua âncora adequada.
    """
    comp = competition_name or globals().get("league_name")
    learning = competition_learning_profile(comp, competition_df) if comp else {}
    prior = learning.get("goal_prior") if learning else GOAL_ENVIRONMENT_PRIORS.get(comp)
    if prior is None:
        return max(float(raw_total), 0.20), {"prior_weight": 0.0, "prior": None, "stage": "normal", "learning": learning}

    games = []
    for row in (a, b):
        try:
            g = float(row.get("Jogos", 0) if hasattr(row, "get") else row["Jogos"])
            if math.isfinite(g): games.append(max(g, 0.0))
        except Exception:
            pass
    avg_games = sum(games) / len(games) if games else 0.0

    continental = comp in CONTEXTUAL_COMPETITIONS
    if continental and avg_games <= 2:
        prior_weight, stage = 0.58, "início / amostra curta"
    elif continental and avg_games <= 5:
        prior_weight, stage = 0.44, "fase inicial / intermediária"
    elif continental:
        prior_weight, stage = 0.30, "amostra consolidada"
    else:
        # Ligas nacionais também respeitam seu ambiente de gols, mas os dados
        # das equipes têm predominância quando a amostra já é suficiente.
        prior_weight = 0.34 if avg_games < 5 else (0.24 if avg_games < 10 else 0.16)
        stage = "liga nacional"

    adjusted = float(raw_total) * (1.0 - prior_weight) + float(prior) * prior_weight
    # Copas sul-americanas têm historicamente contexto mais travado; a pequena
    # redução evita que médias domésticas abertas sejam transportadas integralmente.
    if comp in {"CONMEBOL Libertadores", "CONMEBOL Sul-Americana"}:
        adjusted *= 0.97
    return max(adjusted, 0.20), {"prior_weight": prior_weight, "prior": prior, "stage": stage, "learning": learning}

def build_opportunities(a, b, team_a, team_b, competition_df=None):
    """Seleciona destaques apenas quando a base já é estatisticamente utilizável.

    Oportunidade não é sinônimo de média alta. Enquanto a amostra da competição
    estiver em Cautela/Inconclusiva, o mercado continua visível no painel, mas não
    recebe selo de oportunidade. Finalizações e faltas permanecem fora dos destaques
    até que a calibração histórica específica desses mercados seja concluída.
    """
    candidates = []

    # v24: oportunidade é habilitada pela amostra DA PRÓPRIA MÉTRICA.
    # Um N alto em escanteios, por exemplo, nunca libera oportunidade de gols.
    goal_sample = _gm_pair_metric_sample(a, b, ["Gols pró", "Gols contra"], 0)
    corner_sample = _gm_pair_metric_sample(a, b, ["Escanteios"], 0)
    card_sample = _gm_pair_metric_sample(a, b, ["Amarelos"], 0)

    def add_market(group, emoji, label, lam, lines, basis, min_good=70, metric_sample=0):
        if int(metric_sample or 0) < 8:
            return
        opts = []
        for line in lines:
            pct = prob_over_half_line(max(float(lam), 0.05), line) * 100
            if min_good <= pct <= 92:
                opts.append((line, pct))
        if not opts:
            return
        strong = [(line, pct) for line, pct in opts if pct >= 80]
        # Maior linha ainda forte. Se não houver 80%+, escolhe a MAIOR linha que
        # mantém a confiança mínima — evita cair sempre no +0,5 só para mostrar
        # uma porcentagem muito alta e pouco informativa.
        if strong:
            line, pct = max(strong, key=lambda x: x[0])
        else:
            line, pct = max(opts, key=lambda x: x[0])
        level = "🟢 Forte" if pct >= 80 else ("🟡 Boa" if pct >= 70 else "🟠 Moderada")
        candidates.append({
            "Mercado": f"{emoji} Mais de {str(line).replace('.', ',')} {label}",
            "Chance": round(pct, 1),
            "Leitura": level,
            "Base": basis,
            "Categoria": group,
            "Direcao": "over",
            "Linha": line,
        })

    # Gols totais.
    a_gf = metric_value(a, "Gols pró"); a_ga = metric_value(a, "Gols contra")
    b_gf = metric_value(b, "Gols pró"); b_ga = metric_value(b, "Gols contra")
    if None not in (a_gf, a_ga, b_gf, b_ga):
        lam_a = max(((a_gf + b_ga) / 2) * 1.08, 0.05)
        lam_b = max(((b_gf + a_ga) / 2) / 1.08, 0.05)
        raw_goal_total = lam_a + lam_b
        lam_total, goal_ctx = competition_adjusted_goal_total(raw_goal_total, a, b, league_name, competition_df)
        add_market(
            "Gols", "⚽", "gols", lam_total, (0.5, 1.5, 2.5, 3.5, 4.5, 5.5),
            f"Projeção ajustada ao perfil da competição: {lam_total:.2f} gols", min_good=65, metric_sample=goal_sample
        )

    specs = [
        ("Escanteios", "⛳", "escanteios", (4.5, 5.5, 6.5, 7.5, 8.5, 9.5, 10.5, 11.5)),
        ("Cartões", "🟨", "cartões", (1.5, 2.5, 3.5, 4.5, 5.5, 6.5)),
        # Faltas, finalizações e chutes no alvo continuam visíveis como projeção,
        # mas ficam fora de Oportunidades até calibração histórica específica.
    ]
    for metric, emoji, label, lines in specs:
        # Cartões usa amarelos + vermelhos e substitui a antiga duplicidade Amarelos/Cartões.
        if metric == "Cartões":
            ay, by = metric_value(a, "Amarelos"), metric_value(b, "Amarelos")
            ar, br = metric_value(a, "Vermelhos"), metric_value(b, "Vermelhos")
            if ay is None or by is None:
                continue
            lam = ay + by + (ar or 0) + (br or 0)
        else:
            av, bv = metric_value(a, metric), metric_value(b, metric)
            if av is None or bv is None:
                continue
            lam = av + bv
        _metric_sample = card_sample if metric == "Cartões" else corner_sample
        add_market(metric, emoji, label, lam, lines, f"Média combinada: {lam:.2f}", metric_sample=_metric_sample)

    # Exibe primeiro as linhas fortes; uma única sugestão por categoria.
    candidates.sort(key=lambda x: (-x["Chance"], x["Categoria"]))
    return candidates[:6]


def _gm_attack_defense_count_projection(team_row, opponent_row, metric, against_metric, venue="neutral"):
    """Cruza produção própria com volume cedido pelo adversário.

    Só classifica a projeção individual como robusta quando os dois lados do
    cruzamento existem e têm cobertura mínima. A média ofensiva isolada continua
    utilizável como referência, mas não recebe confiança artificial.
    """
    own = metric_value(team_row, metric)
    opp_allowed = metric_value(opponent_row, against_metric)
    if own is None:
        return None

    try:
        own_n = int(float(team_row.get(f"_n_{metric}", 0) or 0))
    except Exception:
        own_n = 0
    try:
        allowed_n = int(float(opponent_row.get(f"_n_{against_metric}", 0) or 0))
    except Exception:
        allowed_n = 0

    if opp_allowed is not None and own_n >= 5 and allowed_n >= 5:
        # Produção própria pesa ligeiramente mais; concessão adversária impede
        # que a projeção seja uma simples repetição da média do time.
        value = 0.56 * float(own) + 0.44 * float(opp_allowed)
        reliable = True
        method = "ataque + concessão adversária"
        coverage = min(own_n, allowed_n)
    else:
        value = float(own)
        reliable = False
        method = "produção própria apenas"
        coverage = own_n

    # Ajuste de mando pequeno e simétrico. Não cria volume novo; apenas reflete
    # a tendência contextual quando já existe uma base quantitativa.
    if venue == "home":
        value *= 1.025
    elif venue == "away":
        value *= 0.975

    return {
        "value": max(value, 0.01),
        "reliable": reliable,
        "method": method,
        "coverage": coverage,
        "own": own,
        "opp_allowed": opp_allowed,
    }


def match_expectations(a, b, competition_df=None):
    """Projeções a partir das médias atuais; só retorna mercados realmente disponíveis."""
    out = {}
    a_gf, a_ga = metric_value(a, "Gols pró"), metric_value(a, "Gols contra")
    b_gf, b_ga = metric_value(b, "Gols pró"), metric_value(b, "Gols contra")
    if None not in (a_gf, a_ga, b_gf, b_ga):
        lam_a = max(((a_gf + b_ga) / 2) * 1.08, 0.05)
        lam_b = max(((b_gf + a_ga) / 2) / 1.08, 0.05)
        raw_total = lam_a + lam_b
        adjusted_total, goal_ctx = competition_adjusted_goal_total(raw_total, a, b, league_name, competition_df)
        scale = adjusted_total / raw_total if raw_total > 0 else 1.0
        lam_a, lam_b = lam_a * scale, lam_b * scale
        out["Gols"] = {"total": adjusted_total, "home": lam_a, "away": lam_b, "context": goal_ctx}
    ac, bc = metric_value(a, "Escanteios"), metric_value(b, "Escanteios")
    if ac is not None and bc is not None:
        out["Escanteios"] = {"total": max(ac + bc, 0.05), "home": max(ac, 0.01), "away": max(bc, 0.01)}
    ay, by = metric_value(a, "Amarelos"), metric_value(b, "Amarelos")
    ar, br = metric_value(a, "Vermelhos"), metric_value(b, "Vermelhos")
    if ay is not None and by is not None:
        home_cards, away_cards = ay + (ar or 0), by + (br or 0)
        out["Cartões"] = {"total": max(home_cards + away_cards, 0.05), "home": max(home_cards, 0.01), "away": max(away_cards, 0.01)}
    shot_h = _gm_attack_defense_count_projection(a, b, "Finalizações", "Finalizações contra", "home")
    shot_a = _gm_attack_defense_count_projection(b, a, "Finalizações", "Finalizações contra", "away")
    if shot_h is not None and shot_a is not None:
        reliable = bool(shot_h["reliable"] and shot_a["reliable"])
        out["Finalizações"] = {
            "total": max(shot_h["value"] + shot_a["value"], 0.05),
            "home": shot_h["value"], "away": shot_a["value"],
            "individual_reliable": reliable,
            "method": "ataque x defesa" if reliable else "referência ofensiva parcial",
            "home_detail": shot_h, "away_detail": shot_a,
            "probabilities_calibrated": False,
        }

    sot_h = _gm_attack_defense_count_projection(a, b, "Chutes no alvo", "Chutes no alvo contra", "home")
    sot_a = _gm_attack_defense_count_projection(b, a, "Chutes no alvo", "Chutes no alvo contra", "away")
    if sot_h is not None and sot_a is not None:
        reliable = bool(sot_h["reliable"] and sot_a["reliable"])
        out["Chutes no alvo"] = {
            "total": max(sot_h["value"] + sot_a["value"], 0.05),
            "home": sot_h["value"], "away": sot_a["value"],
            "individual_reliable": reliable,
            "method": "ataque x defesa" if reliable else "referência ofensiva parcial",
            "home_detail": sot_h, "away_detail": sot_a,
            "probabilities_calibrated": False,
        }
    return out


def expected_goals_by_half(team_a, team_b, matches, total_expected):
    """Usa apenas placares reais de intervalo; não divide a projeção ao meio artificialmente."""
    samples = []
    for m in reversed(matches or []):
        if m.get("home") not in (team_a, team_b) and m.get("away") not in (team_a, team_b):
            continue
        hthg, htag = m.get("ht_hg"), m.get("ht_ag")
        if hthg is None or htag is None:
            continue
        try:
            ht = float(hthg) + float(htag)
            ft = float(m.get("hg", 0)) + float(m.get("ag", 0))
        except (TypeError, ValueError):
            continue
        samples.append((ht, ft))
        if len(samples) >= 30:
            break
    # v31: 3–7 jogos já são uma base real válida em Cautela segundo a regra
    # oficial do GM SCORE; exigir 6 aqui criava Inconclusivo desnecessário.
    if len(samples) < 3:
        return None
    ft_total = sum(ft for _, ft in samples)
    if ft_total <= 0:
        return None
    share = max(0.30, min(0.58, sum(ht for ht, _ in samples) / ft_total))
    first = total_expected * share
    return {"first": first, "second": max(total_expected - first, 0), "games": len(samples)}


def _prob_color(pct):
    if pct >= 80:
        return "#22d36b", "rgba(34,211,107,.13)"
    if pct >= 65:
        return "#f59e0b", "rgba(245,158,11,.11)"
    return "#64748b", "rgba(100,116,139,.10)"


def _prob_circle(pct):
    border, bg = _prob_color(pct)
    return f'<div style="min-width:48px;height:36px;border-radius:10px;border:1px solid {border};background:{bg};display:flex;align-items:center;justify-content:center;font-weight:850;font-size:12px;color:#f8fafc;margin:auto">{pct:.0f}%</div>'


def render_probability_matrix(title, emoji, rows, lines):
    line_headers = "".join(
        f'<div style="text-align:center;font-size:10px;color:#94a3b8;font-weight:750">+{str(line).replace(".", ",")}</div>'
        for line in lines
    )
    html = (
        f'<div style="background:#0d141c;border:1px solid rgba(34,211,107,.28);border-radius:16px;padding:13px 12px 11px;margin:7px 0 14px;overflow-x:auto">'
        f'<div style="display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:12px">'
        f'<div style="font-weight:850;color:#f8fafc;font-size:14px">{emoji} {title}</div>'
        f'<div style="font-size:10px;color:#64748b;white-space:nowrap">probabilidade estimada</div></div>'
        f'<div style="min-width:{150 + 58*len(lines)}px;display:grid;grid-template-columns:135px repeat({len(lines)},52px);gap:6px;align-items:center"><div></div>{line_headers}'
    )
    for label, lam in rows:
        html += f'<div style="font-size:11px;color:#cbd5e1;font-weight:750;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="{label}">{label}</div>'
        for line in lines:
            pct = prob_over_half_line(max(float(lam), 0.01), line) * 100
            html += _prob_circle(pct)
    html += '</div><div style="margin-top:10px;font-size:10px;color:#64748b">🟢 80%+ &nbsp; • &nbsp; 🟠 65–79% &nbsp; • &nbsp; ⚪ abaixo de 65%</div></div>'
    st.markdown(html, unsafe_allow_html=True)


def _gm_expectation_card(cards):
    if not cards:
        return
    items = "".join(
        f'<div class="gm-ex-item"><b>{value:.1f}</b><span>{label}</span></div>' for label, value in cards
    )
    html = f"""
    <style>
    .gm-ex-wrap{{background:#0d141c;border:1px solid rgba(34,211,107,.28);border-radius:18px;padding:14px;margin:.45rem 0 1rem}}
    .gm-ex-title{{font-weight:900;color:#f8fafc;font-size:1.05rem;margin-bottom:11px}}
    .gm-ex-grid{{display:grid;grid-template-columns:repeat(5,1fr);gap:7px}}
    .gm-ex-item{{background:#111b27;border:1px solid rgba(148,163,184,.14);border-radius:12px;padding:10px 6px;text-align:center;min-width:0}}
    .gm-ex-item b{{display:block;color:#fff;font-size:1.15rem;line-height:1.1}}
    .gm-ex-item span{{display:block;color:#94a3b8;font-size:.64rem;line-height:1.15;margin-top:5px}}
    @media(max-width:520px){{.gm-ex-grid{{grid-template-columns:repeat(3,1fr)}}}}
    </style>
    <section class="gm-ex-wrap"><div class="gm-ex-title">📈 Expectativa da partida</div><div class="gm-ex-grid">{items}</div></section>
    """
    st.markdown(html, unsafe_allow_html=True)


def _gm_market_status(sample_games, available=True, specific_sample=None):
    """Classifica robustez sem transformar ausência de dados em número fictício."""
    if not available:
        return "inconclusivo", "⚪", "Inconclusivo"
    n = specific_sample if specific_sample is not None else sample_games
    try:
        n = int(n or 0)
    except Exception:
        n = 0
    if n < 3:
        return "inconclusivo", "⚪", "Inconclusivo"
    if n < 8:
        return "cautela", "🟠", "Cautela"
    return "conclusivo", "🟢", "Conclusivo"


def _gm_pct_lines(lam, lines):
    if lam is None:
        return []
    out = []
    for line in lines:
        p = prob_over_half_line(max(float(lam), 0.01), line)
        if p is not None:
            out.append((line, p * 100.0))
    return out


def _gm_market_card(title, status_tuple, projection=None, lines=None, note=None, compact_rows=None):
    state, icon, label = status_tuple
    status_color = {"conclusivo":"#22d36b", "cautela":"#f59e0b", "inconclusivo":"#94a3b8"}.get(state, "#94a3b8")
    body = ""
    if state == "inconclusivo":
        body = '<div class="gm-mkt-empty">Dados insuficientes para uma estimativa confiável neste mercado.</div>'
    else:
        if projection is not None:
            body += f'<div class="gm-mkt-proj"><span>Projeção GM</span><b>{projection}</b></div>'
        if compact_rows:
            body += '<div class="gm-mkt-rows">' + ''.join(
                f'<div><span>{html.escape(str(k))}</span><b>{html.escape(str(v))}</b></div>' for k,v in compact_rows
            ) + '</div>'
        if lines:
            body += '<div class="gm-mkt-lines">' + ''.join(
                f'<div><span>+{str(line).replace(".",",")}</span><b>{pct:.0f}%</b></div>' for line,pct in lines
            ) + '</div>'
    if note:
        body += f'<div class="gm-mkt-note">{html.escape(str(note))}</div>'
    st.markdown(
        f'<div class="gm-mkt-card"><div class="gm-mkt-head"><strong>{title}</strong>'
        f'<span style="color:{status_color}">{icon} {label}</span></div>{body}</div>',
        unsafe_allow_html=True,
    )


def _gm_team_market_card(title, status_tuple, team_a, team_b, home_projection=None, away_projection=None,
                         home_lines=None, away_lines=None, note=None):
    """Card de mercado por equipe, com projeção e linhas individuais sem fabricar dados."""
    state, icon, label = status_tuple
    status_color = {"conclusivo":"#22d36b", "cautela":"#f59e0b", "inconclusivo":"#94a3b8"}.get(state, "#94a3b8")

    def _team_block(team, projection, lines):
        if state == "inconclusivo" or projection is None:
            return (
                '<div class="gm-team-mkt">'
                f'<div class="gm-team-name">{html.escape(str(team))}</div>'
                '<div class="gm-team-empty">Dados insuficientes</div>'
                '</div>'
            )
        line_html = ''
        if lines:
            line_html = '<div class="gm-team-lines">' + ''.join(
                f'<div><span>+{str(line).replace(".",",")}</span><b>{pct:.0f}%</b></div>'
                for line, pct in lines
            ) + '</div>'
        proj_txt = f"{float(projection):.2f}".replace('.', ',')
        return (
            '<div class="gm-team-mkt">'
            f'<div class="gm-team-name">{html.escape(str(team))}</div>'
            f'<div class="gm-team-proj"><span>Projeção GM</span><b>{proj_txt}</b></div>'
            f'{line_html}'
            '</div>'
        )

    body = '<div class="gm-team-grid">' + _team_block(team_a, home_projection, home_lines) + _team_block(team_b, away_projection, away_lines) + '</div>'
    if note:
        body += f'<div class="gm-mkt-note">{html.escape(str(note))}</div>'
    st.markdown(
        f'<div class="gm-mkt-card"><div class="gm-mkt-head"><strong>{title}</strong>'
        f'<span style="color:{status_color}">{icon} {label}</span></div>{body}</div>',
        unsafe_allow_html=True,
    )


def render_core_markets_dashboard(a, b, team_a, team_b, df, probs=None, sample_games=0):
    """Painel oficial de mercados GM SCORE.

    Todos os mercados principais aparecem sempre. Quando a fonte não oferece base
    suficiente, a resposta é explicitamente inconclusiva em vez de estimada à força.
    """
    ex = match_expectations(a, b, df) or {}
    matches = list(df.attrs.get("matches", []) or []) if isinstance(df, pd.DataFrame) else []
    goal_split = None
    if "Gols" in ex:
        # v20: prioriza a divisão por tempo recuperada pela APIfootball. A
        # participação do 1º tempo é aplicada à projeção total do modelo, de
        # modo que o mercado por tempo continue coerente com o total esperado.
        g1a, g1b = metric_value(a, "Gols 1T"), metric_value(b, "Gols 1T")
        g2a, g2b = metric_value(a, "Gols 2T"), metric_value(b, "Gols 2T")
        half_api_n = _gm_pair_metric_sample(a, b, ["Gols 1T", "Gols 2T"], 0)
        if all(v is not None for v in (g1a, g1b, g2a, g2b)) and half_api_n >= 3:
            first_obs = max(float(g1a) + float(g1b), 0.0)
            second_obs = max(float(g2a) + float(g2b), 0.0)
            observed_total = first_obs + second_obs
            if observed_total > 0:
                first_share = max(0.20, min(0.65, first_obs / observed_total))
                total_proj = float(ex["Gols"]["total"])
                goal_split = {
                    "first": total_proj * first_share,
                    "second": total_proj * (1.0 - first_share),
                    "games": int(half_api_n),
                    "source": "APIfootball · divisão real por tempo",
                }
        if goal_split is None:
            goal_split = expected_goals_by_half(team_a, team_b, matches, ex["Gols"]["total"])

    st.markdown('''
    <style>
    .gm-mkt-card{background:#0d141c;border:1px solid rgba(34,211,107,.22);border-radius:15px;padding:12px 13px;margin:8px 0}
    .gm-mkt-head{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:10px;color:#f8fafc}
    .gm-mkt-head strong{font-size:.94rem}.gm-mkt-head span{font-size:.72rem;font-weight:800;white-space:nowrap}
    .gm-mkt-proj{display:flex;justify-content:space-between;align-items:end;background:#111b27;border-radius:11px;padding:9px 10px;margin-bottom:8px}
    .gm-mkt-proj span{color:#94a3b8;font-size:.72rem}.gm-mkt-proj b{font-size:1.15rem;color:#fff}
    .gm-mkt-lines{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}.gm-mkt-lines div,.gm-mkt-rows div{background:#111b27;border:1px solid rgba(148,163,184,.12);border-radius:10px;padding:8px;text-align:center}
    .gm-mkt-lines span,.gm-mkt-rows span{display:block;color:#94a3b8;font-size:.68rem}.gm-mkt-lines b,.gm-mkt-rows b{display:block;color:#f8fafc;font-size:.93rem;margin-top:2px}
    .gm-mkt-rows{display:grid;grid-template-columns:repeat(2,1fr);gap:6px}
    .gm-mkt-note,.gm-mkt-empty{color:#94a3b8;font-size:.72rem;line-height:1.4;margin-top:8px}.gm-mkt-empty{padding:7px 1px}
    .gm-team-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.gm-team-mkt{background:#101923;border:1px solid rgba(148,163,184,.12);border-radius:12px;padding:10px;min-width:0}
    .gm-team-name{font-weight:800;color:#f8fafc;font-size:.84rem;margin-bottom:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.gm-team-proj{display:flex;justify-content:space-between;align-items:end;gap:6px;margin-bottom:8px}.gm-team-proj span{color:#94a3b8;font-size:.64rem}.gm-team-proj b{color:#fff;font-size:1.05rem}
    .gm-team-lines{display:grid;grid-template-columns:repeat(2,1fr);gap:5px}.gm-team-lines div{background:#0d141c;border:1px solid rgba(148,163,184,.12);border-radius:8px;padding:6px;text-align:center}.gm-team-lines span{display:block;color:#94a3b8;font-size:.62rem}.gm-team-lines b{display:block;color:#f8fafc;font-size:.84rem;margin-top:1px}.gm-team-empty{color:#94a3b8;font-size:.7rem}
    @media(max-width:520px){.gm-mkt-lines{grid-template-columns:repeat(3,1fr)}.gm-team-grid{grid-template-columns:1fr 1fr}}
    </style>
    ''', unsafe_allow_html=True)

    st.markdown("#### 🧭 Mercados essenciais GM SCORE")
    st.caption("Os campos principais aparecem sempre. Quando a base não sustenta um cálculo, o mercado é marcado como inconclusivo.")
    recovery_active = bool(a.get("_gm_recovery_used", False) or b.get("_gm_recovery_used", False))
    try:
        _market_profile = gm_auth_get_profile()
    except Exception:
        _market_profile = None
    _market_is_admin = (_market_profile or {}).get("role") == "admin"
    if recovery_active and _market_is_admin:
        st.caption("🔎 Recuperação de dados ativa: o GM SCORE cruzou histórico recente e cobertura específica por mercado. A confiança é calculada separadamente para gols, escanteios, cartões e finalizações; nenhum número é criado para preencher lacunas.")
    competition_fallback_active = bool(a.get("_gm_competition_fallback_used", False) or b.get("_gm_competition_fallback_used", False))
    if competition_fallback_active:
        st.caption("🟠 Cobertura complementar da competição: uma ou mais métricas sem histórico individual suficiente foram sustentadas pela distribuição real da própria competição. Esses mercados ficam limitados a Cautela e não são promovidos artificialmente a Conclusivo.")

    goal_sample = _gm_pair_metric_sample(a, b, ["Gols pró", "Gols contra"], sample_games)
    corner_sample = _gm_pair_metric_sample(a, b, ["Escanteios"], sample_games)
    card_sample = _gm_pair_metric_sample(a, b, ["Amarelos"], sample_games)
    shot_sample = _gm_pair_metric_sample(a, b, ["Finalizações"], sample_games)
    sot_sample = _gm_pair_metric_sample(a, b, ["Chutes no alvo"], sample_games)

    tabs = st.tabs(["🏆 Resultado", "⚽ Gols", "⛳ Escanteios", "🟨 Cartões", "🎯 Finalizações", "📊 Outros dados"])

    with tabs[0]:
        status_result = _gm_market_status(sample_games, available=bool(probs), specific_sample=goal_sample)
        rows = None
        if probs:
            _display_1x2 = _gm_display_1x2_percentages(probs)
            rows = [(f"🏠 {team_a}", f"{_display_1x2['home']}%"), ("🤝 Empate", f"{_display_1x2['draw']}%"), (f"✈️ {team_b}", f"{_display_1x2['away']}%")]
        _gm_market_card("🏆 Resultado final", status_result, compact_rows=rows,
                        note="Distribuição 1X2 final do modelo; casa + empate + fora = 100%." if probs else None)

        if probs:
            p1x=float(probs['home'])+float(probs['draw']); px2=float(probs['draw'])+float(probs['away']); p12=float(probs['home'])+float(probs['away'])
            dc_rows=[("1X",f"{p1x:.0f}%"),("X2",f"{px2:.0f}%"),("12",f"{p12:.0f}%")]
        else:
            dc_rows=None
        _gm_market_card("🛡️ Dupla chance", status_result, compact_rows=dc_rows,
                        note="Cenários sobrepostos; não devem ser somados entre si." if probs else None)

    with tabs[1]:
        g = ex.get("Gols")
        g_status = _gm_market_status(sample_games, available=bool(g), specific_sample=goal_sample)
        _gm_market_card("⚽ Gols na partida", g_status,
                        projection=f"{g['total']:.2f}".replace('.', ',') if g else None,
                        lines=_gm_pct_lines(g['total'], [0.5,1.5,2.5,3.5,4.5]) if g else None)

        split_available = bool(goal_split)
        split_n = goal_split.get("games", 0) if goal_split else 0
        first_status = _gm_market_status(sample_games, available=split_available, specific_sample=split_n)
        second_status = _gm_market_status(sample_games, available=split_available, specific_sample=split_n)
        _gm_market_card("⏱️ Gols — 1º tempo", first_status,
                        projection=f"{goal_split['first']:.2f}".replace('.', ',') if goal_split else None,
                        lines=_gm_pct_lines(goal_split['first'], [0.5,1.5,2.5]) if goal_split else None,
                        note=(f"Base específica: {split_n} partidas com intervalo disponível · {goal_split.get('source', 'histórico da competição')}." if goal_split else None))
        _gm_market_card("⏱️ Gols — 2º tempo", second_status,
                        projection=f"{goal_split['second']:.2f}".replace('.', ',') if goal_split else None,
                        lines=_gm_pct_lines(goal_split['second'], [0.5,1.5,2.5]) if goal_split else None,
                        note=(f"Base específica: {split_n} partidas com intervalo disponível · {goal_split.get('source', 'histórico da competição')}." if goal_split else None))

        if g:
            rows = [(team_a, f"{g['home']:.2f}".replace('.', ',')), (team_b, f"{g['away']:.2f}".replace('.', ','))]
        else:
            rows = None
        _gm_market_card("👥 Gols por equipe", g_status, compact_rows=rows,
                        note="Projeção ofensiva de cada equipe ajustada ao contexto da competição." if g else None)

        if g:
            btts_yes=(1-math.exp(-max(g['home'],0.01)))*(1-math.exp(-max(g['away'],0.01)))*100
            btts_rows=[("Sim",f"{btts_yes:.0f}%"),("Não",f"{100-btts_yes:.0f}%")]
        else:
            btts_rows=None
        _gm_market_card("🤝 Ambas marcam", g_status, compact_rows=btts_rows,
                        note="Estimativa derivada das projeções individuais de gols." if g else None)

    with tabs[2]:
        c = ex.get("Escanteios")
        c_status = _gm_market_status(sample_games, available=bool(c), specific_sample=corner_sample)
        _gm_market_card("⛳ Escanteios na partida", c_status,
                        projection=f"{c['total']:.2f}".replace('.', ',') if c else None,
                        lines=_gm_pct_lines(c['total'], [6.5,7.5,8.5,9.5,10.5]) if c else None)
        c1a, c1b = metric_value(a, "Escanteios 1T"), metric_value(b, "Escanteios 1T")
        c2a, c2b = metric_value(a, "Escanteios 2T"), metric_value(b, "Escanteios 2T")
        c1_sample = _gm_pair_metric_sample(a, b, ["Escanteios 1T"], 0)
        c2_sample = _gm_pair_metric_sample(a, b, ["Escanteios 2T"], 0)
        c1_total = (c1a + c1b) if c1a is not None and c1b is not None else None
        c2_total = (c2a + c2b) if c2a is not None and c2b is not None else None
        c1_status = _gm_market_status(sample_games, available=c1_total is not None, specific_sample=c1_sample)
        c2_status = _gm_market_status(sample_games, available=c2_total is not None, specific_sample=c2_sample)
        _gm_market_card(
            "⏱️ Escanteios — 1º tempo", c1_status,
            projection=f"{c1_total:.2f}".replace('.', ',') if c1_total is not None else None,
            lines=_gm_pct_lines(c1_total, [2.5,3.5,4.5,5.5,6.5]) if c1_total is not None else None,
            note=(f"Base específica recuperada: {c1_sample} partidas com divisão por tempo." if c1_total is not None
                  else "Separação por tempo não encontrada com cobertura suficiente após a busca complementar."),
        )
        _gm_market_card(
            "⏱️ Escanteios — 2º tempo", c2_status,
            projection=f"{c2_total:.2f}".replace('.', ',') if c2_total is not None else None,
            lines=_gm_pct_lines(c2_total, [2.5,3.5,4.5,5.5,6.5]) if c2_total is not None else None,
            note=(f"Base específica recuperada: {c2_sample} partidas com divisão por tempo." if c2_total is not None
                  else "Separação por tempo não encontrada com cobertura suficiente após a busca complementar."),
        )
        _gm_team_market_card(
            "👥 Escanteios por equipe", c_status, team_a, team_b,
            home_projection=c['home'] if c else None,
            away_projection=c['away'] if c else None,
            home_lines=_gm_pct_lines(c['home'], [2.5,3.5,4.5,5.5]) if c else None,
            away_lines=_gm_pct_lines(c['away'], [2.5,3.5,4.5,5.5]) if c else None,
            note="Linhas individuais calculadas somente a partir da projeção disponível para cada equipe; a confiança segue a qualidade da base." if c else None,
        )

    with tabs[3]:
        c = ex.get("Cartões")
        c_status = _gm_market_status(sample_games, available=bool(c), specific_sample=card_sample)
        _gm_market_card("🟨 Cartões totais", c_status,
                        projection=f"{c['total']:.2f}".replace('.', ',') if c else None,
                        lines=_gm_pct_lines(c['total'], [1.5,2.5,3.5,4.5,5.5]) if c else None,
                        note="Cartões são tratados como estimativa estatística; média alta não gera recomendação automaticamente." if c else None)
        _gm_team_market_card(
            "👥 Cartões por equipe", c_status, team_a, team_b,
            home_projection=c['home'] if c else None,
            away_projection=c['away'] if c else None,
            home_lines=_gm_pct_lines(c['home'], [0.5,1.5,2.5,3.5]) if c else None,
            away_lines=_gm_pct_lines(c['away'], [0.5,1.5,2.5,3.5]) if c else None,
            note="Probabilidades individuais são estimativas do modelo; média elevada não implica recomendação automática." if c else None,
        )
        if c:
            both1=(prob_over_half_line(c['home'],0.5) or 0)*(prob_over_half_line(c['away'],0.5) or 0)*100
            both2=(prob_over_half_line(c['home'],1.5) or 0)*(prob_over_half_line(c['away'],1.5) or 0)*100
            r1=[("Ambas 1+",f"{both1:.0f}%")]
            r2=[("Ambas 2+",f"{both2:.0f}%")]
        else:
            r1=r2=None
        _gm_market_card("🟨 Ambas as equipes recebem 1+ cartão", c_status, compact_rows=r1,
                        note="Probabilidade conjunta derivada das projeções de cartões de cada equipe." if c else None)
        _gm_market_card("🟨 Ambas as equipes recebem 2+ cartões", c_status, compact_rows=r2,
                        note="Mercado mais exigente; não vira oportunidade apenas por ter média elevada." if c else None)

    with tabs[4]:
        s = ex.get("Finalizações")
        s_status = _gm_market_status(sample_games, available=bool(s), specific_sample=shot_sample)
        _gm_market_card(
            "🎯 Total de finalizações na partida", s_status,
            projection=f"{s['total']:.2f}".replace('.', ',') if s else None,
            lines=None,
            note=("Projeção de volume pelo cruzamento ataque × concessão adversária quando a fonte possui os dois lados. "
                  "Percentuais por linha ficam suspensos até a calibração histórica específica de finalizações.") if s else None,
        )
        t = ex.get("Chutes no alvo")
        t_status = _gm_market_status(sample_games, available=bool(t), specific_sample=sot_sample)
        _gm_market_card(
            "🥅 Finalizações no alvo na partida", t_status,
            projection=f"{t['total']:.2f}".replace('.', ',') if t else None,
            lines=None,
            note=("Projeção de volume pelo cruzamento ataque × concessão adversária quando disponível. "
                  "Probabilidades de over/under não são exibidas antes da calibração histórica desse mercado.") if t else None,
        )
        individual_shots_ok = bool(s and s.get("individual_reliable", False))
        s_team_status = _gm_market_status(sample_games, available=individual_shots_ok, specific_sample=shot_sample)
        srows=[(team_a,f"{s['home']:.2f}".replace('.',',')),(team_b,f"{s['away']:.2f}".replace('.',','))] if individual_shots_ok else None
        _gm_market_card(
            "👥 Finalizações por equipe", s_team_status, compact_rows=srows,
            note=(("Projeção individual cruza produção ofensiva e finalizações cedidas pelo adversário.")
                  if individual_shots_ok else
                  ("A fonte atual não oferece cobertura suficiente de produção + concessão para as duas equipes; "
                   "a divisão individual fica Inconclusiva em vez de assumir 50/50.")) if s else None,
        )
        individual_sot_ok = bool(t and t.get("individual_reliable", False))
        t_team_status = _gm_market_status(sample_games, available=individual_sot_ok, specific_sample=sot_sample)
        trows=[(team_a,f"{t['home']:.2f}".replace('.',',')),(team_b,f"{t['away']:.2f}".replace('.',','))] if individual_sot_ok else None
        _gm_market_card(
            "👥 Finalizações no alvo por equipe", t_team_status, compact_rows=trows,
            note=(("Projeção individual cruza chutes no alvo produzidos e cedidos pelo adversário.")
                  if individual_sot_ok else
                  ("Cobertura defensiva insuficiente para separar as equipes com segurança; mercado individual mantido Inconclusivo.")) if t else None,
        )


    with tabs[5]:
        posse_a, posse_b = metric_value(a, "Posse (%)"), metric_value(b, "Posse (%)")
        posse_n = _gm_pair_metric_sample(a, b, ["Posse (%)"], 0)
        posse_rows = [(team_a, f"{posse_a:.1f}%"), (team_b, f"{posse_b:.1f}%")] if posse_a is not None and posse_b is not None else None
        _gm_market_card("⚪ Posse de bola", _gm_market_status(sample_games, available=bool(posse_rows), specific_sample=posse_n), compact_rows=posse_rows)

        falta_a, falta_b = metric_value(a, "Faltas"), metric_value(b, "Faltas")
        falta_n = _gm_pair_metric_sample(a, b, ["Faltas"], 0)
        falta_rows = [(team_a, f"{falta_a:.2f}".replace('.', ',')), (team_b, f"{falta_b:.2f}".replace('.', ','))] if falta_a is not None and falta_b is not None else None
        _gm_market_card("🚫 Faltas por equipe", _gm_market_status(sample_games, available=bool(falta_rows), specific_sample=falta_n), compact_rows=falta_rows)

        off_a, off_b = metric_value(a, "Impedimentos"), metric_value(b, "Impedimentos")
        off_n = _gm_pair_metric_sample(a, b, ["Impedimentos"], 0)
        off_rows = [(team_a, f"{off_a:.2f}".replace('.', ',')), (team_b, f"{off_b:.2f}".replace('.', ','))] if off_a is not None and off_b is not None else None
        _gm_market_card("🚩 Impedimentos por equipe", _gm_market_status(sample_games, available=bool(off_rows), specific_sample=off_n), compact_rows=off_rows)

    return ex


def render_match_probability_dashboard(a, b, team_a, team_b, df, probs=None, sample_games=0):
    ex = match_expectations(a, b, df)
    if not ex:
        return ex
    cards = []
    if "Gols" in ex: cards.append(("Gols esperados", ex["Gols"]["total"]))
    if "Escanteios" in ex: cards.append(("Escanteios", ex["Escanteios"]["total"]))
    if "Cartões" in ex: cards.append(("Cartões", ex["Cartões"]["total"]))
    if "Finalizações" in ex: cards.append(("Finalizações", ex["Finalizações"]["total"]))
    if "Chutes no alvo" in ex: cards.append(("No alvo", ex["Chutes no alvo"]["total"]))
    _gm_expectation_card(cards)
    render_core_markets_dashboard(a, b, team_a, team_b, df, probs=probs, sample_games=sample_games)
    return ex

def _team_form(team, matches, n=5):
    pts = 0.0
    gd = 0.0
    used = 0
    for m in reversed(matches):
        if m["home"] != team and m["away"] != team:
            continue
        gf = m["hg"] if m["home"] == team else m["ag"]
        ga = m["ag"] if m["home"] == team else m["hg"]
        pts += 3 if gf > ga else 1 if gf == ga else 0
        gd += gf - ga
        used += 1
        if used >= n:
            break
    if not used:
        return 0.5, 0.0
    return pts / (3 * used), gd / used


def _season_strength(team, matches):
    pts = gf = ga = games = 0.0
    for m in matches:
        if m["home"] != team and m["away"] != team:
            continue
        tg = m["hg"] if m["home"] == team else m["ag"]
        ta = m["ag"] if m["home"] == team else m["hg"]
        gf += tg; ga += ta; games += 1
        pts += 3 if tg > ta else 1 if tg == ta else 0
    if not games:
        return 0.5, 0.0
    return pts / (3 * games), (gf - ga) / games


def _h2h_team_key(name):
    key = fixture_team_key(name) if "fixture_team_key" in globals() else clean_col(str(name or ""))
    aliases = {
        "fc_porto": "porto", "porto_fc": "porto",
        "manchester_city_fc": "manchester_city", "man_city": "manchester_city",
        "internazionale": "inter", "inter_milan": "inter", "fc_internazionale_milano": "inter",
        "real_madrid_cf": "real_madrid",
    }
    return aliases.get(key, key)


def _verified_h2h_matches(home, away):
    # Procura por nomes canônicos; o dicionário contém somente resultados
    # verificados para cobrir lacunas históricas das fontes abertas.
    hk, ak = _h2h_team_key(home), _h2h_team_key(away)
    for pair, rows in VERIFIED_H2H_MATCHES.items():
        keys = {_h2h_team_key(x) for x in pair}
        if keys == {hk, ak}:
            return list(rows)
    return []


def _h2h_details(home, away, matches, n=8):
    hk, ak = _h2h_team_key(home), _h2h_team_key(away)
    pool = []
    seen = set()
    for m in list(matches or []) + _verified_h2h_matches(home, away):
        mh, ma = _h2h_team_key(m.get("home")), _h2h_team_key(m.get("away"))
        if {mh, ma} != {hk, ak}:
            continue
        try:
            hg, ag = float(m.get("hg")), float(m.get("ag"))
        except Exception:
            continue
        sig = (mh, ma, hg, ag, str(m.get("date") or ""))
        if sig in seen:
            continue
        seen.add(sig)
        pool.append((m, mh, ma, hg, ag))
    pool = pool[-n:]
    home_wins = away_wins = draws = 0
    pts_h = pts_a = 0.0
    for m, mh, ma, hg, ag in pool:
        if hg == ag:
            draws += 1; pts_h += 1; pts_a += 1
        else:
            winner_key = mh if hg > ag else ma
            if winner_key == hk:
                home_wins += 1; pts_h += 3
            elif winner_key == ak:
                away_wins += 1; pts_a += 3
    games = len(pool)
    if not games:
        return {"home_share": .5, "away_share": .5, "games": 0, "home_wins": 0, "away_wins": 0, "draws": 0}
    total = max(pts_h + pts_a, 1.0)
    return {
        "home_share": pts_h / total, "away_share": pts_a / total, "games": games,
        "home_wins": home_wins, "away_wins": away_wins, "draws": draws,
    }


def _h2h(home, away, matches, n=6):
    d = _h2h_details(home, away, matches, n)
    return d["home_share"], d["away_share"], d["games"]


def _home_away_rates(team, matches, as_home=True):
    gf = ga = games = 0.0
    for m in matches:
        if as_home and m.get("home") == team:
            gf += float(m.get("hg", 0)); ga += float(m.get("ag", 0)); games += 1
        elif not as_home and m.get("away") == team:
            gf += float(m.get("ag", 0)); ga += float(m.get("hg", 0)); games += 1
    if not games:
        return None
    return gf / games, ga / games, int(games)


def _poisson_result_probs(lambda_home, lambda_away, max_goals=8):
    ph = pd = pa = 0.0
    for hg in range(max_goals + 1):
        p_hg = math.exp(-lambda_home) * (lambda_home ** hg) / math.factorial(hg)
        for ag in range(max_goals + 1):
            p_ag = math.exp(-lambda_away) * (lambda_away ** ag) / math.factorial(ag)
            p = p_hg * p_ag
            if hg > ag: ph += p
            elif hg == ag: pd += p
            else: pa += p
    total = ph + pd + pa
    if total <= 0:
        return .40, .29, .31
    return ph / total, pd / total, pa / total


def victory_probabilities(home, away, df):
    """Probabilidade 1X2 calibrada para evitar favoritismos exagerados.

    Combina força ofensiva/defensiva da temporada, desempenho casa/fora,
    fase recente e H2H com peso pequeno. O resultado final sofre regressão
    à média e limites conservadores, especialmente quando a amostra é curta.
    """
    matches = df.attrs.get("matches", [])
    # Algumas ligas atuais oferecem classificação completa, mas não o histórico
    # partida a partida na fonte sem chave. Ainda assim podemos estimar 1X2 de
    # forma conservadora usando gols pró/contra da temporada.
    if not matches:
        try:
            rh = df[df["Time"] == home].iloc[0]
            ra = df[df["Time"] == away].iloc[0]
            hgf, hga = metric_value(rh, "Gols pró"), metric_value(rh, "Gols contra")
            agf, aga = metric_value(ra, "Gols pró"), metric_value(ra, "Gols contra")
            hgms = float(rh.get("Jogos", 0) or 0); agms = float(ra.get("Jogos", 0) or 0)
            if None in (hgf, hga, agf, aga) or min(hgms, agms) < 1:
                return None
            lam_h = max(0.15, ((hgf + aga) / 2) * 1.08)
            lam_a = max(0.12, ((agf + hga) / 2) / 1.08)
            ph, pd_, pa = _poisson_result_probs(lam_h, lam_a)
            # Regressão adicional à média porque não há forma/H2H jogo a jogo.
            prior = (0.42, 0.29, 0.29)
            sample = min(hgms, agms)
            w = min(0.72, 0.38 + sample / 45.0)
            ph = ph * w + prior[0] * (1-w)
            pd_ = pd_ * w + prior[1] * (1-w)
            pa = pa * w + prior[2] * (1-w)
            total = ph + pd_ + pa
            return {
                "home": ph/total*100, "draw": pd_/total*100, "away": pa/total*100,
                "home_form": 50.0, "away_form": 50.0, "h2h_games": 0,
                "expected_home_goals": lam_h, "expected_away_goals": lam_a,
            }
        except Exception:
            return None

    # Médias da liga por equipe/jogo.
    valid = [m for m in matches if m.get("hg") is not None and m.get("ag") is not None]
    if not valid:
        return None
    league_home = sum(float(m["hg"]) for m in valid) / len(valid)
    league_away = sum(float(m["ag"]) for m in valid) / len(valid)
    league_team = max((league_home + league_away) / 2, 0.65)

    # Força geral da temporada.
    home_row = df[df["Time"] == home]
    away_row = df[df["Time"] == away]
    if home_row.empty or away_row.empty:
        return None
    hr, ar = home_row.iloc[0], away_row.iloc[0]
    hgf = metric_value(hr, "Gols pró") or league_team
    hga = metric_value(hr, "Gols contra") or league_team
    agf = metric_value(ar, "Gols pró") or league_team
    aga = metric_value(ar, "Gols contra") or league_team

    # Casa/fora específico quando há amostra; caso contrário recua à média geral.
    hsplit = _home_away_rates(home, matches, True)
    asplit = _home_away_rates(away, matches, False)
    h_games = hsplit[2] if hsplit else 0
    a_games = asplit[2] if asplit else 0
    h_home_gf, h_home_ga = (hsplit[0], hsplit[1]) if hsplit else (hgf, hga)
    a_away_gf, a_away_ga = (asplit[0], asplit[1]) if asplit else (agf, aga)

    # Shrink de amostra: em poucos jogos, aproxima os números da média da liga.
    shrink_h = min(h_games / 8.0, 1.0)
    shrink_a = min(a_games / 8.0, 1.0)
    h_home_gf = shrink_h * h_home_gf + (1 - shrink_h) * hgf
    h_home_ga = shrink_h * h_home_ga + (1 - shrink_h) * hga
    a_away_gf = shrink_a * a_away_gf + (1 - shrink_a) * agf
    a_away_ga = shrink_a * a_away_ga + (1 - shrink_a) * aga

    # Expectativa de gols: mistura geral + casa/fora e limita extremos.
    lam_h = 0.55 * ((hgf + aga) / 2) + 0.45 * ((h_home_gf + a_away_ga) / 2)
    lam_a = 0.55 * ((agf + hga) / 2) + 0.45 * ((a_away_gf + h_home_ga) / 2)
    lam_h *= max(0.90, min(1.12, league_home / league_team))
    lam_a *= max(0.90, min(1.08, league_away / league_team))

    # Fase recente ajusta pouco (máx. ~8%), evitando supervalorizar 3-5 jogos.
    hf, _ = _team_form(home, matches, 6)
    af, _ = _team_form(away, matches, 6)
    form_delta = max(-0.08, min(0.08, (hf - af) * 0.12))
    lam_h *= 1 + form_delta
    lam_a *= 1 - form_delta

    # H2H tem influência mínima e apenas com amostra razoável.
    hh, ah, h2n = _h2h(home, away, matches, 6)
    if h2n >= 3:
        h2_delta = max(-0.025, min(0.025, (hh - ah) * 0.04))
        lam_h *= 1 + h2_delta
        lam_a *= 1 - h2_delta

    lam_h = max(0.35, min(lam_h, 2.75))
    lam_a = max(0.30, min(lam_a, 2.55))
    ph, pd_, pa = _poisson_result_probs(lam_h, lam_a)

    # Regressão à distribuição-base. Mais forte no início da temporada.
    sample = min(int(hr.get("Jogos", 0) or 0), int(ar.get("Jogos", 0) or 0))
    model_weight = min(0.84, 0.58 + sample * 0.018)
    baseline = (0.43, 0.28, 0.29)
    ph = model_weight * ph + (1 - model_weight) * baseline[0]
    pd_ = model_weight * pd_ + (1 - model_weight) * baseline[1]
    pa = model_weight * pa + (1 - model_weight) * baseline[2]

    # Evita 1X2 artificialmente extremos com bases gratuitas/parciais.
    floor = 0.07
    probs = [max(floor, ph), max(floor, pd_), max(floor, pa)]
    total = sum(probs)
    probs = [p / total for p in probs]
    max_idx = max(range(3), key=lambda i: probs[i])
    if probs[max_idx] > 0.72:
        excess = probs[max_idx] - 0.72
        probs[max_idx] = 0.72
        others = [i for i in range(3) if i != max_idx]
        denom = probs[others[0]] + probs[others[1]]
        if denom > 0:
            probs[others[0]] += excess * probs[others[0]] / denom
            probs[others[1]] += excess * probs[others[1]] / denom

    return {
        "home": probs[0] * 100,
        "draw": probs[1] * 100,
        "away": probs[2] * 100,
        "home_form": hf * 100,
        "away_form": af * 100,
        "h2h_games": h2n,
        "expected_home_goals": lam_h,
        "expected_away_goals": lam_a,
    }


def parse_today_from_openfootball_text(text, target_date, competition):
    month_map = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,"Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}
    current = None
    found = []
    date_re = re.compile(r"^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+([A-Z][a-z]{2})\s+(\d{1,2})(?:\s+(\d{4}))?$")
    match_re = re.compile(r"^(?:(\d{1,2}:\d{2})\s+)?(.+?)\s+v\s+(.+?)(?:\s+\d+\s*-\s*\d+.*)?$")
    for raw in text.splitlines():
        line = raw.strip()
        dm = date_re.match(line)
        if dm:
            mon = month_map.get(dm.group(1)); day = int(dm.group(2)); yr = int(dm.group(3) or target_date.year)
            # Temporadas europeias atravessam o ano: Sep-Dec usam ano inicial; Jan-Jun podem usar o seguinte.
            if not dm.group(3) and mon and target_date.month <= 6 and mon >= 7:
                yr -= 1
            try: current = date(yr, mon, day)
            except Exception: current = None
            continue
        if current is None or abs((current - target_date).days) > 1 or not line or line.startswith(("#","=","▪")):
            continue
        mm = match_re.match(line)
        if not mm: continue
        home = re.sub(r"\s+\([A-Z]{3}\)$", "", mm.group(2)).strip()
        away = re.sub(r"\s+\([A-Z]{3}\)$", "", mm.group(3)).strip()
        br_time, br_date = fixture_time_brasilia(mm.group(1) or "", competition, current)
        if br_date not in (None, target_date):
            continue
        if br_date is None:
            # Sem horário, o calendário ainda fornece a data explícita do bloco.
            br_date = current
        found.append({"competition":competition,"home":home,"away":away,"time":br_time,"br_date":br_date,"source":"OpenFootball"})
    return found



class _DailyFixtureLinkParser(HTMLParser):
    """Extrai links de partidas de uma página diária sem depender de BeautifulSoup."""
    def __init__(self):
        super().__init__()
        self.links = []
        self._href = None
        self._parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self._href = dict(attrs).get("href")
            self._parts = []

    def handle_data(self, data):
        if self._href is not None:
            txt = str(data).strip()
            if txt:
                self._parts.append(txt)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._parts).strip()))
            self._href = None
            self._parts = []


def _competition_from_livescore_path(path):
    p = path.lower().strip("/")
    checks = [
        (("england", "premier-league"), "Inglaterra - Premier League"),
        (("spain", "laliga"), "Espanha - La Liga"),
        (("italy", "serie-a"), "Itália - Serie A"),
        (("germany", "bundesliga"), "Alemanha - Bundesliga"),
        (("france", "ligue-1"), "França - Ligue 1"),
        (("portugal", "primeira-liga"), "Portugal - Liga Portugal"),
        (("netherlands", "eredivisie"), "Holanda - Eredivisie"),
        (("scotland", "premiership"), "Escócia - Premiership"),
        (("turkiye", "super-lig"), "Turquia - Süper Lig"),
        (("turkey", "super-lig"), "Turquia - Süper Lig"),
        (("saudi-arabia", "saudi-professional-league"), "Arábia Saudita - Saudi Pro League"),
        (("saudi-arabia", "pro-league"), "Arábia Saudita - Saudi Pro League"),
        (("brazil", "serie-a"), "Brasil - Série A"),
        (("brazil", "serie-b"), "Brasil - Série B"),
        (("usa", "major-league-soccer"), "Estados Unidos - MLS"),
        (("usa", "mls"), "Estados Unidos - MLS"),
        (("argentina", "liga-profesional"), "Argentina - Liga Profesional"),
        (("argentina", "primera-division"), "Argentina - Liga Profesional"),
        (("mexico", "liga-mx"), "México - Liga MX"),
        (("colombia", "primera-a"), "Colômbia - Primera A"),
    ]
    for parts, comp in checks:
        if all(part in p for part in parts):
            return comp
    if "copa-libertadores" in p:
        return "CONMEBOL Libertadores"
    if "copa-sudamericana" in p:
        return "CONMEBOL Sul-Americana"
    if "champions-league" in p and "women" not in p:
        return "UEFA Champions League"
    if "europa-league" in p:
        return "UEFA Europa League"
    if "conference-league" in p:
        return "UEFA Conference League"
    return None


def _pretty_slug_team(slug):
    special = {"fc":"FC", "cf":"CF", "ac":"AC", "sc":"SC", "afc":"AFC", "psg":"PSG", "rb":"RB", "neom":"NEOM"}
    words = []
    for w in slug.split("-"):
        words.append(special.get(w.lower(), w.capitalize()))
    return " ".join(words)


def _is_allowed_daily_fixture_path(path, competition):
    """Evita feminino e divisões secundárias acidentais no calendário diário.

    A Série B do Brasil é mantida porque é uma competição explicitamente
    disponível no app. As demais divisões inferiores não são aceitas.
    """
    p = str(path or "").lower()
    blocked_women = ("women", "womens", "feminino", "feminina", "femenino", "femenina", "frauen", "femminile")
    if any(token in p for token in blocked_women):
        return False

    # Divisões inferiores que podem aparecer em páginas agregadas.
    lower_tier_tokens = (
        "championship", "league-one", "league-two", "segunda-division",
        "segunda-liga", "serie-b", "2-bundesliga", "ligue-2",
        "eerste-divisie", "segunda-division-profesional", "primera-b",
        "liga-de-expansion", "ascenso", "segunda-division"
    )
    if competition == "Brasil - Série B":
        return True
    return not any(token in p for token in lower_tier_tokens)


@st.cache_data(ttl=900, show_spinner=False)
def load_livescore_today(source_date):
    """Fonte diária complementar. Horários são normalizados para Brasília (America/Sao_Paulo)."""
    url = f"https://www.livescore.mobi/football/{source_date:%Y-%m-%d}/?tz=0"
    r = request_first([url], timeout=20)
    parser = _DailyFixtureLinkParser()
    parser.feed(r.text)
    fixtures = []
    for href, label in parser.links:
        if not href or "-vs-" not in href:
            continue
        comp = _competition_from_livescore_path(href)
        if not comp or comp not in COMPETITIONS:
            continue
        if not _is_allowed_daily_fixture_path(href, comp):
            continue
        # O calendário do LiveScore muda ocasionalmente o formato do href.
        # Primeiro tentamos extrair os clubes da URL; se isso falhar, usamos o
        # próprio texto do link (ex.: "15:00AFC Bournemouth - Brentford").
        clean_href = str(href).split("?", 1)[0]
        m = re.search(r"/([^/]+-vs-[^/]+)/\d+/?$", clean_href)
        home = away = None
        if m:
            matchup = m.group(1)
            home_slug, away_slug = matchup.split("-vs-", 1)
            home, away = _pretty_slug_team(home_slug), _pretty_slug_team(away_slug)
        if not home or not away:
            label_match = re.search(
                r"(?:^|\s)(?:[0-2]?\d:[0-5]\d)?\s*([^|]+?)\s+-\s+([^|]+?)\s*$",
                str(label or "").strip(),
            )
            if label_match:
                home, away = label_match.group(1).strip(), label_match.group(2).strip()
        if not home or not away:
            continue
        tm = re.search(r"\b([0-2]?\d:[0-5]\d)\b", label or "")
        if tm:
            # O LiveScore.mobi entrega os horários da listagem diária em UTC.
            # Interpretar esse valor como horário local da competição deslocava
            # partidas europeias várias horas para trás (ex.: 19:30 UTC ->
            # 14:30 ao tratá-lo incorretamente como Europe/Madrid). Convertemos
            # sempre de UTC para America/Sao_Paulo (horário de Brasília).
            status, br_date = fixture_time_brasilia(
                tm.group(1), comp, source_date, source_tz="UTC"
            )
        elif re.search(r"\bFT\b", label or "", re.I):
            status, br_date = "FT", source_date
        elif re.search(r"\d+'", label or ""):
            status, br_date = "AO VIVO", source_date
        else:
            status, br_date = "", source_date
        fixtures.append({"competition": comp, "home": home, "away": away, "time": status, "br_date": br_date})
    return fixtures


def fixture_team_key(name):
    """Normaliza nomes equivalentes vindos de fontes diferentes para deduplicação."""
    raw = clean_col(str(name or ""))
    tokens = [t for t in raw.split("_") if t and t not in {
        "fc","cf","ec","ac","sc","afc","fbpa","club","clube","de","da","do","dos","das",
        "football","futebol","calcio","soccer","cd","ud","ad","se","aa"
    }]
    # Pequenos aliases frequentes entre calendários públicos.
    alias = {
        "vitoria_bahia": "vitoria", "esporte_vitoria": "vitoria",
        "gremio_porto_alegrense": "gremio", "gremio_rs": "gremio",
        "internacional_porto_alegre": "internacional",
        "atletico_mineiro": "atletico_mg", "athletico_paranaense": "athletico_pr",
    }
    key = "_".join(tokens)
    return alias.get(key, key)


def complete_current_roster(df, competition_name):
    """Completa o elenco oficial e, quando exigido, remove qualquer clube extra."""
    roster = CURRENT_TEAM_ROSTERS.get(competition_name)
    if not roster or df is None:
        return df
    attrs = dict(getattr(df, "attrs", {}))
    out = df.copy()
    if "Time" not in out.columns:
        return df

    if competition_name in STRICT_OFFICIAL_ROSTERS:
        # Canonicaliza variantes como "FC Porto" -> "Porto", mas descarta antes
        # qualquer indicação de U19/Sub-19/Youth/reservas/feminino.
        canonical = []
        for name in out["Time"].astype(str):
            if not is_main_senior_team_name(name):
                canonical.append(None)
                continue
            resolved = resolve_team_name(name, roster)
            canonical.append(resolved)
        out["Time"] = canonical
        out = out[out["Time"].notna()].copy()
        # Se duas fontes/variações virarem o mesmo clube, preserva a linha com
        # maior amostra de jogos (ou a primeira quando não houver amostra).
        if "Jogos" in out.columns:
            out["__games"] = pd.to_numeric(out["Jogos"], errors="coerce").fillna(-1)
            out = out.sort_values("__games", ascending=False).drop_duplicates("Time", keep="first").drop(columns="__games")
        else:
            out = out.drop_duplicates("Time", keep="first")

    existing = {fixture_team_key(x): x for x in out["Time"].dropna().astype(str)}
    missing = []
    for team in roster:
        key = fixture_team_key(team)
        # equivalência flexível para nomes abreviados/FC/Club etc.
        if key in existing:
            continue
        resolved = resolve_team_name(team, list(existing.values())) if existing else None
        if resolved and fixture_team_key(resolved) == key:
            continue
        row = {c: None for c in out.columns}
        row["Time"] = team
        if "Jogos" in row:
            row["Jogos"] = 0
        missing.append(row)
    if missing:
        out = pd.concat([out, pd.DataFrame(missing)], ignore_index=True)
    # Ordenação estável pelo elenco oficial; eventuais clubes extras ficam ao final.
    order = {fixture_team_key(t): i for i, t in enumerate(roster)}
    out["__ord"] = out["Time"].map(lambda x: order.get(fixture_team_key(x), 9999))
    out = out.sort_values(["__ord", "Time"]).drop(columns="__ord").reset_index(drop=True)
    out.attrs.update(attrs)
    out.attrs["official_roster"] = roster
    out.attrs["roster_completed"] = True
    return out


def _fixture_quality(f):
    home, away = str(f.get("home") or ""), str(f.get("away") or "")
    tm = str(f.get("time") or "")
    has_time = bool(re.fullmatch(r"\d{2}:\d{2}", tm))
    accents = sum(ord(ch) > 127 for ch in home + away)
    return (100 if has_time else 0) + accents * 3 + len(home) + len(away)


INVALID_FIXTURE_NAMES = {
    "n.n", "n.n.", "nn", "tbd", "tba", "unknown", "a definir", "to be decided",
    "winner", "loser", "vencedor", "perdedor", "bye", "-", "?",
}

def valid_fixture_team(name):
    text = str(name or "").strip()
    if not text:
        return False
    low = re.sub(r"\s+", " ", text.lower()).strip()
    if low in INVALID_FIXTURE_NAMES:
        return False
    if re.fullmatch(r"(?:n\.?\s*n\.?|tbd|tba|unknown)(?:\s*\d+)?", low, re.I):
        return False
    return is_main_senior_team_name(text)


def valid_daily_fixture(f):
    home, away = f.get("home"), f.get("away")
    if not valid_fixture_team(home) or not valid_fixture_team(away):
        return False
    if fixture_team_key(home) == fixture_team_key(away):
        return False
    comp = f.get("competition")
    if comp in STRICT_OFFICIAL_ROSTERS:
        roster = CURRENT_TEAM_ROSTERS.get(comp, [])
        return bool(resolve_team_name(home, roster) and resolve_team_name(away, roster))
    return True


def _fixture_is_finished(f):
    """Bloqueia partidas já encerradas na agenda de análise pré-jogo."""
    terminal_tokens = {
        "ft", "aet", "ap", "pen", "finished", "completed", "complete",
        "final", "ended", "after extra time", "after penalties", "encerrado",
        "encerrada", "finalizado", "finalizada",
    }
    values = [
        f.get("status"), f.get("state"), f.get("status_type"),
        f.get("time"), f.get("display_status"),
    ]
    for value in values:
        txt = str(value or "").strip().lower()
        if not txt:
            continue
        if txt in terminal_tokens:
            return True
        if re.search(r"\b(?:ft|finished|completed|final|ended)\b", txt, re.I):
            return True
    return False


def fixture_matches_selected_date(f, target_date):
    """Validação final da agenda usando a data local de Brasília.

    As fontes chamadas nesta etapa já são consultadas pela data selecionada.
    V118: a data local de Brasília passa a ser obrigatória na barreira final.
    A raiz dos jogos de ontem reaparecendo em Hoje era aceitar ``br_date`` ausente
    em fallbacks antigos. Cada coletor deve provar a data antes da renderização;
    registro sem data válida é rejeitado em vez de ser encaixado no dia solicitado.
    Partidas encerradas continuam bloqueadas da agenda pré-jogo.
    """
    if isinstance(target_date, pd.Timestamp):
        target_date = target_date.date()
    if isinstance(target_date, datetime):
        target_date = target_date.date()

    if _fixture_is_finished(f):
        return False

    br_date = f.get("br_date")
    if br_date in (None, ""):
        return False
    if isinstance(br_date, pd.Timestamp):
        br_date = br_date.date()
    if isinstance(br_date, datetime):
        br_date = br_date.astimezone(BRASILIA_TZ).date() if br_date.tzinfo else br_date.date()
    if isinstance(br_date, str):
        try:
            parsed = pd.to_datetime(br_date, errors="coerce")
            br_date = None if pd.isna(parsed) else parsed.date()
        except Exception:
            br_date = None

    # Data ausente/ilegível não é evidência suficiente para pertencer ao dia.
    return False if br_date is None else br_date == target_date


# Fonte principal complementar da agenda: calendário público do SofaScore.
# O GM SCORE já usa o SofaScore para H2H/odds, portanto esta rotina reaproveita
# a mesma fonte apenas para DESCOBRIR partidas por data. Ela não altera dados
# históricos, estatísticas, probabilidades ou qualquer cálculo existente.
SOFASCORE_TOURNAMENT_ALIASES = {
    "premier league": "Inglaterra - Premier League",
    "laliga": "Espanha - La Liga",
    "la liga": "Espanha - La Liga",
    "serie a": "Itália - Serie A",  # validado pelo país abaixo
    "bundesliga": "Alemanha - Bundesliga",
    "ligue 1": "França - Ligue 1",
    "liga portugal": "Portugal - Liga Portugal",
    "primeira liga": "Portugal - Liga Portugal",
    "eredivisie": "Holanda - Eredivisie",
    "premiership": "Escócia - Premiership",
    "super lig": "Turquia - Süper Lig",
    "süper lig": "Turquia - Süper Lig",
    "brasileirao serie a": "Brasil - Série A",
    "brasileirão série a": "Brasil - Série A",
    "brasileirao serie b": "Brasil - Série B",
    "brasileirão série b": "Brasil - Série B",
    "saudi pro league": "Arábia Saudita - Saudi Pro League",
    "mls": "Estados Unidos - MLS",
    "major league soccer": "Estados Unidos - MLS",
    "liga profesional": "Argentina - Liga Profesional",
    "liga profesional de futbol": "Argentina - Liga Profesional",
    "liga profesional de fútbol": "Argentina - Liga Profesional",
    "liga mx": "México - Liga MX",
    "primera a": "Colômbia - Primera A",
    "uefa champions league": "UEFA Champions League",
    "champions league": "UEFA Champions League",
    "uefa europa league": "UEFA Europa League",
    "europa league": "UEFA Europa League",
    "uefa conference league": "UEFA Conference League",
    "conference league": "UEFA Conference League",
    "conmebol libertadores": "CONMEBOL Libertadores",
    "copa libertadores": "CONMEBOL Libertadores",
    "libertadores": "CONMEBOL Libertadores",
    "conmebol sudamericana": "CONMEBOL Sul-Americana",
    "copa sudamericana": "CONMEBOL Sul-Americana",
    "sudamericana": "CONMEBOL Sul-Americana",
}


def _norm_fixture_label(value):
    value = clean_col(value or "").replace("_", "-")
    value = re.sub(r"[^a-z0-9à-ÿ -]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _competition_from_sofascore_event(event):
    """Mapeia somente as 21 competições cadastradas no GM SCORE.

    Usa torneio + país/categoria para impedir que ligas homônimas (por exemplo,
    Serie A de outro país) entrem na agenda.
    """
    tournament = event.get("tournament") or {}
    unique = tournament.get("uniqueTournament") or {}
    category = tournament.get("category") or unique.get("category") or {}

    names = [
        unique.get("name"), unique.get("slug"),
        tournament.get("name"), tournament.get("slug"),
    ]
    joined = " ".join(_norm_fixture_label(x) for x in names if x)
    country = _norm_fixture_label(category.get("name") or category.get("slug") or "")

    # A agenda do GM SCORE é exclusiva do futebol profissional masculino.
    # Bloqueia torneios de base, reservas e femininos antes de mapear nomes
    # parecidos com as competições principais (ex.: U21 Premier League).
    if SECONDARY_COMPETITION_RE.search(joined):
        return None

    # Competições continentais primeiro: independem do país da categoria.
    continental = (
        ("champions league", "UEFA Champions League"),
        ("europa league", "UEFA Europa League"),
        ("conference league", "UEFA Conference League"),
        ("libertadores", "CONMEBOL Libertadores"),
        ("sudamericana", "CONMEBOL Sul-Americana"),
    )
    for token, comp in continental:
        if token in joined:
            return comp

    # Ligas nacionais com desambiguação por país quando necessário.
    country_rules = [
        (("premier league",), ("england", "inglaterra"), "Inglaterra - Premier League"),
        (("laliga", "la liga"), ("spain", "espanha"), "Espanha - La Liga"),
        (("serie a",), ("italy", "italia", "itália"), "Itália - Serie A"),
        (("bundesliga",), ("germany", "alemanha"), "Alemanha - Bundesliga"),
        (("ligue 1",), ("france", "franca", "frança"), "França - Ligue 1"),
        (("liga portugal", "primeira liga"), ("portugal",), "Portugal - Liga Portugal"),
        (("eredivisie",), ("netherlands", "holanda"), "Holanda - Eredivisie"),
        (("premiership",), ("scotland", "escocia", "escócia"), "Escócia - Premiership"),
        (("super lig", "süper lig"), ("turkey", "turkiye", "türkiye", "turquia"), "Turquia - Süper Lig"),
        (("brasileirao serie a", "brasileirão série a", "brasileirao", "brasileirão"), ("brazil", "brasil"), "Brasil - Série A"),
        (("serie b", "brasileirao serie b", "brasileirão série b"), ("brazil", "brasil"), "Brasil - Série B"),
        (("saudi pro league", "professional league"), ("saudi arabia", "arabia saudita", "arábia saudita"), "Arábia Saudita - Saudi Pro League"),
        (("mls", "major league soccer"), ("usa", "united states", "estados unidos"), "Estados Unidos - MLS"),
        (("liga profesional",), ("argentina",), "Argentina - Liga Profesional"),
        (("liga mx",), ("mexico", "méxico"), "México - Liga MX"),
        (("primera a",), ("colombia", "colômbia"), "Colômbia - Primera A"),
    ]
    for tour_tokens, countries, comp in country_rules:
        if any(t in joined for t in tour_tokens) and (not country or any(c in country for c in countries)):
            # Brasileirão genérico precisa distinguir A/B pelo nome completo.
            if comp == "Brasil - Série A" and "serie b" in joined:
                continue
            return comp
    return None


def _sofascore_schedule_json(day_iso, timeout=12):
    errors = []
    urls = [
        f"https://api.sofascore.com/api/v1/sport/football/scheduled-events/{day_iso}",
        f"https://www.sofascore.com/api/v1/sport/football/scheduled-events/{day_iso}",
    ]
    header_variants = [HEADERS, {"Accept": "application/json"}, None]
    for url in urls:
        for headers in header_variants:
            try:
                r = requests.get(url, headers=headers, timeout=timeout)
                if r.status_code == 200 and r.content:
                    data = r.json()
                    if isinstance(data, dict) and isinstance(data.get("events"), list):
                        return data
                errors.append(f"{r.status_code} {url}")
            except Exception as exc:
                errors.append(str(exc))
    raise RuntimeError(errors[-1] if errors else "SofaScore indisponível")


@st.cache_data(ttl=1800, show_spinner=False)
def load_sofascore_fixtures_for_date(target_date):
    """Retorna a agenda completa das competições do app na data de Brasília."""
    if isinstance(target_date, pd.Timestamp):
        target_date = target_date.date()
    if isinstance(target_date, datetime):
        target_date = target_date.date()

    fixtures = []
    # O endpoint é organizado por dia do calendário da fonte. Consultar os dias
    # adjacentes evita perder partidas na virada UTC/Brasília.
    for source_date in (target_date - timedelta(days=1), target_date, target_date + timedelta(days=1)):
        try:
            payload = _sofascore_schedule_json(source_date.isoformat())
        except Exception:
            continue
        for ev in payload.get("events", []) or []:
            comp = _competition_from_sofascore_event(ev)
            if not comp or comp not in COMPETITIONS:
                continue
            home = str((ev.get("homeTeam") or {}).get("name") or "").strip()
            away = str((ev.get("awayTeam") or {}).get("name") or "").strip()
            if not home or not away:
                continue

            br_date = None
            br_time = ""
            ts = ev.get("startTimestamp")
            if ts is not None:
                try:
                    dt_br = datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(BRASILIA_TZ)
                    br_date = dt_br.date()
                    br_time = dt_br.strftime("%H:%M")
                except Exception:
                    pass
            if br_date != target_date:
                continue
            status_obj = ev.get("status") or {}
            fixtures.append({
                "competition": comp,
                "home": home,
                "away": away,
                "time": br_time,
                "br_date": br_date,
                "source": "SofaScore",
                "event_id": ev.get("id"),
                "status": status_obj.get("description") or status_obj.get("type") or "",
                "status_type": status_obj.get("type") or "",
            })
    return fixtures


# Fonte adicional para a agenda por data. Usa os calendários públicos da ESPN
# apenas para descobrir partidas (não altera as bases estatísticas antigas).
ESPN_FIXTURE_LEAGUES = {
    "Inglaterra - Premier League": "eng.1",
    "Espanha - La Liga": "esp.1",
    "Itália - Serie A": "ita.1",
    "Alemanha - Bundesliga": "ger.1",
    "França - Ligue 1": "fra.1",
    "Portugal - Liga Portugal": "por.1",
    "Holanda - Eredivisie": "ned.1",
    "Escócia - Premiership": "sco.1",
    "Turquia - Süper Lig": "tur.1",
    "Brasil - Série A": "bra.1",
    "Brasil - Série B": "bra.2",
    "Arábia Saudita - Saudi Pro League": "ksa.1",
    "Estados Unidos - MLS": "usa.1",
    "Argentina - Liga Profesional": "arg.1",
    "México - Liga MX": "mex.1",
    "Colômbia - Primera A": "col.1",
    "CONMEBOL Libertadores": "conmebol.libertadores",
    "CONMEBOL Sul-Americana": "conmebol.sudamericana",
    "UEFA Champions League": "uefa.champions",
    "UEFA Europa League": "uefa.europa",
    "UEFA Conference League": "uefa.europa.conf",
}


# Fonte pública complementar de agenda. Não participa de probabilidades/odds;
# serve apenas para descobrir partidas que uma das agendas principais omita.
THESPORTSDB_LEAGUE_ALIASES = {
    "english premier league": "Inglaterra - Premier League",
    "premier league": "Inglaterra - Premier League",
    "spanish la liga": "Espanha - La Liga", "la liga": "Espanha - La Liga",
    "italian serie a": "Itália - Serie A", "serie a": "Itália - Serie A",
    "german bundesliga": "Alemanha - Bundesliga", "bundesliga": "Alemanha - Bundesliga",
    "french ligue 1": "França - Ligue 1", "ligue 1": "França - Ligue 1",
    "portuguese primeira liga": "Portugal - Liga Portugal", "liga portugal": "Portugal - Liga Portugal",
    "dutch eredivisie": "Holanda - Eredivisie", "eredivisie": "Holanda - Eredivisie",
    "scottish premiership": "Escócia - Premiership",
    "turkish super lig": "Turquia - Süper Lig", "super lig": "Turquia - Süper Lig",
    "brazilian serie a": "Brasil - Série A", "brazilian serie b": "Brasil - Série B",
    "saudi pro league": "Arábia Saudita - Saudi Pro League",
    "american major league soccer": "Estados Unidos - MLS", "major league soccer": "Estados Unidos - MLS",
    "argentinian primera division": "Argentina - Liga Profesional", "liga profesional de futbol": "Argentina - Liga Profesional",
    "mexican primera league": "México - Liga MX", "liga mx": "México - Liga MX",
    "colombian primera a": "Colômbia - Primera A",
    "copa libertadores": "CONMEBOL Libertadores", "copa sudamericana": "CONMEBOL Sul-Americana",
    "uefa champions league": "UEFA Champions League", "uefa europa league": "UEFA Europa League",
    "uefa europa conference league": "UEFA Conference League", "uefa conference league": "UEFA Conference League",
}


def _gm_competition_from_public_league_name(value):
    norm = _gm_api_norm(value)
    for token, comp in THESPORTSDB_LEAGUE_ALIASES.items():
        if _gm_api_norm(token) == norm or _gm_api_norm(token) in norm:
            return comp
    return None


@st.cache_data(ttl=1800, show_spinner=False)
def load_thesportsdb_fixtures_for_date(target_date):
    """Agenda suplementar via TheSportsDB (chave pública de teste 3).

    Usada somente como redundância de calendário; nunca entra no motor
    estatístico, odds, pagamentos ou autenticação.
    """
    if isinstance(target_date, pd.Timestamp): target_date = target_date.date()
    if isinstance(target_date, datetime): target_date = target_date.date()
    try:
        target_date = target_date if isinstance(target_date, date) else pd.to_datetime(target_date).date()
    except Exception:
        return []
    try:
        r = requests.get(
            "https://www.thesportsdb.com/api/v1/json/3/eventsday.php",
            params={"d": target_date.isoformat(), "s": "Soccer"}, timeout=15,
            headers={"Accept": "application/json"},
        )
        if r.status_code != 200: return []
        data = r.json() if r.content else {}
    except Exception:
        return []
    out=[]
    for ev in (data or {}).get("events", []) or []:
        if not isinstance(ev, dict): continue
        comp = _gm_competition_from_public_league_name(ev.get("strLeague") or ev.get("strLeagueAlternate") or "")
        if not comp or comp not in COMPETITIONS: continue
        home=str(ev.get("strHomeTeam") or "").strip(); away=str(ev.get("strAwayTeam") or "").strip()
        if not home or not away: continue
        br_date=None; br_time=""
        raw_ts=str(ev.get("strTimestamp") or "").strip()
        if raw_ts:
            try:
                dt=pd.to_datetime(raw_ts, utc=True, errors="coerce")
                if not pd.isna(dt):
                    dt_br=dt.tz_convert(BRASILIA_TZ); br_date=dt_br.date(); br_time=dt_br.strftime("%H:%M")
            except Exception: pass
        if br_date != target_date: continue
        if not br_time:
            tm=str(ev.get("strTime") or "").strip()
            if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?",tm): br_time=tm[:5].zfill(5)
        out.append({"competition":comp,"home":home,"away":away,"time":br_time,"br_date":br_date,
                    "source":"TheSportsDB","event_id":str(ev.get("idEvent") or "").strip(),
                    "status":str(ev.get("strStatus") or "").strip()})
    return out


# V108: identidade canônica de clubes na AGENDA. As fontes públicas usam nomes,
# abreviações e grafias diferentes para o mesmo clube. Esta tabela não cria
# partidas e não participa do motor estatístico: ela somente converte variantes
# confirmadas para um único nome de exibição antes da deduplicação.
GM_FIXTURE_CANONICAL_TEAM_ALIASES = {
    "Hapoel Beer-Sheva": ["Hapoel Be'er Sheva", "Hapoel Beer Sheva", "H. Beer Sheva", "H Beer Sheva"],
    "Dinamo Zagreb": ["GNK Dinamo", "GNK Dinamo Zagreb", "Din. Zagreb"],
    "Sturm Graz": ["SK Sturm Graz"],
    "Rennes": ["Stade Rennais", "Stade Rennes", "Stade Rennais FC"],
    "Olympiacos": ["Olympiacos Piraeus", "Olympiakos Piraeus", "Olympiacos FC"],
    "Jagiellonia Białystok": ["Jagiellonia", "Jagiellonia Bialystok"],
    "Athletic Club": ["Ath Bilbao", "Athletic Bilbao", "Ath. Bilbao"],
    "LDU Quito": ["Ldu De Quito", "Liga de Quito", "Liga Deportiva Universitaria de Quito"],
    "Botafogo": ["Botafogo FR", "Botafogo RJ", "Botafogo de Futebol e Regatas"],
    "Grêmio": ["Grêmio FBPA", "Gremio FBPA", "Gremio"],
    "Estudiantes de La Plata": ["Estudiantes", "Estudiantes L.P.", "Estudiantes LP"],
    "Internacional de Bogotá": ["Inter Bogotá", "Internacional de Bogota", "La Equidad", "CD La Equidad"],
    "Atlético Nacional": ["Atl. Nacional", "Atletico Nacional"],
    "Deportivo La Coruña": ["Dep. A Coruna", "Dep. La Coruna", "Deportivo", "Deportivo A Coruna", "Deportivo La Coruna", "RC Deportivo", "RC Deportivo La Coruna"],
}


@lru_cache(maxsize=16384)
def _fixture_alias_key_cached(name):
    """Chave forte de identidade para aliases da agenda.

    Diferente de ``fixture_team_key``, remove pontuação interna antes de comparar.
    Assim, grafias como ``Be'er``/``Beer`` e ``L.P.``/``LP`` caem na mesma
    identidade sem depender do texto exibido pela fonte.
    """
    raw = unicodedata.normalize("NFKD", str(name or ""))
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch)).lower()
    raw = re.sub(r"[^a-z0-9]+", " ", raw)
    noise = {
        "fc", "cf", "ec", "ac", "sc", "afc", "fbpa", "club", "clube",
        "football", "futebol", "calcio", "soccer", "cd", "ud", "ad", "se", "aa"
    }
    tokens = [tok for tok in raw.split() if tok and tok not in noise]
    return "".join(tokens)


def _fixture_alias_key(name):
    return _fixture_alias_key_cached(str(name or ""))


@lru_cache(maxsize=16384)
def gm_fixture_canonical_team_name(name):
    """Retorna um único nome de exibição para variantes confirmadas do mesmo clube."""
    raw = str(name or "").strip()
    if not raw:
        return raw
    key = _fixture_alias_key(raw)
    for canonical, variants in GM_FIXTURE_CANONICAL_TEAM_ALIASES.items():
        if key == _fixture_alias_key(canonical):
            return canonical
        for variant in variants:
            if key == _fixture_alias_key(variant):
                return canonical
    # Reaproveita aliases oficiais já auditados no projeto quando houver
    # correspondência EXATA normalizada; nunca usa aproximação para renomear.
    for canonical, variants in (globals().get("GM_APIFOOTBALL_TEAM_ALIASES") or {}).items():
        if key == _fixture_alias_key(canonical):
            return canonical
        if any(key == _fixture_alias_key(v) for v in (variants or [])):
            return canonical
    return raw


def _fixture_compare_tokens(name):
    """Tokens canônicos SOMENTE para comparar nomes de clubes entre agendas."""
    name = gm_fixture_canonical_team_name(name)
    key = fixture_team_key(name)
    if not key:
        return []
    aliases = {
        "utd": "united", "man": "manchester", "intl": "internacional",
        "internazionale": "inter", "munchen": "muenchen", "muenchen": "muenchen",
        "din": "dinamo", "ldu": "liga", "lp": "plata", "atl": "atletico",
    }
    tokens = [aliases.get(tok, tok) for tok in key.split("_") if tok]
    noise = {"sk", "stade", "fr", "rj", "fc", "cf"}
    compact = [tok for tok in tokens if tok not in noise]
    return compact or tokens


def _fixture_identity_key(name):
    """Identidade textual de fallback quando não existe ID oficial resolvido."""
    return _fixture_alias_key(gm_fixture_canonical_team_name(name))


@st.cache_data(ttl=21600, show_spinner=False)
def gm_fixture_official_team_identity(name, competition, team_id=None):
    """Resolve a identidade do clube contra o cadastro oficial da competição.

    V110: a raiz das duplicatas era comparar textos vindos de provedores diferentes.
    Agora a agenda tenta converter CADA nome para o ``team_id`` oficial da APIfootball
    antes da deduplicação. O texto passa a ser apenas fallback. Uma aproximação só é
    aceita quando existe um único candidato forte no catálogo da própria competição.

    V116: o ID oficial continua sendo a identidade estrutural, mas o nome devolvido
    pelo provedor também passa pela camada canônica de exibição. Isso evita que um
    cadastro oficial com nome comercial antigo (ex.: clube renomeado) sobrescreva o
    nome atual já auditado pelo GM SCORE. A correção vale para qualquer data/tela que
    reutilize este resolvedor e não altera o ID, fixture_id nem cálculos estatísticos.
    """
    raw = str(name or "").strip()
    tid = str(team_id or "").strip()
    if not raw and not tid:
        return {"id": "", "name": raw, "resolved": False}
    try:
        catalog = gm_team_visual_catalog(competition) if competition else {"by_id": {}, "by_name": {}}
        by_id = catalog.get("by_id", {}) or {}
        by_name = catalog.get("by_name", {}) or {}

        # IDs trazidos pela fonte oficial têm precedência absoluta.
        if tid and tid in by_id:
            item = by_id[tid]
            return {"id": str(item.get("id") or tid), "name": gm_fixture_canonical_team_name(str(item.get("name") or raw)), "resolved": True}

        exact = by_name.get(_gm_api_norm(raw))
        if exact:
            return {"id": str(exact.get("id") or ""), "name": gm_fixture_canonical_team_name(str(exact.get("name") or raw)), "resolved": True}

        canonical = gm_fixture_canonical_team_name(raw)
        exact = by_name.get(_gm_api_norm(canonical))
        if exact:
            return {"id": str(exact.get("id") or ""), "name": gm_fixture_canonical_team_name(str(exact.get("name") or canonical)), "resolved": True}

        ranked = []
        for item in by_id.values():
            official = str(item.get("name") or "").strip()
            if not official:
                continue
            score = max(
                _gm_recovery_team_similarity(raw, official),
                _gm_recovery_team_similarity(canonical, official),
            )
            if _fixture_names_equivalent_text_only(canonical, official):
                score = max(score, 0.96)
            ranked.append((score, official, str(item.get("id") or "")))
        ranked.sort(key=lambda x: x[0], reverse=True)
        if ranked:
            best = ranked[0]
            second = ranked[1][0] if len(ranked) > 1 else 0.0
            # 0.92 cobre abreviações/subconjuntos como Hapoel Be'er ->
            # Hapoel Beer-Sheva. A margem impede escolher entre clubes ambíguos.
            if best[0] >= 0.92 and (best[0] - second >= 0.06 or best[0] >= 0.985):
                return {"id": best[2], "name": gm_fixture_canonical_team_name(best[1]), "resolved": bool(best[2])}
    except Exception:
        pass
    return {"id": tid, "name": canonical if 'canonical' in locals() else raw, "resolved": False}


def _fixture_names_equivalent_text_only(a, b):
    """Equivalência textual sem consultar catálogo; evita recursão no resolvedor V110."""
    ca, cb = gm_fixture_canonical_team_name(a), gm_fixture_canonical_team_name(b)
    if _fixture_identity_key(ca) == _fixture_identity_key(cb):
        return True
    ta_list, tb_list = _fixture_compare_tokens(ca), _fixture_compare_tokens(cb)
    if not ta_list or not tb_list:
        return False
    ka, kb = "_".join(ta_list), "_".join(tb_list)
    if ka == kb:
        return True
    if min(len(ka), len(kb)) >= 5 and (ka in kb or kb in ka):
        return True
    ta, tb = set(ta_list), set(tb_list)
    inter = len(ta & tb)
    coverage = inter / min(len(ta), len(tb))
    return inter >= 1 and (coverage >= 0.80 or ta.issubset(tb) or tb.issubset(ta))


def _fixture_names_equivalent(a, b):
    return _fixture_names_equivalent_text_only(a, b)


def _fixture_source_priority(f):
    """Prioridade de fonte para horário/status da agenda em TODAS as ligas."""
    src = _gm_api_norm((f or {}).get("source"))
    if "liga direta" in src:
        return 90
    if src == "apifootball" or ("apifootball" in src and "prediction" not in src):
        return 85
    if "espn" in src:
        return 80
    if "sofascore" in src:
        return 75
    if "thesportsdb" in src:
        return 65
    if "prediction" in src:
        return 20
    return 50


def _fixture_merge_score(f):
    tm = str((f or {}).get("time") or "").strip()
    has_time = bool(re.fullmatch(r"\d{2}:\d{2}", tm))
    return _fixture_source_priority(f) * 1000 + (100 if has_time else 0) + _fixture_quality(f)


def _merge_fixture_unique(best_list, candidate):
    """Mescla a mesma partida entre fontes e normaliza o nome oficial exibido."""
    if not valid_daily_fixture(candidate):
        return
    candidate = dict(candidate)
    comp = str(candidate.get("competition") or "")
    ch = gm_fixture_official_team_identity(candidate.get("home"), comp, candidate.get("home_team_id"))
    ca = gm_fixture_official_team_identity(candidate.get("away"), comp, candidate.get("away_team_id"))
    candidate["home"] = ch.get("name") or gm_fixture_canonical_team_name(candidate.get("home"))
    candidate["away"] = ca.get("name") or gm_fixture_canonical_team_name(candidate.get("away"))
    if ch.get("id"): candidate["home_team_id"] = ch.get("id")
    if ca.get("id"): candidate["away_team_id"] = ca.get("id")
    for i, old in enumerate(best_list):
        old_comp = str(old.get("competition") or "")
        if old_comp != comp:
            continue
        oh = gm_fixture_official_team_identity(old.get("home"), old_comp, old.get("home_team_id"))
        oa = gm_fixture_official_team_identity(old.get("away"), old_comp, old.get("away_team_id"))
        old["home"] = oh.get("name") or gm_fixture_canonical_team_name(old.get("home"))
        old["away"] = oa.get("name") or gm_fixture_canonical_team_name(old.get("away"))
        if oh.get("id"): old["home_team_id"] = oh.get("id")
        if oa.get("id"): old["away_team_id"] = oa.get("id")

        old_mid = str(old.get("match_id") or "").strip()
        new_mid = str(candidate.get("match_id") or "").strip()
        same_match_id = bool(old_mid and new_mid and old_mid == new_mid)
        old_hid, old_aid = str(old.get("home_team_id") or "").strip(), str(old.get("away_team_id") or "").strip()
        new_hid, new_aid = str(candidate.get("home_team_id") or "").strip(), str(candidate.get("away_team_id") or "").strip()
        same_team_ids = bool(old_hid and old_aid and new_hid and new_aid and old_hid == new_hid and old_aid == new_aid)
        home_same = bool(old_hid and new_hid and old_hid == new_hid) or _fixture_names_equivalent(old.get("home"), candidate.get("home"))
        away_same = bool(old_aid and new_aid and old_aid == new_aid) or _fixture_names_equivalent(old.get("away"), candidate.get("away"))
        same_names = home_same and away_same

        # V111 — reconciliação por EVENTO, não por grafia.
        # A raiz das duplicatas entre provedores é que um mesmo jogo pode chegar
        # com IDs incompatíveis/ausentes e nomes comerciais diferentes. Se duas
        # entradas são da mesma competição, no MESMO horário, e um dos lados já
        # foi identificado como o mesmo clube, elas representam o mesmo evento:
        # um clube não disputa dois jogos da mesma competição no mesmo instante.
        # O outro lado é então reconciliado pelo próprio evento, sem exigir uma
        # lista infinita de aliases (Prague/Praha, Be'er/Beer-Sheva etc.).
        old_time = str(old.get("time") or "").strip()
        new_time = str(candidate.get("time") or "").strip()
        same_kickoff = bool(
            re.fullmatch(r"\d{2}:\d{2}", old_time)
            and re.fullmatch(r"\d{2}:\d{2}", new_time)
            and old_time == new_time
        )
        same_event_by_anchor = bool(same_kickoff and (home_same or away_same))

        if not (same_match_id or same_team_ids or same_names or same_event_by_anchor):
            continue

        # Horário/status vêm da fonte mais confiável. Se a fonte escolhida usa
        # nomes abreviados, preserva nomes mais completos da outra fonte.
        if _fixture_merge_score(candidate) > _fixture_merge_score(old):
            merged = dict(candidate)
            if len(str(old.get("home") or "")) > len(str(merged.get("home") or "")):
                merged["home"] = old.get("home")
            if len(str(old.get("away") or "")) > len(str(merged.get("away") or "")):
                merged["away"] = old.get("away")
            best_list[i] = merged
        else:
            # Mesmo mantendo a fonte antiga, aproveita o nome oficial mais rico.
            if len(str(candidate.get("home") or "")) > len(str(old.get("home") or "")):
                best_list[i]["home"] = candidate.get("home")
            if len(str(candidate.get("away") or "")) > len(str(old.get("away") or "")):
                best_list[i]["away"] = candidate.get("away")
        # V87: metadados visuais/IDs podem vir de uma fonte diferente daquela
        # escolhida para horário/status. Eles nunca participam dos cálculos.
        for meta_key in ("home_team_id", "away_team_id"):
            if not str(best_list[i].get(meta_key) or "").strip() and str(candidate.get(meta_key) or "").strip():
                best_list[i][meta_key] = candidate.get(meta_key)
        return
    best_list.append(dict(candidate))


def _espn_get_json(url, timeout=15):
    """Consulta ESPN sem o User-Agent de navegador usado pelas demais fontes.

    A borda pública da ESPN pode responder 403 para User-Agents de navegador
    simulados. Por isso a agenda usa uma chamada isolada com o User-Agent padrão
    do ``requests`` e Accept JSON. Isso não altera nenhuma outra fonte do app.
    """
    errors = []
    header_variants = (
        {"Accept": "application/json"},
        {},
        {"User-Agent": "python-requests/2.x", "Accept": "application/json"},
    )
    for headers in header_variants:
        try:
            r = requests.get(url, headers=headers or None, timeout=timeout)
            if r.status_code == 200 and r.content:
                data = r.json()
                if isinstance(data, dict):
                    return data
            errors.append(f"HTTP {r.status_code}")
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError(errors[-1] if errors else "ESPN indisponível")


ESPN_SEASON_SLUG_MAP = {
    "english-premier-league": "Inglaterra - Premier League",
    "spanish-laliga": "Espanha - La Liga",
    "italian-serie-a": "Itália - Serie A",
    "german-bundesliga": "Alemanha - Bundesliga",
    "french-ligue-1": "França - Ligue 1",
    "portuguese-primeira-liga": "Portugal - Liga Portugal",
    "dutch-eredivisie": "Holanda - Eredivisie",
    "scottish-premiership": "Escócia - Premiership",
    "turkish-super-lig": "Turquia - Süper Lig",
    "brazilian-serie-a": "Brasil - Série A",
    "brazilian-serie-b": "Brasil - Série B",
    "saudi-pro-league": "Arábia Saudita - Saudi Pro League",
    "major-league-soccer": "Estados Unidos - MLS",
    "argentine-liga-profesional": "Argentina - Liga Profesional",
    "mexican-liga-bbva-mx": "México - Liga MX",
    "mexican-liga-mx": "México - Liga MX",
    "colombian-primera-a": "Colômbia - Primera A",
    "copa-libertadores": "CONMEBOL Libertadores",
    "copa-sudamericana": "CONMEBOL Sul-Americana",
    "uefa-champions-league": "UEFA Champions League",
    "uefa-europa-league": "UEFA Europa League",
    "uefa-conference-league": "UEFA Conference League",
}


def _competition_from_espn_event(event, fallback=None):
    """Identifica a competição de um evento ESPN sem aceitar torneios fora do app."""
    if fallback in COMPETITIONS:
        return fallback

    candidates = []
    season = event.get("season", {}) or {}
    for value in (season.get("slug"), season.get("displayName"), event.get("league")):
        if value:
            candidates.append(clean_col(value).replace("_", "-"))

    # Alguns envelopes incluem liga dentro da competição/evento.
    for contest in event.get("competitions", []) or []:
        lg = contest.get("league") or contest.get("type") or {}
        if isinstance(lg, dict):
            for value in (lg.get("slug"), lg.get("name"), lg.get("abbreviation")):
                if value:
                    candidates.append(clean_col(value).replace("_", "-"))

    joined = " ".join(candidates)
    for token, competition in ESPN_SEASON_SLUG_MAP.items():
        if token in joined:
            return competition
    return None


def _fixtures_from_espn_payload(data, target_date, fallback_competition=None):
    fixtures = []
    for event in data.get("events", []) or []:
        competition = _competition_from_espn_event(event, fallback_competition)
        if not competition or competition not in COMPETITIONS:
            continue
        comps = event.get("competitions", []) or []
        if not comps:
            continue
        contest = comps[0]
        competitors = contest.get("competitors", []) or []
        home = away = None
        for c in competitors:
            team = c.get("team", {}) or {}
            name = team.get("displayName") or team.get("shortDisplayName") or team.get("name")
            if c.get("homeAway") == "home":
                home = name
            elif c.get("homeAway") == "away":
                away = name
        if not home or not away:
            continue

        raw_dt = event.get("date") or contest.get("date")
        br_time = ""
        br_date = None
        if raw_dt:
            try:
                dt = pd.to_datetime(raw_dt, utc=True, errors="coerce")
                if not pd.isna(dt):
                    dt_br = dt.tz_convert(BRASILIA_TZ)
                    br_date = dt_br.date()
                    br_time = dt_br.strftime("%H:%M")
            except Exception:
                pass
        if br_date != target_date:
            continue

        status_obj = contest.get("status") or event.get("status") or {}
        status_type = status_obj.get("type") if isinstance(status_obj, dict) else {}
        if not isinstance(status_type, dict):
            status_type = {}
        fixtures.append({
            "competition": competition,
            "home": home,
            "away": away,
            "time": br_time,
            "br_date": br_date,
            "source": "ESPN",
            "status": status_type.get("description") or status_type.get("detail") or "",
            "state": status_type.get("state") or "",
            "status_type": status_type.get("name") or status_type.get("shortDetail") or "",
        })
    return fixtures


@st.cache_data(ttl=1800, show_spinner=False)
def load_espn_fixtures_for_date(target_date):
    """Agenda ESPN por data, isolada das bases estatísticas do GM SCORE.

    Consulta primeiro o placar global e depois as ligas individualmente. Também
    consulta os dias UTC adjacentes para não perder jogos noturnos que, em UTC,
    aparecem no dia seguinte. O filtro final sempre usa a data de Brasília.
    """
    if isinstance(target_date, pd.Timestamp):
        target_date = target_date.date()

    fixtures = []
    query_dates = [target_date - timedelta(days=1), target_date, target_date + timedelta(days=1)]

    # 1) Fonte global: uma única agenda ampla reduz risco de rate-limit e cobre
    # competições em que o slug individual da ESPN muda ao longo da temporada.
    for query_date in query_dates:
        date_token = query_date.strftime("%Y%m%d")
        try:
            data = _espn_get_json(
                "https://site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard"
                f"?dates={date_token}&limit=1000",
                timeout=15,
            )
            fixtures.extend(_fixtures_from_espn_payload(data, target_date))
        except Exception:
            pass

    # 2) Complemento liga a liga. É importante para eventos cujo payload global
    # não expõe informação suficiente para identificar a competição.
    for competition, league_slug in ESPN_FIXTURE_LEAGUES.items():
        for query_date in query_dates:
            date_token = query_date.strftime("%Y%m%d")
            try:
                data = _espn_get_json(
                    "https://site.api.espn.com/apis/site/v2/sports/soccer/"
                    f"{league_slug}/scoreboard?dates={date_token}&limit=100",
                    timeout=12,
                )
                fixtures.extend(_fixtures_from_espn_payload(data, target_date, competition))
                # Se esta data já retornou jogos para a liga, não precisamos das
                # demais variantes UTC para a maioria dos campeonatos europeus.
            except Exception:
                continue
    return fixtures


@st.cache_data(ttl=600, show_spinner=False)
def load_apifootball_prediction_fixtures_for_date(target_date, competition=None):
    """Recupera a agenda futura pelo endpoint get_predictions.

    O mesmo endpoint já alimenta as Oportunidades GM e costuma disponibilizar
    partidas futuras mesmo quando ``get_events`` retorna uma grade parcial.
    Aqui ele é usado SOMENTE para descobrir confrontos/horários; probabilidades
    dele não entram na tela de agenda nem substituem o motor estatístico.
    """
    if isinstance(target_date, pd.Timestamp):
        target_date = target_date.date()
    if isinstance(target_date, datetime):
        target_date = target_date.date()
    if not isinstance(target_date, date):
        try:
            target_date = pd.to_datetime(target_date).date()
        except Exception:
            return []

    league_filter = None
    if competition is not None:
        league_filter = str((GM_APIFOOTBALL_FIXED_LEAGUE_IDS or {}).get(competition) or "").strip()
        if not league_filter:
            return []

    payload, err = gm_apifootball_request(
        "get_predictions",
        **{"from": target_date.isoformat(), "to": target_date.isoformat()},
    )
    if err or not isinstance(payload, list):
        return []

    reverse_ids = {str(v): k for k, v in (GM_APIFOOTBALL_FIXED_LEAGUE_IDS or {}).items()}
    fixtures = []
    for ev in payload:
        if not isinstance(ev, dict):
            continue
        lid = str(ev.get("league_id") or "").strip()
        if league_filter and lid != league_filter:
            continue
        comp = competition if competition is not None else reverse_ids.get(lid)
        if not comp or comp not in COMPETITIONS:
            continue
        home = str(ev.get("match_hometeam_name") or "").strip()
        away = str(ev.get("match_awayteam_name") or "").strip()
        if not home or not away:
            continue
        # V119: nunca inventar a data de uma previsão. O endpoint de predictions
        # pode devolver registros antigos/incompletos mesmo numa consulta por intervalo.
        # Sem match_date explícito e válido, o registro NÃO pode descobrir um jogo do dia.
        raw_date = str(ev.get("match_date") or "").strip()
        if not raw_date:
            continue
        try:
            event_date = pd.to_datetime(raw_date, errors="coerce")
            br_date = None if pd.isna(event_date) else event_date.date()
        except Exception:
            br_date = None
        if br_date != target_date:
            continue
        fixtures.append({
            "competition": comp,
            "home": home,
            "away": away,
            # get_predictions é usado como RECUPERAÇÃO de confronto. O campo
            # match_time desse endpoint pode vir em outro fuso; por segurança a
            # agenda só recebe horário dele se outra fonte oficial não existir.
            # Guardamos o valor bruto apenas para diagnóstico interno.
            "time": "",
            "prediction_time_raw": str(ev.get("match_time") or "").strip(),
            "br_date": br_date,
            "match_id": str(ev.get("match_id") or "").strip(),
            "league_id": lid,
            "home_team_id": str(ev.get("match_hometeam_id") or "").strip(),
            "away_team_id": str(ev.get("match_awayteam_id") or "").strip(),
            "status": str(ev.get("match_status") or "").strip(),
            "source": "APIfootball · predictions",
        })
    return fixtures


GM_DOMESTIC_ROSTER_GUARD_COMPETITIONS = {
    "Inglaterra - Premier League", "Espanha - La Liga", "Itália - Serie A",
    "Alemanha - Bundesliga", "França - Ligue 1", "Portugal - Liga Portugal",
    "Holanda - Eredivisie", "Escócia - Premiership", "Turquia - Süper Lig",
    "Brasil - Série A", "Brasil - Série B", "Arábia Saudita - Saudi Pro League",
    "Estados Unidos - MLS", "Argentina - Liga Profesional", "México - Liga MX",
    "Colômbia - Primera A",
}


@st.cache_data(ttl=1800, show_spinner=False)
def gm_apifootball_current_roster_names(competition):
    """Obtém o elenco atual da liga pelo league_id auditado; nunca inventa clubes."""
    league_id = str((GM_APIFOOTBALL_FIXED_LEAGUE_IDS or {}).get(competition) or "").strip()
    if not league_id:
        return []
    try:
        teams, err = gm_apifootball_league_teams(league_id)
    except Exception:
        return []
    if err or not isinstance(teams, list):
        return []
    names = []
    for item in teams:
        if not isinstance(item, dict):
            continue
        name = str(item.get("team_name") or "").strip()
        if name and is_main_senior_team_name(name):
            names.append(name)
    return names


def gm_fixture_matches_official_league_roster(fixture):
    """Bloqueia divisões erradas em fontes de fallback sem cortar a fonte oficial.

    Partidas APIfootball já vinculadas ao league_id auditado são aceitas diretamente.
    Nas demais fontes, validamos as duas equipes contra o elenco atual da mesma liga.
    Se a API não devolver elenco suficiente, a função falha aberta para não ocultar
    partidas legítimas por indisponibilidade temporária do provedor.
    """
    fixture = fixture or {}
    competition = str(fixture.get("competition") or "")
    if competition not in GM_DOMESTIC_ROSTER_GUARD_COMPETITIONS:
        return True
    league_id = str((GM_APIFOOTBALL_FIXED_LEAGUE_IDS or {}).get(competition) or "").strip()
    source = str(fixture.get("source") or "")
    fixture_league_id = str(fixture.get("league_id") or "").strip()
    if source.startswith("APIfootball") and league_id and fixture_league_id == league_id:
        return True
    roster = gm_apifootball_current_roster_names(competition)
    minimum = int((MIN_TEAMS or {}).get(competition) or 0)
    # Exige cobertura razoável antes de usar o elenco como trava.
    if not roster or (minimum and len(roster) < max(8, int(minimum * 0.70))):
        return True
    home = str(fixture.get("home") or "").strip()
    away = str(fixture.get("away") or "").strip()
    if not home or not away:
        return False
    home_ok = any(_gm_team_name_match(home, team) for team in roster)
    away_ok = any(_gm_team_name_match(away, team) for team in roster)
    return bool(home_ok and away_ok)


@st.cache_data(ttl=900, show_spinner=False)
def load_apifootball_fixtures_for_date(target_date):
    """Agenda oficial das 21 competições via APIfootball.

    Esta é a fonte primária de descoberta de partidas por data. Usa os league_id
    auditados já empregados no motor estatístico e pede explicitamente o fuso de
    Brasília, evitando perdas de jogos por diferença UTC/local. Outras agendas
    permanecem como complementos/fallback e nunca substituem uma partida oficial
    retornada aqui.
    """
    if isinstance(target_date, pd.Timestamp):
        target_date = target_date.date()
    if isinstance(target_date, datetime):
        target_date = target_date.date()
    if not isinstance(target_date, date):
        try:
            target_date = pd.to_datetime(target_date).date()
        except Exception:
            return []

    reverse_ids = {str(v): k for k, v in (GM_APIFOOTBALL_FIXED_LEAGUE_IDS or {}).items()}
    if not reverse_ids:
        return []

    payload, err = gm_apifootball_request(
        "get_events",
        **{"from": target_date.isoformat(), "to": target_date.isoformat()},
        timezone="America/Sao_Paulo",
    )
    if err or not isinstance(payload, list):
        return []

    fixtures = []
    for ev in payload:
        if not isinstance(ev, dict):
            continue
        comp = reverse_ids.get(str(ev.get("league_id") or "").strip())
        if not comp:
            continue
        home = str(ev.get("match_hometeam_name") or "").strip()
        away = str(ev.get("match_awayteam_name") or "").strip()
        if not home or not away:
            continue
        match_date = str(ev.get("match_date") or "").strip()
        if not match_date:
            continue
        try:
            parsed_match_date = pd.to_datetime(match_date, errors="coerce")
            br_date = None if pd.isna(parsed_match_date) else parsed_match_date.date()
        except Exception:
            br_date = None
        if br_date != target_date:
            continue
        time_txt = str(ev.get("match_time") or "").strip()
        fixtures.append({
            "competition": comp,
            "home": home,
            "away": away,
            "time": time_txt,
            "br_date": target_date,
            "match_id": str(ev.get("match_id") or "").strip(),
            "league_id": str(ev.get("league_id") or "").strip(),
            "home_team_id": str(ev.get("match_hometeam_id") or "").strip(),
            "away_team_id": str(ev.get("match_awayteam_id") or "").strip(),
            "status": str(ev.get("match_status") or "").strip(),
            "source": "APIfootball",
        })
    return fixtures


@st.cache_data(ttl=600, show_spinner=False)
def load_apifootball_competition_fixtures_for_date(competition, target_date):
    """Consulta DIRETAMENTE uma competição por league_id + data.

    A consulta global do provedor pode, em alguns momentos, retornar uma grade
    parcial. Para a tela da competição selecionada fazemos uma chamada focada
    no league_id auditado, garantindo que um jogo não desapareça apenas porque
    outra fonte retornou uma lista incompleta.
    """
    league_id = (GM_APIFOOTBALL_FIXED_LEAGUE_IDS or {}).get(competition)
    if not league_id:
        return []
    if isinstance(target_date, pd.Timestamp):
        target_date = target_date.date()
    if isinstance(target_date, datetime):
        target_date = target_date.date()
    if not isinstance(target_date, date):
        try:
            target_date = pd.to_datetime(target_date).date()
        except Exception:
            return []

    payload, err = gm_apifootball_request(
        "get_events",
        **{
            "from": target_date.isoformat(),
            "to": target_date.isoformat(),
            "league_id": str(league_id),
        },
        timezone="America/Sao_Paulo",
    )
    if err or not isinstance(payload, list):
        return []

    fixtures = []
    for ev in payload:
        if not isinstance(ev, dict):
            continue
        if str(ev.get("league_id") or "").strip() != str(league_id):
            continue
        home = str(ev.get("match_hometeam_name") or "").strip()
        away = str(ev.get("match_awayteam_name") or "").strip()
        if not home or not away:
            continue
        match_date = str(ev.get("match_date") or "").strip()
        if not match_date:
            continue
        try:
            parsed_match_date = pd.to_datetime(match_date, errors="coerce")
            event_date = None if pd.isna(parsed_match_date) else parsed_match_date.date()
        except Exception:
            event_date = None
        if event_date != target_date:
            continue
        fixtures.append({
            "competition": competition,
            "home": home,
            "away": away,
            "time": str(ev.get("match_time") or "").strip(),
            "br_date": event_date,
            "match_id": str(ev.get("match_id") or "").strip(),
            "league_id": str(ev.get("league_id") or "").strip(),
            "home_team_id": str(ev.get("match_hometeam_id") or "").strip(),
            "away_team_id": str(ev.get("match_awayteam_id") or "").strip(),
            "status": str(ev.get("match_status") or "").strip(),
            "source": "APIfootball · liga direta",
        })
    return fixtures


@st.cache_data(ttl=900, show_spinner=False)
def load_apifootball_all_competitions_fixtures_for_date(target_date):
    """Varredura de cobertura da agenda nas 21 competições suportadas.

    A consulta global de ``get_events`` pode retornar uma grade parcial. Esta
    rotina consulta cada ``league_id`` auditado separadamente e serve SOMENTE
    para completar a agenda de partidas. Não fornece dados ao motor estatístico
    e não altera probabilidades, médias, thresholds ou critérios de análise.
    """
    if isinstance(target_date, pd.Timestamp):
        target_date = target_date.date()
    if isinstance(target_date, datetime):
        target_date = target_date.date()
    if not isinstance(target_date, date):
        try:
            target_date = pd.to_datetime(target_date).date()
        except Exception:
            return []

    # V121: as 21 consultas independentes eram executadas em série e eram o maior
    # gargalo da aba Jogos. Mantemos exatamente as mesmas fontes e respostas, mas
    # fazemos I/O em paralelo com um limite conservador de workers. O cache de cada
    # competição continua intacto; nenhuma regra de agenda ou cálculo é alterada.
    fixtures = []
    competitions = list(GM_APIFOOTBALL_FIXED_LEAGUE_IDS.keys())
    def _load_one(_competition):
        try:
            return load_apifootball_competition_fixtures_for_date(_competition, target_date) or []
        except Exception:
            return []
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(competitions)))) as pool:
        futures = [pool.submit(_load_one, competition) for competition in competitions]
        for future in as_completed(futures):
            try:
                fixtures.extend(future.result() or [])
            except Exception:
                pass
    return fixtures


@st.cache_data(ttl=3600, show_spinner=False)
def load_fixtures_for_date(target_date):
    """Carrega somente jogos das competições suportadas para uma data de Brasília."""
    today = target_date
    try:
        _day_iso = pd.to_datetime(today).date().isoformat()
    except Exception:
        _day_iso = str(today)

    # V149: agenda diária compartilhada e persistente. Depois da primeira montagem,
    # Jogos/Home/Dicas reutilizam o mesmo resultado mesmo após reruns do Streamlit.
    try:
        _disk = _gm_daily_pick_disk_cache_load(_day_iso, "fixtures", ttl=1800)
    except Exception:
        _disk = None
    if isinstance(_disk, dict) and isinstance(_disk.get("fixtures"), list):
        return _disk.get("fixtures") or []

    fixtures = []

    # v42: primeiro recupera confrontos pelo endpoint de previsões, que já é
    # usado pelas Oportunidades e costuma trazer a grade futura completa mesmo
    # quando get_events está parcial. Depois todas as outras fontes complementam.
    try:
        fixtures.extend(load_apifootball_prediction_fixtures_for_date(today))
    except Exception:
        pass

    # v35: APIfootball passa a ser a primeira fonte da agenda porque os 21
    # league_id já foram auditados no próprio GM SCORE. Isso evita depender de
    # mapeamento por nome e corrige omissões pontuais (ex.: Premier League).
    try:
        fixtures.extend(load_apifootball_fixtures_for_date(today))
    except Exception:
        pass

    # v100: cobertura liga a liga. O endpoint global do provedor pode omitir
    # partidas isoladas mesmo quando outras partidas da mesma competição vêm na
    # resposta. Consultar os 21 league_id auditados individualmente evita esse
    # falso negativo (especialmente em mata-matas continentais) e vale para
    # qualquer data escolhida pelo usuário. É somente descoberta de agenda.
    try:
        fixtures.extend(load_apifootball_all_competitions_fixtures_for_date(today))
    except Exception:
        pass

    # SofaScore/ESPN e demais calendários continuam como complementos.
    # já usa essa fonte pública em H2H/odds. ESPN e fontes antigas permanecem como
    # complementos, sem alterar os dados históricos ou os cálculos de análise.
    try:
        fixtures.extend(load_sofascore_fixtures_for_date(today))
    except Exception:
        pass
    try:
        fixtures.extend(load_espn_fixtures_for_date(today))
    except Exception:
        pass
    try:
        fixtures.extend(load_thesportsdb_fixtures_for_date(today))
    except Exception:
        pass

    # Fonte diária abrangente: consulta também o dia UTC seguinte para não perder jogos
    # que ainda pertencem ao dia de Brasília após a conversão de fuso.
    for source_date in (today, today + timedelta(days=1)):
        try:
            for f in load_livescore_today(source_date):
                if f.get("br_date") in (None, today):
                    fixtures.append(f)
        except Exception:
            pass

    # Ligas europeias: arquivo público de fixtures semanais.
    try:
        r = request_first(["https://www.football-data.co.uk/matches/resources/fixtures.csv"])
        fdf = read_csv_bytes(r.content)
        if {"Div", "Date", "HomeTeam", "AwayTeam"}.issubset(fdf.columns):
            dates = pd.to_datetime(fdf["Date"], dayfirst=True, errors="coerce").dt.date
            reverse_codes = {v: k for k, v in EUROPE_LEAGUES.items()}
            # Considera datas locais próximas e filtra somente depois de converter para Brasília.
            for idx, g in fdf.iterrows():
                fixture_date = dates.loc[idx]
                if pd.isna(fixture_date) or abs((fixture_date - today).days) > 1:
                    continue
                comp = reverse_codes.get(str(g["Div"]))
                if comp:
                    br_time, br_date = fixture_time_brasilia(str(g.get("Time", "")), comp, fixture_date, source_tz="UTC")
                    if br_date not in (None, today):
                        continue
                    fixtures.append({"competition": comp, "home": str(g["HomeTeam"]), "away": str(g["AwayTeam"]), "time": br_time, "br_date": br_date or fixture_date, "source": "football-data fixtures"})
    except Exception:
        pass

    # Competições em JSON aberto com calendário da temporada.
    json_comps = [
        ("Brasil - Série A", str(today.year), ["br.1.json"]),
        ("Brasil - Série B", str(today.year), ["br.2.json"]),
        ("Estados Unidos - MLS", str(today.year), ["mls.json", "us.1.json", "usa.1.json"]),
        ("Argentina - Liga Profesional", str(today.year), ["ar.1.json", "arg.1.json", "ar-liga.json"]),
        ("México - Liga MX", str(today.year), ["mx.1.json", "mex.1.json", "liga-mx.json"]),
        ("Colômbia - Primera A", str(today.year), ["co.1.json", "col.1.json", "colombia.1.json"]),
    ]
    saudi_folder = f"{current_season_year('europe')}-{str(current_season_year('europe') + 1)[-2:]}"
    json_comps.append(("Arábia Saudita - Saudi Pro League", saudi_folder, ["sa.1.json", "ksa.1.json", "saudi.1.json", "saudi-pro-league.json"]))

    for comp, folder, filenames in json_comps:
        try:
            urls = []
            for filename in filenames:
                urls.extend([
                    f"https://raw.githubusercontent.com/openfootball/football.json/master/{folder}/{filename}",
                    f"https://github.com/openfootball/football.json/raw/refs/heads/master/{folder}/{filename}",
                ])
            data = load_open_json(urls)
            for m in data.get("matches", []):
                d = pd.to_datetime(m.get("date"), errors="coerce")
                if pd.isna(d) or abs((d.date() - today).days) > 1:
                    continue
                br_time, br_date = fixture_time_brasilia(m.get("time", ""), comp, d.date())
                if br_date not in (None, today):
                    continue
                fixtures.append({"competition": comp, "home": m.get("team1"), "away": m.get("team2"), "time": br_time, "br_date": br_date or d.date(), "source": "OpenFootball JSON"})
        except Exception:
            pass

    # Torneios continentais em calendários públicos OpenFootball.
    yy = str(today.year + 1)[-2:]
    cup_sources = [
        ("CONMEBOL Libertadores", [
            f"https://raw.githubusercontent.com/openfootball/south-america/master/copa-libertadores/{today.year}_copal.txt",
            f"https://github.com/openfootball/south-america/raw/refs/heads/master/copa-libertadores/{today.year}_copal.txt",
        ]),
        ("CONMEBOL Sul-Americana", [
            f"https://raw.githubusercontent.com/openfootball/south-america/master/copa-sudamericana/{today.year}_copas.txt",
            f"https://raw.githubusercontent.com/openfootball/south-america/master/copa-sudamericana/{today.year}_copa_sudamericana.txt",
        ]),
        ("UEFA Champions League", [
            f"https://raw.githubusercontent.com/openfootball/champions-league/master/{today.year}-{yy}/cl.txt",
            f"https://raw.githubusercontent.com/openfootball/champions-league/master/{today.year}/cl.txt",
        ]),
        ("UEFA Europa League", [
            f"https://raw.githubusercontent.com/openfootball/champions-league/master/{today.year}-{yy}/el.txt",
            f"https://raw.githubusercontent.com/openfootball/champions-league/master/{today.year}-{yy}/europa.txt",
        ]),
        ("UEFA Conference League", [
            f"https://raw.githubusercontent.com/openfootball/champions-league/master/{today.year}-{yy}/conf.txt",
            f"https://raw.githubusercontent.com/openfootball/champions-league/master/{today.year}-{yy}/conference.txt",
            f"https://raw.githubusercontent.com/openfootball/champions-league/master/{today.year}-{yy}/ecl.txt",
        ]),
    ]
    for comp, urls in cup_sources:
        try:
            txt = request_first(urls).text
            fixtures.extend(parse_today_from_openfootball_text(txt, today, comp))
        except Exception:
            pass

    # V120 — BARREIRA AUTORITATIVA DE EVENTO POR DATA.
    # A causa estrutural dos jogos de dias errados era permitir que um calendário
    # secundário/fallback criasse sozinho um evento, mesmo quando a APIfootball
    # oficial daquela competição/data já fornecia a grade. A data declarada pelo
    # fallback podia estar stale/incorreta e ainda assim passar pelas barreiras.
    #
    # Regra: quando existe ao menos um fixture APIfootball oficial para a competição
    # na data selecionada, fontes secundárias SOMENTE podem complementar um desses
    # fixtures (nomes/horário/metadados); nunca podem criar um confronto adicional.
    # Se a fonte oficial estiver totalmente vazia para a competição, mantemos os
    # fallbacks para não destruir a cobertura histórica do app. Assim não há
    # correções pontuais por clube/data e a regra vale para qualquer dia consultado.
    official_by_comp = {}
    for _of in fixtures:
        _src = _gm_api_norm((_of or {}).get("source"))
        _comp = str((_of or {}).get("competition") or "")
        _lid = str((_of or {}).get("league_id") or "").strip()
        _expected_lid = str((GM_APIFOOTBALL_FIXED_LEAGUE_IDS or {}).get(_comp) or "").strip()
        if _comp and "apifootball" in _src and "prediction" not in _src and _lid and _lid == _expected_lid:
            if fixture_matches_selected_date(_of, today):
                official_by_comp.setdefault(_comp, []).append(_of)

    def _gm_matches_authoritative_fixture(_f, _official):
        _comp = str((_f or {}).get("competition") or "")
        if _comp != str((_official or {}).get("competition") or ""):
            return False
        _fh = gm_fixture_official_team_identity(_f.get("home"), _comp, _f.get("home_team_id"))
        _fa = gm_fixture_official_team_identity(_f.get("away"), _comp, _f.get("away_team_id"))
        _oh = gm_fixture_official_team_identity(_official.get("home"), _comp, _official.get("home_team_id"))
        _oa = gm_fixture_official_team_identity(_official.get("away"), _comp, _official.get("away_team_id"))
        _fhid, _faid = str(_fh.get("id") or ""), str(_fa.get("id") or "")
        _ohid, _oaid = str(_oh.get("id") or ""), str(_oa.get("id") or "")
        if _fhid and _faid and _ohid and _oaid:
            return _fhid == _ohid and _faid == _oaid
        return bool(
            _fixture_names_equivalent(_fh.get("name") or _f.get("home"), _oh.get("name") or _official.get("home"))
            and _fixture_names_equivalent(_fa.get("name") or _f.get("away"), _oa.get("name") or _official.get("away"))
        )

    if official_by_comp:
        _validated_fixtures = []
        for _f in fixtures:
            _comp = str((_f or {}).get("competition") or "")
            _officials = official_by_comp.get(_comp)
            if not _officials:
                _validated_fixtures.append(_f)
                continue
            _src = _gm_api_norm((_f or {}).get("source"))
            if "apifootball" in _src and "prediction" not in _src:
                _validated_fixtures.append(_f)
                continue
            if any(_gm_matches_authoritative_fixture(_f, _of) for _of in _officials):
                _validated_fixtures.append(_f)
        fixtures = _validated_fixtures

    # Remove duplicados mesmo quando as fontes usam nomes diferentes
    # (ex.: "Vitoria" x "EC Vitória"; "Gremio" x "Grêmio FBPA").
    best = []
    for f in fixtures:
        if not valid_daily_fixture(f) or not fixture_matches_selected_date(f, today):
            continue
        if not gm_fixture_matches_official_league_roster(f):
            continue
        comp = f.get("competition")
        ff = dict(f)
        if comp in STRICT_OFFICIAL_ROSTERS:
            roster = CURRENT_TEAM_ROSTERS.get(comp, [])
            ff["home"] = resolve_team_name(ff.get("home"), roster) or ff.get("home")
            ff["away"] = resolve_team_name(ff.get("away"), roster) or ff.get("away")
        _merge_fixture_unique(best, ff)
    result = [x for x in best if valid_daily_fixture(x)]
    try:
        _gm_daily_pick_disk_cache_save(_day_iso, {"fixtures": result}, "fixtures")
    except Exception:
        pass
    return result


@st.cache_data(ttl=1800, show_spinner=False)
def load_competition_fixtures_for_date(competition, target_date):
    """Agenda focada por competição + data.

    A consulta focada evita depender de uma busca global com dezenas de ligas.
    Usa primeiro o endpoint individual da competição e depois reaproveita as
    fontes públicas antigas apenas como complemento. Não altera dados de análise.
    """
    if competition not in COMPETITIONS:
        return []
    if isinstance(target_date, pd.Timestamp):
        target_date = target_date.date()
    if isinstance(target_date, datetime):
        target_date = target_date.date()

    fixtures = []

    # v42: fonte de recuperação principal para partidas futuras. O endpoint
    # get_predictions já abastece a seleção diária e retorna confrontos/horários
    # mesmo quando o calendário get_events vem incompleto.
    try:
        fixtures.extend(load_apifootball_prediction_fixtures_for_date(target_date, competition))
    except Exception:
        pass

    # v39: consulta focada por league_id primeiro. Isso evita depender da resposta
    # global do provedor, que pode vir parcial em determinados momentos.
    try:
        fixtures.extend(load_apifootball_competition_fixtures_for_date(competition, target_date))
    except Exception:
        pass

    # Mantém a consulta global como segunda confirmação.
    try:
        fixtures.extend([f for f in load_apifootball_fixtures_for_date(target_date) if f.get("competition") == competition])
    except Exception:
        pass

    slug = ESPN_FIXTURE_LEAGUES.get(competition)
    if slug:
        for qd in (target_date - timedelta(days=1), target_date, target_date + timedelta(days=1)):
            try:
                token = qd.strftime("%Y%m%d")
                data = _espn_get_json(
                    f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard?dates={token}&limit=200",
                    timeout=12,
                )
                fixtures.extend(_fixtures_from_espn_payload(data, target_date, competition))
            except Exception:
                pass

    # SofaScore complementa nomes/horários e torneios continentais.
    try:
        fixtures.extend([f for f in load_sofascore_fixtures_for_date(target_date) if f.get("competition") == competition])
    except Exception:
        pass
    try:
        fixtures.extend([f for f in load_thesportsdb_fixtures_for_date(target_date) if f.get("competition") == competition])
    except Exception:
        pass

    # v40: SEMPRE mescla as fontes anteriores. Antes, bastava uma fonte devolver
    # um único jogo para o fallback ser ignorado; isso podia ocultar partidas da
    # mesma rodada. Agora cada fonte complementa as demais e a deduplicação final
    # escolhe a melhor versão de cada confronto.
    try:
        fixtures.extend([f for f in load_fixtures_for_date(target_date) if f.get("competition") == competition])
    except Exception:
        pass

    best = []
    for f in fixtures:
        if (
            f.get("competition") != competition
            or not valid_daily_fixture(f)
            or not fixture_matches_selected_date(f, target_date)
        ):
            continue
        if not gm_fixture_matches_official_league_roster(f):
            continue
        ff = dict(f)
        if competition in STRICT_OFFICIAL_ROSTERS:
            roster = CURRENT_TEAM_ROSTERS.get(competition, [])
            ff["home"] = resolve_team_name(ff.get("home"), roster) or ff.get("home")
            ff["away"] = resolve_team_name(ff.get("away"), roster) or ff.get("away")
        _merge_fixture_unique(best, ff)
    return sorted(best, key=lambda f: str(f.get("time") or "99:99"))


def load_today_fixtures():
    """Compatibilidade com as telas antigas: jogos da data atual em Brasília."""
    return load_fixtures_for_date(datetime.now(BRASILIA_TZ).date())

def validate_current_data(df, season_type, competition_name):
    """Impede que uma base antiga seja apresentada como temporada atual."""
    if df is None or df.empty:
        raise RuntimeError("sem dados da temporada vigente")

    updated = df.attrs.get("updated_until")
    if updated is None or pd.isna(updated):
        return df

    updated = pd.Timestamp(updated)
    now = pd.Timestamp.now()
    # Para ligas em andamento, uma defasagem grande normalmente indica dataset abandonado.
    # 35 dias tolera pausas internacionais e intervalos de calendário.
    if updated.year < now.year - 1:
        raise RuntimeError(f"base desatualizada (último jogo em {updated:%d/%m/%Y})")
    if season_type == "calendar" and updated.year != now.year:
        raise RuntimeError(f"a fonte não contém a temporada {now.year}")
    if season_type == "europe":
        expected = current_season_year("europe")
        if updated.year < expected:
            raise RuntimeError(f"a fonte não contém a temporada {season_label(expected, 'europe')}")
    return df


# ============================================================
# INTERFACE
# ============================================================
# A troca de competição na tela principal é tratada por callback.
# Isso altera apenas o estado da interface: as rotinas antigas de coleta,
# médias e probabilidades permanecem intactas. O callback roda antes do
# rerun completo do Streamlit, então a nova competição já é usada ao
# carregar equipes e jogos do dia.
def _on_main_competition_change():
    _new_comp = st.session_state.get("main_league_widget")
    if _new_comp not in COMPETITIONS:
        return
    st.session_state.selected_competition = _new_comp
    st.session_state.league_widget = _new_comp
    st.session_state.selected_home = None
    st.session_state.selected_away = None
    st.session_state.loaded_home = None
    st.session_state.loaded_away = None
    st.session_state.loaded_competition = _new_comp
    st.session_state.pop("home_widget", None)
    st.session_state.pop("away_widget", None)
    st.session_state.pop("_synced_loaded_signature", None)
    # Ao trocar a competição, a lista de jogos da nova competição volta
    # a aparecer na tela principal.
    st.session_state.pop("_main_games_hidden_competition", None)

if "_goto_comp" in st.session_state:
    st.session_state.selected_competition = st.session_state.pop("_goto_comp")
    st.session_state.selected_home = st.session_state.pop("_goto_home", None)
    st.session_state.selected_away = st.session_state.pop("_goto_away", None)
    st.session_state.loaded_home = st.session_state.selected_home
    st.session_state.loaded_away = st.session_state.selected_away
    st.session_state.loaded_competition = st.session_state.selected_competition
    st.session_state.league_widget = st.session_state.selected_competition
    st.session_state.main_league_widget = st.session_state.selected_competition
    # V84: uma partida escolhida na aba Jogos já é um confronto carregado.
    # Oculta a agenda interna da Home para entrar diretamente na análise, usando
    # exatamente o mesmo motor estatístico da seleção normal.
    if st.session_state.get("loaded_home") and st.session_state.get("loaded_away"):
        st.session_state["_main_games_hidden_competition"] = st.session_state.selected_competition
        st.session_state["_synced_loaded_signature"] = (
            f"{st.session_state.selected_competition}|{st.session_state.loaded_home}|{st.session_state.loaded_away}"
        )

    # V81: quando a análise veio da agenda geral de Jogos, preserva também a data.
    # Isso mantém o contexto completo Data → Competição → Partida e permite voltar
    # exatamente para o mesmo dia sem uma nova navegação manual.
    _goto_date = st.session_state.pop("_goto_date", None)
    if _goto_date is not None:
        try:
            if isinstance(_goto_date, str):
                _goto_date = datetime.fromisoformat(_goto_date).date()
            _goto_comp_key = clean_col(str(st.session_state.selected_competition))
            st.session_state[f"main_fixture_date_{_goto_comp_key}"] = _goto_date
            st.session_state[f"main_match_choice_{_goto_comp_key}"] = "📅 Jogos da data"
        except Exception:
            pass

if "selected_competition" not in st.session_state:
    st.session_state.selected_competition = list(COMPETITIONS.keys())[0]
if "selected_home" not in st.session_state:
    st.session_state.selected_home = None
if "selected_away" not in st.session_state:
    st.session_state.selected_away = None

# Datas da agenda usadas pela tela principal.
# Mantidas fora da sidebar para que a navegação lateral possa ficar exclusiva
# para conta/VIP/Admin sem remover dependências da agenda de jogos.
_brasilia_today = datetime.now(BRASILIA_TZ).date()
_date_options = [_brasilia_today + timedelta(days=i) for i in range(7)]

def _agenda_date_label(d):
    if d == _brasilia_today:
        return f"Hoje · {d:%d/%m}"
    weekdays = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
    return f"{weekdays[d.weekday()]} · {d:%d/%m}"
# A barra lateral autenticada fica dedicada exclusivamente à conta, VIP/renovação
# e, para administradores, ao acesso do painel administrativo.
# Os controles de análise permanecem na área principal.
league_name = st.session_state.selected_competition
config = COMPETITIONS[league_name]
used_year = current_season_year(config["season"])
period = int(st.session_state.get("analysis_period", 10))

def load_current_season():
    def roster_only_fallback(errors):
        roster = CURRENT_TEAM_ROSTERS.get(league_name)
        if not roster:
            return None
        rows = []
        for team in roster:
            row = {m: None for m in DISPLAY_METRICS}
            row["Time"] = team; row["Jogos"] = 0
            rows.append(row)
        out = pd.DataFrame(rows)
        out.attrs["updated_until"] = None
        out.attrs["matches"] = []
        out.attrs["season_source"] = "official_roster_only"
        out.attrs["load_warnings"] = list(errors)
        return out
    if config["kind"] == "football_data":
        # Fonte principal: Football-Data, porque entrega gols + estatísticas de jogo
        # (escanteios, cartões, faltas, finalizações etc. quando disponíveis).
        # Se estiver fora do ar ou incompleta, usa tabela vigente como fallback,
        # sem recuar para temporada anterior e sem inventar métricas ausentes.
        errors = []
        try:
            games = load_football_data(config["code"], used_year)
            # Valida a lista de participantes no bruto, inclusive nos períodos curtos.
            ensure_team_coverage(games, league_name)
            out = averages_football_data(games, period)
            out.attrs["season_source"] = "football_data_detailed"
            return complete_current_roster(out, league_name)
        except Exception as exc:
            errors.append(str(exc))
        try:
            out = load_livescore_season(league_name, used_year)
            out.attrs["season_source"] = "current_standings_fallback"
            return complete_current_roster(out, league_name)
        except Exception as exc:
            errors.append(str(exc))
        fallback = roster_only_fallback(errors)
        if fallback is not None:
            return fallback
        raise RuntimeError("fontes da temporada atual indisponíveis: " + " | ".join(errors[-2:]))

    errors = []
    if config["kind"] == "hybrid_extra":
        # 1) Football-Data quando a liga está coberta; 2) OpenFootball quando o
        # arquivo da temporada realmente existe; 3) standings/resultados atuais
        # da competição. Nunca recua silenciosamente para o ano anterior.
        try:
            games = load_extra_football_data(config["extra_code"], used_year)
            # Rejeita CSV parcial antes de transformá-lo em médias.
            ensure_team_coverage(games, league_name)
            out = averages_football_data(games, period)
            return complete_current_roster(out, league_name)
        except Exception as exc:
            errors.append(str(exc))
        try:
            out = load_open_results(config["id"], used_year, config["season"])
            ensure_team_coverage(out, league_name)
            return complete_current_roster(out, league_name)
        except Exception as exc:
            errors.append(str(exc))
        try:
            return complete_current_roster(load_livescore_season(league_name, used_year), league_name)
        except Exception as exc:
            errors.append(str(exc))
        fallback = roster_only_fallback(errors)
        if fallback is not None:
            return fallback
        raise RuntimeError("fontes da temporada atual indisponíveis: " + " | ".join(errors[-3:]))

    if config["kind"] == "brasileirao_stats":
        return load_brasileirao_dataset(used_year)

    # Competições que antes dependiam apenas de caminhos OpenFootball inexistentes
    # em 2026 agora têm fallback para a própria página da temporada atual.
    try:
        out = load_open_results(config["id"], used_year, config["season"])
        ensure_team_coverage(out, league_name)
        return complete_current_roster(out, league_name)
    except Exception as exc:
        errors.append(str(exc))
    try:
        return complete_current_roster(load_livescore_season(league_name, used_year), league_name)
    except Exception as exc:
        errors.append(str(exc))
    fallback = roster_only_fallback(errors)
    if fallback is not None:
        return fallback
    raise RuntimeError("fontes da temporada atual indisponíveis: " + " | ".join(errors[-2:]))



def resolve_team_name(candidate, teams):
    """Resolve o nome exibido na agenda para o nome existente na base de análise.

    v46: usa a mesma equivalência canônica adotada pela agenda/deduplicação.
    Isso impede que nomes oficiais e abreviados (ex.: Manchester United / Man Utd,
    Coventry City / Coventry) sejam exibidos corretamente mas falhem ao tocar em
    Analisar. A regra é global e vale para todas as competições suportadas.
    """
    if candidate in teams:
        return candidate
    if not candidate:
        return None
    c = clean_col(candidate)
    exact = {clean_col(t): t for t in teams}
    if c in exact:
        return exact[c]

    # Primeiro reaproveita a equivalência robusta usada na própria agenda.
    # É propositalmente executada antes do matching por similaridade para evitar
    # associações fracas entre clubes diferentes da mesma cidade.
    if "_fixture_names_equivalent" in globals():
        equivalent = [t for t in teams if _fixture_names_equivalent(candidate, t)]
        if len(equivalent) == 1:
            return equivalent[0]
        if len(equivalent) > 1:
            # Em caso de mais de uma possibilidade, escolhe a forma com maior
            # sobreposição lexical; nunca escolhe apenas pelo primeiro resultado.
            ck = set(_fixture_compare_tokens(candidate))
            ranked = []
            for t in equivalent:
                tk = set(_fixture_compare_tokens(t))
                union = ck | tk
                score = (len(ck & tk) / len(union)) if union else 0.0
                ranked.append((score, t))
            ranked.sort(key=lambda x: x[0], reverse=True)
            if ranked and (len(ranked) == 1 or ranked[0][0] > ranked[1][0]):
                return ranked[0][1]

    # V149: normaliza abreviações frequentes de provedores antes da similaridade.
    # Ex.: "Nottm Forest" <-> "Nottingham Forest". A regra continua exigindo
    # correspondência lexical forte e nunca escolhe entre dois candidatos empatados.
    token_aliases = {
        "nottm": "nottingham", "notts": "nottingham",
        "utd": "united", "man": "manchester",
        "weds": "wednesday", "intl": "internacional",
    }
    def _tokens(value):
        raw = [x for x in clean_col(value).split("_") if x]
        noise = {"fc","cf","ac","sc","ec","club","de","da","do"}
        return {token_aliases.get(x, x) for x in raw if x not in noise}

    c_tokens = _tokens(candidate)
    ranked = []
    for t in teams:
        tt = _tokens(t)
        if not c_tokens or not tt:
            continue
        inter = len(c_tokens & tt)
        union = len(c_tokens | tt)
        jaccard = (inter / union) if union else 0.0
        coverage = inter / min(len(c_tokens), len(tt))
        score = max(jaccard, coverage * 0.92)
        ranked.append((score, t))
    ranked.sort(key=lambda x: x[0], reverse=True)
    if not ranked or ranked[0][0] < 0.60:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.08 and ranked[0][0] < 0.96:
        return None
    return ranked[0][1]


def gm_share_market_sections(team_a, team_b, probs, expectations, a, b, df, sample_games=0):
    """Espelha no compartilhamento todos os dados disponíveis dos Mercados Essenciais."""
    ex = expectations or {}
    sections = []

    def add(title, rows):
        clean = [[str(k), str(v)] for k, v in (rows or []) if v not in (None, "")]
        if clean:
            sections.append({"title": title, "rows": clean})

    def proj_lines(prefix, item, lines):
        rows = []
        if item:
            rows.append(("Projeção GM", f"{float(item['total']):.2f}".replace('.', ',')))
            for line, pct in _gm_pct_lines(item['total'], lines):
                rows.append((f"Mais de {str(line).replace('.', ',')}", f"{pct:.0f}%"))
        add(prefix, rows)

    # Resultado + odds justas + dupla chance.
    if probs:
        d = _gm_display_1x2_percentages(probs)
        add("🏆 Resultado", [
            (f"Vitória {team_a}", f"{d['home']}% · odd justa {100/max(float(probs['home']),0.5):.2f}"),
            ("Empate", f"{d['draw']}% · odd justa {100/max(float(probs['draw']),0.5):.2f}"),
            (f"Vitória {team_b}", f"{d['away']}% · odd justa {100/max(float(probs['away']),0.5):.2f}"),
            ("Dupla chance 1X", f"{float(probs['home'])+float(probs['draw']):.0f}%"),
            ("Dupla chance X2", f"{float(probs['draw'])+float(probs['away']):.0f}%"),
            ("Dupla chance 12", f"{float(probs['home'])+float(probs['away']):.0f}%"),
        ])

    # Gols.
    g = ex.get("Gols")
    if g:
        rows=[("Projeção GM", f"{g['total']:.2f}".replace('.', ','))]
        rows += [(f"Mais de {str(line).replace('.', ',')}", f"{pct:.0f}%") for line,pct in _gm_pct_lines(g['total'], [0.5,1.5,2.5,3.5,4.5])]
        add("⚽ Gols na partida", rows)
        add("👥 Gols por equipe", [(team_a, f"{g['home']:.2f}".replace('.', ',')), (team_b, f"{g['away']:.2f}".replace('.', ','))])
        btts=(1-math.exp(-max(g['home'],0.01)))*(1-math.exp(-max(g['away'],0.01)))*100
        add("🤝 Ambas marcam", [("Sim", f"{btts:.0f}%"), ("Não", f"{100-btts:.0f}%")])

        matches=list(df.attrs.get("matches", []) or []) if isinstance(df, pd.DataFrame) else []
        goal_split=None
        g1a,g1b=metric_value(a,"Gols 1T"),metric_value(b,"Gols 1T")
        g2a,g2b=metric_value(a,"Gols 2T"),metric_value(b,"Gols 2T")
        half_n=_gm_pair_metric_sample(a,b,["Gols 1T","Gols 2T"],0)
        if all(v is not None for v in (g1a,g1b,g2a,g2b)) and half_n>=3:
            first=max(float(g1a)+float(g1b),0.0); second=max(float(g2a)+float(g2b),0.0)
            if first+second>0:
                share=max(.20,min(.65,first/(first+second)))
                goal_split={"first":float(g['total'])*share,"second":float(g['total'])*(1-share)}
        if goal_split is None:
            goal_split=expected_goals_by_half(team_a,team_b,matches,g['total'])
        if goal_split:
            for title,key in (("⏱️ Gols — 1º tempo","first"),("⏱️ Gols — 2º tempo","second")):
                val=goal_split[key]
                rows=[("Projeção GM",f"{val:.2f}".replace('.',','))]
                rows += [(f"Mais de {str(line).replace('.', ',')}",f"{pct:.0f}%") for line,pct in _gm_pct_lines(val,[0.5,1.5,2.5])]
                add(title,rows)

    # Escanteios.
    c=ex.get("Escanteios")
    if c:
        rows=[("Projeção GM",f"{c['total']:.2f}".replace('.',','))]
        rows += [(f"Mais de {str(line).replace('.', ',')}",f"{pct:.0f}%") for line,pct in _gm_pct_lines(c['total'],[6.5,7.5,8.5,9.5,10.5])]
        add("⛳ Escanteios na partida",rows)
        for title,metric,lines in (("⏱️ Escanteios — 1º tempo","Escanteios 1T",[2.5,3.5,4.5,5.5,6.5]),("⏱️ Escanteios — 2º tempo","Escanteios 2T",[2.5,3.5,4.5,5.5,6.5])):
            va,vb=metric_value(a,metric),metric_value(b,metric)
            if va is not None and vb is not None:
                total=va+vb; rr=[("Projeção GM",f"{total:.2f}".replace('.',','))]
                rr += [(f"Mais de {str(line).replace('.', ',')}",f"{pct:.0f}%") for line,pct in _gm_pct_lines(total,lines)]
                add(title,rr)
        rr=[]
        for team,val in ((team_a,c['home']),(team_b,c['away'])):
            rr.append((team,f"{val:.2f}".replace('.',',')))
            rr += [(f"{team} +{str(line).replace('.', ',')}",f"{pct:.0f}%") for line,pct in _gm_pct_lines(val,[2.5,3.5,4.5,5.5])]
        add("👥 Escanteios por equipe",rr)

    # Cartões.
    c=ex.get("Cartões")
    if c:
        rows=[("Projeção GM",f"{c['total']:.2f}".replace('.',','))]
        rows += [(f"Mais de {str(line).replace('.', ',')}",f"{pct:.0f}%") for line,pct in _gm_pct_lines(c['total'],[1.5,2.5,3.5,4.5,5.5])]
        add("🟨 Cartões totais",rows)
        rr=[]
        for team,val in ((team_a,c['home']),(team_b,c['away'])):
            rr.append((team,f"{val:.2f}".replace('.',',')))
            rr += [(f"{team} +{str(line).replace('.', ',')}",f"{pct:.0f}%") for line,pct in _gm_pct_lines(val,[0.5,1.5,2.5,3.5])]
        add("👥 Cartões por equipe",rr)
        both1=(prob_over_half_line(c['home'],0.5) or 0)*(prob_over_half_line(c['away'],0.5) or 0)*100
        both2=(prob_over_half_line(c['home'],1.5) or 0)*(prob_over_half_line(c['away'],1.5) or 0)*100
        add("🟨 Cartões — ambas equipes",[("Ambas 1+",f"{both1:.0f}%"),("Ambas 2+",f"{both2:.0f}%")])

    # Finalizações.
    s=ex.get("Finalizações")
    if s:
        add("🎯 Finalizações",[("Total da partida",f"{s['total']:.2f}".replace('.',','))])
        if s.get("individual_reliable",False):
            add("👥 Finalizações por equipe",[(team_a,f"{s['home']:.2f}".replace('.',',')),(team_b,f"{s['away']:.2f}".replace('.',','))])
    t=ex.get("Chutes no alvo")
    if t:
        add("🥅 Finalizações no alvo",[("Total da partida",f"{t['total']:.2f}".replace('.',','))])
        if t.get("individual_reliable",False):
            add("👥 No alvo por equipe",[(team_a,f"{t['home']:.2f}".replace('.',',')),(team_b,f"{t['away']:.2f}".replace('.',','))])

    # Outros dados.
    for title,metric,suffix in (("⚪ Posse de bola","Posse (%)","%"),("🚫 Faltas por equipe","Faltas",""),("🚩 Impedimentos por equipe","Impedimentos","")):
        va,vb=metric_value(a,metric),metric_value(b,metric)
        if va is not None and vb is not None:
            if suffix:
                rows=[(team_a,f"{va:.1f}{suffix}"),(team_b,f"{vb:.1f}{suffix}")]
            else:
                rows=[(team_a,f"{va:.2f}".replace('.',',')),(team_b,f"{vb:.2f}".replace('.',','))]
            add(title,rows)
    return sections


def _gm_share_highlights(team_a, team_b, probs, opportunities, expectations):
    """V93: resumo visual ampliado, sem alterar probabilidades do motor. Somente 70%–95%."""
    items = []

    def add(label, chance, base=""):
        try:
            pct = float(chance)
        except Exception:
            return
        if 70.0 <= pct <= 95.0:
            items.append({"label": str(label), "chance": pct, "base": str(base or "")})

    # Oportunidades já aprovadas pelo próprio motor/evidência.
    for x in (opportunities or []):
        add(x.get("Mercado"), x.get("Chance"), x.get("Base"))

    # Resultado e dupla chance usam as probabilidades finais já calculadas.
    if probs:
        ph, pd, pa = float(probs.get("home", 0)), float(probs.get("draw", 0)), float(probs.get("away", 0))
        add(f"Vitória {team_a}", ph, "Resultado")
        add("Empate", pd, "Resultado")
        add(f"Vitória {team_b}", pa, "Resultado")
        add(f"{team_a} ou empate (1X)", ph + pd, "Dupla chance")
        add(f"Empate ou {team_b} (X2)", pd + pa, "Dupla chance")
        add("Sem empate (12)", ph + pa, "Dupla chance")

    # Linhas derivadas das mesmas projeções exibidas no painel.
    ex = expectations or {}
    for key, emoji, lines in (
        ("Gols", "⚽", [0.5, 1.5, 2.5, 3.5, 4.5]),
        ("Escanteios", "⛳", [6.5, 7.5, 8.5, 9.5, 10.5]),
        ("Cartões", "🟨", [2.5, 3.5, 4.5, 5.5, 6.5]),
    ):
        item = ex.get(key)
        if not item:
            continue
        for line, pct in _gm_pct_lines(float(item.get("total") or 0), lines):
            add(f"{emoji} Mais de {str(line).replace('.', ',')} {key.lower()}", pct, key)

    # Remove duplicatas textuais, mantém a maior chance e limita a cinco destaques.
    unique = {}
    for item in items:
        k = _gm_api_norm(item["label"])
        if not k:
            continue
        if k not in unique or item["chance"] > unique[k]["chance"]:
            unique[k] = item
    return sorted(unique.values(), key=lambda x: (-x["chance"], x["label"]))[:8]


def render_share_button(team_a, team_b, league_name, probs, opportunities, expectations, a, b, df=None, sample_games=0):
    """V93: arte com brasões oficiais do confronto e até 8 probabilidades entre 70% e 95%."""
    import json as _json

    highlights = _gm_share_highlights(team_a, team_b, probs, opportunities, expectations)
    # V94: reutiliza exatamente os mesmos brasões resolvidos para o cabeçalho da
    # partida e os incorpora no payload da arte. O canvas do navegador não
    # depende mais de permissão CORS do servidor externo dos escudos.
    home_visual = gm_team_visual(team_a, league_name, gm_current_match_team_id(team_a, "home"))
    away_visual = gm_team_visual(team_b, league_name, gm_current_match_team_id(team_b, "away"))
    home_logo = gm_badge_data_uri(home_visual.get("badge"))
    away_logo = gm_badge_data_uri(away_visual.get("badge"))
    league_visual = gm_league_visual(league_name)
    league_logo = gm_badge_data_uri(league_visual.get("logo"))
    match_datetime = gm_current_match_datetime()
    data = _json.dumps({
        "title": f"{team_a} × {team_b}",
        "league": str(league_name),
        "home": team_a,
        "away": team_b,
        "home_logo": home_logo,
        "away_logo": away_logo,
        "league_logo": league_logo,
        "match_datetime": match_datetime,
        "highlights": highlights,
    }, ensure_ascii=False)

    st.caption("A imagem compartilhada resume até 8 destaques com probabilidade estimada entre 70% e 95%.")
    html = f"""
    <div style='font-family:Inter,Arial,sans-serif'>
      <button id='shareBtn' style='width:100%;padding:12px 16px;border:1px solid #1fe387;border-radius:12px;background:linear-gradient(90deg,#08783f,#10b865);color:white;font-size:16px;font-weight:800;cursor:pointer'>📲 Compartilhar resumo GM SCORE</button>
      <div id='msg' style='font-size:12px;color:#94a3b8;margin-top:6px'></div>
    </div>
    <script>
    const D = {data};
    const C={{bg:'#07100f',panel:'#0b1716',border:'#176e4b',green:'#24e58b',text:'#f8fafc',muted:'#a8b4c2',soft:'#263845'}};
    function rr(c,x,y,w,h,r,fill,stroke=null,lw=1){{const q=Math.min(r,w/2,h/2);c.beginPath();c.moveTo(x+q,y);c.arcTo(x+w,y,x+w,y+h,q);c.arcTo(x+w,y+h,x,y+h,q);c.arcTo(x,y+h,x,y,q);c.arcTo(x,y,x+w,y,q);c.closePath();if(fill){{c.fillStyle=fill;c.fill();}}if(stroke){{c.lineWidth=lw;c.strokeStyle=stroke;c.stroke();}}}}
    function tx(c,v,x,y,size=28,weight='400',color=C.text,align='left'){{c.save();c.fillStyle=color;c.font=`${{weight}} ${{size}}px Arial`;c.textAlign=align;c.textBaseline='alphabetic';c.fillText(String(v),x,y);c.restore();}}
    function wrap(c,v,x,y,maxW,lineH,size=22,weight='700',color=C.text){{c.save();c.fillStyle=color;c.font=`${{weight}} ${{size}}px Arial`;let line='',yy=y;for(const w of String(v).split(' ')){{const t=line+w+' ';if(c.measureText(t).width>maxW&&line){{c.fillText(line.trim(),x,yy);yy+=lineH;line=w+' ';}}else line=t;}}if(line)c.fillText(line.trim(),x,yy);c.restore();return yy;}}
    function loadImg(src){{return new Promise(resolve=>{{if(!src)return resolve(null);const i=new Image();if(/^https?:/i.test(src))i.crossOrigin='anonymous';i.onload=()=>resolve(i);i.onerror=()=>resolve(null);i.src=src;}});}}
    document.getElementById('shareBtn').onclick=async()=>{{
      const W=1080,H=1180+Math.max(0,D.highlights.length-3)*128,scale=2;
      const canvas=document.createElement('canvas');canvas.width=W*scale;canvas.height=H*scale;const c=canvas.getContext('2d');c.scale(scale,scale);c.fillStyle=C.bg;c.fillRect(0,0,W,H);
      tx(c,'GM',70,82,50,'900','#fff');tx(c,'SCORE',165,82,50,'900',C.green);tx(c,'ANÁLISE • ESTATÍSTICAS • PROBABILIDADES',70,118,18,'700','#93e9bc');
      rr(c,60,160,960,260,22,C.panel,C.border,2);
      const [hi,ai,li]=await Promise.all([loadImg(D.home_logo),loadImg(D.away_logo),loadImg(D.league_logo)]);
      // V98: coluna central realmente independente dos clubes.
      // Logo, competição, × e data/hora compartilham exatamente o mesmo eixo X.
      const CX=540, LEAGUE_MAX_W=300;
      if(li)c.drawImage(li,CX-29,174,58,58);
      let leagueSize=20;
      c.save();c.font=`800 ${{leagueSize}}px Arial`;
      while(leagueSize>14 && c.measureText(String(D.league).toUpperCase()).width>LEAGUE_MAX_W){{leagueSize--;c.font=`800 ${{leagueSize}}px Arial`;}}
      c.restore();
      tx(c,String(D.league).toUpperCase(),CX,258,leagueSize,'800',C.muted,'center');
      tx(c,'×',CX,302,40,'900',C.green,'center');
      if(D.match_datetime)tx(c,D.match_datetime,CX,336,17,'700',C.muted,'center');
      if(hi)c.drawImage(hi,218,218,92,92);else tx(c,'⚽',264,285,58,'700',C.muted,'center');
      if(ai)c.drawImage(ai,770,218,92,92);else tx(c,'⚽',816,285,58,'700',C.muted,'center');
      tx(c,D.home,264,355,27,'800',C.text,'center');tx(c,D.away,816,355,27,'800',C.text,'center');
      let y=475;tx(c,'DESTAQUES DA ANÁLISE',70,y,29,'900',C.text);tx(c,'70%–95%',1010,y,22,'900',C.green,'right');y+=35;
      if(!D.highlights.length){{rr(c,60,y,960,150,18,C.panel,C.border,1);tx(c,'Nenhum mercado ficou na faixa de 70% a 95%.',540,y+72,24,'700',C.muted,'center');tx(c,'A análise completa continua disponível no GM SCORE.',540,y+108,18,'500',C.muted,'center');y+=180;}}
      else{{D.highlights.forEach((r,idx)=>{{rr(c,60,y,960,112,18,C.panel,C.border,1);wrap(c,r.label,88,y+42,690,28,23,'800',C.text);if(r.base)tx(c,r.base,88,y+84,16,'600',C.muted);tx(c,`${{Math.round(r.chance)}}%`,980,y+66,34,'900',C.green,'right');y+=128;}});}}
      tx(c,'Probabilidades estatísticas. Não representam garantia de resultado.',70,H-78,17,'500',C.muted);tx(c,'GM SCORE',1010,H-78,22,'900',C.green,'right');
      canvas.toBlob(async blob=>{{const safe=(D.home+'-x-'+D.away).replace(/[^a-z0-9áàãâéêíóôõúç_-]+/gi,'-').replace(/-+/g,'-');const file=new File([blob],`GM-SCORE-${{safe}}.jpg`,{{type:'image/jpeg'}});try{{if(navigator.share&&(!navigator.canShare||navigator.canShare({{files:[file]}}))){{await navigator.share({{title:`GM SCORE — ${{D.title}}`,text:`GM SCORE — ${{D.league}} — ${{D.title}}`,files:[file]}});document.getElementById('msg').innerText='Resumo criado. Escolha onde compartilhar.';}}else{{const dl=document.createElement('a');dl.href=URL.createObjectURL(blob);dl.download=file.name;dl.click();document.getElementById('msg').innerText='Resumo criado e salvo.';}}}}catch(e){{document.getElementById('msg').innerText='Compartilhamento cancelado.';}}}},'image/jpeg',.94);
    }};
    </script>
    """
    components.html(html, height=74)


def gm_resolve_direct_fixture_team(payload, side, teams):
    """Resolve a equipe clicada na agenda sem adivinhar outro clube.

    V92: ID oficial da APIfootball é a primeira referência. O nome exibido na
    agenda fica como fallback através do resolvedor já usado pelo GM SCORE.
    Esta função só escolhe a linha/equipe correta; não participa dos cálculos.
    """
    payload = payload or {}
    raw_name = str(payload.get(side) or "").strip()
    team_id = str(payload.get(f"{side}_team_id") or "").strip()
    competition = str(payload.get("competition") or "").strip()

    # 1) ID oficial -> nome oficial da liga -> nome existente na base.
    if team_id and competition:
        try:
            official = gm_team_visual(raw_name, competition, team_id)
            official_name = str((official or {}).get("name") or "").strip()
            if official_name:
                resolved = resolve_team_name(official_name, teams)
                if resolved:
                    return resolved
        except Exception:
            pass

    # 2) Nome transportado pela própria partida.
    resolved = resolve_team_name(raw_name, teams)
    if resolved:
        return resolved

    # 3) Aliases auditados da APIfootball, ainda restritos à mesma equipe.
    try:
        variants = [raw_name]
        variants.extend(GM_APIFOOTBALL_TEAM_ALIASES.get(raw_name, []) or [])
        raw_norm = _gm_api_norm(raw_name)
        for canonical, aliases in (GM_APIFOOTBALL_TEAM_ALIASES or {}).items():
            group = [str(canonical)] + [str(x) for x in (aliases or [])]
            if raw_norm and raw_norm in {_gm_api_norm(x) for x in group}:
                variants.extend(group)
        found = []
        for variant in variants:
            r = resolve_team_name(variant, teams)
            if r and r not in found:
                found.append(r)
        if len(found) == 1:
            return found[0]
    except Exception:
        pass
    return None


_GM_DIRECT_PROFILE_CACHE = {}
_GM_DIRECT_PROFILE_CACHE_TTL = 1800

def gm_hydrate_direct_fixture_rows(df, payload):
    """Completa somente linhas ausentes da partida aberta pela agenda.

    Usa a mesma fonte APIfootball que o motor já consulta em todas as análises.
    Não muda fórmulas nem substitui linhas existentes; apenas evita que um clube
    oficial futuro, ainda ausente na tabela-base, torne o caminho Jogos inútil.
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return df
    payload = payload or {}
    competition = str(payload.get("competition") or "").strip()
    home = str(payload.get("home") or "").strip()
    away = str(payload.get("away") or "").strip()
    if not competition or not home or not away or "Time" not in df.columns:
        return df
    teams = df["Time"].dropna().astype(str).tolist()
    if gm_resolve_direct_fixture_team(payload, "home", teams) and gm_resolve_direct_fixture_team(payload, "away", teams):
        return df
    _cache_key = (competition, home, away)
    _cached = _GM_DIRECT_PROFILE_CACHE.get(_cache_key)
    if _cached and (time.time() - float(_cached.get("at") or 0)) < _GM_DIRECT_PROFILE_CACHE_TTL:
        profiles = _cached.get("profiles")
    else:
        try:
            profiles = gm_apifootball_pair_profiles(home, away, competition_name=competition, limit_per_team=20, lookback_days=520)
            _GM_DIRECT_PROFILE_CACHE[_cache_key] = {"at": time.time(), "profiles": profiles}
        except Exception:
            return df
    if not isinstance(profiles, dict):
        return df
    out = df.copy()
    attrs = dict(getattr(df, "attrs", {}))
    for side, display_name in (("home", home), ("away", away)):
        current = out["Time"].dropna().astype(str).tolist()
        if gm_resolve_direct_fixture_team(payload, side, current):
            continue
        profile = profiles.get(display_name)
        row = (profile or {}).get("row") if isinstance(profile, dict) else None
        if not isinstance(row, dict):
            continue
        row = dict(row)
        row["Time"] = display_name
        # Alinha apenas colunas já conhecidas pela base; metadados _n_* usados
        # pelo motor são preservados quando presentes no perfil recuperado.
        for col in out.columns:
            row.setdefault(col, None)
        out = pd.concat([out, pd.DataFrame([row])], ignore_index=True, sort=False)
    out.attrs.update(attrs)
    return out


def render_analysis():
    global period
    # V91: antes de qualquer validação/carregamento, reidrata atomicamente o
    # confronto vindo da aba Jogos. Assim reruns do Streamlit ou query params não
    # conseguem deixar a tela dedicada sem as equipes selecionadas.
    _pre_direct = st.session_state.get("gm_games_direct_match") or {}
    if st.session_state.get("gm_analysis_origin") == "games" and _pre_direct:
        _pc = str(_pre_direct.get("competition") or "")
        _ph = str(_pre_direct.get("home") or "")
        _pa = str(_pre_direct.get("away") or "")
        if _pc in COMPETITIONS and _ph and _pa:
            st.session_state.selected_competition = _pc
            st.session_state.selected_home = _ph
            st.session_state.selected_away = _pa
            st.session_state.loaded_home = _ph
            st.session_state.loaded_away = _pa
            st.session_state.loaded_competition = _pc
            st.session_state.league_widget = _pc
            st.session_state.main_league_widget = _pc
            st.session_state["_main_games_hidden_competition"] = _pc

    # V89: quando o confronto veio da aba Jogos, a análise vira uma tela dedicada.
    # O período continua sendo exatamente o já escolhido pelo usuário (10 por padrão),
    # mas os controles da Home não são renderizados neste fluxo.
    _games_direct_view = st.session_state.get("gm_analysis_origin") == "games"
    if _games_direct_view and _pre_direct:
        # Atualiza também os globais consumidos pelas funções legadas do motor.
        # Isso não muda cálculo; apenas garante que elas apontem para a competição
        # do confronto clicado neste mesmo ciclo de execução.
        global league_name, config, used_year
        _effective_comp = str(_pre_direct.get("competition") or "")
        if _effective_comp in COMPETITIONS:
            league_name = _effective_comp
            config = COMPETITIONS[league_name]
            used_year = current_season_year(config["season"])
    if _games_direct_view:
        try:
            period = int(st.session_state.get("analysis_period", 10))
        except Exception:
            period = 10
        if period not in [5, 10, 20, 0]:
            period = 10
    else:
        period = st.selectbox(
            "📊 Período da análise", [5, 10, 20, 0],
            index=[5, 10, 20, 0].index(int(st.session_state.get("analysis_period", 10))) if int(st.session_state.get("analysis_period", 10)) in [5, 10, 20, 0] else 1,
            format_func=lambda n: "Temporada" if n == 0 else f"Últimos {n} jogos",
            key="analysis_period",
        )
    # Não reaproveita uma partida carregada de outra competição.
    if st.session_state.get("loaded_competition") != league_name:
        st.session_state.loaded_home = None
        st.session_state.loaded_away = None
        st.session_state.loaded_competition = league_name

    with st.spinner("Carregando a temporada atual..."):
        try:
            df = validate_current_data(load_current_season(), config["season"], league_name)
        except Exception as exc:
            st.error(f"Não foi possível carregar dados confiáveis da temporada atual ({season_label(used_year, config['season'])}).")
            st.caption(f"Detalhe: {exc}")
            st.info("Esta competição não usa temporada antiga como substituta.")
            return

    # V92: se a agenda abriu um clube oficial que ainda não existe na tabela
    # principal da competição, completa somente essa linha pela APIfootball — a
    # mesma fonte estatística já usada pelo motor. Fórmulas permanecem intactas.
    if _games_direct_view and _pre_direct:
        df = gm_hydrate_direct_fixture_rows(df, _pre_direct)

    updated_until = df.attrs.get("updated_until")
    teams = df["Time"].dropna().tolist()
    if len(teams) < 2:
        st.warning("Ainda não há equipes suficientes para análise.")
        return

    resolved_home = resolve_team_name(st.session_state.selected_home, teams)
    resolved_away = resolve_team_name(st.session_state.selected_away, teams)
    loaded_home_now = resolve_team_name(st.session_state.get("loaded_home"), teams)
    loaded_away_now = resolve_team_name(st.session_state.get("loaded_away"), teams)
    default_home = loaded_home_now or resolved_home or teams[0]
    default_away = loaded_away_now or resolved_away or (teams[1] if len(teams) > 1 else teams[0])

    # A competição também pode ser alterada diretamente na tela principal.
    # O valor é sincronizado com a barra lateral; ao trocar de campeonato,
    # limpamos o confronto anterior antes de carregar as equipes da nova liga.
    if st.session_state.get("main_league_widget") != league_name:
        st.session_state["main_league_widget"] = league_name

    if _games_direct_view:
        main_league_name = league_name
    else:
        main_league_name = st.selectbox(
            "🏆 Competição",
            list(COMPETITIONS.keys()),
            key="main_league_widget",
            format_func=competition_display_name,
            on_change=_on_main_competition_change,
        )

    # O confronto pode ser escolhido de duas formas logo após a competição:
    # 1) pelos jogos oficiais da data; 2) manualmente entre as equipes da liga.
    # No celular usamos duas opções horizontais, em vez de duas colunas fixas,
    # para preservar a legibilidade dos nomes e dos horários.
    main_games_hidden = st.session_state.get("_main_games_hidden_competition") == league_name

    if not main_games_hidden:
        st.markdown("### ⚽ Escolha o confronto")
        choice_key = f"main_match_choice_{clean_col(league_name)}"
        choice_mode = st.radio(
            "Forma de seleção",
            ["📅 Jogos da data", "🎯 Selecionar equipes"],
            horizontal=True,
            key=choice_key,
            label_visibility="collapsed",
        )

        if choice_mode == "📅 Jogos da data":
            main_fixture_date = st.selectbox(
                "📅 Data dos jogos",
                _date_options,
                key=f"main_fixture_date_{clean_col(league_name)}",
                format_func=_agenda_date_label,
            )
            # V102: a agenda da Home é deliberadamente filtrada pela competição
            # selecionada acima. Deixa isso explícito e oferece acesso imediato à
            # agenda global para evitar que jogos de OUTRA competição pareçam
            # ausentes (ex.: Sul-Americana enquanto Libertadores está selecionada).
            st.caption(
                f"🕒 Horário de Brasília · exibindo somente **{competition_display_name(league_name)}** "
                "nesta lista · toque em **Analisar** para carregar o confronto"
            )
            if st.button(
                "🌐 Ver todos os jogos desta data",
                use_container_width=True,
                key=f"main_open_all_games_{main_fixture_date}",
            ):
                st.session_state["gm_games_page_date"] = main_fixture_date
                st.session_state["gm_main_view"] = "games"
                try:
                    st.query_params["gm_view"] = "games"
                except Exception:
                    pass
                st.rerun()

            # v43: a agenda NÃO depende mais do elenco estatístico carregado.
            # O calendário e a base de estatísticas têm ciclos de atualização diferentes;
            # usar o roster como barreira fazia jogos oficiais desaparecerem quando um
            # promovido/renomeado ainda não estava resolvido na base estatística.
            # A validação de profissional masculino continua em valid_daily_fixture().
            if st.button("🔄 Atualizar agenda", use_container_width=True, key=f"refresh_fixture_{clean_col(league_name)}_{main_fixture_date}"):
                try:
                    load_competition_fixtures_for_date.clear()
                except Exception:
                    pass
                for _fn in (
                    load_apifootball_prediction_fixtures_for_date,
                    load_apifootball_competition_fixtures_for_date,
                    load_apifootball_all_competitions_fixtures_for_date,
                    load_apifootball_fixtures_for_date,
                    load_sofascore_fixtures_for_date,
                    load_espn_fixtures_for_date,
                    load_thesportsdb_fixtures_for_date,
                    load_fixtures_for_date,
                ):
                    try:
                        _fn.clear()
                    except Exception:
                        pass
                st.rerun()

            try:
                today_fixtures = load_competition_fixtures_for_date(league_name, main_fixture_date)
            except Exception:
                today_fixtures = []

            # Filtro final apenas de integridade/data/status; não exige presença do clube
            # na base estatística para que o jogo seja VISÍVEL na agenda.
            safe_fixtures = []
            for f in today_fixtures:
                if not valid_daily_fixture(f) or not fixture_matches_selected_date(f, main_fixture_date):
                    continue
                _merge_fixture_unique(safe_fixtures, dict(f))
            today_fixtures = sorted(safe_fixtures, key=lambda f: str(f.get("time") or "99:99"))

            # Diagnóstico compacto para administrador: mostra exatamente onde a agenda
            # está sendo perdida sem expor chaves ou segredos.
            try:
                _diag_profile = gm_auth_get_profile()
            except Exception:
                _diag_profile = None
            if (_diag_profile or {}).get("role") == "admin":
                with st.expander("🧪 Diagnóstico da agenda (admin)", expanded=False):
                    # V150: expander fechado ainda executa Python no Streamlit.
                    # As chamadas extras de rede só rodam sob solicitação explícita.
                    _run_agenda_diag = st.button(
                        "Executar diagnóstico da agenda",
                        key=f"gm_run_agenda_diag_{main_fixture_date}_{league_name}",
                    )
                    _diag_sources = []
                    _checks = [
                        ("APIfootball · predictions", lambda: load_apifootball_prediction_fixtures_for_date(main_fixture_date, league_name)),
                        ("APIfootball · liga direta", lambda: load_apifootball_competition_fixtures_for_date(league_name, main_fixture_date)),
                        ("APIfootball · global", lambda: [x for x in load_apifootball_fixtures_for_date(main_fixture_date) if x.get("competition") == league_name]),
                        ("SofaScore", lambda: [x for x in load_sofascore_fixtures_for_date(main_fixture_date) if x.get("competition") == league_name]),
                        ("ESPN", lambda: [x for x in load_espn_fixtures_for_date(main_fixture_date) if x.get("competition") == league_name]),
                        ("TheSportsDB", lambda: [x for x in load_thesportsdb_fixtures_for_date(main_fixture_date) if x.get("competition") == league_name]),
                    ]
                    for _label, _call in (_checks if _run_agenda_diag else []):
                        try:
                            _rows = _call() or []
                            _diag_sources.append((_label, _rows, None))
                        except Exception as _exc:
                            _diag_sources.append((_label, [], type(_exc).__name__))
                    for _label, _rows, _err in _diag_sources:
                        st.markdown(f"**{_label}: {len(_rows)} jogo(s)**" + (f" · erro: {_err}" if _err else ""))
                        for _r in _rows[:6]:
                            st.caption(f"{_r.get('time') or '—'} · {_r.get('home')} × {_r.get('away')} · status={_r.get('status') or '—'} · data={_r.get('br_date') or '—'}" + (f" · bruto={_r.get('prediction_time_raw')}" if _r.get('prediction_time_raw') else ""))
                    st.caption(f"Após filtros/deduplicação: {len(today_fixtures)} jogo(s).")

            if today_fixtures:
                for i, f in enumerate(today_fixtures):
                    game_home = f.get("home")
                    game_away = f.get("away")
                    time_text = str(f.get("time") or "").strip()
                    if time_text and time_text.lower() != "nan":
                        st.markdown(f"**⚽ {game_home} × {game_away}**  \n🕒 {time_text}")
                    else:
                        st.markdown(f"**⚽ {game_home} × {game_away}**")
                    if st.button(
                        "🔎 Analisar",
                        key=f"main_game_{main_fixture_date}_{i}_{clean_col(str(game_home))}_{clean_col(str(game_away))}",
                        use_container_width=True,
                    ):
                        # V149: a agenda já possui identidade estrutural do fixture.
                        # Primeiro usa IDs oficiais para obter o nome canônico; o
                        # resolvedor textual fica apenas como fallback para a base histórica.
                        home_identity = gm_fixture_official_team_identity(
                            game_home, league_name, (f or {}).get("home_team_id")
                        )
                        away_identity = gm_fixture_official_team_identity(
                            game_away, league_name, (f or {}).get("away_team_id")
                        )
                        home_candidate = (home_identity or {}).get("name") or game_home
                        away_candidate = (away_identity or {}).get("name") or game_away
                        resolved_game_home = resolve_team_name(home_candidate, teams) or resolve_team_name(game_home, teams)
                        resolved_game_away = resolve_team_name(away_candidate, teams) or resolve_team_name(game_away, teams)
                        # V150: fixture oficial entra no fluxo direto mesmo se uma
                        # equipe ainda não estiver na base histórica principal. A
                        # análise já possui hidratação real pela APIfootball.
                        _direct_home = resolved_game_home or str(home_candidate or game_home)
                        _direct_away = resolved_game_away or str(away_candidate or game_away)
                        st.session_state["gm_games_direct_match"] = {
                            "fixture_id": (f or {}).get("fixture_id") or (f or {}).get("match_id"),
                            "match_id": (f or {}).get("match_id") or (f or {}).get("fixture_id"),
                            "league_id": (f or {}).get("league_id"),
                            "competition": league_name,
                            "date": str(main_fixture_date),
                            "time": (f or {}).get("time"),
                            "home": str(game_home or _direct_home),
                            "away": str(game_away or _direct_away),
                            "home_team_id": (f or {}).get("home_team_id") or (home_identity or {}).get("id"),
                            "away_team_id": (f or {}).get("away_team_id") or (away_identity or {}).get("id"),
                        }
                        st.session_state["gm_analysis_origin"] = "games"
                        st.session_state.selected_home = _direct_home
                        st.session_state.selected_away = _direct_away
                        st.session_state.loaded_home = _direct_home
                        st.session_state.loaded_away = _direct_away
                        st.session_state.loaded_competition = league_name
                        st.session_state["_main_games_hidden_competition"] = league_name
                        st.session_state["home_widget"] = _direct_home
                        st.session_state["away_widget"] = _direct_away
                        st.session_state["_synced_loaded_signature"] = f"{league_name}|{_direct_home}|{_direct_away}"
                        st.rerun()
            else:
                st.info("Nenhum jogo profissional masculino encontrado para esta competição na data selecionada. Você ainda pode usar **🎯 Selecionar equipes**.")

        else:
            st.caption("Escolha qualquer confronto entre as equipes profissionais da competição, mesmo sem jogo marcado nesta data.")
            manual_c1, manual_c2 = st.columns(2)
            with manual_c1:
                manual_team_a = st.selectbox(
                    "🏠 Time da casa",
                    teams,
                    index=teams.index(default_home),
                    key="home_widget",
                )
            with manual_c2:
                manual_team_b = st.selectbox(
                    "✈️ Time visitante",
                    teams,
                    index=teams.index(default_away),
                    key="away_widget",
                )

            if manual_team_a == manual_team_b:
                st.warning("Selecione duas equipes diferentes.")
            elif st.button(
                "⚽ Carregar equipes",
                type="primary",
                use_container_width=True,
                key="load_teams_btn",
            ):
                st.session_state.selected_home = manual_team_a
                st.session_state.selected_away = manual_team_b
                st.session_state.loaded_home = manual_team_a
                st.session_state.loaded_away = manual_team_b
                st.session_state.loaded_competition = league_name
                st.session_state["_main_games_hidden_competition"] = league_name
                st.session_state["_synced_loaded_signature"] = f"{league_name}|{manual_team_a}|{manual_team_b}"
                st.rerun()

        st.markdown("---")

    # V90: no fluxo Jogos → Partida, resolve o confronto diretamente do payload
    # persistente criado no clique. Isso evita que reruns/query params apaguem a
    # seleção antes de o motor estatístico receber as equipes.
    if _games_direct_view:
        _direct = st.session_state.get("gm_games_direct_match") or {}
        _direct_comp = str(_direct.get("competition") or "")
        if _direct_comp == league_name:
            _dh = gm_resolve_direct_fixture_team(_direct, "home", teams)
            _da = gm_resolve_direct_fixture_team(_direct, "away", teams)
            if _dh and _da:
                st.session_state.selected_home = _dh
                st.session_state.selected_away = _da
                st.session_state.loaded_home = _dh
                st.session_state.loaded_away = _da
                st.session_state.loaded_competition = league_name
                st.session_state["_main_games_hidden_competition"] = league_name
                st.session_state["_synced_loaded_signature"] = f"{league_name}|{_dh}|{_da}"

    if _games_direct_view and _pre_direct:
        loaded_home = gm_resolve_direct_fixture_team(_pre_direct, "home", teams)
        loaded_away = gm_resolve_direct_fixture_team(_pre_direct, "away", teams)
    else:
        loaded_home = resolve_team_name(st.session_state.get("loaded_home"), teams)
        loaded_away = resolve_team_name(st.session_state.get("loaded_away"), teams)

    if loaded_home and loaded_away:
        st.caption(f"✅ Jogo carregado: {loaded_home} × {loaded_away}")
        if not _games_direct_view and st.button("↩️ Escolher outro confronto", use_container_width=True, key="choose_another_match"):
            st.session_state.loaded_home = None
            st.session_state.loaded_away = None
            st.session_state.selected_home = None
            st.session_state.selected_away = None
            st.session_state.pop("_main_games_hidden_competition", None)
            st.session_state.pop("_synced_loaded_signature", None)
            st.rerun()
    else:
        if _games_direct_view:
            st.error("Não foi possível associar esta partida à base atual da competição.")
            st.caption("Volte aos jogos e tente novamente. Nenhuma análise foi calculada com equipes incorretas.")
        else:
            st.info("Escolha um jogo da data ou selecione as equipes manualmente para gerar a análise.")
        return

    team_a, team_b = loaded_home, loaded_away
    if team_a == team_b:
        st.warning("Selecione duas equipes diferentes.")
        return

    raw_a = df[df["Time"] == team_a].iloc[0]
    raw_b = df[df["Time"] == team_b].iloc[0]
    # Em torneios com pouca amostra, complementa com o histórico individual
    # doméstico, força da liga, H2H de edições anteriores e baseline do torneio.
    a, b, analysis_context = contextual_analysis_rows(team_a, team_b, league_name, df, recent_games=10)
    season_text = season_label(used_year, config["season"])

    # O modelo da própria competição é prioritário quando já há amostra. Quando
    # ela ainda é curta ou vazia, usamos a base contextual entre competições.
    comp_sample = min(int(float(raw_a.get("Jogos", 0) or 0)), int(float(raw_b.get("Jogos", 0) or 0)))
    probs = victory_probabilities(team_a, team_b, df) if comp_sample >= 6 else None
    if probs is None and analysis_context:
        probs = contextual_victory_probabilities(team_a, team_b, a, b, analysis_context)
    if probs is None:
        probs = victory_probabilities(team_a, team_b, df)

    # Antes das odds, aplica uma camada independente de qualidade estrutural.
    # Isso corrige especialmente cruzamentos entre ligas: potencial ofensivo,
    # ClubElo/força global, nível doméstico e forma têm precedência sobre uma
    # amostra curta do torneio continental.
    if probs:
        quality_prior = global_quality_prior(team_a, team_b, df=df, ctx=analysis_context)
        probs = calibrate_with_global_quality(
            probs, quality_prior, sample=comp_sample,
            contextual=(league_name in CONTEXTUAL_COMPETITIONS),
        )

    # Mercado público entra apenas como calibrador, sem API key. Em torneios
    # com pouca amostra recebe peso moderado; em ligas maduras o modelo próprio
    # do GM SCORE permanece dominante.
    market_odds = fetch_public_market_odds(team_a, team_b, league_name)
    moneyline = market_odds if market_odds and not market_odds.get("error") else None
    if probs and moneyline:
        base_market_weight = 0.34 if comp_sample < 6 else 0.22
        market_weight = adaptive_market_weight(probs, moneyline, base_market_weight)
        probs = calibrate_result_with_market(probs, moneyline, market_weight)

    # v65: aprende gradualmente o ambiente real de resultado da própria competição.
    # A camada só é ativada após 30 jogos concluídos e nunca supera 8% do 1X2 final.
    # O guardrail estrutural abaixo continua sendo a última proteção contra inversões.
    if probs:
        probs = apply_competition_result_learning(probs, league_name, df)

    # Checagem final orientada por evidências independentes. Só atua quando pelo
    # menos dois sinais fortes concordam (ex.: H2H + nível da liga; Elo + mercado).
    # Isso evita que mando ou amostra curta invertam um favorito estrutural real.
    if probs:
        probs = apply_evidence_consensus_guardrail(
            probs, quality_prior if 'quality_prior' in locals() else None,
            ctx=analysis_context, moneyline=moneyline,
        )

    # O novo cabeçalho usa somente a partida realmente carregada e as mesmas
    # probabilidades finais que alimentam o restante do painel. Não cria dados.
    gm_render_match_hero(
        team_a, team_b, league_name, season_text, probs=probs,
        updated_until=updated_until, sample=comp_sample,
    )

    # Transparência da qualidade da base permanece imediatamente antes da análise.
    data_learning = render_data_intelligence_status(league_name, df)

    if probs:
        # O 1X2 principal e as odds justas já estão no cabeçalho da partida.
        # A dupla chance aparece de forma compacta dentro de Mercados Essenciais.
        eval_bits = ["força atual", "potencial ofensivo/defensivo", "forma recente", "nível da liga"]
        if analysis_context and analysis_context.get("h2h_games", 0):
            eval_bits.append("confronto direto histórico verificado")
        if moneyline:
            eval_bits.append("mercado público como validação externa")
        try:
            _reading_profile = gm_auth_get_profile()
        except Exception:
            _reading_profile = None
        if (_reading_profile or {}).get("role") == "admin":
            st.caption("📌 Leitura GM SCORE considera " + ", ".join(eval_bits) + ". O favoritismo é uma estimativa do cruzamento dos dados, não uma garantia de resultado.")

        # Quando existe cotação pública validada, mantemos apenas a leitura de
        # mercado/edge. A odd justa em si não é repetida em um bloco separado.
        if moneyline:
            render_market_value_panel(team_a, team_b, probs, moneyline)

    expectations = render_match_probability_dashboard(a, b, team_a, team_b, df, probs=probs, sample_games=comp_sample)

    # v27: registra, de forma silenciosa e idempotente, somente previsões de
    # partidas oficiais futuras localizadas na APIfootball. O registro persiste
    # no Supabase para que, após o jogo, o GM SCORE consiga comparar previsão x
    # realizado sem depender de memória local do Streamlit. Falhas nesta camada
    # nunca bloqueiam a análise do cliente.
    try:
        gm_calibration_capture_prediction(
            league_name, team_a, team_b, probs, expectations, a, b,
            model_build=GM_BUILD,
        )
    except Exception:
        pass

    # Diagnóstico visível somente para administrador. Se alguma fonte pública
    # falhar em produção, estes Ns mostram imediatamente se a falha ocorreu na
    # localização da equipe, no histórico ou na leitura das estatísticas.
    try:
        _diag_profile = gm_auth_get_profile()
    except Exception:
        _diag_profile = None
    if (_diag_profile or {}).get("role") == "admin" and analysis_context and analysis_context.get("data_recovery_debug"):
        with st.expander("🧪 Diagnóstico de cobertura (admin)", expanded=False):
            st.caption("N = partidas em que a métrica foi realmente encontrada. Fontes não são somadas para evitar duplicação de amostra.")
            _dbg = analysis_context.get("data_recovery_debug") or {}
            for _side_key, _team_label in (("home", team_a), ("away", team_b)):
                st.markdown(f"**{_team_label}**")
                _side = _dbg.get(_side_key) or {}
                _rows = []
                for _src_key, _src_label in (("apifootball", "APIfootball (principal)"), ("sofascore", "SofaScore"), ("espn", "ESPN"), ("football_data", "Football-Data histórico"), ("merged", "Base final por métrica")):
                    _d = _side.get(_src_key) or {}
                    _cov = _d.get("coverage") or {}
                    _rows.append({
                        "Fonte": _src_label,
                        "Jogos": int(_d.get("games", 0) or 0),
                        "Detalhados": int(_d.get("detailed", 0) or 0),
                        "Escanteios": int(_cov.get("Escanteios", 0) or 0),
                        "Cartões": int(_cov.get("Amarelos", 0) or 0),
                        "Finalizações": int(_cov.get("Finalizações", 0) or 0),
                        "No alvo": int(_cov.get("Chutes no alvo", 0) or 0),
                    })
                st.dataframe(pd.DataFrame(_rows), hide_index=True, use_container_width=True)

    opportunities = build_opportunities(a, b, team_a, team_b, df)
    st.markdown("#### ⭐ Oportunidades GM SCORE")
    # v24: a mensagem usa a mesma regra do seletor: N específico por mercado.
    _opp_metric_samples = {
        "gols": _gm_pair_metric_sample(a, b, ["Gols pró", "Gols contra"], 0),
        "escanteios": _gm_pair_metric_sample(a, b, ["Escanteios"], 0),
        "cartões": _gm_pair_metric_sample(a, b, ["Amarelos"], 0),
    }
    if max(_opp_metric_samples.values(), default=0) < 8:
        st.caption("Destaques suspensos nesta partida: nenhum mercado habilitado possui amostra específica consolidada (N ≥ 8). Os mercados continuam visíveis acima conforme a cobertura disponível.")
    elif analysis_context:
        st.caption("Cada oportunidade exige amostra específica consolidada (N ≥ 8) no próprio mercado; cobertura de uma métrica não libera outra.")
    if opportunities:
        for item in opportunities:
            c1, c2, c3 = st.columns([4.8, 1.3, 1.5])
            c1.markdown(f"**{item['Mercado']}**  \n<small>{item['Base']}</small>", unsafe_allow_html=True)
            chance = item["Chance"]
            chance_color = "#16a34a" if chance >= 80 else "var(--text-color)"
            c2.markdown(f'<div style="text-align:center"><div style="font-size:.85rem;color:#6b7280">Chance</div><div style="font-size:1.65rem;font-weight:800;color:{chance_color}">{chance:.0f}%</div></div>', unsafe_allow_html=True)
            c3.markdown(f"**{item['Leitura']}**")
            st.divider()
    else:
        st.info("🔎 Nenhuma oportunidade atingiu os critérios atuais do GM SCORE para destaque nesta partida.")

    st.markdown("### 📲 Compartilhar")
    render_share_button(team_a, team_b, league_name, probs, opportunities, expectations, a, b, df=df, sample_games=comp_sample)

    try:
        _avg_profile = gm_auth_get_profile()
    except Exception:
        _avg_profile = None
    if (_avg_profile or {}).get("role") == "admin":
        with st.expander("📊 Ver médias usadas na análise"):
            metric_emojis = {"Gols pró":"⚽","Gols contra":"🥅","Escanteios":"⛳","Amarelos":"🟨","Vermelhos":"🟥","Faltas":"🚫","Finalizações":"🎯","Chutes no alvo":"🥅","Posse (%)":"⚪","Impedimentos":"🚩"}
            rows=[]
            for metric in DISPLAY_METRICS:
                if metric == "Jogos" or metric not in a.index or metric not in b.index: continue
                av,bv=a[metric],b[metric]
                if pd.isna(av) and pd.isna(bv): continue
                rows.append({"Dado":f"{metric_emojis.get(metric,'📌')} {metric}",team_a:"N/D" if pd.isna(av) else round(float(av),2),team_b:"N/D" if pd.isna(bv) else round(float(bv),2)})
            if rows: st.dataframe(pd.DataFrame(rows),hide_index=True,use_container_width=True)



# ============================================================
# AUDITORIA APIfootball — LIGAS + EQUIPES (ADMIN)
# ============================================================
# Não altera as competições do cliente automaticamente. Serve para confirmar,
# com a chave/plano realmente ativos no Streamlit, quais ligas existem e quais
# equipes a API associa a cada uma delas. IDs oficiais ficam prontos para a
# integração estatística sem depender apenas de comparação por nome.
GM_APIFOOTBALL_LEAGUE_TARGETS = {
    "Inglaterra - Premier League": {"country": ["england"], "league": ["premier league"]},
    "Espanha - La Liga": {"country": ["spain"], "league": ["la liga"]},
    "Itália - Serie A": {"country": ["italy"], "league": ["serie a"]},
    "Alemanha - Bundesliga": {"country": ["germany"], "league": ["bundesliga"]},
    "França - Ligue 1": {"country": ["france"], "league": ["ligue 1"]},
    "Portugal - Liga Portugal": {"country": ["portugal"], "league": ["primeira liga", "liga portugal"]},
    "Holanda - Eredivisie": {"country": ["netherlands"], "league": ["eredivisie"]},
    "Escócia - Premiership": {"country": ["scotland"], "league": ["premiership"]},
    "Turquia - Süper Lig": {"country": ["turkey", "turkiye"], "league": ["super lig", "süper lig"]},
    "Brasil - Série A": {"country": ["brazil"], "league": ["serie a"]},
    "Brasil - Série B": {"country": ["brazil"], "league": ["serie b"]},
    "Arábia Saudita - Saudi Pro League": {"country": ["saudi arabia", "saudi arabia kingdom"], "league": ["saudi pro league", "pro league", "professional league"]},
    "Estados Unidos - MLS": {"country": ["usa", "united states"], "league": ["mls", "major league soccer"]},
    "Argentina - Liga Profesional": {"country": ["argentina"], "league": ["liga profesional", "liga profesional argentina", "primera division", "primera división"]},
    "México - Liga MX": {"country": ["mexico"], "league": ["liga mx"]},
    "Colômbia - Primera A": {"country": ["colombia"], "league": ["primera a", "primera division"]},
    "CONMEBOL Libertadores": {"country": ["south america", "conmebol", "world"], "league": ["copa libertadores", "libertadores"]},
    "CONMEBOL Sul-Americana": {"country": ["south america", "conmebol", "world"], "league": ["copa sudamericana", "sudamericana", "sul-americana"]},
    "UEFA Champions League": {"country": ["europe", "eurocups"], "league": ["champions league"]},
    "UEFA Europa League": {"country": ["europe", "eurocups"], "league": ["europa league"]},
    "UEFA Conference League": {"country": ["europe", "eurocups"], "league": ["conference league"]},
}

# IDs fixos das 21 competições foram inicializados no topo do arquivo (v45).


# Aliases confirmados visualmente na resposta real da API. O nome exibido ao
# cliente continua sendo o do GM SCORE; isto só melhora a resolução interna.
GM_APIFOOTBALL_TEAM_ALIASES = {
    "América-MG": ["América Mineiro"],
    "Athletic-MG": ["Athletic Club MG"],
    "Atlético-GO": ["Atlético Goianiense"],
    "Atlético-MG": ["Atlético Mineiro", "Clube Atlético Mineiro", "Atletico Mineiro", "Atletico-MG"],
    "Athletico-PR": ["Athletico Paranaense", "Atletico Paranaense"],
    "Náutico": ["Náutico FC"],
    "São Bernardo": ["São Bernardo FC"],
    "Sport": ["Sport Recife"],
    "D.C. United": ["DC United"],
    "Red Bull New York": ["New York RB"],
    "San Jose Earthquakes": ["SJ Earthquakes"],
    "Sporting Kansas City": ["Sporting KC"],
    "Inter Miami CF": ["Inter Miami"],
    "Paris Saint-Germain": ["PSG"],
    "Independiente del Valle": ["Independiente Valle"],
    "AZ Alkmaar": ["AZ"],
    "GNK Dinamo": ["Dinamo Zagreb"],
    "Hapoel Beer-Sheva": ["Hapoel Be'er Sheva"],
    "Lyon": ["Olympique Lyonnais"],
    "N.E.C.": ["NEC"],
    "OFI Crete": ["OFI"],
    "Olympiacos": ["Olympiakos Piraeus"],
    "Union SG": ["Union Saint-Gilloise"],
    "Bournemouth": ["AFC Bournemouth"],
    "Celta": ["Celta de Vigo"],
    "Jagiellonia": ["Jagiellonia Białystok"],
    "Juventus": ["Juventus FC"],
    "Leverkusen": ["Bayer Leverkusen"],
    "Marseille": ["Olympique Marseille"],
    "Omonia": ["Omonia Nicosia"],
    "Arsenal": ["Arsenal FC"],
    "Inter": ["Internazionale"],
    "Leipzig": ["RB Leipzig"],
    "América": ["Club América"],
    "Atlético de San Luis": ["Atlético San Luis"],
    "FC Juárez": ["Juárez"],
    "Alianza FC": ["Alianza"],
    "Deportivo Pereira": ["Deportivo Pereira FC"],
    # Mudança oficial de identidade para 2026: é a mesma ficha/clube da antiga La Equidad.
    "Internacional de Bogotá": ["La Equidad", "CD La Equidad", "Club Deportivo La Equidad"],
    "Jaguares de Córdoba": ["Jaguares de Córdoba FC"],
}

# Entradas especiais que aparecem em algumas listas da API, mas não são clubes
# regulares da competição e não devem causar falsa divergência no elenco.
GM_APIFOOTBALL_NON_REGULAR_TEAMS = {
    "Estados Unidos - MLS": {"liga mx all stars", "mls all stars", "miami"},
}

GM_APIFOOTBALL_USEFUL_CANDIDATES = [
    ("Inglaterra - Championship", ["england"], ["championship"]),
    ("Alemanha - 2. Bundesliga", ["germany"], ["2. bundesliga", "2 bundesliga"]),
    ("Espanha - Segunda División", ["spain"], ["segunda division", "segunda división"]),
    ("Itália - Serie B", ["italy"], ["serie b"]),
    ("França - Ligue 2", ["france"], ["ligue 2"]),
    ("Bélgica - Pro League", ["belgium"], ["pro league", "first division a"]),
    ("Áustria - Bundesliga", ["austria"], ["bundesliga"]),
    ("Dinamarca - Superliga", ["denmark"], ["superliga"]),
    ("Suíça - Super League", ["switzerland"], ["super league"]),
    ("Grécia - Super League", ["greece"], ["super league"]),
    ("Brasil - Copa do Brasil", ["brazil"], ["copa do brasil"]),
    ("Chile - Primera División", ["chile"], ["primera division", "primera división"]),
    ("Uruguai - Primera División", ["uruguay"], ["primera division", "primera división"]),
    ("Paraguai - División Profesional", ["paraguay"], ["division profesional", "división profesional"]),
    ("Equador - LigaPro", ["ecuador"], ["liga pro", "ligapro"]),
    ("Japão - J1 League", ["japan"], ["j1 league", "j-league"]),
    ("Coreia do Sul - K League 1", ["korea republic", "south korea"], ["k league 1"]),
    ("Austrália - A-League", ["australia"], ["a-league", "a league"]),
]

def _gm_api_norm(value):
    txt = unicodedata.normalize("NFKD", str(value or ""))
    txt = "".join(ch for ch in txt if not unicodedata.combining(ch)).lower()
    txt = re.sub(r"[^a-z0-9]+", " ", txt).strip()
    return re.sub(r"\s+", " ", txt)

@st.cache_data(ttl=3600, show_spinner=False)
def gm_apifootball_all_leagues():
    payload, err = gm_apifootball_request("get_leagues")
    return (payload if isinstance(payload, list) else []), err

@st.cache_data(ttl=3600, show_spinner=False)
def gm_apifootball_league_teams(league_id):
    payload, err = gm_apifootball_request("get_teams", league_id=str(league_id))
    return (payload if isinstance(payload, list) else []), err

def _gm_league_name_score(league_name, aliases):
    league = _gm_api_norm(league_name)
    best = 0.0
    for raw in aliases or []:
        alias = _gm_api_norm(raw)
        if not alias:
            continue
        if league == alias:
            best = max(best, 100.0)
        elif alias in league:
            # Evita falsos positivos graves como "primera division" -> "primera d".
            best = max(best, 72.0 - max(0, len(league.split()) - len(alias.split())) * 2.0)
        elif league in alias and len(league) >= 8:
            best = max(best, 58.0)
    return best


def _gm_country_score(country_name, aliases):
    country = _gm_api_norm(country_name)
    vals = [_gm_api_norm(x) for x in (aliases or []) if _gm_api_norm(x)]
    if not vals:
        return 0.0
    if any(country == x for x in vals):
        return 20.0
    if any(x in country or country in x for x in vals if len(x) >= 4 and len(country) >= 4):
        return 10.0
    return 0.0


def _gm_match_api_league(leagues, countries, names):
    """Resolve uma liga sem aceitar correspondência fraca por abreviação.

    Competições continentais podem vir categorizadas como World/Europe/South
    America; por isso o nome oficial pesa mais que a categoria do país.
    """
    candidates = []
    for item in leagues or []:
        nscore = _gm_league_name_score(item.get("league_name"), names)
        if nscore < 58:
            continue
        cscore = _gm_country_score(item.get("country_name"), countries)
        candidates.append((nscore + cscore, item))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    # Um nome apenas parcialmente parecido e sem país compatível não é seguro.
    if candidates[0][0] < 72:
        return None
    return candidates[0][1]


def _gm_api_league_candidates(leagues, countries, names, limit=12):
    """Lista candidatos plausíveis; a auditoria usa os elencos para desempatar."""
    ranked = []
    for item in leagues or []:
        nscore = _gm_league_name_score(item.get("league_name"), names)
        if nscore < 58:
            continue
        cscore = _gm_country_score(item.get("country_name"), countries)
        ranked.append((nscore + cscore, item))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in ranked[:max(1, int(limit))]]

@lru_cache(maxsize=32768)
def _gm_team_name_match_cached(a, b):
    def variants(value):
        raw = str(value or "").strip()
        vals = [raw]
        for canonical, aliases in GM_APIFOOTBALL_TEAM_ALIASES.items():
            pool = [canonical] + list(aliases or [])
            norm_pool = {_gm_api_norm(x) for x in pool}
            if _gm_api_norm(raw) in norm_pool:
                vals.extend(pool)
                break
        return vals

    def basic(x, y):
        aa, bb = _gm_api_norm(x), _gm_api_norm(y)
        stop = {"fc", "cf", "sc", "ac", "ec", "club", "clube", "de", "do", "da", "the"}
        ta = [z for z in aa.split() if z not in stop]
        tb = [z for z in bb.split() if z not in stop]
        aa2, bb2 = " ".join(ta), " ".join(tb)
        if aa2 == bb2:
            return True
        if aa2 and bb2 and min(len(aa2), len(bb2)) >= 5 and (aa2 in bb2 or bb2 in aa2):
            return True
        sa, sb = set(ta), set(tb)
        return bool(sa and sb and len(sa & sb) / max(len(sa | sb), 1) >= 0.72)

    return any(basic(x, y) for x in variants(a) for y in variants(b))


def _gm_team_name_match(a, b):
    # V117: comparação de nomes é pura; a mesma dupla é consultada dezenas de
    # vezes durante merge de fontes e auditorias. Mantém exatamente a regra atual.
    return _gm_team_name_match_cached(str(a or ""), str(b or ""))

def gm_render_apifootball_league_audit():
    try:
        profile = gm_auth_get_profile()
    except Exception:
        profile = None
    if (profile or {}).get("role") != "admin":
        return

    with st.expander("🌍 Auditoria APIfootball — ligas e equipes (admin)", expanded=False):
        st.caption("Consulta a cobertura disponível para a chave/plano ativos. Não altera ligas nem equipes automaticamente e nunca exibe a API key.")
        if not st.button("🔎 Verificar todas as ligas e equipes", key="gm_run_full_api_league_audit", use_container_width=True):
            return
        with st.spinner("Conferindo as 21 competições e as equipes na APIfootball..."):
            leagues, err = gm_apifootball_all_leagues()
            if err or not leagues:
                st.error("A APIfootball não retornou a lista de competições disponível para esta chave.")
                st.caption(f"Diagnóstico: {err or 'lista vazia'}")
                return

            rows = []
            team_details = []
            found_ids = set()
            for comp in COMPETITIONS.keys():
                target = GM_APIFOOTBALL_LEAGUE_TARGETS.get(comp, {})
                expected = CURRENT_TEAM_ROSTERS.get(comp) or []
                fixed_id = str(GM_APIFOOTBALL_FIXED_LEAGUE_IDS.get(comp) or "")
                fixed_hit = next((x for x in leagues if str(x.get("league_id") or "") == fixed_id), None) if fixed_id else None
                candidate_hits = ([fixed_hit] if fixed_hit else []) + [
                    x for x in _gm_api_league_candidates(leagues, target.get("country", []), target.get("league", []))
                    if not fixed_hit or str(x.get("league_id") or "") != fixed_id
                ]
                evaluated = []
                for cand in candidate_hits:
                    cand_lid = str(cand.get("league_id") or "")
                    cand_teams, cand_err = gm_apifootball_league_teams(cand_lid) if cand_lid else ([], "missing_id")
                    raw_names = [str(x.get("team_name") or "").strip() for x in cand_teams if str(x.get("team_name") or "").strip()]
                    blocked = GM_APIFOOTBALL_NON_REGULAR_TEAMS.get(comp, set())
                    cand_names = [name for name in raw_names if _gm_api_norm(name) not in blocked]
                    matched = sum(1 for name in expected if any(_gm_team_name_match(name, api) for api in cand_names)) if expected else 0
                    roster_ratio = matched / max(len(expected), 1) if expected else 0.0
                    base_score = _gm_league_name_score(cand.get("league_name"), target.get("league", [])) + _gm_country_score(cand.get("country_name"), target.get("country", []))
                    evaluated.append((roster_ratio, matched, base_score, cand, cand_teams, cand_err, cand_names))
                if not evaluated:
                    rows.append({"GM SCORE": comp, "Status": "❌ Não localizada", "Liga API": "—", "ID": "—", "Equipes API": 0, "Esperado": MIN_TEAMS.get(comp, "—"), "Equipes conferidas": "—"})
                    continue
                # Elenco é o desempate principal quando existe cadastro oficial no GM SCORE.
                evaluated.sort(key=lambda x: ((x[0] if expected else 0), x[2], x[1]), reverse=True)
                roster_ratio, matched_count, _, hit, teams, terr, api_names = evaluated[0]
                # Com elenco cadastrado, rejeita liga homônima claramente errada.
                if expected and roster_ratio < 0.30:
                    rows.append({"GM SCORE": comp, "Status": "❌ Não localizada com segurança", "Liga API": hit.get("league_name") or "—", "ID": str(hit.get("league_id") or "—"), "Equipes API": len(api_names), "Esperado": MIN_TEAMS.get(comp, "—"), "Equipes conferidas": f"apenas {matched_count}/{len(expected)} compatíveis"})
                    team_details.append((comp, str(hit.get("league_id") or ""), api_names, expected, list(expected)))
                    continue
                lid = str(hit.get("league_id") or "")
                found_ids.add(lid)
                missing = []
                matched_api = set()
                pairs = []
                if expected:
                    for name in expected:
                        matches = [(idx, api) for idx, api in enumerate(api_names) if _gm_team_name_match(name, api)]
                        if matches:
                            idx, api = matches[0]
                            matched_api.add(idx)
                            pairs.append((name, api))
                        else:
                            missing.append(name)
                extras = [api for idx, api in enumerate(api_names) if idx not in matched_api]
                min_expected = int(MIN_TEAMS.get(comp, 0) or 0)
                count_mismatch = bool(expected and len(api_names) != len(expected))
                # Torneios continentais normalmente incluem classificatórias; equipes extras
                # não são erro por si só. Em ligas nacionais, diferença de quantidade exige revisão.
                is_continental = comp.startswith("UEFA ") or comp.startswith("CONMEBOL ")
                if terr:
                    status = "⚠️ Liga localizada / equipes indisponíveis"
                elif expected and missing:
                    status = "⚠️ Divergências de equipes"
                elif count_mismatch and not is_continental:
                    status = "⚠️ Quantidade de equipes divergente"
                elif min_expected and len(api_names) < max(2, int(min_expected * 0.70)):
                    status = "⚠️ Lista de equipes parcial"
                else:
                    status = "✅ Coberta"
                if expected and missing:
                    checked = f"{len(missing)} sem correspondência"
                elif count_mismatch and not is_continental:
                    checked = f"GM {len(expected)} × API {len(api_names)}"
                elif expected:
                    checked = "OK"
                else:
                    checked = "lista API"
                rows.append({"GM SCORE": comp, "Status": status, "Liga API": hit.get("league_name") or "—", "ID": lid or "—", "Equipes API": len(api_names), "Esperado": len(expected) if expected else MIN_TEAMS.get(comp, "—"), "Equipes conferidas": checked})
                team_details.append((comp, lid, api_names, expected, missing, extras, pairs, is_continental))

            df_audit = pd.DataFrame(rows)
            st.dataframe(df_audit, hide_index=True, use_container_width=True)
            covered = sum(1 for r in rows if str(r["Status"]).startswith("✅"))
            warnings = sum(1 for r in rows if str(r["Status"]).startswith("⚠️"))
            missing_count = sum(1 for r in rows if str(r["Status"]).startswith("❌"))
            st.markdown(f"**Resumo:** {covered}/{len(rows)} cobertas • {warnings} com revisão • {missing_count} não localizadas")

            with st.expander("👥 Auditoria nominal — divergências e nomes oficiais", expanded=True):
                st.caption("Mostra exatamente o que precisa ser revisado. Em torneios continentais, equipes extras podem ser clubes das fases preliminares e não são tratadas automaticamente como erro.")
                for detail in team_details:
                    # Compatibilidade com registros produzidos antes da ampliação do diagnóstico.
                    comp, lid, api_names, expected, missing = detail[:5]
                    extras = detail[5] if len(detail) > 5 else []
                    pairs = detail[6] if len(detail) > 6 else []
                    is_continental = detail[7] if len(detail) > 7 else False
                    needs_review = bool(missing or (expected and len(api_names) != len(expected) and not is_continental))
                    if not needs_review:
                        continue
                    st.markdown(f"**{comp}** · API league_id `{lid}` · GM `{len(expected) if expected else 0}` × API `{len(api_names)}`")
                    if missing:
                        st.error("GM SCORE sem correspondência segura: " + " • ".join(missing))
                    if extras:
                        label = "Equipes extras/sem par na API" if not is_continental else "Equipes adicionais da API (podem incluir classificatórias)"
                        st.info(label + ": " + " • ".join(extras))
                    if pairs:
                        renamed = [(gm, api) for gm, api in pairs if _gm_api_norm(gm) != _gm_api_norm(api)]
                        if renamed:
                            st.caption("Correspondências por alias: " + " | ".join(f"{gm} ↔ {api}" for gm, api in renamed))

            with st.expander("📋 Lista completa de equipes retornadas", expanded=False):
                for detail in team_details:
                    comp, lid, api_names = detail[0], detail[1], detail[2]
                    st.markdown(f"**{comp}** · API league_id `{lid}` · {len(api_names)} equipes")
                    st.caption(" • ".join(api_names) if api_names else "Nenhuma equipe retornada.")

            suggestions = []
            for label, countries, names in GM_APIFOOTBALL_USEFUL_CANDIDATES:
                hit = _gm_match_api_league(leagues, countries, names)
                if hit and str(hit.get("league_id") or "") not in found_ids:
                    suggestions.append({"Sugestão": label, "Liga API": hit.get("league_name") or "—", "País": hit.get("country_name") or "—", "ID": str(hit.get("league_id") or "—")})
            st.markdown("#### ➕ Ligas úteis disponíveis na chave")
            if suggestions:
                st.dataframe(pd.DataFrame(suggestions), hide_index=True, use_container_width=True)
                st.caption("São apenas candidatas. Nenhuma será adicionada ao GM SCORE sem validação de estatísticas e confirmação.")
            else:
                st.info("Nenhuma das ligas adicionais prioritárias apareceu na cobertura desta chave/plano.")

@st.cache_data(ttl=3600, show_spinner=False)
def gm_apifootball_league_stat_coverage(league_id, lookback_days=420, sample_matches=20):
    """Mede cobertura REAL de estatísticas em partidas recentes de uma liga.

    Uma chamada de get_events por competição é suficiente: contamos somente
    partidas finalizadas e nunca transformamos campo ausente em zero.
    """
    end = date.today()
    start = end - timedelta(days=int(lookback_days))
    payload, err = gm_apifootball_request(
        "get_events", league_id=str(league_id),
        **{"from": start.isoformat(), "to": end.isoformat(), "timezone": "America/Sao_Paulo"}
    )
    if err or not isinstance(payload, list):
        return {"ok": False, "error": err or "unexpected_payload", "n": 0, "metrics": {}}
    finished = []
    for ev in payload:
        if not isinstance(ev, dict):
            continue
        status = _gm_api_norm(ev.get("match_status"))
        if status not in {"finished", "ft", "after et", "after pen"}:
            continue
        finished.append(ev)
    finished.sort(key=lambda x: str(x.get("match_date") or ""), reverse=True)
    chosen = finished[:max(1, int(sample_matches))]
    metric_aliases = {
        "Gols": None,
        "Escanteios": ("Corners", "Corner Kicks"),
        "Cartões": ("Yellow Cards",),
        "Faltas": ("Fouls",),
        "Finalizações": ("Shots Total", "Goal Attempts", "Total Shots"),
        "No alvo": ("Shots On Goal", "Shots on Goal", "On Target"),
        "Impedimentos": ("Offsides",),
        "Posse": ("Ball Possession",),
        "Escanteios 1T": ("Corners", "Corner Kicks"),
        "Finalizações 1T": ("Shots Total", "Goal Attempts", "Total Shots"),
        "No alvo 1T": ("Shots On Goal", "Shots on Goal", "On Target"),
    }
    counts = {k: 0 for k in metric_aliases}
    for ev in chosen:
        stats = _gm_api_stat_map(ev.get("statistics"))
        half = _gm_api_stat_map(ev.get("statistics_1half"))
        hg = ev.get("match_hometeam_ft_score") or ev.get("match_hometeam_score")
        ag = ev.get("match_awayteam_ft_score") or ev.get("match_awayteam_score")
        if _gm_api_has_value(hg) and _gm_api_has_value(ag):
            counts["Gols"] += 1
        for label, aliases in metric_aliases.items():
            if label == "Gols":
                continue
            block = half if label.endswith("1T") else stats
            ok = False
            for alias in aliases or ():
                pair = block.get(alias)
                if pair and _gm_api_has_value(pair.get("home")) and _gm_api_has_value(pair.get("away")):
                    ok = True
                    break
            # Cartões também podem existir no bloco de eventos quando o agregado falta.
            if label == "Cartões" and not ok and isinstance(ev.get("cards"), list):
                ok = True
            if ok:
                counts[label] += 1
    return {"ok": bool(chosen), "error": None, "n": len(chosen), "metrics": counts}


def gm_render_apifootball_stat_audit():
    try:
        profile = gm_auth_get_profile()
    except Exception:
        profile = None
    if (profile or {}).get("role") != "admin":
        return
    with st.expander("📊 Auditoria APIfootball — cobertura estatística das 21 ligas (admin)", expanded=False):
        st.caption("Testa partidas finalizadas reais por league_id. Mede N por métrica e não inventa valores ausentes.")
        if not st.button("📈 Testar estatísticas das 21 ligas", key="gm_run_api_stat_audit", use_container_width=True):
            return
        rows = []
        progress = st.progress(0, text="Verificando cobertura estatística...")
        comps = list(GM_APIFOOTBALL_FIXED_LEAGUE_IDS.items())
        for idx, (comp, lid) in enumerate(comps, start=1):
            result = gm_apifootball_league_stat_coverage(lid, lookback_days=420, sample_matches=20)
            n = int(result.get("n") or 0)
            metrics = result.get("metrics") or {}
            def pct(name):
                return round(100.0 * int(metrics.get(name, 0) or 0) / n) if n else 0
            detail_core = [pct("Escanteios"), pct("Cartões"), pct("Finalizações"), pct("No alvo")]
            if not result.get("ok"):
                status = "❌ Sem amostra"
            elif min(detail_core) >= 70:
                status = "✅ Forte"
            elif max(detail_core) >= 50:
                status = "⚠️ Parcial"
            else:
                status = "🔸 Baixa"
            rows.append({
                "Liga": comp, "ID": lid, "N": n, "Status": status,
                "Gols": f"{pct('Gols')}%", "Escanteios": f"{pct('Escanteios')}%",
                "Cartões": f"{pct('Cartões')}%", "Finalizações": f"{pct('Finalizações')}%",
                "No alvo": f"{pct('No alvo')}%", "Faltas": f"{pct('Faltas')}%",
                "Impedimentos": f"{pct('Impedimentos')}%", "Posse": f"{pct('Posse')}%",
                "Esc. 1T": f"{pct('Escanteios 1T')}%", "Finaliz. 1T": f"{pct('Finalizações 1T')}%",
                "Alvo 1T": f"{pct('No alvo 1T')}%",
            })
            progress.progress(idx / len(comps), text=f"{idx}/{len(comps)} · {comp}")
        progress.empty()
        df = pd.DataFrame(rows)
        st.dataframe(df, hide_index=True, use_container_width=True)
        strong = sum(1 for r in rows if r["Status"] == "✅ Forte")
        partial = sum(1 for r in rows if r["Status"] == "⚠️ Parcial")
        low = len(rows) - strong - partial
        st.markdown(f"**Resumo:** {strong} fortes • {partial} parciais • {low} com baixa/sem amostra")
        st.caption("O objetivo é usar APIfootball como fonte principal onde a cobertura é forte e manter os fallbacks já existentes onde uma métrica específica for curta.")


# ============================================================
# APRENDIZADO / CALIBRAÇÃO AUTOMÁTICA — PREVISÃO X REALIZADO
# ============================================================
# Esta camada NÃO altera pesos do modelo por conta própria. O motor já se adapta
# aos jogos recentes porque as médias e o perfil da competição são recalculados
# com resultados novos. A função abaixo acrescenta a peça que faltava: guardar a
# previsão pré-jogo, buscar o realizado depois do apito final e medir erro/viés.
# Assim, qualquer ajuste futuro pode ser feito somente quando houver evidência.

GM_CALIBRATION_MARKETS = {
    "goals_total": "Gols",
    "corners_total": "Escanteios",
    "cards_total": "Cartões",
    "shots_total": "Finalizações",
    "sot_total": "No alvo",
}


def gm_calibration_rpc(function_name, params=None):
    """RPC isolada da calibração; usa a mesma sessão autenticada protegida por RLS/RPC."""
    client = gm_auth_client_from_session()
    if client is None:
        raise RuntimeError("Sessão autenticada indisponível.")
    result = client.rpc(function_name, params or {}).execute()
    return getattr(result, "data", None)


def _gm_calibration_dt(event):
    """Converte data/hora da APIfootball para ISO UTC, sem adivinhar fuso."""
    d = str((event or {}).get("match_date") or "").strip()
    t = str((event or {}).get("match_time") or "00:00").strip() or "00:00"
    if not d:
        return None
    try:
        ts = pd.Timestamp(f"{d} {t}")
        if ts.tzinfo is None:
            ts = ts.tz_localize("America/Sao_Paulo")
        return ts.tz_convert("UTC")
    except Exception:
        return None


def _gm_calibration_event_match(event, home_team, away_team):
    hn = str((event or {}).get("match_hometeam_name") or "")
    an = str((event or {}).get("match_awayteam_name") or "")
    return _gm_team_name_match(home_team, hn) and _gm_team_name_match(away_team, an)


@st.cache_data(ttl=600, show_spinner=False)
def gm_calibration_find_future_event(competition_name, home_team, away_team):
    """Localiza somente o jogo oficial futuro exato; seleção manual sem agenda não é gravada."""
    league_id = str(GM_APIFOOTBALL_FIXED_LEAGUE_IDS.get(competition_name) or "").strip()
    if not league_id:
        return None
    today = date.today()
    payload, err = gm_apifootball_request(
        "get_events",
        league_id=league_id,
        **{
            "from": (today - timedelta(days=1)).isoformat(),
            "to": (today + timedelta(days=14)).isoformat(),
            "timezone": "America/Sao_Paulo",
        },
    )
    if err or not isinstance(payload, list):
        return None
    now_utc = pd.Timestamp.now(tz="UTC")
    hits = []
    for ev in payload:
        if not isinstance(ev, dict) or not _gm_calibration_event_match(ev, home_team, away_team):
            continue
        status = _gm_api_norm(ev.get("match_status"))
        if status in {"finished", "ft", "after et", "after pen", "cancelled", "canceled", "postponed", "abandoned"}:
            continue
        kickoff = _gm_calibration_dt(ev)
        if kickoff is None or kickoff <= now_utc + pd.Timedelta(minutes=3):
            continue
        mid = str(ev.get("match_id") or "").strip()
        if not mid:
            continue
        hits.append((kickoff, ev))
    if not hits:
        return None
    hits.sort(key=lambda x: x[0])
    kickoff, ev = hits[0]
    return {
        "match_id": str(ev.get("match_id") or ""),
        "kickoff_at": kickoff.isoformat(),
        "home_team_id": str(ev.get("match_hometeam_id") or ""),
        "away_team_id": str(ev.get("match_awayteam_id") or ""),
        "api_home": str(ev.get("match_hometeam_name") or home_team),
        "api_away": str(ev.get("match_awayteam_name") or away_team),
        "league_id": league_id,
    }


def _gm_calibration_number(value):
    try:
        v = float(value)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def _gm_calibration_sample_payload(a, b):
    return {
        "goals": int(_gm_pair_metric_sample(a, b, ["Gols pró", "Gols contra"], 0) or 0),
        "corners": int(_gm_pair_metric_sample(a, b, ["Escanteios"], 0) or 0),
        "cards": int(_gm_pair_metric_sample(a, b, ["Amarelos"], 0) or 0),
        "shots": int(_gm_pair_metric_sample(a, b, ["Finalizações"], 0) or 0),
        "sot": int(_gm_pair_metric_sample(a, b, ["Chutes no alvo"], 0) or 0),
    }


def gm_calibration_capture_prediction(competition_name, home_team, away_team, probs, expectations, a, b, model_build=None):
    """Persiste uma fotografia pré-jogo somente quando há evento oficial futuro correspondente."""
    if not st.session_state.get("gm_auth_access_token"):
        return False
    event = gm_calibration_find_future_event(competition_name, home_team, away_team)
    if not event:
        return False
    ex = expectations or {}
    pred = {}
    if isinstance(probs, dict):
        ph = _gm_calibration_number(probs.get("home")); pdw = _gm_calibration_number(probs.get("draw")); pa = _gm_calibration_number(probs.get("away"))
        if None not in (ph, pdw, pa):
            # O restante do app trabalha em percentuais 0..100; o histórico usa 0..1.
            pred["p_home"] = max(0.0, min(ph / 100.0, 1.0))
            pred["p_draw"] = max(0.0, min(pdw / 100.0, 1.0))
            pred["p_away"] = max(0.0, min(pa / 100.0, 1.0))
    for src_key, prefix in (("Gols", "goals"), ("Escanteios", "corners"), ("Cartões", "cards"), ("Finalizações", "shots"), ("Chutes no alvo", "sot")):
        item = ex.get(src_key) if isinstance(ex, dict) else None
        if not isinstance(item, dict):
            continue
        for part in ("total", "home", "away"):
            v = _gm_calibration_number(item.get(part))
            if v is not None:
                pred[f"{prefix}_{part}"] = max(v, 0.0)
    if not pred:
        return False

    # Evita RPC repetida em cada rerun da mesma tela; o banco também é idempotente.
    signature = f"{event['match_id']}|{model_build or GM_BUILD}|{round(float(pred.get('goals_total', -1)), 3)}|{round(float(pred.get('p_home', -1)), 4)}"
    if st.session_state.get("_gm_calibration_last_capture") == signature:
        return True
    gm_calibration_rpc("gm_calibration_upsert_prediction", {
        "p_match_id": event["match_id"],
        "p_competition": competition_name,
        "p_league_id": event.get("league_id") or "",
        "p_kickoff_at": event["kickoff_at"],
        "p_home_team": home_team,
        "p_away_team": away_team,
        "p_home_team_id": event.get("home_team_id") or "",
        "p_away_team_id": event.get("away_team_id") or "",
        "p_predictions": pred,
        "p_samples": _gm_calibration_sample_payload(a, b),
        "p_model_build": str(model_build or GM_BUILD),
    })
    st.session_state["_gm_calibration_last_capture"] = signature
    return True


def _gm_calibration_stat_total(stats, aliases):
    for alias in aliases:
        pair = stats.get(alias)
        if not pair:
            continue
        h = _gm_calibration_number(pair.get("home")); a = _gm_calibration_number(pair.get("away"))
        if h is not None and a is not None:
            return h, a, h + a
    return None, None, None


def gm_calibration_actuals_from_event(event):
    """Extrai realizado da mesma API oficial usada pelo motor, sem transformar ausência em zero."""
    if not isinstance(event, dict):
        return None
    status = _gm_api_norm(event.get("match_status"))
    if status not in {"finished", "ft", "after et", "after pen"}:
        return None
    hg = _gm_calibration_number(event.get("match_hometeam_ft_score") or event.get("match_hometeam_score"))
    ag = _gm_calibration_number(event.get("match_awayteam_ft_score") or event.get("match_awayteam_score"))
    if hg is None or ag is None:
        return None
    stats = _gm_api_stat_map(event.get("statistics"))
    out = {
        "final": True,
        "home_goals": hg,
        "away_goals": ag,
        "goals_total": hg + ag,
        "result": "home" if hg > ag else ("away" if ag > hg else "draw"),
    }
    for key, aliases in (
        ("corners", ("Corners", "Corner Kicks")),
        ("shots", ("Shots Total", "Goal Attempts", "Total Shots")),
        ("sot", ("Shots On Goal", "Shots on Goal", "On Target")),
    ):
        h, a, total = _gm_calibration_stat_total(stats, aliases)
        if total is not None:
            out[f"{key}_home"] = h; out[f"{key}_away"] = a; out[f"{key}_total"] = total

    yh, ya, _ = _gm_calibration_stat_total(stats, ("Yellow Cards",))
    rh, ra, _ = _gm_calibration_stat_total(stats, ("Red Cards",))
    if yh is not None and ya is not None:
        ch = yh + (rh or 0.0); ca = ya + (ra or 0.0)
        out["cards_home"] = ch; out["cards_away"] = ca; out["cards_total"] = ch + ca
    elif isinstance(event.get("cards"), list):
        ch = ca = 0.0
        for card in event.get("cards") or []:
            if not isinstance(card, dict):
                continue
            ct = _gm_api_norm(card.get("card"))
            if "yellow" not in ct and "red" not in ct:
                continue
            if card.get("home_fault"):
                ch += 1.0
            elif card.get("away_fault"):
                ca += 1.0
        out["cards_home"] = ch; out["cards_away"] = ca; out["cards_total"] = ch + ca
    return out


@st.cache_data(ttl=300, show_spinner=False)
def gm_calibration_fetch_finished_event(match_id):
    payload, err = gm_apifootball_request("get_events", match_id=str(match_id), timezone="America/Sao_Paulo")
    if err:
        return None
    return _gm_api_extract_event(payload)


def gm_calibration_auto_settle(limit=2, force=False):
    """Liquida previsões pendentes quando qualquer usuário autenticado usa o app.

    Não exige tarefa em background: se ninguém abrir o aplicativo, os jogos ficam
    pendentes e são recuperados automaticamente na próxima utilização.
    """
    if not st.session_state.get("gm_auth_access_token"):
        return {"checked": 0, "settled": 0}
    now_mono = time.monotonic()
    last = st.session_state.get("_gm_calibration_last_settle_tick")
    if not force and isinstance(last, (int, float)) and now_mono - float(last) < 1800:
        return {"checked": 0, "settled": 0}
    st.session_state["_gm_calibration_last_settle_tick"] = now_mono
    pending = gm_calibration_rpc("gm_calibration_pending", {"p_limit": max(1, min(int(limit), 30))}) or []
    if isinstance(pending, dict):
        pending = [pending]
    checked = settled = 0
    for row in pending:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("match_id") or "").strip()
        if not mid:
            continue
        checked += 1
        ev = gm_calibration_fetch_finished_event(mid)
        actuals = gm_calibration_actuals_from_event(ev)
        if not actuals:
            continue
        try:
            gm_calibration_rpc("gm_calibration_settle", {"p_match_id": mid, "p_actuals": actuals})
            settled += 1
        except Exception:
            continue
    return {"checked": checked, "settled": settled}


def _gm_calibration_admin_rows(limit=2500):
    rows = gm_admin_rpc("gm_admin_calibration_rows", {"p_limit": int(limit)}) or []
    return [r for r in rows if isinstance(r, dict)]


def _gm_calibration_bias_status(n, bias_pct):
    if n < 30:
        return "🟠 Amostra em formação"
    if bias_pct is None:
        return "⚪ Sem base"
    ab = abs(float(bias_pct))
    if ab <= 8:
        return "🟢 Sem viés relevante"
    if ab <= 15:
        return "🟡 Monitorar"
    return "🔴 Viés consistente"


def gm_render_calibration_dashboard():
    try:
        profile = gm_auth_get_profile()
    except Exception:
        profile = None
    if (profile or {}).get("role") != "admin":
        return
    with st.expander("🧠 Calibração automática — previsão × realizado (admin)", expanded=False):
        st.caption(
            "O GM SCORE aprende gradualmente com resultados novos: atualiza médias, ambiente de gols e, após amostra suficiente, o perfil 1X2 da competição. "
            "Esta calibração também guarda previsão pré-jogo e compara com o realizado para medir erro e viés sem reagir a poucos jogos."
        )
        c1, c2 = st.columns(2)
        if c1.button("🔄 Sincronizar resultados pendentes", use_container_width=True, key="gm_calibration_sync_now"):
            try:
                sync = gm_calibration_auto_settle(limit=30, force=True)
                st.success(f"Sincronização concluída: {sync['settled']} resultado(s) atualizado(s) entre {sync['checked']} verificado(s).")
            except Exception as exc:
                st.warning("A estrutura de calibração ainda não está ativa no Supabase ou houve falha transitória.")
                st.caption(f"Detalhe: {type(exc).__name__}")
        if c2.button("♻️ Atualizar painel", use_container_width=True, key="gm_calibration_refresh_panel"):
            st.rerun()
        try:
            rows = _gm_calibration_admin_rows()
        except Exception as exc:
            st.info("Execute uma única vez o arquivo SQL de calibração no Supabase para ativar este painel.")
            st.caption(f"Enquanto isso, o motor estatístico atual continua funcionando normalmente. ({type(exc).__name__})")
            return
        if not rows:
            st.info("Nenhuma previsão oficial pré-jogo registrada ainda. O histórico começa a ser construído automaticamente conforme partidas futuras forem analisadas.")
            return
        settled_rows = [r for r in rows if isinstance(r.get("actuals"), dict) and r.get("actuals", {}).get("final")]
        pending_n = len(rows) - len(settled_rows)
        m1, m2, m3 = st.columns(3)
        m1.metric("Previsões registradas", len(rows))
        m2.metric("Jogos conferidos", len(settled_rows))
        m3.metric("Pendentes", pending_n)
        if not settled_rows:
            st.caption("Os indicadores de erro aparecerão quando os primeiros jogos registrados terminarem.")
            return

        metric_rows = []
        for key, label in GM_CALIBRATION_MARKETS.items():
            vals = []
            for r in settled_rows:
                pred = r.get("predictions") or {}; act = r.get("actuals") or {}
                pv = _gm_calibration_number(pred.get(key)); av = _gm_calibration_number(act.get(key))
                if pv is None or av is None:
                    continue
                vals.append((pv, av))
            if not vals:
                continue
            n = len(vals)
            mae = sum(abs(p-a) for p,a in vals) / n
            bias = sum(p-a for p,a in vals) / n
            actual_mean = sum(a for _,a in vals) / n
            bias_pct = (100.0 * bias / actual_mean) if actual_mean > 1e-9 else None
            metric_rows.append({
                "Mercado": label, "N": n, "MAE": round(mae, 2), "Viés médio": round(bias, 2),
                "Viés %": "—" if bias_pct is None else f"{bias_pct:+.1f}%",
                "Leitura": _gm_calibration_bias_status(n, bias_pct),
            })
        if metric_rows:
            st.markdown("**Erro por mercado**")
            st.dataframe(pd.DataFrame(metric_rows), hide_index=True, use_container_width=True)

        briers = []
        for r in settled_rows:
            pred = r.get("predictions") or {}; act = r.get("actuals") or {}
            vals = [_gm_calibration_number(pred.get(k)) for k in ("p_home", "p_draw", "p_away")]
            result = str(act.get("result") or "")
            if any(v is None for v in vals) or result not in {"home", "draw", "away"}:
                continue
            y = [1.0 if result == x else 0.0 for x in ("home", "draw", "away")]
            briers.append(sum((float(p)-yy)**2 for p,yy in zip(vals,y)) / 3.0)
        if briers:
            st.caption(f"1X2 · Brier multiclasses médio: **{sum(briers)/len(briers):.3f}** em **N={len(briers)}** jogos. Quanto menor, melhor; use a tendência ao longo do tempo, não uma partida isolada.")

        comp_data = {}
        for r in settled_rows:
            comp = str(r.get("competition") or "—")
            d = comp_data.setdefault(comp, {"N": 0, "g": [], "c": [], "k": []})
            d["N"] += 1
            pred = r.get("predictions") or {}; act = r.get("actuals") or {}
            for slot, key in (("g", "goals_total"), ("c", "corners_total"), ("k", "cards_total")):
                pv = _gm_calibration_number(pred.get(key)); av = _gm_calibration_number(act.get(key))
                if pv is not None and av is not None:
                    d[slot].append(pv-av)
        comp_rows = []
        for comp, d in sorted(comp_data.items(), key=lambda kv: (-kv[1]["N"], kv[0])):
            def mb(slot):
                arr = d[slot]
                return "—" if not arr else f"{sum(arr)/len(arr):+.2f}"
            comp_rows.append({"Competição": comp, "N": d["N"], "Viés gols": mb("g"), "Viés esc.": mb("c"), "Viés cartões": mb("k")})
        st.markdown("**Monitoramento por competição**")
        st.dataframe(pd.DataFrame(comp_rows), hide_index=True, use_container_width=True)
        st.caption("Regra operacional: N < 30 continua em formação. A adaptação automática do ambiente 1X2 só começa em N ≥ 30, com peso de 2% a no máximo 8%; alertas de viés continuam servindo para investigação, sem correções agressivas.")



# ============================================================
# OPORTUNIDADES GM DO DIA — V57
# ============================================================
# Três faixas independentes, todas construídas com dados/odds pré-jogo reais:
# 1) Matadeira: faixa mais conservadora, alvo preferencial 1,60–1,85.
# 2) Dica do Dia: antiga Seleção GM do Dia, alvo 1,90–2,10.
# 3) Bingo: múltipla conservadora, com várias partidas se necessário, odd >= 3,50.
# Se uma faixa não atingir os critérios, ela fica oficialmente sem seleção.
# O Bingo é propositalmente EXCLUÍDO do aproveitamento principal.
GM_DAILY_PICK_PROFILES = {
    "matadeira": {
        "label": "🛡️ Matadeira", "short": "Matadeira", "min": 1.50, "max": 1.89,
        "preferred_min": 1.60, "preferred_max": 1.85,
        "description": "Mais conservadora",
    },
    "dica": {
        "label": "⭐ Dica do Dia", "short": "Dica", "min": 1.90, "max": 2.10,
        "preferred_min": 1.90, "preferred_max": 2.10,
        "description": "Risco intermediário",
    },
    "bingo": {
        "label": "🎰 Bingo", "short": "Bingo", "min": 3.50, "max": None,
        "preferred_min": 4.00, "preferred_max": None,
        "description": "Mais arriscada",
    },
}


def gm_daily_pick_rpc(function_name, params=None):
    client = gm_auth_client_from_session()
    if client is None:
        raise RuntimeError("Sessão autenticada indisponível.")
    result = client.rpc(function_name, params or {}).execute()
    return getattr(result, "data", None)


def _gm_daily_num(value):
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(str(value).replace(",", "."))
    except Exception:
        return None


def _gm_daily_norm_bookmaker(value):
    return re.sub(r"[^a-z0-9]+", "", _gm_api_norm(value))


def _gm_daily_time_label(value):
    """Normaliza o horário pré-jogo para HH:MM em Brasília quando a fonte já o entrega local."""
    raw = str(value or "").strip()
    if not raw:
        return "--:--"
    m = re.search(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)", raw)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return raw[:5] if len(raw) >= 5 else raw


def _gm_daily_time_key(value):
    label = _gm_daily_time_label(value)
    m = re.fullmatch(r"(\d{2}):(\d{2})", label)
    if not m:
        return 24 * 60 + 1
    return int(m.group(1)) * 60 + int(m.group(2))


def _gm_daily_sort_legs(legs):
    return sorted(
        [dict(x) for x in (legs or []) if isinstance(x, dict)],
        key=lambda x: (
            str(x.get("date") or ""),
            _gm_daily_time_key(x.get("time")),
            _gm_api_norm(x.get("competition")),
            _gm_api_norm(x.get("home")),
        ),
    )


GM_DAILY_PICK_DISK_CACHE_TTL = 1800

def _gm_daily_pick_disk_cache_path(target_date_iso, kind="source"):
    safe_day = re.sub(r"[^0-9-]", "", str(target_date_iso or ""))[:10]
    return f"/tmp/gm_score_daily_pick_{kind}_{safe_day}.json"

def _gm_daily_pick_disk_cache_load(target_date_iso, kind="source", ttl=GM_DAILY_PICK_DISK_CACHE_TTL):
    """V148: cache local persistente entre reruns/restarts do Streamlit."""
    path = _gm_daily_pick_disk_cache_path(target_date_iso, kind)
    try:
        age = time.time() - os.path.getmtime(path)
        if age < 0 or age > float(ttl):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:
        return None

def _gm_daily_pick_disk_cache_save(target_date_iso, payload, kind="source"):
    path = _gm_daily_pick_disk_cache_path(target_date_iso, kind)
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"), default=str)
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False

def _gm_daily_pick_disk_cache_clear(target_date_iso):
    for kind in ("source", "candidates", "fixtures"):
        try:
            os.remove(_gm_daily_pick_disk_cache_path(target_date_iso, kind))
        except FileNotFoundError:
            pass
        except Exception:
            pass

@st.cache_data(ttl=600, show_spinner=False)
def gm_daily_pick_source_payload(target_date_iso):
    """Fonte das aprovações dirigida pela grade oficial de partidas.

    V114: a busca deixa de depender apenas dos endpoints globais por data. Primeiro
    identifica as partidas oficiais das 21 competições suportadas e, para cada
    ``match_id`` que não veio no lote global, consulta previsão/odd diretamente pelo
    evento. Isso evita que uma resposta parcial por data faça uma partida existente
    na aba Jogos desaparecer do funil das Dicas do Dia. Os critérios estatísticos
    permanecem inalterados.
    """
    if not gm_apifootball_api_key():
        return {"predictions": [], "odds": [], "fixtures": [], "error": "secret_missing"}

    # V148: se outra execução já coletou a grade/odds/previsões recentemente,
    # reutiliza a base local em vez de repetir dezenas de chamadas por partida.
    disk_cached = _gm_daily_pick_disk_cache_load(target_date_iso, "source")
    if disk_cached is not None:
        disk_cached = dict(disk_cached)
        disk_cached["persistent_cache_hit"] = True
        return disk_cached

    try:
        target_day = pd.to_datetime(target_date_iso).date()
    except Exception:
        return {"predictions": [], "odds": [], "fixtures": [], "error": "invalid_date"}

    # 1) Grade oficial: é ela que define O QUE precisa ser pesquisado.
    try:
        fixtures = load_apifootball_all_competitions_fixtures_for_date(target_day) or []
    except Exception:
        try:
            fixtures = load_apifootball_fixtures_for_date(target_day) or []
        except Exception:
            fixtures = []

    allowed_ids = {str(v) for v in (GM_APIFOOTBALL_FIXED_LEAGUE_IDS or {}).values()}
    official_match_ids = []
    seen_fixture_ids = set()
    for fx in fixtures:
        if not isinstance(fx, dict):
            continue
        league_id = str(fx.get("league_id") or "").strip()
        mid = str(fx.get("match_id") or "").strip()
        if mid and league_id in allowed_ids and mid not in seen_fixture_ids:
            seen_fixture_ids.add(mid)
            official_match_ids.append(mid)

    # 2) Lotes globais continuam sendo o caminho rápido.
    predictions, pred_err = gm_apifootball_request(
        "get_predictions", **{"from": target_date_iso, "to": target_date_iso}
    )
    if pred_err or not isinstance(predictions, list):
        predictions = []

    odds, odds_err = gm_apifootball_request(
        "get_odds", **{"from": target_date_iso, "to": target_date_iso}
    )
    if odds_err or not isinstance(odds, list):
        odds = []

    # 3) V149 PERFORMANCE: o preparo interativo usa somente os lotes por data.
    # As consultas individuais por match_id eram a causa do bloqueio de vários
    # minutos em dias com muitos jogos. Elas não alteravam os thresholds; apenas
    # tentavam completar cobertura. Agora o ADM recebe rapidamente os mercados
    # que já possuem previsão + odds no lote oficial, sem baixar critérios.
    pred_by_match = {
        str(r.get("match_id") or "").strip(): r
        for r in predictions if isinstance(r, dict) and str(r.get("match_id") or "").strip()
    }
    odds_by_match = {}
    for row in odds:
        if isinstance(row, dict):
            mid = str(row.get("match_id") or "").strip()
            if mid:
                odds_by_match.setdefault(mid, []).append(row)

    missing_prediction_ids = [mid for mid in official_match_ids if mid not in pred_by_match]
    missing_odds_ids = [mid for mid in official_match_ids if mid not in odds_by_match]
    direct_prediction_hits = 0
    direct_odds_hits = 0
    merged_predictions = list(pred_by_match.values())
    merged_odds = [row for rows in odds_by_match.values() for row in rows]

    # Só é erro de fonte quando nem os lotes nem a pesquisa dirigida produziram
    # dados. Ter fixture sem previsão/odd é um diagnóstico, não uma falsa ausência.
    source_error = None
    if not merged_predictions and not merged_odds:
        if pred_err:
            source_error = f"predictions:{pred_err}"
        elif odds_err:
            source_error = f"odds:{odds_err}"
        else:
            source_error = "no_prediction_or_odds_data"

    result = {
        "predictions": merged_predictions,
        "odds": merged_odds,
        "fixtures": fixtures,
        "official_fixture_ids": official_match_ids,
        "direct_prediction_hits": direct_prediction_hits,
        "direct_odds_hits": direct_odds_hits,
        "batch_missing_predictions": len(missing_prediction_ids),
        "batch_missing_odds": len(missing_odds_ids),
        "batch_only_fastpath": True,
        "error": source_error,
    }
    # Só persiste uma coleta útil; erro de fonte não fica "preso" no cache.
    if not source_error:
        _gm_daily_pick_disk_cache_save(target_date_iso, result, "source")
    return result


def _gm_daily_prob_over_05_from_over15(prob_over15):
    """Deriva P(mais de 0,5) de P(mais de 1,5) sob Poisson.

    Não inventa uma odd: a odd continua vindo do bookmaker (campo o+0.5).
    A derivação serve apenas para estimar a probabilidade do modelo quando o
    provedor entrega prob_O_1, mas não uma probabilidade explícita para O0.5.
    """
    p = _gm_daily_num(prob_over15)
    if p is None:
        return None
    q = max(0.0001, min(0.9999, float(p) / 100.0))
    lo, hi = 0.0001, 12.0
    for _ in range(70):
        lam = (lo + hi) / 2.0
        p_ge_2 = 1.0 - math.exp(-lam) * (1.0 + lam)
        if p_ge_2 < q:
            lo = lam
        else:
            hi = lam
    lam = (lo + hi) / 2.0
    return max(0.0, min(99.8, (1.0 - math.exp(-lam)) * 100.0))


def _gm_daily_market_specs():
    """Mercados com odd pré-jogo REAL disponível no provedor atual.

    A diversidade oficial das oportunidades só usa mercados cujo preço vem do
    bookmaker. Mercados estatísticos como escanteios/cartões/finalizações/1T/2T
    podem reforçar a leitura, mas não recebem odd inventada.
    """
    return [
        ("1", "Vitória da casa", "prob_HW", "odd_1"),
        ("2", "Vitória do visitante", "prob_AW", "odd_2"),
        ("1X", "Casa ou empate", "prob_HW_D", "odd_1x"),
        ("X2", "Fora ou empate", "prob_AW_D", "odd_x2"),
        ("12", "Sem empate", "prob_HW_AW", "odd_12"),
        ("O0.5", "Mais de 0,5 gol", "__derived_o05__", "o+0.5"),
        ("O1.5", "Mais de 1,5 gols", "prob_O_1", "o+1.5"),
        ("U1.5", "Menos de 1,5 gols", "prob_U_1", "u+1.5"),
        ("O2.5", "Mais de 2,5 gols", "prob_O", "o+2.5"),
        ("U2.5", "Menos de 2,5 gols", "prob_U", "u+2.5"),
        ("O3.5", "Mais de 3,5 gols", "prob_O_3", "o+3.5"),
        ("U3.5", "Menos de 3,5 gols", "prob_U_3", "u+3.5"),
        ("BTTS_Y", "Ambas marcam — Sim", "prob_bts", "bts_yes"),
        ("BTTS_N", "Ambas marcam — Não", "prob_ots", "bts_no"),
    ]


def _gm_daily_market_family(code):
    code = str(code or "").upper().strip()
    if code in {"1", "2"}:
        return "resultado"
    if code in {"1X", "X2", "12"}:
        return "dupla_chance"
    if code in {"O0.5", "O1.5", "U1.5", "O2.5", "U2.5", "O3.5", "U3.5"}:
        return "gols"
    if code in {"BTTS_Y", "BTTS_N"}:
        return "ambas_marcam"
    return "outro"


def _gm_daily_confidence_band(prob):
    p = float(prob or 0.0)
    if p >= 90: return "Muito alta"
    if p >= 82: return "Alta"
    if p >= 75: return "Boa"
    return "Moderada"

def _gm_daily_pick_bookmaker_rows(odds):
    by_match = {}
    for row in odds or []:
        if isinstance(row, dict):
            mid = str(row.get("match_id") or "").strip()
            if mid: by_match.setdefault(mid, []).append(row)
    return by_match


def _gm_daily_best_odd_row(rows):
    if not rows: return None
    def rank(row):
        b = _gm_daily_norm_bookmaker(row.get("odd_bookmakers") or row.get("bookmaker"))
        pref = 3 if "betano" in b else 2 if "bet365" in b else 1
        return (pref, str(row.get("odd_date") or row.get("updated") or ""))
    return max(rows, key=rank)


def _gm_daily_best_odd_row_for_market(rows, odd_key):
    """V113: escolhe a casa por MERCADO, não uma única linha para a partida inteira.

    Uma casa pode ter 1X2 e não ter O/U/BTTS (ou vice-versa). A lógica antiga
    elegia primeiro uma linha do bookmaker e depois procurava todos os mercados
    nela, descartando preços reais existentes em outras linhas do mesmo jogo.
    """
    available = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        odd = _gm_daily_num(row.get(odd_key))
        if odd is None or odd <= 1.01:
            continue
        available.append(row)
    return _gm_daily_best_odd_row(available)


def _gm_daily_kickoff_at(target_date, time_value):
    """Converte data + horário informado pela agenda em datetime de Brasília."""
    try:
        day = target_date.date() if isinstance(target_date, datetime) else target_date
        if isinstance(day, pd.Timestamp):
            day = day.date()
        if not isinstance(day, date):
            day = pd.to_datetime(day).date()
        label = _gm_daily_time_label(time_value)
        m = re.fullmatch(r"(\d{2}):(\d{2})", label)
        if not m:
            return None
        return datetime(day.year, day.month, day.day, int(m.group(1)), int(m.group(2)), tzinfo=BRASILIA_TZ)
    except Exception:
        return None


def gm_daily_pick_candidates(target_date, cutoff_at=None):
    """Monta candidatos somente entre partidas ainda não iniciadas.

    Quando cutoff_at é informado (geração das oportunidades do dia), qualquer jogo
    com início anterior ao horário de geração + margem operacional fica fora. Isso
    impede publicar Bingo/Matadeira com partidas já encerradas, em andamento ou
    prestes a começar.
    """
    target_iso = target_date.isoformat() if hasattr(target_date, "isoformat") else str(target_date)
    payload = gm_daily_pick_source_payload(target_iso)
    if payload.get("error"):
        return [], {"error": payload.get("error"), "fixtures": len(payload.get("official_fixture_ids") or []), "predictions": len(payload.get("predictions") or []), "odds": len(payload.get("odds") or []), "direct_prediction_hits": int(payload.get("direct_prediction_hits") or 0), "direct_odds_hits": int(payload.get("direct_odds_hits") or 0)}
    allowed_ids = {str(v) for v in (GM_APIFOOTBALL_FIXED_LEAGUE_IDS or {}).values()}
    odd_map = _gm_daily_pick_bookmaker_rows(payload.get("odds") or [])
    fixture_map = {}
    try:
        for fx in load_apifootball_fixtures_for_date(target_date):
            mid_fx = str((fx or {}).get("match_id") or "").strip()
            if mid_fx:
                fixture_map[mid_fx] = {
                    "time": _gm_daily_time_label((fx or {}).get("time")),
                    "status": _gm_api_norm((fx or {}).get("status")),
                }
    except Exception:
        fixture_map = {}

    if cutoff_at is not None and getattr(cutoff_at, "tzinfo", None) is None:
        cutoff_at = cutoff_at.replace(tzinfo=BRASILIA_TZ)

    blocked_status_tokens = {
        "finished", "ft", "after et", "after pen", "cancelled", "canceled",
        "postponed", "abandoned", "awarded", "live", "in play", "inplay",
        "halftime", "half time", "1st half", "2nd half",
    }
    candidates = []
    excluded_past = excluded_status = excluded_unknown_time = 0
    for pred in payload.get("predictions") or []:
        if not isinstance(pred, dict) or str(pred.get("league_id") or "") not in allowed_ids:
            continue
        home, away = str(pred.get("match_hometeam_name") or "").strip(), str(pred.get("match_awayteam_name") or "").strip()
        if not home or not away or not valid_fixture_team(home) or not valid_fixture_team(away):
            continue
        mid = str(pred.get("match_id") or "").strip()
        fx = fixture_map.get(mid) or {}
        status = _gm_api_norm(fx.get("status") or pred.get("match_status") or "")
        if status and any(tok in status for tok in blocked_status_tokens):
            excluded_status += 1
            continue
        time_label = _gm_daily_time_label(fx.get("time") or pred.get("match_time") or "")
        kickoff_at = _gm_daily_kickoff_at(target_date, time_label)
        if cutoff_at is not None:
            if kickoff_at is None:
                excluded_unknown_time += 1
                continue
            if kickoff_at <= cutoff_at:
                excluded_past += 1
                continue

        match_odd_rows = odd_map.get(mid) or []
        if not match_odd_rows:
            continue
        for code, label, pkey, okey in _gm_daily_market_specs():
            # V113: procura o preço real deste mercado em todas as casas disponíveis.
            # Não descarta mais O/U, dupla chance ou BTTS só porque a casa preferida
            # para 1X2 não publicou aquele campo.
            odd_row = _gm_daily_best_odd_row_for_market(match_odd_rows, okey)
            if not odd_row:
                continue
            bookmaker = str(odd_row.get("odd_bookmakers") or odd_row.get("bookmaker") or "Mercado").strip()
            if pkey == "__derived_o05__":
                p = _gm_daily_prob_over_05_from_over15(pred.get("prob_O_1"))
            else:
                p = _gm_daily_num(pred.get(pkey))
            odd = _gm_daily_num(odd_row.get(okey))
            if p is None or odd is None or odd <= 1.01 or p <= 0 or p >= 100:
                continue
            implied = 100.0 / odd
            edge = p - implied
            candidates.append({
                "match_id": mid, "date": target_iso, "time": time_label,
                "kickoff_at": kickoff_at.isoformat() if kickoff_at else None,
                "league_id": str(pred.get("league_id") or ""), "competition": str(pred.get("league_name") or ""),
                "home": home, "away": away, "market_code": code, "market": label,
                "probability": round(p, 2), "odd": round(odd, 3),
                "implied_probability": round(implied, 2), "edge": round(edge, 2), "bookmaker": bookmaker,
                "market_family": _gm_daily_market_family(code),
                "confidence_band": _gm_daily_confidence_band(p),
            })
    return candidates, {
        "error": None, "fixtures": len(payload.get("official_fixture_ids") or []),
        "predictions": len(payload.get("predictions") or []),
        "odds": len(payload.get("odds") or []), "candidates": len(candidates),
        "direct_prediction_hits": int(payload.get("direct_prediction_hits") or 0),
        "direct_odds_hits": int(payload.get("direct_odds_hits") or 0),
        "excluded_past": excluded_past, "excluded_status": excluded_status,
        "excluded_unknown_time": excluded_unknown_time,
        "cutoff_at": cutoff_at.isoformat() if cutoff_at is not None else None,
    }

def _gm_daily_combo_payload(legs, bet_type, pick_kind="dica"):
    legs = _gm_daily_sort_legs(legs)
    total_odd, model_prob, edge_sum = 1.0, 1.0, 0.0
    for leg in legs:
        total_odd *= float(leg["odd"]); model_prob *= float(leg["probability"]) / 100.0; edge_sum += float(leg["edge"])
    if pick_kind == "matadeira": score = model_prob * 100.0 + edge_sum * .75 - abs(total_odd - 1.72) * 10.0
    elif pick_kind == "bingo": score = model_prob * 100.0 + edge_sum * .55 + min(max(total_odd - 4.0, 0.0), 4.0) * .35
    else: score = model_prob * 100.0 + edge_sum * .65 - abs(total_odd - 2.0) * 12.0
    books = [str(x.get("bookmaker") or "") for x in legs if str(x.get("bookmaker") or "").strip()]
    bookmaker = books[0] if books and all(x == books[0] for x in books) else ("Mercado combinado" if books else "Mercado")
    return {"pick_kind": pick_kind, "bet_type": bet_type, "legs": [dict(x) for x in legs], "total_odd": round(total_odd, 3), "model_probability": round(model_prob * 100.0, 2), "score": round(score, 3), "bookmaker": bookmaker}


def _gm_daily_distinct_matches(legs):
    return len({str(x.get("match_id") or "") for x in legs}) == len(legs)


def _gm_daily_high_margin_candidate(candidate, pick_kind, simple_mode=False):
    """Filtro de qualidade v50: probabilidade domina, com preço real obrigatório."""
    try:
        prob = float(candidate.get("probability") or 0.0)
        odd = float(candidate.get("odd") or 0.0)
        edge = float(candidate.get("edge") or -999.0)
    except Exception:
        return False
    code = str(candidate.get("market_code") or "").upper().strip()
    if code not in {"1", "2", "1X", "X2", "12", "O0.5", "O1.5", "U1.5", "O2.5", "U2.5", "O3.5", "U3.5", "BTTS_Y", "BTTS_N"}:
        return False

    # Matadeira e Dica mantêm faixas conservadoras. No Bingo v52 não existe
    # teto rígido de odd por perna: o preço mínimo é 1,15 e o risco é controlado
    # pela probabilidade estimada + edge + penalização progressiva no ranking.
    if pick_kind == "bingo":
        if odd < 1.15:
            return False
    else:
        if simple_mode:
            max_odd = 2.10 if pick_kind == "dica" else 1.89
        else:
            max_odd = 1.55
        if not (1.015 <= odd <= max_odd):
            return False

    # Não exige edge positivo nos mercados mais protegidos, mas evita aceitar uma
    # perna em que o modelo esteja muito abaixo da probabilidade implícita da casa.
    min_edge = -5.0 if code in {"O0.5", "1X", "X2", "12", "U1.5", "U2.5", "U3.5", "BTTS_N"} else -3.0
    if pick_kind == "bingo" and odd >= 1.70:
        min_edge = -2.0
    if pick_kind == "bingo" and odd >= 2.00:
        min_edge = 0.0
    if edge < min_edge:
        return False

    thresholds = {
        "matadeira": {"O0.5": 88.0, "O1.5": 78.0, "U1.5": 82.0, "O2.5": 80.0, "U2.5": 80.0, "O3.5": 82.0, "U3.5": 80.0, "BTTS_Y": 80.0, "BTTS_N": 80.0, "1X": 82.0, "X2": 82.0, "12": 80.0, "1": 75.0, "2": 75.0},
        "dica":      {"O0.5": 86.0, "O1.5": 76.0, "U1.5": 80.0, "O2.5": 78.0, "U2.5": 78.0, "O3.5": 80.0, "U3.5": 78.0, "BTTS_Y": 78.0, "BTTS_N": 78.0, "1X": 80.0, "X2": 80.0, "12": 78.0, "1": 75.0, "2": 75.0},
        "bingo":     {"O0.5": 82.0, "O1.5": 75.0, "U1.5": 76.0, "O2.5": 75.0, "U2.5": 75.0, "O3.5": 76.0, "U3.5": 75.0, "BTTS_Y": 75.0, "BTTS_N": 75.0, "1X": 76.0, "X2": 76.0, "12": 75.0, "1": 75.0, "2": 75.0},
    }
    needed = thresholds.get(pick_kind, thresholds["dica"]).get(code, 101.0)

    # Curva de proteção do Bingo: odds médias podem entrar com ~75–80% de modelo;
    # odds realmente altas só entram quando a sustentação também sobe. Não há teto
    # fixo, mas quanto maior a odd, mais difícil ela passa a ser aceita.
    if pick_kind == "bingo":
        if odd >= 2.20:
            needed = max(needed, 78.0)
        elif odd >= 1.90:
            needed = max(needed, 76.0)
        elif odd >= 1.65:
            needed = max(needed, 74.0)
        elif odd >= 1.40:
            needed = max(needed, 72.0)
        elif odd < 1.25:
            needed = max(needed, 80.0)

    return prob >= needed


def gm_daily_pick_choose(candidates, pick_kind="dica", avoid_matches=None, avoid_legs=None, history_market_counts=None, history_family_counts=None, history_kind_market_counts=None):
    """Escolhe as três oportunidades com diversidade controlada e qualidade.

    v50:
    - permite vitória simples de favorito quando o modelo sustenta a leitura;
    - mistura famílias resultado / dupla chance / gols quando houver alternativas;
    - usa sorteio determinístico mínimo apenas para desempatar opções quase iguais;
    - evita repetir os mesmos jogos/mercados entre Matadeira, Dica e Bingo;
    - tenta simples antes da múltipla quando ela já cai exatamente na faixa-alvo.
    """
    avoid_matches = set(str(x) for x in (avoid_matches or []) if str(x))
    avoid_legs = set(tuple(x) if isinstance(x, (list, tuple)) else x for x in (avoid_legs or []))
    history_market_counts = dict(history_market_counts or {})
    history_family_counts = dict(history_family_counts or {})
    history_kind_market_counts = dict(history_kind_market_counts or {})
    raw = [dict(c) for c in (candidates or [])]

    def leg_sig(c):
        return (str(c.get("match_id") or ""), str(c.get("market_code") or ""))

    def deterministic_jitter(c):
        seed = f"{c.get('date')}|{c.get('match_id')}|{c.get('market_code')}|{pick_kind}".encode()
        return (int(hashlib.sha256(seed).hexdigest()[:8], 16) % 1000) / 1000.0

    def leg_rank(c):
        odd = float(c.get("odd") or 0.0); prob = float(c.get("probability") or 0.0); edge = float(c.get("edge") or 0.0)
        code = str(c.get("market_code") or "")
        family = _gm_daily_market_family(code)
        family_bonus = {"gols":2.4,"resultado":2.0,"dupla_chance":1.5}.get(family,0.0)
        edge_component = max(-5.0, min(8.0, edge)) * 0.25
        price_penalty = max(0.0, odd - 1.35) * 10.0
        repeat_penalty = 3.5 if str(c.get("match_id") or "") in avoid_matches else 0.0
        # v53: rotação histórica. Mercados muito usados nos últimos dias perdem
        # prioridade, sobretudo quando foram repetidos na mesma categoria. Isso não
        # torna um mercado ruim elegível: a penalização só ordena candidatos que já
        # passaram pelos filtros de qualidade.
        hist_market_penalty = min(8.0, float(history_market_counts.get(code, 0.0)) * 0.55)
        hist_family_penalty = min(5.0, float(history_family_counts.get(family, 0.0)) * 0.28)
        hist_kind_penalty = min(7.0, float(history_kind_market_counts.get((pick_kind, code), 0.0)) * 0.70)
        return prob + family_bonus + edge_component - price_penalty - repeat_penalty - hist_market_penalty - hist_family_penalty - hist_kind_penalty + deterministic_jitter(c) * 0.35

    # 1) Primeiro tenta uma seleção simples excelente dentro da faixa final.
    if pick_kind in {"matadeira", "dica"}:
        lo, hi = ((1.50, 1.89) if pick_kind == "matadeira" else (1.90, 2.10))
        simple_pool = [c for c in raw if _gm_daily_high_margin_candidate(c, pick_kind, simple_mode=True) and lo <= float(c.get("odd") or 0) <= hi and leg_sig(c) not in avoid_legs]
        if simple_pool:
            simple_pool.sort(key=leg_rank, reverse=True)
            top = simple_pool[0]
            # Para uma simples de odd maior, exige sustentação adicional.
            min_simple_prob = 74.0 if pick_kind == "matadeira" else 69.0
            if float(top.get("probability") or 0) >= min_simple_prob:
                return _gm_daily_combo_payload([top], "simple", pick_kind)

    candidates = [c for c in raw if _gm_daily_high_margin_candidate(c, pick_kind, simple_mode=False) and leg_sig(c) not in avoid_legs]
    if not candidates:
        return None

    # Primeira tentativa evita jogos já usados por outra oportunidade do dia.
    primary = [c for c in candidates if str(c.get("match_id") or "") not in avoid_matches]
    candidate_sets = [primary, candidates] if primary else [candidates]

    def combo_rank(legs, total_odd, kind):
        probs=[float(x.get("probability") or 0) for x in legs]; odds=[float(x.get("odd") or 0) for x in legs]; edges=[float(x.get("edge") or 0) for x in legs]
        model_prob=1.0
        for p in probs: model_prob*=p/100.0
        families=[_gm_daily_market_family(x.get("market_code")) for x in legs]
        fam_count=len(set(families)); codes={str(x.get("market_code") or "") for x in legs}
        diversity_bonus=max(0,fam_count-1)*2.4 + max(0,len(codes)-1)*0.75
        concentration_penalty=max(families.count(f)-2 for f in set(families))*3.4 if families else 0.0
        code_counts={c:sum(1 for x in legs if str(x.get("market_code") or "")==c) for c in codes}
        exact_repeat_penalty=sum(max(0,n-1)*1.8 for n in code_counts.values())
        history_combo_penalty=sum(min(3.0,float(history_market_counts.get(str(x.get("market_code") or ""),0.0))*0.18) for x in legs)
        target=1.72 if kind=="matadeira" else 2.00 if kind=="dica" else 4.50
        distance=0.0 if kind=="bingo" else abs(total_odd-target)*4.0

        # v52 Bingo: evita bilhete formado quase todo por 1,15–1,24 e premia mistura
        # de preços. A ideia é intercalar pernas muito fortes com algumas odds médias,
        # sem transformar uma perna alta em atalho para inflar o acumulado.
        odd_mix_bonus = 0.0
        odd_risk_penalty = 0.0
        if kind == "bingo" and odds:
            very_low = sum(1 for o in odds if o < 1.25)
            mid = sum(1 for o in odds if 1.25 <= o < 1.60)
            upper = sum(1 for o in odds if o >= 1.60)
            if very_low and mid:
                odd_mix_bonus += 2.2
            if mid >= 2:
                odd_mix_bonus += 1.8
            if upper >= 1:
                odd_mix_bonus += 1.0
            if very_low > max(1, len(odds)//2):
                odd_risk_penalty += (very_low - max(1, len(odds)//2)) * 2.8
            odd_risk_penalty += sum(max(0.0, o-1.85) * 5.0 for o in odds)

        return model_prob*100 + min(probs)*0.78 + sum(probs)/len(probs)*0.14 + sum(max(-2,min(6,e)) for e in edges)*0.06 + diversity_bonus + odd_mix_bonus - concentration_penalty - exact_repeat_penalty - history_combo_penalty - odd_risk_penalty - distance - max(0,total_odd-8.0)*(0.65 if kind=="bingo" else 0)

    def valid_diversity(legs, kind):
        families=[_gm_daily_market_family(x.get("market_code")) for x in legs]
        # Matadeira e Dica mantêm diversidade como regra dura. No Bingo ela vira
        # preferência de score: em uma grade forte não deixamos a múltipla sumir
        # apenas porque as melhores pernas do dia pertencem à mesma família.
        codes=[str(x.get("market_code") or "") for x in legs]
        if kind == "bingo":
            # v54: diversidade no Bingo passa a ser preferência forte de score, não
            # trava eliminatória. Assim o motor não deixa de publicar em uma grade
            # boa apenas porque os melhores candidatos se concentram em 1–2 mercados.
            # Repetições continuam penalizadas em combo_rank e na rotação histórica.
            return True
        if len(legs) >= 3 and len(set(families)) < 2:
            return False
        max_same = 2
        return all(families.count(f) <= max_same for f in set(families))

    def beam_combo(base, min_legs, max_legs, min_odd, max_odd, kind):
        if len(base) < min_legs: return None
        # Até 3 alternativas por jogo para realmente permitir diversidade de mercado.
        by_match={}
        for c in sorted(base,key=leg_rank,reverse=True):
            bucket=by_match.setdefault(str(c.get("match_id") or ""),[])
            if len(bucket)<3: bucket.append(c)
        pool=[c for b in by_match.values() for c in b][:72]
        # V142 PERFORMANCE: cada combinação é expandida em ordem canônica do pool.
        # Antes, o beam reconstruía a mesma combinação em várias permutações (A+B,
        # B+A, A+C+B...), multiplicando CPU sem mudar nenhuma regra estatística.
        # last_idx elimina essas permutações: thresholds, odds, probabilidades,
        # diversidade e score permanecem exatamente os mesmos.
        # V145 PERFORMANCE: mantém os mesmos filtros, faixas de odds, probabilidades
        # e score, mas evita trabalho Python repetido dentro do beam. Metadados puros
        # de cada perna são pré-calculados uma vez e os estados carregam tuplas/frozenset.
        # A largura continua 650: não reduzimos a profundidade nem os critérios oficiais.
        pool_meta = []
        for c in pool:
            pool_meta.append((
                c,
                str(c.get("match_id") or ""),
                float(c.get("odd") or 1.0),
                _gm_daily_market_family(c.get("market_code")),
                str(c.get("market_code") or ""),
            ))
        states=[(tuple(),1.0,frozenset(),0.0,-1)]; best=None; beam_width=650
        for size in range(1,max_legs+1):
            expanded=[]
            for legs_idx,total,used,_,last_idx in states:
                for idx in range(last_idx+1, len(pool_meta)):
                    c,mid,c_odd,_,_ = pool_meta[idx]
                    if not mid or mid in used:
                        continue
                    no=total*c_odd
                    if max_odd is not None and no > max_odd*1.06:
                        continue
                    nl_idx=legs_idx+(idx,)
                    nl=[pool_meta[i][0] for i in nl_idx]
                    nu=used.union((mid,))
                    rank=combo_rank(nl,no,kind)
                    expanded.append((nl_idx,no,nu,rank,idx))
                    if size>=min_legs and no>=min_odd and (max_odd is None or no<=max_odd) and valid_diversity(nl,kind):
                        combo=_gm_daily_combo_payload(nl,"double" if size==2 else "triple" if size==3 else "multiple",kind)
                        combo["score"]=round(rank,3)
                        combo["model_meta_bingo"]={"strategy":"quality_fallback_bingo_v54","leg_count":len(nl),"markets":sorted({str(x.get('market_code') or '') for x in nl}),"families":sorted(set(_gm_daily_market_family(x.get('market_code')) for x in nl)),"min_leg_probability":round(min(float(x.get('probability') or 0) for x in nl),2),"min_leg_odd":round(min(float(x.get('odd') or 0) for x in nl),3),"max_leg_odd":round(max(float(x.get('odd') or 0) for x in nl),3),"avg_leg_odd":round(sum(float(x.get('odd') or 0) for x in nl)/len(nl),3)}
                        if best is None or combo["score"]>best["score"]:
                            best=combo
            if not expanded:
                break
            # Mesma chave lógica de deduplicação da V144, calculada sem reconstruir
            # dicionários/sets repetidamente.
            uniq={}
            for state in expanded:
                legs_idx,total,used,rank,last_idx=state
                fam_profile=tuple(sorted(pool_meta[i][3] for i in legs_idx))
                key=(tuple(sorted(used)),fam_profile)
                if key not in uniq or rank>uniq[key][3]:
                    uniq[key]=state
            states=sorted(uniq.values(),key=lambda x:x[3],reverse=True)[:beam_width]
        return best

    params = {
        "matadeira": (2,6,1.50,1.89),
        "dica": (2,8,1.90,2.10),
        "bingo": (4,10,3.50,None),
    }[pick_kind]
    for base in candidate_sets:
        result=beam_combo(base,*params,pick_kind)
        if result is not None: return result
    return None

def gm_daily_pick_candidate_options(candidates, pick_kind="dica", limit=5, history_market_counts=None, history_family_counts=None, history_kind_market_counts=None, initial_avoid_legs=None):
    """Gera várias opções privadas para avaliação do ADM, sem publicação automática."""
    base = [dict(c) for c in (candidates or []) if float(c.get("probability") or 0.0) >= 75.0]
    options = []
    used_legs = set(initial_avoid_legs or set())
    seen = set()
    for _ in range(max(1, min(int(limit), 8))):
        chosen = gm_daily_pick_choose(
            base,
            pick_kind,
            avoid_matches=set(),
            avoid_legs=used_legs,
            history_market_counts=history_market_counts,
            history_family_counts=history_family_counts,
            history_kind_market_counts=history_kind_market_counts,
        )
        if not chosen:
            break
        sig = tuple(sorted((str(x.get("match_id") or ""), str(x.get("market_code") or "")) for x in chosen.get("legs") or []))
        if not sig or sig in seen:
            break
        seen.add(sig)
        options.append(chosen)
        used_legs.update(sig)
    return options


def gm_daily_pick_prepare_admin_options(force_refresh=False, per_kind=1):
    """Prepara opções do dia para o ADM sem gravar candidatos em gm_daily_picks."""
    _perf_started = time.perf_counter()
    # V125: evita round-trip de autenticação em cada ação administrativa.
    profile = gm_auth_get_profile(force=False)
    if (profile or {}).get("role") != "admin":
        return {"ok": False, "reason": "admin_only"}
    today = datetime.now(BRASILIA_TZ).date()
    if today < GM_DAILY_PICK_RESET_DATE:
        return {"ok": False, "reason": "reset_window", "options": {}, "rows": []}
    recent_rows = gm_daily_pick_recent(100)
    existing_rows = [r for r in recent_rows if str(r.get("pick_date") or "") == today.isoformat()]
    # V124: uma categoria pode ter várias publicações no mesmo dia.
    # Publicações existentes não encerram mais a busca por novas alternativas.
    missing = ["matadeira", "dica", "bingo"]
    if force_refresh:
        try:
            gm_daily_pick_source_payload.clear()
        except Exception:
            pass
        _gm_daily_pick_disk_cache_clear(today.isoformat())

    cutoff_at = datetime.now(BRASILIA_TZ) + timedelta(minutes=10)
    _source_started = time.perf_counter()

    # V148: a base de candidatos também é persistida por 30 min. O horário de
    # corte é reaplicado abaixo, portanto jogos que ficaram próximos do início
    # não são reutilizados indevidamente.
    cached_candidate_pack = None if force_refresh else _gm_daily_pick_disk_cache_load(today.isoformat(), "candidates")
    if cached_candidate_pack:
        raw_candidates = [dict(c) for c in (cached_candidate_pack.get("candidates") or []) if isinstance(c, dict)]
        cutoff_iso = cutoff_at.isoformat()
        candidates = []
        for c in raw_candidates:
            try:
                ko = datetime.fromisoformat(str(c.get("kickoff_at") or ""))
            except Exception:
                ko = None
            if ko is not None and ko > cutoff_at:
                candidates.append(c)
        meta = dict(cached_candidate_pack.get("meta") or {})
        meta["persistent_candidate_cache_hit"] = True
        meta["cutoff_at"] = cutoff_iso
        meta["candidates"] = len(candidates)
    else:
        candidates, meta = gm_daily_pick_candidates(today, cutoff_at=cutoff_at)
        if not (meta or {}).get("error"):
            _gm_daily_pick_disk_cache_save(
                today.isoformat(),
                {"candidates": candidates, "meta": meta},
                "candidates",
            )
    _source_seconds = time.perf_counter() - _source_started
    if meta.get("error"):
        return {"ok": False, "reason": "source_error", "meta": meta}
    history_market_counts, history_family_counts, history_kind_market_counts = _gm_daily_recent_market_rotation(recent_rows, today)
    published_legs = set()
    for row in existing_rows:
        for leg in (row.get("legs") or []):
            mid = str((leg or {}).get("match_id") or "").strip()
            code = str((leg or {}).get("market_code") or "").strip()
            if mid and code:
                published_legs.add((mid, code))
    # V127: descartes são persistentes. Gera uma fila mais profunda para que, ao
    # descartar uma bet, novas alternativas possam ocupar seu lugar sem ela reaparecer.
    discarded_signatures = _gm_daily_pick_load_discarded(today, force=force_refresh)
    options = {}
    for kind in missing:
        discarded_kind_count = sum(1 for sig in discarded_signatures if sig)
        raw_limit = min(30, max(per_kind, per_kind + discarded_kind_count))
        generated = gm_daily_pick_candidate_options(
            candidates,
            kind,
            limit=raw_limit,
            history_market_counts=history_market_counts,
            history_family_counts=history_family_counts,
            history_kind_market_counts=history_kind_market_counts,
            initial_avoid_legs=published_legs,
        )
        options[kind] = [o for o in generated if _gm_daily_pick_option_signature(o) not in discarded_signatures][:per_kind]
    meta = dict(meta or {})
    meta["perf_source_seconds"] = round(_source_seconds, 3)
    meta["perf_total_seconds"] = round(time.perf_counter() - _perf_started, 3)
    meta["perf_options_per_kind"] = int(per_kind)
    # V144: preserva a base já coletada/analisada na sessão ADM. Assim, pedir mais
    # opções não repete as consultas de odds/previsões das partidas do dia.
    return {"ok": True, "reason": "prepared", "options": options, "meta": meta, "rows": existing_rows, "candidates": candidates}


def gm_daily_pick_expand_cached_admin_options(prepared, per_kind=5):
    """V148: acrescenta novas opções sobre a base pronta, sem refazer coleta pesada."""
    _perf_started = time.perf_counter()
    profile = gm_auth_get_profile(force=False)
    if (profile or {}).get("role") != "admin":
        return {"ok": False, "reason": "admin_only"}
    if not isinstance(prepared, dict) or not prepared.get("ok"):
        return {"ok": False, "reason": "cache_unavailable"}

    candidates = [dict(c) for c in (prepared.get("candidates") or []) if isinstance(c, dict)]
    if not candidates:
        return {"ok": False, "reason": "cache_unavailable"}

    today = datetime.now(BRASILIA_TZ).date()
    recent_rows = gm_daily_pick_recent(100)
    existing_rows = [r for r in recent_rows if str(r.get("pick_date") or "") == today.isoformat()]
    history_market_counts, history_family_counts, history_kind_market_counts = _gm_daily_recent_market_rotation(recent_rows, today)

    published_legs = set()
    for row in existing_rows:
        for leg in (row.get("legs") or []):
            mid = str((leg or {}).get("match_id") or "").strip()
            code = str((leg or {}).get("market_code") or "").strip()
            if mid and code:
                published_legs.add((mid, code))

    discarded_signatures = _gm_daily_pick_load_discarded(today, force=False)
    current_options = prepared.get("options") or {}
    options = {}

    # Em vez de recalcular até 5 combinações por categoria, cada clique acrescenta
    # no máximo UMA nova alternativa por categoria. Isso mantém os mesmos filtros
    # estatísticos e torna "Carregar mais" progressivo e muito mais leve.
    for kind in ("matadeira", "dica", "bingo"):
        existing_opts = [
            dict(o) for o in (current_options.get(kind) or [])
            if isinstance(o, dict) and _gm_daily_pick_option_signature(o) not in discarded_signatures
        ]
        avoid_legs = set(published_legs)
        for opt in existing_opts:
            for leg in (opt.get("legs") or []):
                mid = str((leg or {}).get("match_id") or "").strip()
                code = str((leg or {}).get("market_code") or "").strip()
                if mid and code:
                    avoid_legs.add((mid, code))

        generated = gm_daily_pick_candidate_options(
            candidates,
            kind,
            limit=1,
            history_market_counts=history_market_counts,
            history_family_counts=history_family_counts,
            history_kind_market_counts=history_kind_market_counts,
            initial_avoid_legs=avoid_legs,
        )
        for opt in generated:
            sig = _gm_daily_pick_option_signature(opt)
            if sig and sig not in discarded_signatures and all(
                _gm_daily_pick_option_signature(x) != sig for x in existing_opts
            ):
                existing_opts.append(opt)
        options[kind] = existing_opts

    meta = dict(prepared.get("meta") or {})
    meta["perf_expand_seconds"] = round(time.perf_counter() - _perf_started, 3)
    meta["progressive_expand_v148"] = True
    return {
        "ok": True,
        "reason": "expanded_cached",
        "options": options,
        "meta": meta,
        "rows": existing_rows,
        "candidates": candidates,
    }

def gm_daily_pick_publish_selected(choice):
    """Publica somente uma alternativa explicitamente aprovada pelo administrador."""
    # V125: a sessão ADM já foi validada; não força nova consulta ao Supabase ao publicar.
    profile = gm_auth_get_profile(force=False)
    if (profile or {}).get("role") != "admin":
        return {"ok": False, "reason": "admin_only"}
    if not isinstance(choice, dict):
        return {"ok": False, "reason": "invalid_choice"}
    today = datetime.now(BRASILIA_TZ).date()
    if today < GM_DAILY_PICK_RESET_DATE:
        return {"ok": False, "reason": "reset_window"}
    pick_kind = str(choice.get("pick_kind") or "").strip()
    if pick_kind not in {"matadeira", "dica", "bingo"}:
        return {"ok": False, "reason": "invalid_kind"}
    legs = [dict(x) for x in (choice.get("legs") or []) if isinstance(x, dict)]
    if not legs or any(float(x.get("probability") or 0.0) < 75.0 for x in legs):
        return {"ok": False, "reason": "probability_floor"}
    # V124: não bloqueia uma segunda publicação da mesma categoria no mesmo dia.
    # A identidade de cada aposta permanece no registro individual retornado pela RPC.
    direct_bet_url = _gm_daily_valid_direct_bet_url(choice.get("direct_bet_url"))
    if choice.get("direct_bet_url") and not direct_bet_url:
        return {"ok": False, "reason": "invalid_bet_link"}
    payload = {
        "p_pick_date": today.isoformat(),
        "p_pick_kind": pick_kind,
        "p_status": "pending",
        "p_bet_type": str(choice.get("bet_type") or "none"),
        "p_total_odd": choice.get("total_odd"),
        "p_bookmaker": None,
        "p_bookmaker_url": direct_bet_url,
        "p_legs": legs,
        "p_model_meta": {
            "build": GM_BUILD,
            "pick_kind": pick_kind,
            "approval_flow": "admin_manual_v57",
            "approved_at": datetime.now(BRASILIA_TZ).isoformat(),
            "model_probability": choice.get("model_probability"),
            "score": choice.get("score"),
            "direct_bet_url": direct_bet_url,
            **(choice.get("model_meta_bingo") or {}),
        },
    }
    data = gm_daily_pick_rpc("gm_daily_pick_publish_v2", payload)
    st.session_state.pop("_gm_daily_pick_recent_cache", None)
    return {"ok": True, "reason": "published", "data": data}


def gm_daily_pick_recent(limit=80):
    """V150: leitura oficial com cache curto por sessão para evitar RPC em toda navegação."""
    _limit = max(1, min(int(limit), 100))
    _now = time.time()
    _cache = st.session_state.get("_gm_daily_pick_recent_cache")
    if isinstance(_cache, dict) and int(_cache.get("limit") or 0) >= _limit and (_now - float(_cache.get("at") or 0)) < 30:
        rows = list(_cache.get("rows") or [])[:_limit]
    else:
        rows = gm_daily_pick_rpc("gm_daily_pick_recent_v2", {"p_limit": _limit}) or []
        st.session_state["_gm_daily_pick_recent_cache"] = {"at": _now, "limit": _limit, "rows": list(rows)}
    clean = []
    seen_ids = set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        kind = str(r.get("pick_kind") or "dica")
        if kind not in {"matadeira", "dica", "bingo"}:
            continue
        try:
            pick_day = datetime.fromisoformat(str(r.get("pick_date") or "")).date()
        except Exception:
            continue
        if pick_day < GM_DAILY_PICK_RESET_DATE:
            continue
        row_id = str(r.get("id") or "").strip()
        if row_id:
            if row_id in seen_ids:
                continue
            seen_ids.add(row_id)
        clean.append(r)
    return clean

def _gm_daily_recent_market_rotation(rows, today):
    """Conta uso recente de mercados com peso por recência para incentivar rotação."""
    market_counts, family_counts, kind_market_counts = {}, {}, {}
    for row in rows or []:
        try:
            d = datetime.fromisoformat(str(row.get("pick_date") or "")).date()
        except Exception:
            continue
        age = (today - d).days
        if age <= 0 or age > 10:
            continue
        weight = 4.0 if age == 1 else 3.0 if age == 2 else 2.0 if age <= 5 else 1.0
        kind = str(row.get("pick_kind") or "dica")
        for leg in (row.get("legs") or []):
            code = str((leg or {}).get("market_code") or "").strip()
            if not code:
                continue
            fam = _gm_daily_market_family(code)
            market_counts[code] = market_counts.get(code, 0.0) + weight
            family_counts[fam] = family_counts.get(fam, 0.0) + weight
            key = (kind, code)
            kind_market_counts[key] = kind_market_counts.get(key, 0.0) + weight
    return market_counts, family_counts, kind_market_counts


def gm_daily_pick_publish_today(force_refresh=False):
    profile = gm_auth_get_profile(force=True)
    if (profile or {}).get("role") != "admin": return {"ok": False, "reason": "admin_only"}
    today = datetime.now(BRASILIA_TZ).date()
    if today < GM_DAILY_PICK_RESET_DATE: return {"ok": False, "reason": "reset_window"}
    recent_rows = gm_daily_pick_recent(100); existing_rows=[r for r in recent_rows if str(r.get("pick_date") or "") == today.isoformat()]
    history_market_counts, history_family_counts, history_kind_market_counts = _gm_daily_recent_market_rotation(recent_rows, today)
    existing_kinds={str(r.get("pick_kind") or "dica") for r in existing_rows}; missing=[k for k in ("matadeira","dica","bingo") if k not in existing_kinds]
    if not missing: return {"ok": True, "reason": "exists", "rows": existing_rows}
    if force_refresh:
        try: gm_daily_pick_source_payload.clear()
        except Exception: pass
    cutoff_at = datetime.now(BRASILIA_TZ) + timedelta(minutes=10)
    candidates, meta=gm_daily_pick_candidates(today, cutoff_at=cutoff_at)
    if meta.get("error"): return {"ok":False,"reason":"source_error","meta":meta}
    published=[]
    used_matches=set(); used_legs=set()
    # Também respeita oportunidades já existentes hoje para reduzir repetição.
    for row in existing_rows:
        for leg in (row.get("legs") or []):
            mid=str((leg or {}).get("match_id") or "").strip(); code=str((leg or {}).get("market_code") or "").strip()
            if mid: used_matches.add(mid)
            if mid and code: used_legs.add((mid,code))
    for pick_kind in missing:
        chosen=gm_daily_pick_choose(candidates,pick_kind,avoid_matches=used_matches,avoid_legs=used_legs,history_market_counts=history_market_counts,history_family_counts=history_family_counts,history_kind_market_counts=history_kind_market_counts)
        if chosen is None:
            payload={"p_pick_date":today.isoformat(),"p_pick_kind":pick_kind,"p_status":"no_pick","p_bet_type":"none","p_total_odd":None,"p_bookmaker":None,"p_bookmaker_url":None,"p_legs":[],"p_model_meta":{**meta,"build":GM_BUILD,"pick_kind":pick_kind,"reason":"quality_filter"}}
        else:
            payload={"p_pick_date":today.isoformat(),"p_pick_kind":pick_kind,"p_status":"pending","p_bet_type":chosen["bet_type"],"p_total_odd":chosen["total_odd"],"p_bookmaker":chosen.get("bookmaker"),"p_bookmaker_url":None,"p_legs":chosen["legs"],"p_model_meta":{**meta,"build":GM_BUILD,"pick_kind":pick_kind,"model_probability":chosen["model_probability"],"score":chosen["score"],**(chosen.get("model_meta_bingo") or {})}}
        data=gm_daily_pick_rpc("gm_daily_pick_publish_v2",payload); published.append({"pick_kind":pick_kind,"chosen":chosen,"data":data})
        if chosen:
            for leg in chosen.get("legs") or []:
                mid=str(leg.get("match_id") or "").strip(); code=str(leg.get("market_code") or "").strip()
                if mid: used_matches.add(mid)
                if mid and code: used_legs.add((mid,code))
    return {"ok":True,"reason":"published","published":published,"meta":meta}


def _gm_daily_event_finished(ev): return _gm_api_norm((ev or {}).get("match_status")) in {"finished","ft","after et","after pen"}
def _gm_daily_event_void(ev): return _gm_api_norm((ev or {}).get("match_status")) in {"cancelled","canceled","postponed","abandoned","awarded"}


def _gm_daily_evaluate_leg(leg, ev):
    if _gm_daily_event_void(ev): return "void"
    if not _gm_daily_event_finished(ev): return "pending"
    hs=_gm_daily_num(ev.get("match_hometeam_ft_score") or ev.get("match_hometeam_score")); aas=_gm_daily_num(ev.get("match_awayteam_ft_score") or ev.get("match_awayteam_score"))
    if hs is None or aas is None: return "pending"
    code=str(leg.get("market_code") or ""); total=hs+aas
    if code=="1": win=hs>aas
    elif code=="X": win=hs==aas
    elif code=="2": win=aas>hs
    elif code=="1X": win=hs>=aas
    elif code=="X2": win=aas>=hs
    elif code=="12": win=hs!=aas
    elif code=="O0.5": win=total>0.5
    elif code=="O1.5": win=total>1.5
    elif code=="U1.5": win=total<1.5
    elif code=="O2.5": win=total>2.5
    elif code=="U2.5": win=total<2.5
    elif code=="O3.5": win=total>3.5
    elif code=="U3.5": win=total<3.5
    elif code=="BTTS_Y": win=hs>0 and aas>0
    elif code=="BTTS_N": win=not(hs>0 and aas>0)
    else: return "pending"
    return "green" if win else "red"


def _gm_daily_pick_settle_exact(row, final, detail=None):
    """V152: liquida uma publicação específica pelo id."""
    pick_id = row.get("id")
    if not pick_id:
        raise RuntimeError("Publicação sem id.")
    data = gm_daily_pick_rpc("gm_daily_pick_settle_by_id_v3", {
        "p_pick_id": pick_id,
        "p_status": str(final),
        "p_result_summary": {"legs": detail or [], "settled_by_build": GM_BUILD},
    })
    st.session_state.pop("_gm_daily_pick_recent_cache", None)
    return data


def gm_daily_pick_settle_pending(limit=80):
    try:
        profile = gm_auth_get_profile()
    except Exception:
        profile = None
    if (profile or {}).get("role") != "admin":
        return {"checked": 0, "settled": 0, "pending": 0, "errors": []}

    st.session_state.pop("_gm_daily_pick_recent_cache", None)
    pending_rows = [r for r in gm_daily_pick_recent(limit) if str(r.get("status") or "") == "pending"]
    checked = settled = 0
    errors = []

    for row in pending_rows:
        legs = row.get("legs") or []
        if not isinstance(legs, list) or not legs:
            continue
        leg_results, detail = [], []
        for leg in legs:
            mid = str((leg or {}).get("match_id") or "").strip()
            if not mid:
                leg_results.append("pending")
                detail.append({"match_id": "", "result": "pending", "reason": "missing_match_id"})
                continue
            try:
                events, err = gm_apifootball_request("get_events", match_id=mid, timezone="America/Sao_Paulo")
                ev = events[0] if not err and isinstance(events, list) and events else None
                result = _gm_daily_evaluate_leg(leg, ev or {})
                leg_results.append(result)
                detail.append({
                    "match_id": mid, "result": result,
                    "status": None if not ev else ev.get("match_status"),
                    "score": None if not ev else f"{ev.get('match_hometeam_score','')}–{ev.get('match_awayteam_score','')}",
                })
            except Exception as exc:
                leg_results.append("pending")
                detail.append({"match_id": mid, "result": "pending", "reason": type(exc).__name__})

        checked += 1
        if "red" in leg_results:
            final = "red"
        elif leg_results and all(x == "green" for x in leg_results):
            final = "green"
        elif leg_results and all(x in {"green", "void"} for x in leg_results) and "void" in leg_results:
            final = "void"
        else:
            continue

        try:
            _gm_daily_pick_settle_exact(row, final, detail)
            settled += 1
        except Exception as exc:
            errors.append(f"{row.get('id')}: {type(exc).__name__}")

    st.session_state.pop("_gm_daily_pick_recent_cache", None)
    return {"checked": checked, "settled": settled, "pending": max(0, checked-settled), "errors": errors}

def _gm_daily_status_badge(status): return {"green":"🟢 GREEN","red":"🔴 RED","void":"⚪ VOID","pending":"🟡 PENDENTE","no_pick":"⚫ SEM SELEÇÃO"}.get(str(status),"—")
def _gm_daily_bet_type_label(value, legs_count=0): return f"Múltipla ({int(legs_count or 0)} jogos)" if str(value)=="multiple" else {"simple":"Simples","double":"Dupla","triple":"Tripla","none":"Sem seleção"}.get(str(value),str(value or "—").title())


def _gm_daily_pick_card(row, target_date, pick_kind, is_admin=False):
    profile=GM_DAILY_PICK_PROFILES[pick_kind]
    display_label = profile["label"] if (is_admin or pick_kind != "dica") else "📊 Dica Principal"
    if not row: st.info(f"{display_label}: ainda não preparada para este dia."); return
    if str(row.get("status"))=="no_pick": st.info(f"{display_label}: hoje não houve combinação que atingisse os critérios de qualidade. Nenhuma aposta foi forçada."); return
    legs=_gm_daily_sort_legs(row.get("legs") or []); total_odd=_gm_daily_num(row.get("total_odd")) or 0.0; btype=_gm_daily_bet_type_label(row.get("bet_type"),len(legs))
    bookmaker=str(row.get("bookmaker") or "").strip()
    title_meta=" • ".join(x for x in (bookmaker, profile.get("description")) if x)
    title_suffix=f'<span class="gm-pick-muted" style="font-weight:600;margin-left:.45rem">{html.escape(title_meta)}</span>' if title_meta else ""
    card=[f'<div class="gm-pick-card gm-kind-{pick_kind}"><div class="gm-pick-head"><div><div class="gm-pick-title">{html.escape(display_label)}{title_suffix}</div><div class="gm-pick-muted">{target_date:%d/%m/%Y} • {html.escape(btype)} • {_gm_daily_status_badge(row.get("status"))}</div></div><div><div class="gm-pick-muted">ODD TOTAL</div><div class="gm-pick-odd">{total_odd:.2f}</div></div></div>']
    for leg in legs:
        leg_odd=_gm_daily_num(leg.get("odd")) or 0.0; time_label=_gm_daily_time_label(leg.get("time"))
        prob=_gm_daily_num(leg.get("probability")); conf=str(leg.get("confidence_band") or (_gm_daily_confidence_band(prob) if prob is not None else ""))
        prob_txt=(f" • prob. estimada {prob:.0f}% • {html.escape(conf)}" if prob is not None else "")
        leg_comp = str(leg.get("competition") or "")
        game_text = f"{str(leg.get('home') or '')} × {str(leg.get('away') or '')}"
        card.append(f'<div class="gm-pick-leg"><div class="gm-pick-market">{html.escape(str(leg.get("market") or ""))}<span style="float:right">{leg_odd:.2f}</span></div><div class="gm-pick-muted">🕒 {html.escape(time_label)} • ⚽ {html.escape(game_text)} • {html.escape(leg_comp)}{prob_txt}</div></div>')
    card.append('</div>'); st.markdown("".join(card),unsafe_allow_html=True)
    direct_url = _gm_daily_row_direct_bet_url(row)
    if direct_url:
        safe_url = html.escape(direct_url, quote=True)
        st.markdown(f'''<a class="gm-score-bet-cta" href="{safe_url}" target="_blank" rel="noopener noreferrer"><span class="gm-score-bet-target">🎯</span><span><strong>Ir para a aposta</strong><small>GM SCORE</small></span><span class="gm-score-bet-arrow">›</span></a>''', unsafe_allow_html=True)


def gm_render_daily_pick_page():
    st.markdown("## 💡 Dicas do Dia")
    try:
        profile=gm_auth_get_profile()
    except Exception:
        profile=None
    # V75: esta aba é sempre a experiência do cliente, inclusive quando acessada pelo ADM.
    # Toda função administrativa fica isolada na Central Administrativa, aberta pela aba Conta.
    is_admin=False
    st.markdown('''<div class="gm-risk-rule"><span class="gm-risk-green">QUANTO MAIOR A ODD</span><span class="gm-risk-arrow">→</span><span class="gm-risk-red">MENORES AS CHANCES</span></div>''',unsafe_allow_html=True)

    if is_admin:
        try:
            gm_daily_pick_settle_pending(limit=40)
        except Exception:
            pass

    try:
        rows=gm_daily_pick_recent(100)
    except Exception:
        st.warning("As Oportunidades GM ainda não estão ativas nesta instalação.")
        if is_admin:
            st.caption("Confirme que as RPCs v2 das Oportunidades do Dia estão ativas no Supabase.")
        return

    today=datetime.now(BRASILIA_TZ).date(); yesterday=today-timedelta(days=1)
    if today < GM_DAILY_PICK_RESET_DATE:
        st.info(f"Novo ciclo das Dicas do Dia começa em {GM_DAILY_PICK_RESET_DATE:%d/%m/%Y}. O histórico anterior foi encerrado.")
        return
    def lookup_all(day,kind):
        return [r for r in rows if str(r.get("pick_date") or "")==day.isoformat() and str(r.get("pick_kind") or "dica")==kind]

    def lookup(day,kind):
        matches = lookup_all(day, kind)
        return matches[0] if matches else None

    if is_admin:
        st.markdown("### 🧑‍💼 Aprovação do administrador")
        st.caption("Estas opções são privadas. Nada abaixo fica visível aos clientes até você tocar em **Aprovar e publicar**.")
        cache_key=f"gm_daily_admin_options_{today.isoformat()}"
        if cache_key not in st.session_state:
            try:
                with st.spinner("Preparando opções para avaliação do ADM..."):
                    st.session_state[cache_key]=gm_daily_pick_prepare_admin_options(force_refresh=False, per_kind=5)
            except Exception as exc:
                st.session_state[cache_key]={"ok":False,"reason":type(exc).__name__}

        a1,a2=st.columns(2)
        if a1.button("🔄 Atualizar opções para aprovação",use_container_width=True,key="gm_daily_pick_refresh_candidates"):
            try:
                with st.spinner("Atualizando odds e probabilidades..."):
                    st.session_state[cache_key]=gm_daily_pick_prepare_admin_options(force_refresh=True, per_kind=5)
                st.rerun()
            except Exception as exc:
                st.error("Não foi possível atualizar as opções agora."); st.caption(type(exc).__name__)
        if a2.button("✅ Conferir resultados publicados",use_container_width=True,key="gm_daily_pick_settle_now"):
            try:
                result=gm_daily_pick_settle_pending(limit=100)
                if result.get("errors"):
                    st.error("Erro ao gravar resultado. Confirme a RPC v3 do V152 no Supabase.")
                    st.caption(" • ".join(result.get("errors")[:3]))
                else:
                    st.success(f"Conferência concluída: {result.get('settled',0)} atualizada(s) · {result.get('pending',0)} pendente(s).")
                st.rerun()
            except Exception as exc:
                st.error("Não foi possível conferir os resultados agora."); st.caption(type(exc).__name__)

        prepared=st.session_state.get(cache_key) or {}
        if not prepared.get("ok"):
            if prepared.get("reason")=="source_error":
                st.warning("A fonte de odds/probabilidades não respondeu de forma completa. Tente atualizar novamente.")
            else:
                st.info("As opções privadas ainda não puderam ser preparadas.")
        else:
            option_map=prepared.get("options") or {}
            for kind in ("matadeira","dica","bingo"):
                published_count = len(lookup_all(today, kind))
                if published_count:
                    st.caption(f"{GM_DAILY_PICK_PROFILES[kind]['label']}: {published_count} publicada(s) hoje · você pode aprovar outras.")
                opts=[o for o in (option_map.get(kind) or []) if not _gm_daily_pick_is_discarded(o, today)]
                with st.expander(f"{GM_DAILY_PICK_PROFILES[kind]['label']} — {len(opts)} opção(ões) para avaliar",expanded=(kind=="matadeira")):
                    if not opts:
                        st.caption("Nenhuma alternativa atingiu os filtros mínimos nesta atualização.")
                    for idx,opt in enumerate(opts,1):
                        legs=_gm_daily_sort_legs(opt.get("legs") or [])
                        total_odd=_gm_daily_num(opt.get("total_odd")) or 0.0
                        model_prob=_gm_daily_num(opt.get("model_probability")) or 0.0
                        st.markdown(f"**Opção {idx} · {_gm_daily_bet_type_label(opt.get('bet_type'),len(legs))} · odd {total_odd:.2f}**")
                        st.caption(f"Probabilidade combinada estimada: {model_prob:.1f}% · todas as pernas ≥ 75%")
                        for leg in legs:
                            st.markdown(f"- **{leg.get('market')}** @ {(_gm_daily_num(leg.get('odd')) or 0):.2f} · {(_gm_daily_num(leg.get('probability')) or 0):.0f}% · {leg.get('home')} × {leg.get('away')} · {_gm_daily_time_label(leg.get('time'))}")
                        direct_bet_url, bet_link_invalid = gm_daily_pick_admin_bet_link(kind, idx, "gm_daily")
                        approve_col, discard_col = st.columns([2,1])
                        if approve_col.button("✅ Aprovar e publicar",use_container_width=True,type="primary",key=f"gm_approve_{kind}_{idx}",disabled=bet_link_invalid):
                            try:
                                publish_opt=dict(opt)
                                publish_opt["direct_bet_url"]=direct_bet_url
                                with st.spinner("Publicando..."):
                                    result=gm_daily_pick_publish_selected(publish_opt)
                                if result.get("ok"):
                                    _gm_daily_pick_remove_cached_option(cache_key, kind, opt)
                                    st.toast("Oportunidade publicada com sucesso.", icon="✅")
                                    st.rerun()
                                else:
                                    st.warning("Esta alternativa não pôde ser publicada pelos critérios de segurança.")
                            except Exception as exc:
                                st.error("Falha ao publicar a oportunidade aprovada."); st.caption(type(exc).__name__)
                        if discard_col.button("✕ Descartar",use_container_width=True,key=f"gm_discard_{kind}_{idx}"):
                            _gm_daily_pick_discard(opt, today)
                            _gm_daily_pick_remove_cached_option(cache_key, kind, opt)
                            st.toast("Opção descartada permanentemente.", icon="🗑️")
                            st.rerun()
                        if idx < len(opts):
                            st.divider()

    st.markdown('''
    <style>
    .gm-risk-rule{display:flex;justify-content:center;align-items:center;gap:12px;flex-wrap:wrap;background:#0e151d;border:1px solid rgba(148,163,184,.20);border-radius:14px;padding:12px 14px;margin:.55rem 0 1rem;font-weight:950;letter-spacing:.02em;text-align:center}.gm-risk-green{color:#34e681}.gm-risk-red{color:#fb7185}.gm-risk-arrow{color:#94a3b8}
    .gm-pick-card{background:linear-gradient(145deg,#0d1718,#0b1118);border:1px solid rgba(34,197,94,.62);border-radius:18px;padding:16px;margin:.65rem 0 1rem;box-shadow:0 12px 30px rgba(0,0,0,.18)}.gm-kind-matadeira{border-color:rgba(52,230,129,.72)}.gm-kind-dica{border-color:rgba(250,204,21,.55)}.gm-kind-bingo{border-color:rgba(251,113,133,.58)}
    .gm-pick-head{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}.gm-pick-title{font-weight:950;font-size:1.1rem;color:#34e681}.gm-pick-odd{font-size:1.6rem;font-weight:950;color:#34e681}.gm-pick-leg{background:#111b25;border:1px solid rgba(148,163,184,.12);border-radius:12px;padding:10px 12px;margin-top:8px}.gm-pick-muted{color:#94a3b8;font-size:.76rem}.gm-pick-market{color:#f8fafc;font-weight:850}.gm-pick-history{display:flex;gap:7px;flex-wrap:wrap;margin-top:8px}.gm-pick-dot{padding:7px 9px;border-radius:10px;background:#101923;border:1px solid rgba(148,163,184,.12);font-size:.75rem;font-weight:800}.gm-score-bet-cta{display:flex;align-items:center;justify-content:center;gap:9px;width:min(100%,360px);min-height:44px;margin:.45rem auto .9rem;padding:7px 14px;border:1px solid rgba(52,230,129,.72);border-radius:13px;background:#0d1419;color:#f8fafc!important;text-decoration:none!important;box-shadow:none}.gm-score-bet-cta:hover{background:#101b1c;border-color:#34e681}.gm-score-bet-target{color:#34e681;font-size:1rem}.gm-score-bet-cta strong{display:block;font-size:.88rem;line-height:1.05}.gm-score-bet-cta small{display:block;margin-top:3px;color:#34e681;font-size:.57rem;font-weight:900;letter-spacing:.16em}.gm-score-bet-arrow{margin-left:4px;color:#34e681;font-size:1.25rem;font-weight:900}
    </style>''',unsafe_allow_html=True)

    if is_admin:
        st.markdown("### 📅 Hoje — publicado para clientes")
    # V146: cada aprovação é um card próprio. Não existe limite de uma publicação
    # por categoria: 3 Matadeiras + 2 Dicas + 1 Bingo => 6 cards no cliente Pro.
    for kind in ("matadeira","dica","bingo"):
        today_rows = lookup_all(today, kind)
        if not today_rows:
            _gm_daily_pick_card(None,today,kind,is_admin=is_admin)
        else:
            for pick_number, row in enumerate(today_rows, 1):
                if len(today_rows) > 1:
                    st.caption(f"{GM_DAILY_PICK_PROFILES[kind]['short']} #{pick_number}")
                _gm_daily_pick_card(row,today,kind,is_admin=is_admin)

    with st.expander("📆 Ontem — seleções e resultados",expanded=False):
        found=False
        for kind in ("matadeira","dica","bingo"):
            kind_rows=lookup_all(yesterday,kind)
            if not kind_rows: continue
            found=True; label=GM_DAILY_PICK_PROFILES[kind]["label"]
            for pick_number,row in enumerate(kind_rows,1):
                numbered = f" #{pick_number}" if len(kind_rows) > 1 else ""
                if str(row.get("status"))=="no_pick": st.markdown(f"**{label}{numbered}: ⚫ Sem seleção**"); continue
                odd=_gm_daily_num(row.get("total_odd")) or 0.0; st.markdown(f"**{label}{numbered} · {_gm_daily_status_badge(row.get('status'))} · odd {odd:.2f}**")
                for leg in _gm_daily_sort_legs(row.get("legs") or []): st.caption(f"🕒 {_gm_daily_time_label(leg.get('time'))} • ⚽ {leg.get('home')} × {leg.get('away')} — {leg.get('market')} @ {(_gm_daily_num(leg.get('odd')) or 0.0):.2f}")
        if not found: st.caption("Ainda não há oportunidades registradas para ontem.")

    standard=[r for r in rows if str(r.get("pick_kind") or "dica") in {"matadeira","dica"} and str(r.get("status") or "") in {"green","red"}]
    unique_dates=[]
    for r in standard:
        d=str(r.get("pick_date") or "")
        if d and d not in unique_dates: unique_dates.append(d)
        if len(unique_dates)>=10: break
    perf=[r for r in standard if str(r.get("pick_date") or "") in set(unique_dates)]
    st.markdown("### 📊 Aproveitamento — últimos 10 dias")
    st.caption("Calculado somente com **Matadeira + Dica do Dia oficialmente aprovadas e publicadas**. O **Bingo não entra** nesta porcentagem.")
    if not perf:
        st.caption("O histórico começa a aparecer conforme Matadeira e Dica publicadas forem encerradas.")
    else:
        greens=sum(1 for r in perf if str(r.get("status"))=="green"); reds=sum(1 for r in perf if str(r.get("status"))=="red"); m1,m2,m3=st.columns(3); m1.metric("Greens",greens); m2.metric("Reds",reds); m3.metric("Aproveitamento",f"{100*greens/max(1,greens+reds):.0f}%")
        chips=[]
        for d in unique_dates:
            day_rows=[r for r in perf if str(r.get("pick_date") or "")==d]; dg=sum(1 for r in day_rows if str(r.get("status"))=="green"); dr=sum(1 for r in day_rows if str(r.get("status"))=="red")
            try: label=datetime.fromisoformat(d).strftime("%d/%m")
            except Exception: label=d[-5:]
            icon="🟢" if dr==0 and dg>0 else "🔴" if dg==0 and dr>0 else "🟡"; chips.append(f'<span class="gm-pick-dot">{icon} {html.escape(label)} · {dg}/{dg+dr}</span>')
        st.markdown('<div class="gm-pick-history">'+''.join(chips)+'</div>',unsafe_allow_html=True)

    if is_admin:
        with st.expander("ℹ️ Como funcionam as oportunidades"):
            st.markdown("- O motor prepara várias alternativas privadas para o **ADM** e exige **75%+ em cada perna** da vitrine.\n- **Só a alternativa aprovada** é gravada em `gm_daily_picks` e mostrada aos clientes.\n- **Matadeira:** odd oficial entre **1,50 e 1,89**.\n- **Dica do Dia:** odd oficial entre **1,90 e 2,10**.\n- **Bingo:** múltipla de **4 a 10 jogos**, odd mínima **3,50**.\n- Mercados elegíveis continuam exigindo **odd pré-jogo real**; o GM SCORE não inventa cotação.\n- O aproveitamento oficial usa apenas Matadeira + Dica publicadas; o Bingo permanece separado.")
            st.caption("Quanto maior a odd, menor tende a ser a probabilidade conjunta. Odds e probabilidades são estimativas pré-jogo, não garantia de retorno. Aposte com responsabilidade.")



@st.cache_data(ttl=900, show_spinner=False)
def gm_games_prepared_fixtures(target_date):
    """Agenda já validada/deduplicada para renderização rápida.

    V121: a V120 recalculava identidade oficial, roster e deduplicação em TODO
    rerun do Streamlit, inclusive ao tocar em widgets sem relação com a agenda.
    Esta camada memoriza somente o resultado visual da mesma pipeline existente.
    O botão Atualizar jogos invalida explicitamente este cache.
    """
    fixtures = load_fixtures_for_date(target_date) or []
    safe = []
    for f in fixtures:
        comp = str((f or {}).get("competition") or "")
        if comp not in COMPETITIONS:
            continue
        if not valid_daily_fixture(f) or not fixture_matches_selected_date(f, target_date):
            continue
        if not gm_fixture_matches_official_league_roster(f):
            continue
        _merge_fixture_unique(safe, dict(f))
    unique = []
    for fixture in safe:
        ff = dict(fixture)
        comp = str(ff.get("competition") or "")
        home_identity = gm_fixture_official_team_identity(ff.get("home"), comp, ff.get("home_team_id"))
        away_identity = gm_fixture_official_team_identity(ff.get("away"), comp, ff.get("away_team_id"))
        ff["home"] = home_identity.get("name") or gm_fixture_canonical_team_name(ff.get("home"))
        ff["away"] = away_identity.get("name") or gm_fixture_canonical_team_name(ff.get("away"))
        if home_identity.get("id"):
            ff["home_team_id"] = home_identity.get("id")
        if away_identity.get("id"):
            ff["away_team_id"] = away_identity.get("id")
        _merge_fixture_unique(unique, ff)
    return sorted(unique, key=lambda f: (str(f.get("time") or "99:99"), str(f.get("competition") or ""), _fixture_identity_key(f.get("home")), _fixture_identity_key(f.get("away"))))

def gm_render_games_page():
    """Agenda única das competições GM SCORE, sem alterar fontes ou cálculos."""
    st.markdown("## ⚽ Jogos")
    st.caption("Todos os jogos das competições GM SCORE em ordem de horário de Brasília.")
    target_date = st.selectbox("📅 Data dos jogos", _date_options, key="gm_games_page_date", format_func=_agenda_date_label)
    if st.button("🔄 Atualizar jogos", use_container_width=True, key=f"gm_games_refresh_{target_date}"):
        for _fn in (load_apifootball_prediction_fixtures_for_date, load_apifootball_competition_fixtures_for_date, load_apifootball_all_competitions_fixtures_for_date, load_apifootball_fixtures_for_date, load_sofascore_fixtures_for_date, load_espn_fixtures_for_date, load_thesportsdb_fixtures_for_date, load_fixtures_for_date, gm_games_prepared_fixtures):
            try: _fn.clear()
            except Exception: pass
        st.rerun()
    try:
        with st.spinner("Carregando jogos do dia..."):
            safe = gm_games_prepared_fixtures(target_date) or []
    except Exception:
        safe = []
    if not safe:
        st.info("Nenhum jogo das competições GM SCORE foi localizado para esta data.")
        return
    st.markdown(f"### {len(safe)} jogo(s) · {_agenda_date_label(target_date)}")
    for i, f in enumerate(safe):
        comp = str(f.get("competition") or ""); home = str(f.get("home") or ""); away = str(f.get("away") or ""); tm = str(f.get("time") or "—")
        card_html = '<div id="gm-game-{}" class="gm-game-card"><div class="gm-game-time">{}</div><div class="gm-game-body"><div class="gm-game-league">{}</div><div class="gm-game-teams">{} × {}</div></div></div>'.format(i, html.escape(tm), html.escape(competition_display_name(comp)), html.escape(home), html.escape(away))
        st.markdown(card_html, unsafe_allow_html=True)
        if st.button("📊 Analisar", use_container_width=False, key=f"gm_games_analyze_{target_date}_{i}_{clean_col(comp)}"):
            # V81: transporta o contexto completo do jogo e neutraliza o gm_view=games
            # que pode permanecer na URL da navegação mobile. Sem isso, o query param
            # poderia devolver o usuário imediatamente à agenda após o rerun.
            # V90: payload persistente e atômico da partida. A tela dedicada não
            # depende mais dos widgets/seletores da Home para descobrir o confronto.
            st.session_state["gm_games_direct_match"] = {
                "competition": comp,
                "home": home,
                "away": away,
                "date": target_date.isoformat() if hasattr(target_date, "isoformat") else str(target_date),
                "time": tm,
                # V92: identidade oficial da partida viaja junto com a navegação.
                # Estes IDs servem apenas para resolver o confronto correto; não
                # alteram fórmulas, probabilidades ou critérios estatísticos.
                "match_id": str(f.get("match_id") or "").strip(),
                "league_id": str(f.get("league_id") or "").strip(),
                "home_team_id": str(f.get("home_team_id") or "").strip(),
                "away_team_id": str(f.get("away_team_id") or "").strip(),
            }
            # V91: hidrata imediatamente o mesmo estado usado pela análise normal.
            # O payload continua como fonte persistente, mas a abertura não depende
            # de um segundo rerun para consumir _goto_*.
            st.session_state.selected_competition = comp
            st.session_state.selected_home = home
            st.session_state.selected_away = away
            st.session_state.loaded_home = home
            st.session_state.loaded_away = away
            st.session_state.loaded_competition = comp
            st.session_state.league_widget = comp
            st.session_state.main_league_widget = comp
            st.session_state["_main_games_hidden_competition"] = comp
            st.session_state["_synced_loaded_signature"] = f"{comp}|{home}|{away}"
            try:
                _comp_key = clean_col(str(comp))
                st.session_state[f"main_fixture_date_{_comp_key}"] = target_date
                st.session_state[f"main_match_choice_{_comp_key}"] = "📅 Jogos da data"
            except Exception:
                pass
            # Limpa resíduos do mecanismo antigo para que não sobrescrevam o payload.
            st.session_state.pop("_goto_comp", None)
            st.session_state.pop("_goto_home", None)
            st.session_state.pop("_goto_away", None)
            st.session_state.pop("_goto_date", None)
            st.session_state["gm_games_return_date"] = target_date
            st.session_state["gm_games_return_label"] = _agenda_date_label(target_date)
            st.session_state["gm_games_return_index"] = i
            st.session_state["gm_analysis_origin"] = "games"
            # V83: cada abertura de uma partida é uma nova navegação. O Streamlit
            # pode preservar o scroll da agenda anterior; este sinal força a análise
            # a começar no topo sem perder a data usada no botão de retorno.
            st.session_state["gm_analysis_scroll_top"] = True
            st.session_state["gm_main_view"] = "analysis"
            try:
                st.query_params["gm_view"] = "analysis"
            except Exception:
                pass
            st.rerun()

    # V84: ao voltar de uma análise, restaura a região da agenda onde o usuário
    # estava. A data permanece no selectbox e o card escolhido vira a âncora.
    _restore_index = st.session_state.pop("gm_games_restore_index", None)
    if _restore_index is not None:
        try:
            _restore_index = int(_restore_index)
            components.html(
                f"""
                <script>
                (() => {{
                  const reveal = () => {{
                    try {{
                      const doc = window.parent.document;
                      const el = doc.getElementById('gm-game-{_restore_index}');
                      if (el) el.scrollIntoView({{block:'center', inline:'nearest', behavior:'instant'}});
                    }} catch (e) {{}}
                  }};
                  reveal(); setTimeout(reveal, 100); setTimeout(reveal, 350); setTimeout(reveal, 700);
                }})();
                </script>
                """, height=0, scrolling=False
            )
        except Exception:
            pass


def gm_render_news_page():
    """Feed compacto de novidades, mantendo as mesmas RPCs e estados de leitura."""
    st.markdown("## 📰 Novidades")
    st.caption("Atualizações, lançamentos e avisos do GM SCORE.")
    try:
        rows = gm_list_news() or []
    except Exception:
        st.warning("Não foi possível carregar as novidades agora.")
        return
    hidden = set(st.session_state.get("gm_news_hidden_session", []))
    rows = [r for r in rows if str((r or {}).get("id") or "") not in hidden]

    # V80: novidades não lidas têm prioridade visual. Dentro de cada grupo,
    # a ordem permanece cronológica da publicação mais recente para a mais antiga.
    def _gm_news_sort_key(row):
        try:
            ts = pd.to_datetime((row or {}).get("published_at"), utc=True, errors="coerce")
            published_ts = float(ts.timestamp()) if not pd.isna(ts) else float("-inf")
        except Exception:
            published_ts = float("-inf")
        is_unread = 0 if bool((row or {}).get("is_read")) else 1
        return (is_unread, published_ts)

    rows = sorted(rows, key=_gm_news_sort_key, reverse=True)
    if not rows:
        st.info("Nenhuma novidade publicada no momento.")
        return

    st.markdown('''<style>
    .gm-news-feed-card{position:relative;background:linear-gradient(145deg,rgba(15,24,32,.96),rgba(9,15,21,.98));border:1px solid rgba(148,163,184,.18);border-radius:16px;padding:13px 14px 12px;margin:.45rem 0 .22rem;overflow:hidden}
    .gm-news-feed-card.new{border-color:rgba(52,230,129,.42);box-shadow:inset 3px 0 0 #34e681}
    .gm-news-feed-card.featured{background:linear-gradient(145deg,rgba(12,42,31,.88),rgba(9,18,22,.98))}
    .gm-news-feed-top{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:7px}
    .gm-news-feed-category{font-size:.70rem;font-weight:900;letter-spacing:.055em;text-transform:uppercase;color:#34e681}
    .gm-news-feed-state{font-size:.66rem;font-weight:850;color:#94a3b8;background:rgba(148,163,184,.09);padding:3px 7px;border-radius:999px}
    .gm-news-feed-title{font-size:1.02rem;font-weight:900;line-height:1.22;color:#f8fafc;margin:0 0 5px}
    .gm-news-feed-msg{font-size:.84rem;line-height:1.42;color:#c5ced8;margin:0;white-space:pre-wrap}
    .gm-news-feed-meta{font-size:.68rem;color:#7f8997;margin-top:9px}
    @media(max-width:768px){.gm-news-feed-card{padding:12px 12px 11px;border-radius:14px}.gm-news-feed-title{font-size:.96rem}.gm-news-feed-msg{font-size:.80rem}}
    </style>''', unsafe_allow_html=True)

    for row in rows:
        news_id = str(row.get("id") or "")
        title = str(row.get("title") or "Novidade")
        message = str(row.get("message") or "")
        category = str(row.get("category") or "novidade")
        category_label = str(GM_NEWS_CATEGORIES.get(category, "🆕 Novidade"))
        is_read = bool(row.get("is_read"))
        featured = bool(row.get("is_featured"))
        classes = "gm-news-feed-card" + (" new" if not is_read else "") + (" featured" if featured else "")
        state = "NOVA" if not is_read else "LIDA"
        feature_badge = " • DESTAQUE" if featured else ""
        card = f'''<div class="{classes}"><div class="gm-news-feed-top"><div class="gm-news-feed-category">{html.escape(category_label)}</div><div class="gm-news-feed-state">{state}{feature_badge}</div></div><div class="gm-news-feed-title">{html.escape(title)}</div><div class="gm-news-feed-msg">{html.escape(message)}</div><div class="gm-news-feed-meta">{html.escape(gm_news_format_datetime(row.get("published_at")))}</div></div>'''
        st.markdown(card, unsafe_allow_html=True)
        c1, c2, _ = st.columns([1.1, 1, 2.8])
        with c1:
            if not is_read and news_id and st.button("✓ Lida", key=f"gm_news_page_read_{news_id}"):
                try:
                    gm_news_rpc("gm_mark_news_read", {"p_news_id": news_id}); gm_invalidate_unread_news_cache(); st.rerun()
                except Exception:
                    st.warning("Não foi possível atualizar a leitura agora.")
        with c2:
            if news_id and st.button("Ocultar", key=f"gm_news_page_hide_{news_id}"):
                hidden_now = set(st.session_state.get("gm_news_hidden_session", [])); hidden_now.add(news_id); st.session_state["gm_news_hidden_session"] = list(hidden_now); st.rerun()


def gm_render_daily_pick_free_page():
    # V151: prévia das Dicas atuais sem renderizar seleção nem link no plano Free.
    st.markdown("## 💡 Dicas do Dia")
    st.markdown(
        '<div class="gm-risk-rule"><span class="gm-risk-green">QUANTO MAIOR A ODD</span><span class="gm-risk-arrow">→</span><span class="gm-risk-red">MENORES AS CHANCES</span></div>',
        unsafe_allow_html=True,
    )
    try:
        rows = gm_daily_pick_recent(140) or []
    except Exception:
        st.caption("As Dicas do Dia não puderam ser carregadas agora.")
        return

    today = datetime.now(BRASILIA_TZ).date()
    today_iso = today.isoformat()
    current = [
        r for r in rows
        if str(r.get("pick_date") or "") == today_iso
        and str(r.get("status") or "") == "pending"
        and str(r.get("pick_kind") or "dica") in {"matadeira", "dica", "bingo"}
    ]
    current.sort(key=lambda r: (
        {"matadeira": 0, "dica": 1, "bingo": 2}.get(str(r.get("pick_kind") or "dica"), 9),
        str(r.get("created_at") or ""),
    ))

    if current:
        plural = len(current) != 1
        teaser = (
            f'<div class="gm-free-picks-teaser"><b>🔥 {len(current)} oportunidade'
            f'{"s" if plural else ""} PRO disponível{"is" if plural else ""} hoje</b>'
            '<div>Veja confrontos, horários e indicadores. A seleção exata e o acesso à aposta são exclusivos do GM SCORE Pro.</div></div>'
        )
        st.markdown(teaser, unsafe_allow_html=True)

        for idx, row in enumerate(current):
            kind = str(row.get("pick_kind") or "dica")
            profile = GM_DAILY_PICK_PROFILES.get(kind, GM_DAILY_PICK_PROFILES["dica"])
            label = "📊 Dica Principal" if kind == "dica" else profile.get("label", "Dica do Dia")
            legs = _gm_daily_sort_legs(row.get("legs") or [])
            total_odd = _gm_daily_num(row.get("total_odd")) or 0.0
            btype = _gm_daily_bet_type_label(row.get("bet_type"), len(legs))

            card = [
                f'<div class="gm-pick-card gm-kind-{html.escape(kind)}">',
                '<div class="gm-pick-head"><div>',
                f'<div class="gm-pick-title">{html.escape(label)}</div>',
                f'<div class="gm-pick-muted">{today:%d/%m/%Y} • {html.escape(btype)} • {_gm_daily_status_badge(row.get("status"))}</div>',
                '</div><div><div class="gm-pick-muted">ODD TOTAL</div>',
                f'<div class="gm-pick-odd">{total_odd:.2f}</div></div></div>',
            ]

            for leg in legs:
                # FREE: market, odd da perna, bookmaker e URL não entram no HTML.
                time_label = _gm_daily_time_label(leg.get("time"))
                prob = _gm_daily_num(leg.get("probability"))
                conf = str(leg.get("confidence_band") or (_gm_daily_confidence_band(prob) if prob is not None else ""))
                prob_txt = f" • prob. estimada {prob:.0f}% • {html.escape(conf)}" if prob is not None else ""
                comp = str(leg.get("competition") or "")
                game_text = f"{str(leg.get('home') or '')} × {str(leg.get('away') or '')}"
                card.append(
                    '<div class="gm-pick-leg">'
                    '<div class="gm-free-market-lock">'
                    '<span class="gm-free-market-blur">MERCADO EXCLUSIVO PRO</span>'
                    '<span class="gm-free-market-lock-label">🔒 Seleção exclusiva PRO</span>'
                    '</div>'
                    f'<div class="gm-pick-muted">🕒 {html.escape(time_label)} • ⚽ {html.escape(game_text)} • {html.escape(comp)}{prob_txt}</div>'
                    '</div>'
                )

            card.append("</div>")
            st.markdown("".join(card), unsafe_allow_html=True)

            unlock_key = str(row.get("id") or idx)
            if st.button("🔒 Desbloquear aposta com PRO", use_container_width=True, key=f"gm_free_daily_unlock_{unlock_key}"):
                st.session_state["_gm_free_daily_upgrade_open"] = unlock_key
            if st.session_state.get("_gm_free_daily_upgrade_open") == unlock_key:
                gm_render_pro_lock(
                    "Seja PRO para acessar esta aposta",
                    "O GM SCORE Pro libera a seleção exata, o mercado indicado e o acesso direto à aposta.",
                    key=f"gm_free_daily_pro_{unlock_key}",
                )
    else:
        st.info("Ainda não há Dicas do Dia publicadas para hoje.")

    st.markdown(
        '<style>'
        '.gm-free-picks-teaser{background:linear-gradient(145deg,#0e1818,#0b1118);border:1px solid rgba(52,230,129,.42);border-radius:15px;padding:13px 14px;margin:.65rem 0 1rem;color:#f8fafc}'
        '.gm-free-picks-teaser b{color:#34e681;font-size:1rem}.gm-free-picks-teaser div{color:#a8b3c2;font-size:.78rem;margin-top:4px}'
        '.gm-free-market-lock{position:relative;min-height:35px;margin-bottom:5px;border-radius:9px;overflow:hidden;background:#0b1219;border:1px solid rgba(148,163,184,.12)}'
        '.gm-free-market-blur{display:block;padding:8px 10px;color:#d7dee7;font-weight:900;letter-spacing:.04em;filter:blur(6px);opacity:.42;user-select:none}'
        '.gm-free-market-lock-label{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#f8fafc;font-weight:900;font-size:.82rem;background:rgba(10,15,21,.58);backdrop-filter:blur(1px)}'
        '</style>',
        unsafe_allow_html=True,
    )

    past = [
        r for r in rows
        if str(r.get("pick_date") or "") < today_iso
        and str(r.get("status") or "") in {"green", "red", "void"}
    ]
    past.sort(key=lambda r: (str(r.get("pick_date") or ""), str(r.get("created_at") or "")), reverse=True)

    st.markdown("### 📊 Resultados anteriores")
    st.caption("Histórico público das Dicas do Dia já encerradas.")
    if not past:
        st.caption("Ainda não há resultados anteriores publicados.")
        return

    for row in past[:30]:
        kind = str(row.get("pick_kind") or "dica")
        label = GM_DAILY_PICK_PROFILES.get(kind, {}).get("label", "Dica do Dia")
        if kind == "dica":
            label = "📊 Dica Principal"
        status = _gm_daily_status_badge(row.get("status"))
        odd = _gm_daily_num(row.get("total_odd")) or 0.0
        date_txt = str(row.get("pick_date") or "")
        try:
            date_txt = datetime.fromisoformat(date_txt).strftime("%d/%m/%Y")
        except Exception:
            pass
        with st.expander(f"{date_txt} · {label} · {status} · odd {odd:.2f}", expanded=False):
            legs = _gm_daily_sort_legs(row.get("legs") or [])
            if not legs:
                st.caption("Resultado registrado sem detalhamento de pernas.")
            for leg in legs:
                leg_odd = _gm_daily_num(leg.get("odd")) or 0.0
                st.markdown(f"**{leg.get('market') or 'Mercado'}** @ {leg_odd:.2f}")
                st.caption(f"🕒 {_gm_daily_time_label(leg.get('time'))} • ⚽ {leg.get('home')} × {leg.get('away')} • {leg.get('competition') or ''}")

    settled_standard = [
        r for r in past
        if str(r.get("pick_kind") or "dica") in {"matadeira", "dica"}
        and str(r.get("status") or "") in {"green", "red"}
    ]
    if settled_standard:
        greens = sum(1 for r in settled_standard if str(r.get("status")) == "green")
        reds = sum(1 for r in settled_standard if str(r.get("status")) == "red")
        c1, c2, c3 = st.columns(3)
        c1.metric("Greens", greens)
        c2.metric("Reds", reds)
        c3.metric("Aproveitamento", f"{100 * greens / max(1, greens + reds):.0f}%")


def gm_render_account_page(profile):
    """Conta do ADM espelha a experiência VIP e acrescenta somente o acesso à Central Administrativa."""
    profile = profile or {}; is_admin = profile.get("role") == "admin"
    st.markdown("## 👤 Minha Conta")
    st.markdown(f"**{html.escape(str(profile.get('nome') or profile.get('email') or 'GM SCORE'))}**")

    tier = gm_product_tier(profile)
    suspended_now = bool(gm_pro_suspended(profile)) if not is_admin else False
    if is_admin:
        st.success("🛡️ GM SCORE ADM")
    elif tier == "pro":
        st.success("⭐ GM SCORE PRO")
    elif suspended_now:
        st.info("○ GM SCORE FREE · acesso PRO suspenso pelo ADM")
    else:
        st.info("○ GM SCORE FREE")
    vip_until_raw = profile.get("vip_until")
    if vip_until_raw:
        try:
            vip_until_dt = datetime.fromisoformat(str(vip_until_raw).replace("Z", "+00:00"))
            now_vip = datetime.now(vip_until_dt.tzinfo)
            remaining_seconds = (vip_until_dt - now_vip).total_seconds()
            remaining_days = max(0, math.ceil(remaining_seconds / 86400))
            vip_until_br = vip_until_dt.astimezone(ZoneInfo("America/Sao_Paulo")).strftime("%d/%m/%Y às %H:%M")
            remaining_label = "1 dia restante" if remaining_days == 1 else f"{remaining_days} dias restantes"
            if tier == "pro":
                st.markdown(
                    f'<div class="gm-account-vip-meta"><span class="gm-account-days">📅 {remaining_label}</span>'
                    f'<span class="gm-account-expiry">Vencimento: {vip_until_br}</span></div>',
                    unsafe_allow_html=True,
                )
            elif suspended_now:
                st.caption(f"Validade original preservada até {vip_until_br}. O conteúdo PRO permanece bloqueado enquanto houver suspensão administrativa.")
        except Exception:
            pass

    with st.expander("💳 Renovar Pro" if tier == "pro" else "⭐ Desbloquear GM SCORE Pro", expanded=False):
        for plan in GM_VIP_PLANS:
            st.markdown(f"**PRO {str(plan['title']).upper()} · {plan['pix_price']} no Pix**")
            saving = str(plan.get("saving") or "").strip()
            if saving: st.caption(saving)
            gm_checkout_button(plan, "pix", "⚡ Renovar com Pix", primary=True)
            if plan.get("card_price"):
                gm_checkout_button(plan, "card", "💳 Renovar com cartão"); st.caption(plan.get("card_text") or "")
        st.caption("O prazo é acrescentado somente após a confirmação válida do Mercado Pago.")
    if st.button("⭐ Avaliar GM SCORE", use_container_width=True, key="gm_account_review"):
        st.session_state["gm_reviews_open"] = True; st.rerun()

    st.link_button("✈️ Suporte pelo Telegram", "https://t.me/suport_gm", use_container_width=True)
    if st.button("🚪 Sair", use_container_width=True, key="gm_account_logout"):
        gm_auth_sign_out(); st.session_state.pop("gm_admin_panel_open", None); st.rerun()


def gm_render_admin_mobile_workspace_switch(profile, admin_mode=False):
    """Alternância compacta Cliente/ADM no mobile, sem misturar o backoffice à navegação do cliente."""
    if not profile or profile.get("role") != "admin":
        return
    try:
        params = dict(st.query_params)
    except Exception:
        params = {}
    params["gm_admin_mode"] = "0" if admin_mode else "1"
    href = "?" + urlencode(params, doseq=True)
    label = "👤 Cliente" if admin_mode else "🛡️ ADM"
    st.markdown(
        f'<a class="gm-admin-mobile-switch" href="{html.escape(href, quote=True)}" target="_self">{label}</a>',
        unsafe_allow_html=True,
    )


def gm_render_app_navigation(profile):
    """Navegação mobile própria em HTML; desktop continua usando a sidebar existente."""
    current = str(st.session_state.get("gm_main_view") or "analysis")
    items = [
        ("analysis", "🏠", "Início"),
        ("games", "⚽", "Jogos"),
        ("daily_pick", "💡", "Dicas"),
        ("news", "📰", "Novidades"),
        ("account", "👤", "Conta"),
    ]
    # V79: contador de novidades não lidas no próprio item da navegação,
    # no padrão de badge usado por apps de mensagens/e-mail.
    try:
        news_unread = gm_unread_news_count()
    except Exception:
        news_unread = 0
    try:
        base_params = dict(st.query_params)
    except Exception:
        base_params = {}
    links = []
    for view, icon, label in items:
        params = dict(base_params)
        params["gm_view"] = view
        href = "?" + urlencode(params, doseq=True)
        active = " gm-mobile-nav-active" if current == view else ""
        badge_html = ""
        if view == "news" and news_unread > 0:
            badge_text = "99+" if news_unread > 99 else str(news_unread)
            badge_html = f'<span class="gm-mobile-nav-badge">{badge_text}</span>'
        links.append(
            f'<a class="gm-mobile-nav-item{active}" href="{html.escape(href, quote=True)}" target="_self" '
            f'aria-label="{html.escape(label)}"><span class="gm-mobile-nav-icon-wrap"><span class="gm-mobile-nav-icon">{icon}</span>{badge_html}</span>'
            f'<span class="gm-mobile-nav-label">{html.escape(label)}</span></a>'
        )
    st.markdown('<nav class="gm-mobile-nav-shell">' + "".join(links) + '</nav>', unsafe_allow_html=True)


def gm_render_analysis_top_anchor():
    """Marca o início visual da análise quando a origem foi a aba Jogos."""
    if st.session_state.get("gm_analysis_origin") != "games":
        return
    st.markdown('<div id="gm-analysis-top-anchor"></div>', unsafe_allow_html=True)


def gm_force_analysis_scroll_top_after_render():
    """Após a análise terminar de renderizar, leva a viewport ao início real do confronto."""
    if st.session_state.get("gm_analysis_origin") != "games":
        return
    if not st.session_state.pop("gm_analysis_scroll_top", False):
        return
    components.html(
        """
        <script>
        (() => {
          const go = () => {
            try {
              const doc = window.parent.document;
              const anchor = doc.getElementById('gm-analysis-top-anchor');
              if (anchor) {
                anchor.scrollIntoView({block: 'start', inline: 'nearest', behavior: 'instant'});
                return;
              }
              const main = doc.querySelector('[data-testid="stAppViewContainer"] .main');
              if (main) main.scrollTo({top: 0, left: 0, behavior: 'instant'});
              window.parent.scrollTo(0, 0);
            } catch (e) {}
          };
          go();
          setTimeout(go, 120);
          setTimeout(go, 350);
          setTimeout(go, 800);
        })();
        </script>
        """,
        height=0,
        scrolling=False,
    )


def gm_render_games_return_button():
    """Retorno rápido à mesma data da agenda após abrir uma análise pela aba Jogos."""
    if st.session_state.get("gm_analysis_origin") != "games":
        return
    return_date = st.session_state.get("gm_games_return_date")
    if return_date is None:
        return
    try:
        label = str(st.session_state.get("gm_games_return_label") or _agenda_date_label(return_date))
    except Exception:
        label = "data consultada"
    # V86: botão contextual pequeno e fixo. Só existe enquanto a análise atual
    # tiver sido aberta pela agenda Jogos do Dia.
    if st.button(f"‹ Jogos · {label}", use_container_width=False, key="gm_back_to_games_context"):
        st.session_state["gm_games_page_date"] = return_date
        st.session_state["gm_games_restore_index"] = st.session_state.get("gm_games_return_index")
        st.session_state["gm_main_view"] = "games"
        st.session_state.pop("gm_analysis_origin", None)
        try:
            st.query_params["gm_view"] = "games"
        except Exception:
            pass
        st.rerun()


def gm_render_main_shortcuts():
    st.markdown("### 🚀 Acesso rápido")
    c1,c2=st.columns(2)
    with c1:
        if st.button("⚽ Jogos do dia",use_container_width=True,type="primary",key="gm_home_games"): st.session_state["gm_main_view"]="games"; st.rerun()
    with c2:
        if st.button("💡 Dicas do Dia",use_container_width=True,key="gm_home_daily_pick"): st.session_state["gm_main_view"]="daily_pick"; st.rerun()
    _home_profile = st.session_state.get("gm_auth_profile") or {}
    if str((_home_profile or {}).get("role") or "").lower() == "admin":
        if st.button("🛡️ Área Administrativa", use_container_width=True, key="gm_home_admin"):
            st.session_state["gm_admin_panel_open"] = True
            st.rerun()



st.markdown(r"""
<style>
.gm-game-card{display:flex;align-items:center;gap:14px;background:linear-gradient(145deg,#0d1718,#0b1118);border:1px solid rgba(52,230,129,.22);border-radius:16px;padding:13px 14px;margin:.55rem 0 .28rem}.gm-game-time{font-weight:950;color:#34e681;min-width:54px;font-size:1rem}.gm-game-body{min-width:0;flex:1}.gm-game-league{color:#94a3b8;font-size:.76rem;font-weight:750}.gm-game-teams{color:#f8fafc;font-size:1rem;font-weight:900;margin-top:2px}.gm-game-teams span{color:#34e681;padding:0 4px}
.gm-mobile-nav-shell{display:none}
.gm-admin-mobile-switch{display:none}
div[class*="st-key-gm_admin_active_section"] [data-testid="stRadio"] > div{gap:.42rem!important;flex-wrap:wrap!important}
div[class*="st-key-gm_admin_active_section"] [data-testid="stRadio"] label{border:1px solid rgba(52,230,129,.28)!important;border-radius:10px!important;padding:.42rem .62rem!important;background:rgba(52,230,129,.05)!important}
button[kind="secondary"]:has(+ div),button[kind="primary"]:has(+ div){}
@media (max-width:768px){
.gm-admin-mobile-switch{display:flex!important;position:fixed!important;right:.75rem!important;top:4.15rem!important;z-index:100002!important;align-items:center!important;justify-content:center!important;padding:.38rem .68rem!important;border-radius:999px!important;border:1px solid rgba(52,230,129,.42)!important;background:rgba(7,16,15,.95)!important;color:#eafbf2!important;text-decoration:none!important;font-size:.72rem!important;font-weight:900!important;box-shadow:0 5px 16px rgba(0,0,0,.28)!important;backdrop-filter:blur(8px)!important}
[data-testid="stSidebar"]{display:none!important}
[data-testid="collapsedControl"]{display:none!important}
[data-testid="stAppViewContainer"] .main .block-container{padding-bottom:8.9rem!important}
.gm-mobile-nav-shell{display:flex!important;position:fixed!important;left:.45rem!important;right:.45rem!important;bottom:calc(3.55rem + env(safe-area-inset-bottom))!important;z-index:99999!important;height:4.05rem!important;background:rgba(7,16,15,.985)!important;border:1px solid rgba(52,230,129,.22)!important;border-radius:16px!important;padding:.25rem .18rem!important;box-shadow:0 10px 28px rgba(0,0,0,.46)!important;align-items:stretch!important;justify-content:space-between!important;gap:.06rem!important;box-sizing:border-box!important}
.gm-mobile-nav-item{display:flex!important;flex:1 1 20%!important;min-width:0!important;height:3.45rem!important;align-items:center!important;justify-content:center!important;flex-direction:column!important;gap:.12rem!important;border-radius:11px!important;text-decoration:none!important;color:#9aa7b6!important;background:transparent!important;-webkit-tap-highlight-color:transparent!important}
.gm-mobile-nav-item:visited{color:#9aa7b6!important}.gm-mobile-nav-item:hover{color:#eafbf2!important;background:rgba(52,230,129,.06)!important;text-decoration:none!important}
.gm-mobile-nav-item.gm-mobile-nav-active{color:#34e681!important;background:rgba(52,230,129,.10)!important}
.gm-mobile-nav-item.gm-mobile-nav-active:visited{color:#34e681!important}
.gm-mobile-nav-icon-wrap{position:relative;display:inline-flex;align-items:center;justify-content:center}
.gm-mobile-nav-badge{position:absolute;top:-7px;right:-12px;min-width:18px;height:18px;padding:0 4px;border-radius:999px;background:#ff3b4d;color:#fff;font-size:.60rem;font-weight:950;line-height:18px;text-align:center;box-shadow:0 0 0 2px rgba(6,16,13,.96);z-index:2}
.gm-mobile-nav-icon{display:block!important;font-size:1.08rem!important;line-height:1.05!important;height:1.18rem!important}.gm-mobile-nav-label{display:block!important;font-size:.62rem!important;font-weight:800!important;line-height:1!important;white-space:nowrap!important;overflow:hidden!important;text-overflow:ellipsis!important;max-width:100%!important}
.gm-game-card{padding:11px 12px;border-radius:14px;margin-bottom:.18rem}.gm-game-time{min-width:48px}.gm-game-teams{font-size:.94rem}
.gm-account-vip-meta{display:flex;align-items:center;justify-content:space-between;gap:.6rem;flex-wrap:wrap;margin:.15rem 0 .75rem}.gm-account-days{display:inline-flex;align-items:center;padding:.38rem .7rem;border-radius:999px;border:1px solid rgba(52,230,129,.38);background:rgba(52,230,129,.10);color:#34e681;font-size:.78rem;font-weight:850}.gm-account-expiry{color:#9aa7b6;font-size:.76rem}
/* V86: retorno persistente da análise para a agenda que a originou. */
div[class*="st-key-gm_back_to_games_context"]{position:fixed!important;left:.72rem!important;top:4.15rem!important;z-index:100001!important;width:auto!important;margin:0!important}
div[class*="st-key-gm_back_to_games_context"] [data-testid="stButton"]{width:auto!important;margin:0!important}
div[class*="st-key-gm_back_to_games_context"] button{width:auto!important;min-height:2.15rem!important;padding:.28rem .68rem!important;border-radius:999px!important;border:1px solid rgba(52,230,129,.46)!important;background:rgba(7,16,15,.94)!important;color:#eafbf2!important;font-size:.74rem!important;font-weight:850!important;box-shadow:0 5px 16px rgba(0,0,0,.28)!important;backdrop-filter:blur(8px)!important}
div[class*="st-key-gm_games_analyze_"] [data-testid="stButton"]{display:flex!important;justify-content:flex-end!important;margin:0 0 .72rem!important}
div[class*="st-key-gm_games_analyze_"] button{width:auto!important;min-width:7.8rem!important;min-height:2.35rem!important;padding:.3rem .85rem!important;border-radius:10px!important;border:1px solid rgba(52,230,129,.48)!important;background:rgba(52,230,129,.10)!important;color:#34e681!important;font-size:.82rem!important;font-weight:850!important}
}
@media (min-width:769px){div[class*="st-key-gm_games_analyze_"] [data-testid="stButton"]{display:flex;justify-content:flex-end;margin-bottom:.65rem}div[class*="st-key-gm_games_analyze_"] button{width:auto!important;min-width:8.5rem}/* V115: no desktop o botão contextual fica dentro da área principal. O posicionamento fixo anterior em left:1rem podia ficar escondido atrás da sidebar. */div[class*="st-key-gm_back_to_games_context"]{position:sticky!important;left:auto!important;top:.75rem!important;z-index:100001!important;width:max-content!important;max-width:100%!important;margin:.15rem 0 .7rem!important}div[class*="st-key-gm_back_to_games_context"] [data-testid="stButton"]{width:auto!important;margin:0!important}div[class*="st-key-gm_back_to_games_context"] button{width:auto!important;min-height:2.2rem!important;padding:.3rem .72rem!important;border-radius:999px!important;border:1px solid rgba(52,230,129,.46)!important;background:rgba(7,16,15,.94)!important;color:#eafbf2!important;font-size:.76rem!important;font-weight:850!important;box-shadow:0 5px 16px rgba(0,0,0,.28)!important}}
</style>
""", unsafe_allow_html=True)

# Sincronização leve e silenciosa. Em uso normal verifica no máximo 2 pendências
# a cada 30 minutos por sessão; o administrador pode forçar uma sincronização maior.
try:
    gm_calibration_auto_settle(limit=2, force=False)
except Exception:
    pass

# V141: a barra inferior padrão permanece ativa dentro da Central Administrativa.
_gm_requested_view = str(st.query_params.get("gm_view", "") or "").strip()
if _gm_requested_view in {"analysis", "games", "daily_pick", "news", "account", "admin_results"}:
    st.session_state["gm_main_view"] = _gm_requested_view
    if _gm_profile_after_gate and _gm_profile_after_gate.get("role") == "admin":
        st.session_state["gm_admin_panel_open"] = False
    try:
        del st.query_params["gm_view"]
    except Exception:
        pass

if (_gm_profile_after_gate and _gm_profile_after_gate.get("role") == "admin"
        and bool(st.session_state.get("gm_admin_panel_open"))):
    gm_render_app_navigation(_gm_profile_after_gate)
    gm_render_admin_panel(_gm_profile_after_gate)
    st.stop()

_gm_main_view = str(st.session_state.get("gm_main_view") or "analysis")
# V86: o retorno contextual pertence somente ao fluxo Jogos → Análise. Ao navegar
# deliberadamente para outra área, encerra a sessão de retorno e o botão desaparece.
if _gm_main_view not in {"analysis", "games"}:
    st.session_state.pop("gm_analysis_origin", None)
    st.session_state.pop("gm_games_return_date", None)
    st.session_state.pop("gm_games_return_label", None)
    st.session_state.pop("gm_games_return_index", None)
    st.session_state.pop("gm_games_direct_match", None)
gm_render_app_navigation(_gm_profile_after_gate)

_gm_caps = gm_access_capabilities(_gm_profile_after_gate)
if _gm_main_view == "daily_pick":
    if _gm_caps["current_daily_picks"]:
        gm_render_daily_pick_page()
    else:
        gm_render_daily_pick_free_page()
elif _gm_main_view == "games":
    gm_render_games_page()
elif _gm_main_view == "news":
    gm_render_news_page()
elif _gm_main_view == "account":
    gm_render_account_page(_gm_profile_after_gate)
elif _gm_main_view == "admin_results":
    gm_render_admin_bets_results_page()
else:
    # V89: análises abertas pela aba Jogos usam uma tela dedicada ao confronto.
    # Nada da Home (Apostas do ADM, atalhos, auditorias ou seletores) é exibido.
    # A Home normal permanece integralmente igual quando a origem não é Jogos.
    _gm_games_direct_analysis = st.session_state.get("gm_analysis_origin") == "games"
    gm_render_analysis_top_anchor()
    gm_render_games_return_button()
    if not _gm_games_direct_analysis:
        gm_render_admin_bets_home()
        gm_render_main_shortcuts()
        gm_render_apifootball_league_audit()
        gm_render_apifootball_stat_audit()
        gm_render_calibration_dashboard()
    if _gm_caps["analysis_intelligence"]:
        render_analysis()
        gm_force_analysis_scroll_top_after_render()
    else:
        _free_match = st.session_state.get("gm_games_direct_match") if isinstance(st.session_state.get("gm_games_direct_match"), dict) else {}
        if _free_match:
            _fh = html.escape(str(_free_match.get("home") or _free_match.get("home_name") or "Mandante"))
            _fa = html.escape(str(_free_match.get("away") or _free_match.get("away_name") or "Visitante"))
            _fc = html.escape(str(_free_match.get("competition") or ""))
            _ft = html.escape(str(_free_match.get("time") or "Horário a confirmar"))
            st.markdown(f'<div class="gm-game-card"><div class="gm-game-time">{_ft}</div><div class="gm-game-body"><div class="gm-game-league">{_fc}</div><div class="gm-game-teams">{_fh} <span>×</span> {_fa}</div></div></div>', unsafe_allow_html=True)
        gm_render_pro_lock("Análise completa exclusiva do GM SCORE Pro", "Você pode consultar o jogo, horário e competição no Free. Probabilidades, projeções e oportunidades são conteúdo Pro.", key="gm_analysis_free_pro")

_gm_games_direct_analysis = (
    _gm_main_view == "analysis"
    and st.session_state.get("gm_analysis_origin") == "games"
)
if not _gm_games_direct_analysis:
    st.markdown("---")
    if _gm_main_view != "account":
        st.link_button("✈️ Suporte pelo Telegram", "https://t.me/suport_gm", use_container_width=True)

    st.caption("As chances são estimativas estatísticas e não garantem resultado. Use como apoio à análise e aposte com responsabilidade.")

