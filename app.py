import io
import math
import os
import re
import time
import unicodedata
from html.parser import HTMLParser
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components

# ============================================================
# CONFIGURAÇÃO
# ============================================================
st.set_page_config(
    page_title="GM SCORE",
    page_icon="⚽",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<div class="gm-brand" style="margin:0 0 1.15rem 0">
  <div style="display:flex;align-items:center;gap:.65rem;line-height:1">
    <span style="font-size:2.35rem">⚽</span>
    <span class="gm-brand-title">GM SCORE</span>
  </div>
  <div class="gm-brand-subtitle">ANÁLISE • ESTATÍSTICAS • PROBABILIDADES</div>
  <div class="gm-brand-credit">CRIADO E VALIDADO POR GUILHERME MEDEIROS</div>
</div>
""", unsafe_allow_html=True)

st.markdown("""
<style>
/* Marca GM SCORE. No tema claro usamos preenchimento escuro e uma borda
   verde de gramado REAL ao redor das letras. O text-shadow fica como fallback
   para navegadores que tratem o text-stroke de forma diferente. */
.gm-brand-title {
  font-size:2.35rem;
  font-weight:850;
  letter-spacing:-.04em;
  color:#111827 !important;
  -webkit-text-stroke:2px #16803a !important;
  paint-order:stroke fill;
  text-shadow:
    -1px -1px 0 #16803a,
     1px -1px 0 #16803a,
    -1px  1px 0 #16803a,
     1px  1px 0 #16803a;
}
.gm-brand-subtitle {
  margin-top:.55rem;
  font-size:.82rem;
  font-weight:700;
  letter-spacing:.08em;
  color:color-mix(in srgb, var(--text-color) 68%, transparent);
}
.gm-brand-credit {
  margin-top:.25rem;
  font-size:.78rem;
  color:color-mix(in srgb, var(--text-color) 54%, transparent);
}

/* No tema escuro, muda somente a cor das fontes da identidade GM SCORE. */
@media (prefers-color-scheme: dark) {
  .gm-brand-title {
    color:#f8fafc !important;
    -webkit-text-stroke:0 !important;
    text-shadow:none !important;
  }
  .gm-brand-subtitle { color:#cbd5e1 !important; }
  .gm-brand-credit { color:#94a3b8 !important; }
}
</style>
""", unsafe_allow_html=True)

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
    r"(?:\bu\s*[- ]?1[789]\b|\bsub\s*[- ]?1[789]\b|\byouth\b|\bjunior(?:es|s)?\b|"
    r"\breserv(?:e|es|as?)\b|\bb\s*team\b|\bfemin(?:ino|ina|ine|ine)?\b|\bwomen(?:'s)?\b)",
    re.IGNORECASE,
)

def is_main_senior_team_name(name):
    text = str(name or "").strip()
    if not text:
        return False
    return SECONDARY_TEAM_RE.search(text) is None

COMPETITION_ICONS = {
    "Inglaterra - Premier League": "🇬🇧", "Espanha - La Liga": "🇪🇸",
    "Itália - Serie A": "🇮🇹", "Alemanha - Bundesliga": "🇩🇪",
    "França - Ligue 1": "🇫🇷", "Portugal - Liga Portugal": "🇵🇹",
    "Holanda - Eredivisie": "🇳🇱", "Escócia - Premiership": "🏴",
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
    "Inglaterra - Premier League": 1.15, "Espanha - La Liga": 1.10,
    "Itália - Serie A": 1.08, "Alemanha - Bundesliga": 1.08,
    "França - Ligue 1": 1.04, "Portugal - Liga Portugal": 0.99,
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


def clean_col(text):
    text = str(text).strip().lower()
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")



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


@st.cache_data(ttl=180, show_spinner=False)
def fetch_public_market_odds(home, away, league_name):
    """Busca odds 1X2 em fonte pública, sem chave. Retorna None se a fonte
    não puder ser lida. A análise nunca depende deste retorno para funcionar.
    """
    paths = ODDSPORTAL_PATHS.get(league_name, [])
    # Por último tenta a página geral de futebol, útil para jogos do dia.
    urls = [f"https://www.oddsportal.com/football/{p.strip('/')}/" for p in paths]
    urls.append("https://www.oddsportal.com/football/")
    errors = []
    for url in urls:
        try:
            r = requests.get(url, headers=HEADERS, timeout=12)
            if r.status_code != 200 or len(r.text) < 500:
                errors.append(f"HTTP {r.status_code}")
                continue
            parsed = _public_odds_from_text(_extract_visible_text(r.text), home, away)
            if parsed:
                parsed.update({"source": "OddsPortal", "source_url": url, "updated_at": datetime.now(BRASILIA_TZ).isoformat(timespec="minutes")})
                return parsed
        except Exception as exc:
            errors.append(str(exc))
    return {"error": "odds públicas indisponíveis", "details": errors[-2:]}


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
    """Mercado calibra a chance final, sem substituir a estimativa independente."""
    if not model_probs or not moneyline:
        return model_probs
    w = max(0.0, min(float(market_weight), 0.88))
    out = dict(model_probs)
    out["model_home"] = float(model_probs["home"])
    out["model_draw"] = float(model_probs["draw"])
    out["model_away"] = float(model_probs["away"])
    out["home"] = out["model_home"]*(1-w) + moneyline["home_prob"]*w
    out["draw"] = out["model_draw"]*(1-w) + moneyline["draw_prob"]*w
    out["away"] = out["model_away"]*(1-w) + moneyline["away_prob"]*w
    total = out["home"] + out["draw"] + out["away"]
    for k in ("home", "draw", "away"):
        out[k] = out[k] / total * 100
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
    st.caption("As odds públicas servem apenas para calibrar a leitura. A probabilidade de mercado abaixo remove a margem do 1X2; a odd justa continua sendo calculada pelo modelo independente do GM SCORE.")
    model = {
        "home": probs.get("model_home", probs.get("home")),
        "draw": probs.get("model_draw", probs.get("draw")),
        "away": probs.get("model_away", probs.get("away")),
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
            st.caption(f"Mercado sem margem: {marketp:.1f}% · GM: {model[key]:.1f}%")
            st.markdown(f'<span style="font-weight:700;color:{vr["color"]}">{vr["status"]}</span> · justa **{vr["fair_odd"]:.2f}** · edge **{vr["edge"]:+.1f}%**', unsafe_allow_html=True)
    st.caption(f"Fonte pública de referência: {moneyline.get('source','OddsPortal')} · pode haver pequena defasagem; confira a cotação antes de apostar.")

def to_num(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, str):
        value = value.replace("%", "").replace(",", ".").strip()
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def empty_team(name):
    return {
        "Time": name,
        "Jogos": 0,
        "Gols pró": 0.0,
        "Gols contra": 0.0,
        **{m: 0.0 for m in DISPLAY_METRICS if m not in ("Jogos", "Gols pró", "Gols contra")},
        **{f"_n_{m}": 0 for m in DISPLAY_METRICS if m not in ("Jogos", "Gols pró", "Gols contra")},
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
        if use_a:
            A["Jogos"] += 1
            A["Gols pró"] += ag; A["Gols contra"] += hg
            for metric, (hc, ac) in FD_STATS.items():
                if ac in df.columns: add_metric(A, metric, g.get(ac))
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


def contextual_analysis_rows(team_a, team_b, competition_name, competition_df, recent_games=10):
    """Completa amostras pequenas com histórico doméstico + nível da liga + torneio."""
    base_a = competition_df[competition_df["Time"] == team_a].iloc[0].to_dict()
    base_b = competition_df[competition_df["Time"] == team_b].iloc[0].to_dict()
    ga = int(float(base_a.get("Jogos", 0) or 0)); gb = int(float(base_b.get("Jogos", 0) or 0))
    # Só há necessidade de complemento real quando a competição é continental ou
    # uma das equipes tem menos de 6 jogos na base selecionada.
    if competition_name not in CONTEXTUAL_COMPETITIONS and min(ga, gb) >= 6:
        return pd.Series(base_a), pd.Series(base_b), None

    prof_a = load_team_domestic_profile(team_a, recent_games)
    prof_b = load_team_domestic_profile(team_b, recent_games)
    hist = load_competition_history_context(competition_name, current_season_year(COMPETITIONS[competition_name]["season"]), 5) if competition_name in CONTEXTUAL_COMPETITIONS else {"matches": [], "goal_avg": None, "seasons": 0}
    priors = dict(COMPETITION_PRIORS.get(competition_name, {}))
    if hist.get("goal_avg"):
        priors["Gols"] = max(1.8, min(3.8, float(hist["goal_avg"])))

    def build(base, prof, side):
        out = dict(base)
        cg = int(float(base.get("Jogos", 0) or 0))
        prow = (prof or {}).get("row", {})
        strength = (prof or {}).get("league_strength", 0.95)
        # Priors por equipe; para mercados totais dividimos em duas parcelas.
        goal_team_prior = (priors.get("Gols") / 2.0) if priors.get("Gols") else None
        corner_team_prior = (priors.get("Escanteios") / 2.0) if priors.get("Escanteios") else None
        card_team_prior = (priors.get("Cartões") / 2.0) if priors.get("Cartões") else None
        prior_map = {
            "Gols pró": goal_team_prior, "Gols contra": goal_team_prior,
            "Escanteios": corner_team_prior, "Amarelos": card_team_prior,
            "Vermelhos": 0.10 if priors.get("Cartões") else None,
            "Finalizações": None, "Chutes no alvo": None, "Faltas": None,
        }
        for metric in ["Gols pró", "Gols contra", "Escanteios", "Amarelos", "Vermelhos", "Faltas", "Finalizações", "Chutes no alvo"]:
            out[metric] = _blend_metric(
                base.get(metric), cg, prow.get(metric), prior_map.get(metric),
                strength, metric,
            )
        # Mantém o tamanho de amostra real da competição na tela, mas anota a base
        # contextual separadamente.
        return pd.Series(out)

    a = build(base_a, prof_a, "home")
    b = build(base_b, prof_b, "away")

    # H2H atual + edições anteriores do próprio torneio.
    h2h_pool = list(competition_df.attrs.get("matches", [])) + list(hist.get("matches", []))
    hh, ah, h2n = _h2h(team_a, team_b, h2h_pool, 8)

    def relevance(prof):
        if not prof:
            return 0.50
        league_component = max(0.0, min(1.0, (prof["league_strength"] - 0.82) / 0.36))
        elo = prof.get("elo")
        if elo is not None:
            # ClubElo é a âncora de força global: captura qualidade estrutural e
            # histórico recente em confrontos de níveis diferentes.
            elo_component = max(0.0, min(1.0, (float(elo) - 1350.0) / 700.0))
            return (0.58 * elo_component + 0.20 * league_component +
                    0.14 * prof.get("season_strength", .5) + 0.08 * prof.get("form", .5))
        return 0.48 * league_component + 0.34 * prof.get("season_strength", .5) + 0.18 * prof.get("form", .5)

    ctx = {
        "home_profile": prof_a, "away_profile": prof_b,
        "history": hist, "h2h_games": h2n, "h2h_home": hh, "h2h_away": ah,
        "home_relevance": relevance(prof_a), "away_relevance": relevance(prof_b),
        "competition_games_home": ga, "competition_games_away": gb,
        "priors": priors,
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

    # Mando vale cerca de 45 pontos. Qualidade global deve superar mando quando
    # existe uma diferença clara entre os clubes.
    home_adv = 45.0
    if helo is not None and aelo is not None:
        quality_diff = float(helo) + home_adv - float(aelo)
        # Potencial ofensivo e forma complementam o rating, sem duplicá-lo.
        quality_diff += (hatk - aatk) * 115.0
        quality_diff += (hform - aform) * 55.0
    else:
        # Fallback sem Elo: nível da liga recebe peso alto justamente para não
        # equiparar campanhas domésticas de contextos competitivos distintos.
        quality_diff = home_adv
        quality_diff += (hls - als) * 1150.0
        quality_diff += (hatk - aatk) * 180.0
        quality_diff += (hseason - aseason) * 95.0
        quality_diff += (hform - aform) * 60.0

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
        w = max(0.36, 0.62 - min(float(sample), 8.0) * 0.035)
    else:
        w = max(0.20, 0.36 - min(float(sample), 12.0) * 0.010)

    # Se a hierarquia estrutural aponta favorito claro e o modelo de poucos
    # jogos aponta o adversário, aumenta o guardrail. Ainda não vira 100% prior.
    if qfav != mfav and qgap >= 12:
        w = max(w, 0.62 if contextual else 0.46)
    if qgap >= 24:
        w = max(w, 0.68 if contextual else 0.50)

    vals = [m[i] * (1.0-w) + q[i] * w for i in range(3)]
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
        h2_delta = max(-0.035, min(0.035, (ctx.get("h2h_home", .5) - ctx.get("h2h_away", .5)) * 0.055))
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


def build_opportunities(a, b, team_a, team_b):
    """Seleciona somente a linha mais útil por mercado.

    Regra: entre os overs, prefere a LINHA MAIS ALTA que ainda mantenha 80%+.
    Se nenhuma chegar a 80%, escolhe a de maior probabilidade com pelo menos 70%.
    Isso evita sequências repetitivas (+15,5 e +17,5 do mesmo mercado) e linhas
    excessivamente fáceis quando existe uma alternativa mais ajustada ao jogo.
    """
    candidates = []

    def add_market(group, emoji, label, lam, lines, basis, min_good=70):
        opts = []
        for line in lines:
            pct = prob_over_half_line(max(float(lam), 0.05), line) * 100
            if pct >= min_good:
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
        lam_total = lam_a + lam_b
        add_market("Gols", "⚽", "gols", lam_total, (0.5, 1.5, 2.5, 3.5, 4.5, 5.5), f"Média projetada: {lam_total:.2f} gols", min_good=65)

    specs = [
        ("Escanteios", "⛳", "escanteios", (4.5, 5.5, 6.5, 7.5, 8.5, 9.5, 10.5, 11.5)),
        ("Cartões", "🟨", "cartões", (1.5, 2.5, 3.5, 4.5, 5.5, 6.5)),
        ("Faltas", "🚫", "faltas", (13.5, 15.5, 17.5, 19.5, 21.5, 23.5, 25.5, 27.5)),
        ("Finalizações", "🎯", "finalizações", (13.5, 15.5, 17.5, 19.5, 21.5, 23.5, 25.5)),
        ("Chutes no alvo", "🥅", "chutes no alvo", (3.5, 4.5, 5.5, 6.5, 7.5, 8.5, 9.5)),
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
        add_market(metric, emoji, label, lam, lines, f"Média combinada: {lam:.2f}")

    # Exibe primeiro as linhas fortes; uma única sugestão por categoria.
    candidates.sort(key=lambda x: (-x["Chance"], x["Categoria"]))
    return candidates[:6]


def match_expectations(a, b):
    """Projeções a partir das médias atuais; só retorna mercados realmente disponíveis."""
    out = {}
    a_gf, a_ga = metric_value(a, "Gols pró"), metric_value(a, "Gols contra")
    b_gf, b_ga = metric_value(b, "Gols pró"), metric_value(b, "Gols contra")
    if None not in (a_gf, a_ga, b_gf, b_ga):
        lam_a = max(((a_gf + b_ga) / 2) * 1.08, 0.05)
        lam_b = max(((b_gf + a_ga) / 2) / 1.08, 0.05)
        out["Gols"] = {"total": lam_a + lam_b, "home": lam_a, "away": lam_b}
    ac, bc = metric_value(a, "Escanteios"), metric_value(b, "Escanteios")
    if ac is not None and bc is not None:
        out["Escanteios"] = {"total": max(ac + bc, 0.05), "home": max(ac, 0.01), "away": max(bc, 0.01)}
    ay, by = metric_value(a, "Amarelos"), metric_value(b, "Amarelos")
    ar, br = metric_value(a, "Vermelhos"), metric_value(b, "Vermelhos")
    if ay is not None and by is not None:
        home_cards, away_cards = ay + (ar or 0), by + (br or 0)
        out["Cartões"] = {"total": max(home_cards + away_cards, 0.05), "home": max(home_cards, 0.01), "away": max(away_cards, 0.01)}
    sa, sb = metric_value(a, "Finalizações"), metric_value(b, "Finalizações")
    if sa is not None and sb is not None:
        out["Finalizações"] = {"total": sa + sb, "home": sa, "away": sb}
    ta, tb = metric_value(a, "Chutes no alvo"), metric_value(b, "Chutes no alvo")
    if ta is not None and tb is not None:
        out["Chutes no alvo"] = {"total": ta + tb, "home": ta, "away": tb}
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
    if len(samples) < 6:
        return None
    ft_total = sum(ft for _, ft in samples)
    if ft_total <= 0:
        return None
    share = max(0.30, min(0.58, sum(ht for ht, _ in samples) / ft_total))
    first = total_expected * share
    return {"first": first, "second": max(total_expected - first, 0), "games": len(samples)}


def _prob_color(pct):
    if pct >= 80:
        return "#16a34a", "#f0fdf4"
    if pct >= 65:
        return "#d97706", "#fffbeb"
    return "#dc2626", "#fef2f2"


def _prob_circle(pct):
    border, bg = _prob_color(pct)
    return f'<div style="width:46px;height:46px;border-radius:50%;border:3px solid {border};background:{bg};display:flex;align-items:center;justify-content:center;font-weight:750;font-size:12px;color:#172033;margin:auto">{pct:.0f}%</div>'


def render_probability_matrix(title, emoji, rows, lines):
    line_headers = "".join(
        f'<div style="text-align:center;font-size:11px;color:#64748b;font-weight:650">+{str(line).replace(".", ",")}</div>'
        for line in lines
    )
    html = (
        f'<div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:13px;padding:13px 14px 11px;margin:7px 0 14px;box-shadow:0 1px 2px rgba(15,23,42,.03)">'
        f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:11px">'
        f'<div style="font-weight:700;color:#172033;font-size:14px">{emoji} {title}</div>'
        f'<div style="font-size:10px;color:#94a3b8">probabilidade estimada</div></div>'
        f'<div style="display:grid;grid-template-columns:minmax(105px,1.45fr) repeat({len(lines)},minmax(52px,1fr));gap:7px;align-items:center"><div></div>{line_headers}'
    )
    for label, lam in rows:
        html += f'<div style="font-size:11px;color:#334155;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="{label}">{label}</div>'
        for line in lines:
            pct = prob_over_half_line(max(float(lam), 0.01), line) * 100
            html += _prob_circle(pct)
    html += '</div><div style="margin-top:9px;font-size:10px;color:#94a3b8">🟢 80%+ &nbsp; • &nbsp; 🟡 65–79% &nbsp; • &nbsp; 🔴 abaixo de 65%</div></div>'
    st.markdown(html, unsafe_allow_html=True)


def render_match_probability_dashboard(a, b, team_a, team_b, df):
    ex = match_expectations(a, b)
    if not ex:
        return ex
    st.markdown("### 📈 Expectativa da partida")
    cards = []
    if "Gols" in ex: cards.append(("⚽ Gols esperados", ex["Gols"]["total"]))
    if "Escanteios" in ex: cards.append(("⛳ Escanteios esperados", ex["Escanteios"]["total"]))
    if "Cartões" in ex: cards.append(("🟨 Cartões esperados", ex["Cartões"]["total"]))
    if "Finalizações" in ex: cards.append(("🎯 Finalizações", ex["Finalizações"]["total"]))
    if "Chutes no alvo" in ex: cards.append(("🥅 No alvo", ex["Chutes no alvo"]["total"]))
    if cards:
        cols = st.columns(len(cards))
        for col, (label, value) in zip(cols, cards):
            col.metric(label, f"{value:.1f}".replace(".", ","))
    if "Gols" in ex:
        split = expected_goals_by_half(team_a, team_b, df.attrs.get("matches", []), ex["Gols"]["total"])
        if split:
            st.markdown("#### ⏱️ Distribuição esperada de gols")
            c1, c2, c3 = st.columns(3)
            c1.metric("1º tempo", f"{split['first']:.2f}".replace(".", ","))
            c2.metric("Jogo todo", f"{ex['Gols']['total']:.2f}".replace(".", ","))
            c3.metric("2º tempo", f"{split['second']:.2f}".replace(".", ","))
            st.caption(f"Distribuição baseada em {split['games']} partidas com placar de intervalo disponível.")
        else:
            st.caption("⏱️ Separação por 1º/2º tempo indisponível nesta fonte — o app não divide a média artificialmente.")
    st.markdown("#### 🎯 Probabilidades — Mais de")
    tab_names = []
    if "Gols" in ex: tab_names.append("⚽ Gols")
    if "Escanteios" in ex: tab_names.append("⛳ Escanteios")
    if "Cartões" in ex: tab_names.append("🟨 Cartões")
    if not tab_names:
        return ex
    tabs = st.tabs(tab_names)
    i = 0
    if "Gols" in ex:
        with tabs[i]:
            render_probability_matrix("Frequência de gols", "⚽", [("Partida", ex["Gols"]["total"]), (team_a, ex["Gols"]["home"]), (team_b, ex["Gols"]["away"])], [0.5, 1.5, 2.5, 3.5, 4.5])
        i += 1
    if "Escanteios" in ex:
        with tabs[i]:
            render_probability_matrix("Frequência de escanteios", "⛳", [("Partida", ex["Escanteios"]["total"]), (team_a, ex["Escanteios"]["home"]), (team_b, ex["Escanteios"]["away"])], [2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5, 9.5])
        i += 1
    if "Cartões" in ex:
        with tabs[i]:
            render_probability_matrix("Frequência de cartões", "🟨", [("Partida", ex["Cartões"]["total"]), (team_a, ex["Cartões"]["home"]), (team_b, ex["Cartões"]["away"])], [0.5, 1.5, 2.5, 3.5, 4.5, 5.5])
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


def _h2h(home, away, matches, n=6):
    pts_h = pts_a = games = 0
    for m in reversed(matches):
        if {m["home"], m["away"]} != {home, away}:
            continue
        hg, ag = m["hg"], m["ag"]
        if hg == ag:
            pts_h += 1; pts_a += 1
        else:
            winner = m["home"] if hg > ag else m["away"]
            if winner == home: pts_h += 3
            else: pts_a += 3
        games += 1
        if games >= n: break
    if not games:
        return 0.5, 0.5, 0
    total = max(pts_h + pts_a, 1)
    return pts_h / total, pts_a / total, games


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
        found.append({"competition":competition,"home":home,"away":away,"time":br_time})
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
    url = f"https://www.livescore.mobi/football/{source_date:%Y-%m-%d}/"
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
        m = re.search(r"/([^/]+-vs-[^/]+)/\d+/?(?:\?.*)?$", href)
        if not m:
            continue
        matchup = m.group(1)
        home_slug, away_slug = matchup.split("-vs-", 1)
        home, away = _pretty_slug_team(home_slug), _pretty_slug_team(away_slug)
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


@st.cache_data(ttl=3600, show_spinner=False)
def load_today_fixtures():
    today = datetime.now(ZoneInfo("America/Sao_Paulo")).date()
    fixtures = []

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
                    fixtures.append({"competition": comp, "home": str(g["HomeTeam"]), "away": str(g["AwayTeam"]), "time": br_time})
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
                fixtures.append({"competition": comp, "home": m.get("team1"), "away": m.get("team2"), "time": br_time})
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

    # Remove duplicados mesmo quando as fontes usam nomes diferentes
    # (ex.: "Vitoria" x "EC Vitória"; "Gremio" x "Grêmio FBPA").
    best = {}
    order = []
    for f in fixtures:
        if not valid_daily_fixture(f):
            continue
        comp = f.get("competition")
        if comp in STRICT_OFFICIAL_ROSTERS:
            roster = CURRENT_TEAM_ROSTERS.get(comp, [])
            f = dict(f)
            f["home"] = resolve_team_name(f.get("home"), roster)
            f["away"] = resolve_team_name(f.get("away"), roster)
        key = (f["competition"], fixture_team_key(f["home"]), fixture_team_key(f["away"]))
        if key not in best:
            best[key] = f
            order.append(key)
        elif _fixture_quality(f) > _fixture_quality(best[key]):
            best[key] = f
    return [best[k] for k in order if valid_daily_fixture(best[k])]

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
if "_goto_comp" in st.session_state:
    st.session_state.selected_competition = st.session_state.pop("_goto_comp")
    st.session_state.selected_home = st.session_state.pop("_goto_home", None)
    st.session_state.selected_away = st.session_state.pop("_goto_away", None)
    st.session_state.loaded_home = st.session_state.selected_home
    st.session_state.loaded_away = st.session_state.selected_away
    st.session_state.loaded_competition = st.session_state.selected_competition
    st.session_state.league_widget = st.session_state.selected_competition

if "selected_competition" not in st.session_state:
    st.session_state.selected_competition = list(COMPETITIONS.keys())[0]
if "selected_home" not in st.session_state:
    st.session_state.selected_home = None
if "selected_away" not in st.session_state:
    st.session_state.selected_away = None
st.sidebar.markdown("### ⚽ GM SCORE")
st.sidebar.caption("Configurar análise")
league_name = st.sidebar.selectbox(
    "🏆 Competição", list(COMPETITIONS.keys()),
    index=list(COMPETITIONS.keys()).index(st.session_state.selected_competition)
    if st.session_state.selected_competition in COMPETITIONS else 0,
    key="league_widget",
    format_func=competition_display_name,
)
st.session_state.selected_competition = league_name
config = COMPETITIONS[league_name]
used_year = current_season_year(config["season"])
st.sidebar.caption(f"📅 Temporada atual: {season_label(used_year, config['season'])}")
period = st.sidebar.selectbox(
    "📊 Período", [5, 10, 20, 0], index=1,
    format_func=lambda n: "Temporada" if n == 0 else f"Últimos {n} jogos",
)
if st.sidebar.button("🔄 Atualizar dados", type="primary"):
    st.cache_data.clear(); st.rerun()


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
    if candidate in teams:
        return candidate
    if not candidate:
        return None
    c = clean_col(candidate)
    exact = {clean_col(t): t for t in teams}
    if c in exact:
        return exact[c]
    c_tokens = set(c.split("_")) - {"fc","cf","ac","sc","ec","club","de","da","do"}
    best, best_score = None, 0.0
    for t in teams:
        tt = set(clean_col(t).split("_")) - {"fc","cf","ac","sc","ec","club","de","da","do"}
        if not c_tokens or not tt:
            continue
        score = len(c_tokens & tt) / len(c_tokens | tt)
        if score > best_score:
            best, best_score = t, score
    return best if best_score >= 0.45 else None


def render_share_button(team_a, team_b, league_name, probs, opportunities, expectations, a, b):
    """Cria uma imagem longa com os dados da análise e marca d'água GM SCORE."""
    import json as _json

    result_lines = []
    if probs:
        result_lines = [
            [f"Vitória {team_a}", f"{probs['home']:.0f}%"],
            ["Empate", f"{probs['draw']:.0f}%"],
            [f"Vitória {team_b}", f"{probs['away']:.0f}%"],
        ]

    exp_lines = []
    for key, label in [("Gols","Gols esperados"),("Escanteios","Escanteios esperados"),("Cartões","Cartões esperados"),("Finalizações","Finalizações"),("Chutes no alvo","Chutes no alvo")]:
        item = (expectations or {}).get(key)
        if item:
            exp_lines.append([label, f"{item['total']:.1f}"])

    opp_lines = [[x["Mercado"], f"{x['Chance']:.0f}%", x["Base"]] for x in (opportunities or [])]
    metric_labels = ["Jogos","Gols pró","Gols contra","Escanteios","Amarelos","Vermelhos","Faltas","Finalizações","Chutes no alvo","Posse (%)","Impedimentos"]
    avg_lines = []
    for metric in metric_labels:
        if metric not in a.index or metric not in b.index:
            continue
        av, bv = a[metric], b[metric]
        if pd.isna(av) and pd.isna(bv):
            continue
        def fmt(v):
            if pd.isna(v): return "N/D"
            try:
                return str(int(v)) if metric == "Jogos" else f"{float(v):.2f}"
            except Exception:
                return str(v)
        avg_lines.append([metric, fmt(av), fmt(bv)])

    data = _json.dumps({
        "title": f"{team_a} × {team_b}", "league": competition_display_name(league_name),
        "results": result_lines, "expectations": exp_lines, "opportunities": opp_lines,
        "averages": avg_lines, "home": team_a, "away": team_b,
    }, ensure_ascii=False)

    html = f"""
    <div style='font-family:Arial,sans-serif'>
      <button id='shareBtn' style='width:100%;padding:12px 16px;border:0;border-radius:9px;background:#25D366;color:white;font-size:16px;font-weight:700;cursor:pointer'>📲 Compartilhar análise completa</button>
      <div id='msg' style='font-size:12px;color:#6b7280;margin-top:6px'></div>
    </div>
    <script>
    const D = {data};
    function text(ctx, value, x, y, size=30, weight='normal', color='#172033') {{
      ctx.fillStyle=color; ctx.font=`${{weight}} ${{size}}px Arial`; ctx.fillText(value,x,y);
    }}
    function wrap(ctx, value, x, y, maxWidth, lineHeight) {{
      const words=String(value).split(' '); let line='', yy=y;
      for(const w of words) {{ const t=line+w+' '; if(ctx.measureText(t).width>maxWidth && line) {{ctx.fillText(line,x,yy); yy+=lineHeight; line=w+' ';}} else line=t; }}
      ctx.fillText(line,x,yy); return yy;
    }}
    function watermark(ctx,w,h) {{
      ctx.save(); ctx.globalAlpha=.045; ctx.fillStyle='#172033'; ctx.font='bold 48px Arial'; ctx.translate(w/2,h/2); ctx.rotate(-Math.PI/7);
      for(let y=-h;y<h;y+=180) for(let x=-w;x<w;x+=360) ctx.fillText('GM SCORE',x,y);
      ctx.restore();
    }}
    document.getElementById('shareBtn').onclick = async () => {{
      const extra = D.averages.length*48 + D.opportunities.length*88 + D.expectations.length*58 + D.results.length*58;
      const logicalW=1080, logicalH=Math.max(1900,1050+extra), scale=4/3;
      const canvas=document.createElement('canvas'); canvas.width=Math.round(logicalW*scale); canvas.height=Math.round(logicalH*scale);
      const ctx=canvas.getContext('2d'); ctx.scale(scale,scale); ctx.fillStyle='#fff'; ctx.fillRect(0,0,logicalW,logicalH); watermark(ctx,logicalW,logicalH);
      let y=90; text(ctx,'⚽ GM SCORE',65,y,52,'bold'); y+=62; text(ctx,'ANÁLISE • ESTATÍSTICAS • PROBABILIDADES',65,y,23,'bold','#64748b');
      y+=72; text(ctx,D.title,65,y,44,'bold'); y+=42; text(ctx,D.league,65,y,25,'normal','#64748b'); y+=70;
      const section=(t)=>{{ text(ctx,t,65,y,31,'bold'); y+=30; ctx.strokeStyle='#e5e7eb'; ctx.beginPath();ctx.moveTo(65,y);ctx.lineTo(1015,y);ctx.stroke(); y+=45; }};
      if(D.results.length) {{ section('🏆 Chance de resultado'); for(const r of D.results) {{text(ctx,r[0],80,y,27); text(ctx,r[1],930,y,30,'bold',parseInt(r[1])>=70?'#16a34a':'#172033'); y+=55;}} y+=20; }}
      if(D.expectations.length) {{ section('📈 Expectativa da partida'); for(const r of D.expectations) {{text(ctx,r[0],80,y,27); text(ctx,r[1],930,y,29,'bold'); y+=55;}} y+=20; }}
      if(D.opportunities.length) {{ section('⭐ Melhores linhas para observar'); for(const r of D.opportunities) {{ctx.font='bold 27px Arial';ctx.fillStyle='#172033'; y=wrap(ctx,r[0],80,y,700,34); text(ctx,r[1],930,y,30,'bold',parseInt(r[1])>=80?'#16a34a':'#b7791f'); y+=34; ctx.font='22px Arial';ctx.fillStyle='#64748b'; y=wrap(ctx,r[2],80,y,820,29); y+=45;}} }}
      if(D.averages.length) {{ section('📊 Médias usadas na análise'); text(ctx,'Dado',80,y,23,'bold','#64748b'); text(ctx,D.home,550,y,21,'bold','#64748b'); text(ctx,D.away,820,y,21,'bold','#64748b'); y+=42; for(const r of D.averages) {{text(ctx,r[0],80,y,23);text(ctx,r[1],580,y,23,'bold');text(ctx,r[2],850,y,23,'bold');y+=46;}} }}
      y+=45; text(ctx,'CRIADO E VALIDADO POR GUILHERME MEDEIROS',65,y,21,'bold','#94a3b8'); y+=38; text(ctx,'Estimativas estatísticas; não garantem resultado.',65,y,20,'normal','#94a3b8');
      canvas.toBlob(async blob=>{{
        const file=new File([blob],'gm-score-analise-completa-HD.jpg',{{type:'image/jpeg'}});
        try {{
          if(navigator.share && (!navigator.canShare || navigator.canShare({{files:[file]}}))) {{ await navigator.share({{title:D.title,text:'GM SCORE - análise completa',files:[file]}}); document.getElementById('msg').innerText='Escolha o WhatsApp na tela de compartilhamento.'; }}
          else {{ const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=file.name;a.click();document.getElementById('msg').innerText='A imagem completa foi salva para compartilhar no WhatsApp.'; }}
        }} catch(e) {{ if(e.name!=='AbortError') document.getElementById('msg').innerText='Não foi possível abrir o compartilhamento neste navegador.'; }}
      }},'image/jpeg',0.95);
    }};
    </script>
    """
    components.html(html, height=82)


def render_analysis():
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

    # O cabeçalho de seleção sempre identifica a competição ativa e, quando há
    # jogo carregado, mantém os selectboxes sincronizados com esse confronto.
    st.markdown(f"**🏆 Competição:** {competition_display_name(league_name)}")
    if loaded_home_now and loaded_away_now:
        st.caption(f"✅ Jogo carregado: {loaded_home_now} × {loaded_away_now}")

        # Sincroniza os widgets SOMENTE quando o confronto carregado muda
        # (por exemplo, ao tocar em "Analisar" nos jogos do dia). Não fazemos
        # isso em todo rerun, pois isso impediria o usuário de escolher outra
        # equipe e faria o botão "Carregar equipes" parecer não funcionar.
        loaded_signature = f"{league_name}|{loaded_home_now}|{loaded_away_now}"
        if st.session_state.get("_synced_loaded_signature") != loaded_signature:
            st.session_state["home_widget"] = loaded_home_now
            st.session_state["away_widget"] = loaded_away_now
            st.session_state["_synced_loaded_signature"] = loaded_signature

    c1, c2 = st.columns(2)
    with c1:
        team_a = st.selectbox("🏠 Time da casa", teams, index=teams.index(default_home), key="home_widget")
    with c2:
        team_b = st.selectbox("✈️ Time visitante", teams, index=teams.index(default_away), key="away_widget")
    # A seleção só passa a valer quando o usuário confirma. Isso evita que
    # uma análise antiga permaneça na tela enquanto os seletores já mostram
    # outras equipes (especialmente no celular).
    if team_a == team_b:
        st.warning("Selecione duas equipes diferentes.")
        return

    if st.button("⚽ Carregar equipes", type="primary", use_container_width=True, key="load_teams_btn"):
        st.session_state.selected_home = team_a
        st.session_state.selected_away = team_b
        st.session_state.loaded_home = team_a
        st.session_state.loaded_away = team_b
        st.session_state["_synced_loaded_signature"] = f"{league_name}|{team_a}|{team_b}"
        st.rerun()

    loaded_home = resolve_team_name(st.session_state.get("loaded_home"), teams)
    loaded_away = resolve_team_name(st.session_state.get("loaded_away"), teams)

    # Se o usuário mexeu nos seletores, não deixamos uma análise antiga visível
    # com nomes diferentes. A nova seleção só entra após "Carregar equipes".
    if loaded_home and loaded_away and (team_a != loaded_home or team_b != loaded_away):
        st.info("Você alterou o confronto. Toque em **⚽ Carregar equipes** para carregar esta nova análise.")
        return

    # Na primeira abertura da competição, não exibe uma partida aleatória.
    # Depois do clique, a análise abaixo sempre corresponde exatamente aos
    # dois clubes confirmados no botão Carregar equipes.
    if not loaded_home or not loaded_away:
        st.info("Selecione os dois times e toque em **⚽ Carregar equipes** para gerar a análise.")
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
    st.subheader(f"{team_a} × {team_b}")
    season_text = season_label(used_year, config["season"])
    if updated_until is not None and not pd.isna(updated_until):
        st.caption(f"{league_name} · {season_text} · Dados até {pd.Timestamp(updated_until):%d/%m/%Y}")
    else:
        st.caption(f"{league_name} · {season_text}")

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

    if probs:
        st.markdown("### 🏆 Chance de resultado")
        x, y, z = st.columns(3)
        x.metric(f"🏠 Vitória {team_a}", f"{probs['home']:.0f}%")
        y.metric("🤝 Empate", f"{probs['draw']:.0f}%")
        z.metric(f"✈️ Vitória {team_b}", f"{probs['away']:.0f}%")
        if probs.get("contextual") and analysis_context:
            hp = analysis_context.get("home_profile") or {}
            ap = analysis_context.get("away_profile") or {}
            sources = []
            if hp.get("competition"): sources.append(f"{team_a}: {hp['competition']}")
            if ap.get("competition"): sources.append(f"{team_b}: {ap['competition']}")
            h2n = analysis_context.get("h2h_games", 0)
            histn = analysis_context.get("history", {}).get("seasons", 0)
            detail = " • ".join(sources)
            elo_bits = []
            if probs.get("home_elo") is not None: elo_bits.append(f"Elo {team_a}: {probs['home_elo']:.0f}")
            if probs.get("away_elo") is not None: elo_bits.append(f"Elo {team_b}: {probs['away_elo']:.0f}")
            elo_text = (" • " + " | ".join(elo_bits)) if elo_bits else ""
            st.caption(f"Base contextual: força global do clube + histórico individual + nível da liga doméstica + forma recente + histórico da competição ({histn} edição(ões) encontrada(s))" + (f" + {h2n} confronto(s) direto(s)" if h2n else "") + (f". {detail}" if detail else ".") + elo_text)
        else:
            st.caption("Estimativa calibrada por força ofensiva/defensiva, desempenho casa/fora, fase recente, tamanho da amostra e confronto direto com peso reduzido.")
        if probs.get("quality_calibrated"):
            qparts = []
            if probs.get("home_elo") is not None:
                qparts.append(f"Elo {team_a}: {probs['home_elo']:.0f}")
            if probs.get("away_elo") is not None:
                qparts.append(f"Elo {team_b}: {probs['away_elo']:.0f}")
            qtxt = (" · " + " | ".join(qparts)) if qparts else ""
            st.caption(f"⚖️ Ajuste de qualidade global: força do clube + nível da liga + potencial ofensivo + forma recente ({probs.get('quality_weight',0)*100:.0f}% de peso nesta leitura){qtxt}.")
        if probs.get("market_calibrated") and moneyline:
            st.caption(f"💹 Chance final calibrada com {probs.get('market_weight',0)*100:.0f}% de peso do mercado público sem margem e {100-probs.get('market_weight',0)*100:.0f}% do modelo GM SCORE.")

    if moneyline and probs:
        render_market_value_panel(team_a, team_b, probs, moneyline)
    else:
        st.caption("💹 Odds públicas: não encontrei uma cotação 1X2 confiável para este confronto agora; a análise acima segue somente o modelo GM SCORE.")

    expectations = render_match_probability_dashboard(a, b, team_a, team_b, df)

    opportunities = build_opportunities(a, b, team_a, team_b)
    st.markdown("#### ⭐ Melhores linhas para observar")
    if analysis_context and comp_sample < 6:
        st.caption("Linhas projetadas com base híbrida enquanto a competição ainda tem pouca amostra. A influência dos dados domésticos diminui à medida que o torneio avança.")
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
        st.info("Ainda não há dados suficientes para destacar uma oportunidade.")

    st.markdown("### 📲 Compartilhar")
    render_share_button(team_a, team_b, league_name, probs, opportunities, expectations, a, b)

    with st.expander("📊 Ver médias usadas na análise"):
        metric_emojis = {"Gols pró":"⚽","Gols contra":"🥅","Escanteios":"⛳","Amarelos":"🟨","Vermelhos":"🟥","Faltas":"🚫","Finalizações":"🎯","Chutes no alvo":"🥅","Posse (%)":"⚪","Impedimentos":"🚩"}
        rows=[]
        for metric in DISPLAY_METRICS:
            if metric == "Jogos" or metric not in a.index or metric not in b.index: continue
            av,bv=a[metric],b[metric]
            if pd.isna(av) and pd.isna(bv): continue
            rows.append({"Dado":f"{metric_emojis.get(metric,'📌')} {metric}",team_a:"N/D" if pd.isna(av) else round(float(av),2),team_b:"N/D" if pd.isna(bv) else round(float(bv),2)})
        if rows: st.dataframe(pd.DataFrame(rows),hide_index=True,use_container_width=True)


def render_sidebar_today():
    st.sidebar.markdown("---")
    st.sidebar.markdown("### 📅 Jogos de hoje")
    st.sidebar.caption("🕒 Horário de Brasília")

    try:
        with st.spinner("Buscando jogos do dia..."):
            fixtures = [f for f in load_today_fixtures() if f.get("competition") == league_name]
    except Exception:
        fixtures = []

    # Segurança extra: somente a competição selecionada e confrontos válidos.
    fixtures = [
        f for f in fixtures
        if f.get("competition") == league_name and valid_daily_fixture(f)
    ]
    fixtures = sorted(fixtures, key=lambda f: str(f.get("time") or "99:99"))

    if not fixtures:
        st.sidebar.info("Nenhum jogo desta competição hoje.")
        return

    st.sidebar.caption(f"{len(fixtures)} jogo(s) encontrado(s)")
    for i, f in enumerate(fixtures):
        time_text = str(f.get("time") or "").strip()
        if time_text and time_text.lower() != "nan":
            st.sidebar.markdown(f"**⚽ {f['home']} × {f['away']}**  \n🕒 {time_text}")
        else:
            st.sidebar.markdown(f"**⚽ {f['home']} × {f['away']}**")
        if st.sidebar.button("🔎 Analisar", key=f"side_today_{i}_{clean_col(f['home'])}_{clean_col(f['away'])}", use_container_width=True):
            st.session_state["_goto_comp"] = f["competition"]
            st.session_state["_goto_home"] = f["home"]
            st.session_state["_goto_away"] = f["away"]
            st.rerun()


render_sidebar_today()
render_analysis()

st.caption("As chances são estimativas estatísticas da temporada vigente e não garantem resultado. Use como apoio à análise e aposte com responsabilidade.")

