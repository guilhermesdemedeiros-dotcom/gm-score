import io
import html
import math
import os
import re
import time
import secrets
import unicodedata
from html.parser import HTMLParser
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

# ============================================================
# CONFIGURAÇÃO
# ============================================================
GM_BUILD = "2026-09-11-core-markets-v1"
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


def gm_auth_store_session(access_token, refresh_token, user_id, email=""):
    """Guarda apenas os tokens da sessão atual do navegador Streamlit."""
    if not access_token or not refresh_token or not user_id:
        raise RuntimeError("O Supabase não retornou uma sessão válida.")
    st.session_state["gm_auth_access_token"] = str(access_token)
    st.session_state["gm_auth_refresh_token"] = str(refresh_token)
    st.session_state["gm_auth_user_id"] = str(user_id)
    st.session_state["gm_auth_email"] = str(email or "")
    st.session_state.pop("gm_auth_profile", None)


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


def gm_auth_client_from_session():
    """Restaura a sessão Supabase deste navegador em um cliente isolado."""
    access = st.session_state.get("gm_auth_access_token")
    refresh = st.session_state.get("gm_auth_refresh_token")
    if not access or not refresh:
        return None
    client = gm_new_supabase_client()
    try:
        result = client.auth.set_session(access, refresh)
        # set_session pode renovar tokens expirados. Mantemos a cópia local atualizada.
        session = getattr(result, "session", None)
        if session is not None:
            st.session_state["gm_auth_access_token"] = session.access_token
            st.session_state["gm_auth_refresh_token"] = session.refresh_token
        return client
    except Exception:
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
    token = gm_device_session_token()
    started = bool(st.session_state.get("gm_device_session_started"))
    fn = "gm_validate_session" if started else "gm_start_session"
    data = gm_session_rpc(fn, {"p_session_token": token})
    ok, reason = gm_session_result(data)
    if ok:
        st.session_state["gm_device_session_started"] = True
        # Marca quando esta sessão foi validada no servidor. O heartbeat usa
        # este relógio local apenas para evitar chamadas duplicadas logo após
        # um rerun normal do Streamlit; a autoridade continua sendo o Supabase.
        st.session_state["gm_session_last_validation_monotonic"] = time.monotonic()
    return ok, reason


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
    """Cadastro com retorno público seguro após a confirmação do e-mail."""
    client = gm_new_supabase_client()
    return client.auth.sign_up({
        "email": str(email).strip().lower(),
        "password": str(password),
        "options": {
            "data": {"nome": str(nome).strip(), "terms_accepted": True},
            "email_redirect_to": GM_EMAIL_CONFIRM_REDIRECT,
        },
    })


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


def gm_unread_news_count():
    data = gm_news_rpc("gm_unread_news_count")
    if isinstance(data, list):
        data = data[0] if data else 0
    try:
        return max(0, int(data or 0))
    except Exception:
        return 0


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
                    gm_admin_rpc("gm_admin_publish_news", {
                        "p_title": title.strip(),
                        "p_message": message.strip(),
                        "p_category": category,
                        "p_is_featured": bool(featured),
                    })
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
    status = str(row.get("vip_status") or "pending")
    if status == "active":
        return "🟢 VIP ativo"
    if status == "expired":
        return "⌛ Expirado"
    return "⏳ Pendente"


def gm_render_admin_panel(profile):
    """Painel administrativo. Todas as alterações são validadas novamente pelo banco."""
    if not profile or profile.get("role") != "admin":
        st.error("Acesso administrativo não autorizado.")
        return

    st.markdown("## 🛠 Painel Administrativo")
    st.caption("Gerencie clientes VIP. As operações são validadas no Supabase antes de qualquer alteração.")

    # Navegação principal fica realmente no topo do painel, antes das seções longas.
    top1, top2 = st.columns([1, 1])
    with top1:
        if st.button("← Voltar", use_container_width=True, key="gm_admin_back_top", help="Retornar ao GM SCORE"):
            st.session_state["gm_admin_panel_open"] = False
            st.rerun()
    with top2:
        if st.button("🔄 Atualizar", use_container_width=True, key="gm_admin_refresh", help="Atualizar dados do painel"):
            st.rerun()

    gm_render_admin_news_manager()
    st.markdown("---")
    st.markdown("### 👥 Gestão de clientes VIP")

    try:
        rows = gm_admin_list_users()
    except Exception as exc:
        st.error("Não foi possível carregar os clientes do painel administrativo.")
        st.caption(f"Detalhe técnico: {type(exc).__name__}")
        return

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
        ["Todos", "Pendentes", "VIP ativos", "Expirados", "Bloqueados"],
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
        if status_filter == "VIP ativos" and not (status == "active" and not blocked_now):
            return False
        if status_filter == "Expirados" and not (status == "expired" and not blocked_now):
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
            st.write(f"**Pagamento:** `{payment}`")
            st.write(f"**VIP até:** {vip_until}")
            st.write(f"**Cadastro:** {created}")
            st.write(f"**Aprovado em:** {approved}")

            days = st.selectbox(
                "Período do acesso",
                [30, 90, 180, 365],
                format_func=lambda d: f"{d} dias",
                key=f"gm_admin_days_{uid}",
            )

            c1, c2 = st.columns(2)
            with c1:
                if row.get("vip_status") == "pending":
                    if st.button("✅ Aprovar VIP", use_container_width=True, key=f"gm_admin_approve_{uid}"):
                        try:
                            gm_admin_rpc("gm_admin_approve_user", {"p_user_id": uid, "p_days": int(days)})
                            st.success("Cliente aprovado com sucesso.")
                            st.rerun()
                        except Exception as exc:
                            st.error("Não foi possível aprovar o cliente.")
                            st.caption(str(exc))
                else:
                    st.caption("Aprovação inicial já concluída.")
            with c2:
                if st.button("➕ Renovar VIP", use_container_width=True, key=f"gm_admin_renew_{uid}"):
                    try:
                        gm_admin_rpc("gm_admin_renew_user", {"p_user_id": uid, "p_days": int(days)})
                        st.success("VIP renovado com sucesso.")
                        st.rerun()
                    except Exception as exc:
                        st.error("Não foi possível renovar o VIP.")
                        st.caption(str(exc))

            st.markdown("#### 🎁 Cortesia / liberação manual")
            st.caption("Use para amigos, testes ou liberações sem pagamento. A operação fica registrada como cortesia, não como venda paga.")
            courtesy_days = st.selectbox(
                "Período da cortesia",
                [30, 90, 180],
                format_func=lambda d: {30: "30 dias · 1 mês", 90: "90 dias · 3 meses", 180: "180 dias · 6 meses"}[d],
                key=f"gm_admin_courtesy_days_{uid}",
            )
            if st.button("🎁 Liberar cortesia", use_container_width=True, key=f"gm_admin_courtesy_{uid}"):
                try:
                    gm_admin_rpc("gm_admin_grant_courtesy", {"p_user_id": uid, "p_days": int(courtesy_days)})
                    st.success(f"Cortesia de {courtesy_days} dias liberada com sucesso.")
                    st.rerun()
                except Exception as exc:
                    st.error("Não foi possível liberar a cortesia.")
                    st.caption(str(exc))

            p1, p2 = st.columns(2)
            with p1:
                if payment != "paid":
                    if st.button("💳 Marcar pagamento como pago", use_container_width=True, key=f"gm_admin_paid_{uid}"):
                        try:
                            gm_admin_rpc("gm_admin_set_payment", {"p_user_id": uid, "p_status": "paid"})
                            st.success("Pagamento atualizado.")
                            st.rerun()
                        except Exception as exc:
                            st.error("Não foi possível atualizar o pagamento.")
                            st.caption(str(exc))
                else:
                    st.success("💳 Pagamento confirmado")
            with p2:
                blocked_now = bool(row.get("blocked")) or row.get("vip_status") == "blocked"
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

            if st.button("🔄 Liberar dispositivo / sessão", use_container_width=True, key=f"gm_admin_reset_session_{uid}"):
                try:
                    gm_admin_rpc("gm_admin_reset_session", {"p_user_id": uid})
                    st.success("Sessão liberada. O cliente já pode entrar em outro dispositivo.")
                    st.rerun()
                except Exception as exc:
                    st.error("Não foi possível liberar a sessão do cliente.")
                    st.caption(str(exc))

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
    "🇬🇧 Inglaterra - Premier League",
    "🇪🇸 Espanha - La Liga",
    "🇮🇹 Itália - Serie A",
    "🇩🇪 Alemanha - Bundesliga",
    "🇫🇷 França - Ligue 1",
    "🇵🇹 Portugal - Liga Portugal",
    "🇳🇱 Holanda - Eredivisie",
    "🏴 Escócia - Premiership",
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
    """Vitrine pública do VIP. Todo número exibido aqui é explicitamente demonstrativo."""
    st.markdown("### 🚀 Uma prévia de como o GM SCORE organiza a partida")
    st.caption("Demonstração visual do painel VIP. Os valores abaixo são ilustrativos e não representam uma partida real.")

    st.markdown(
        """
        <style>
        .gm-vip-hero{position:relative;overflow:hidden;border:1px solid rgba(34,197,94,.38);border-radius:22px;padding:20px;
          background:
            radial-gradient(circle at 84% 14%,rgba(74,222,128,.18),transparent 28%),
            linear-gradient(145deg,rgba(22,128,58,.22),rgba(15,23,42,.94));
          box-shadow:0 18px 48px rgba(0,0,0,.22);margin:.35rem 0 1rem 0;color:#f8fafc}
        .gm-vip-hero:after{content:"";position:absolute;right:-42px;bottom:-80px;width:220px;height:220px;border:1px solid rgba(134,239,172,.12);border-radius:50%}
        .gm-vip-kicker{font-size:.70rem;font-weight:850;letter-spacing:.14em;color:#86efac;text-transform:uppercase}
        .gm-vip-title{font-size:1.65rem;font-weight:900;margin:.38rem 0 .2rem;letter-spacing:-.025em}
        .gm-vip-sub{font-size:.87rem;color:#cbd5e1;margin-bottom:1rem;max-width:720px;line-height:1.45}
        .gm-vip-scoregrid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:12px}
        .gm-vip-scorebox{background:rgba(15,23,42,.74);border:1px solid rgba(148,163,184,.20);border-radius:14px;padding:12px;text-align:center}
        .gm-vip-scorebox b{display:block;font-size:1.35rem;color:#fff;margin-top:2px}
        .gm-vip-scorebox span{font-size:.70rem;color:#94a3b8;text-transform:uppercase;letter-spacing:.06em}
        .gm-demo-bars{display:grid;gap:10px;margin-top:15px}
        .gm-bar-label{display:flex;justify-content:space-between;gap:14px;font-size:.82rem;margin-bottom:5px;color:#e2e8f0}
        .gm-bar-label b{color:#bbf7d0}
        .gm-bar-track{height:10px;border-radius:999px;background:rgba(148,163,184,.17);overflow:hidden;border:1px solid rgba(148,163,184,.08)}
        .gm-bar-fill{height:100%;border-radius:999px;background:linear-gradient(90deg,#15803d,#4ade80)}
        .gm-chip{display:inline-block;border:1px solid rgba(74,222,128,.30);background:rgba(22,163,74,.12);color:#bbf7d0;border-radius:999px;padding:5px 9px;margin:3px 3px 3px 0;font-size:.76rem;font-weight:750}
        .gm-section-card{border:1px solid rgba(148,163,184,.22);border-radius:17px;padding:14px 15px;background:rgba(30,41,59,.18);min-height:100%}
        .gm-section-card h4{margin:0 0 7px 0;font-size:.98rem}.gm-section-card p{margin:0;color:inherit;opacity:.78;font-size:.84rem;line-height:1.43}
        .gm-comp-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:9px}
        .gm-comp{border:1px solid rgba(148,163,184,.20);border-radius:12px;padding:9px 10px;background:rgba(30,41,59,.14);font-size:.83rem;font-weight:650}
        .gm-opportunities{display:grid;gap:9px;margin:.55rem 0 1.2rem}
        .gm-opportunity{border:1px solid rgba(148,163,184,.20);border-radius:15px;padding:12px 13px;background:linear-gradient(135deg,rgba(30,41,59,.16),rgba(22,163,74,.05))}
        .gm-opportunity-top{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:7px;font-size:.86rem;font-weight:750}
        .gm-opportunity-top strong{font-size:1.05rem;color:#22c55e}.gm-opportunity small{display:block;margin-top:6px;opacity:.66;font-size:.73rem}
        @media(max-width:700px){.gm-vip-scoregrid{grid-template-columns:1fr 1fr 1fr}.gm-comp-grid{grid-template-columns:1fr}.gm-vip-title{font-size:1.38rem}}
        </style>
        <div class="gm-vip-hero">
          <div class="gm-vip-kicker">Demonstração visual • valores ilustrativos</div>
          <div class="gm-vip-title">⚽ Time A <span style="opacity:.45">×</span> Time B</div>
          <div class="gm-vip-sub">O painel real usa a partida selecionada e os dados disponíveis para transformar histórico, contexto e modelo em probabilidades estimadas.</div>
          <div class="gm-vip-scoregrid">
            <div class="gm-vip-scorebox"><span>Casa</span><b>46%</b></div>
            <div class="gm-vip-scorebox"><span>Empate</span><b>28%</b></div>
            <div class="gm-vip-scorebox"><span>Fora</span><b>26%</b></div>
          </div>
          <div class="gm-demo-bars">
            <div><div class="gm-bar-label"><span>Mais de 1,5 gols</span><b>82%</b></div><div class="gm-bar-track"><div class="gm-bar-fill" style="width:82%"></div></div></div>
            <div><div class="gm-bar-label"><span>Mais de 7,5 escanteios</span><b>74%</b></div><div class="gm-bar-track"><div class="gm-bar-fill" style="width:74%"></div></div></div>
            <div><div class="gm-bar-label"><span>Mais de 3,5 cartões</span><b>68%</b></div><div class="gm-bar-track"><div class="gm-bar-fill" style="width:68%"></div></div></div>
          </div>
          <div style="margin-top:13px">
            <span class="gm-chip">🎯 Probabilidades</span><span class="gm-chip">🛡️ Dupla chance</span><span class="gm-chip">💰 Odds justas</span><span class="gm-chip">📈 Contexto</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown("#### 📊 O que você encontra na análise")
    st.markdown(
        """
        <div class="gm-comp-grid" style="margin-bottom:1rem">
          <div class="gm-section-card"><h4>⚽ Resultado e gols</h4><p>Probabilidade 1X2, dupla chance, gols esperados e linhas de gols calculadas a partir dos dados disponíveis.</p></div>
          <div class="gm-section-card"><h4>🚩 Escanteios</h4><p>Médias, expectativa do confronto e probabilidades estimadas para diferentes linhas.</p></div>
          <div class="gm-section-card"><h4>🟨 Cartões</h4><p>Perfil disciplinar, médias das equipes e expectativa estatística para a partida.</p></div>
          <div class="gm-section-card"><h4>🥅 Finalizações</h4><p>Finalizações, chutes no alvo e leitura do potencial ofensivo quando a fonte fornece esses dados.</p></div>
          <div class="gm-section-card"><h4>📈 Forma e contexto</h4><p>Momento recente, mando, força da competição e confronto direto quando houver base verificada.</p></div>
          <div class="gm-section-card"><h4>💰 Odds justas</h4><p>Probabilidades finais do modelo convertidas em odds justas; comparação de mercado apenas quando houver cotação validada.</p></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown("#### 🎯 Como as oportunidades aparecem")
    st.caption("Exemplo visual com números ilustrativos. No VIP, as linhas são calculadas para a partida realmente selecionada.")
    st.markdown(
        """
        <div class="gm-opportunities">
          <div class="gm-opportunity"><div class="gm-opportunity-top"><span>⚽ Mais de 1,5 gols</span><strong>82%</strong></div><div class="gm-bar-track"><div class="gm-bar-fill" style="width:82%"></div></div><small>Probabilidade estimada • exemplo demonstrativo</small></div>
          <div class="gm-opportunity"><div class="gm-opportunity-top"><span>🚩 Mais de 7,5 escanteios</span><strong>74%</strong></div><div class="gm-bar-track"><div class="gm-bar-fill" style="width:74%"></div></div><small>Probabilidade estimada • exemplo demonstrativo</small></div>
          <div class="gm-opportunity"><div class="gm-opportunity-top"><span>🟨 Mais de 3,5 cartões</span><strong>68%</strong></div><div class="gm-bar-track"><div class="gm-bar-fill" style="width:68%"></div></div><small>Probabilidade estimada • exemplo demonstrativo</small></div>
          <div class="gm-opportunity"><div class="gm-opportunity-top"><span>🛡️ Dupla chance 1X</span><strong>66%</strong></div><div class="gm-bar-track"><div class="gm-bar-fill" style="width:66%"></div></div><small>Probabilidade estimada • exemplo demonstrativo</small></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown("### 🌍 Competições disponíveis no GM SCORE")
    st.caption("As competições abaixo fazem parte da cobertura atual do aplicativo.")
    competition_html = ''.join(f'<div class="gm-comp">{_gm_safe_html(item)}</div>' for item in GM_PUBLIC_COMPETITIONS)
    st.markdown(f'<div class="gm-comp-grid">{competition_html}</div>', unsafe_allow_html=True)

    if not compact:
        st.success("⭐ Com o VIP ativo, o cliente escolhe a partida e recebe a análise gerada com os dados realmente disponíveis para aquela competição.")


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
          <div class="gm-public-title">Informação para enxergar a partida <span>com mais clareza.</span></div>
          <div class="gm-public-copy">Dados, contexto e probabilidades estimadas organizados em uma leitura objetiva. Sem prometer certezas: o foco é identificar os cenários que os dados sustentam.</div>
          <div class="gm-public-pills">
            <span class="gm-public-pill">⚽ Resultado e gols</span>
            <span class="gm-public-pill">🚩 Escanteios</span>
            <span class="gm-public-pill">🟨 Cartões</span>
            <span class="gm-public-pill">🎯 Odds justas</span>
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
    gm_render_payment_plans()

    st.markdown("### 🔐 Como liberar seu acesso")
    st.info(
        "1. Crie sua conta GM SCORE e confirme o e-mail.  2. Entre na conta e escolha seu plano.  "
        "3. O pagamento é feito no Mercado Pago.  4. Após a confirmação válida, o VIP é ativado automaticamente."
    )


def gm_render_match_hero(team_a, team_b, league_name, season_text, probs=None, updated_until=None, sample=None):
    """Cabeçalho visual da partida real, sem alterar nenhum cálculo do modelo."""
    team_a_html = _gm_safe_html(team_a)
    team_b_html = _gm_safe_html(team_b)
    league_html = _gm_safe_html(league_name)
    season_html = _gm_safe_html(season_text)
    meta = f"{league_html} • {season_html}"
    if updated_until is not None and not pd.isna(updated_until):
        try:
            meta += f" • dados até {pd.Timestamp(updated_until):%d/%m/%Y}"
        except Exception:
            pass
    if sample is not None:
        meta += f" • amostra mínima: {int(sample)} jogo(s)"

    if probs:
        home = max(0.0, min(100.0, float(probs.get("home", 0.0))))
        draw = max(0.0, min(100.0, float(probs.get("draw", 0.0))))
        away = max(0.0, min(100.0, float(probs.get("away", 0.0))))
        probability_html = f"""
          <div class="gm-real-probgrid">
            <div class="gm-real-probbox"><span>Vitória casa</span><b>{home:.0f}%</b></div>
            <div class="gm-real-probbox"><span>Empate</span><b>{draw:.0f}%</b></div>
            <div class="gm-real-probbox"><span>Vitória fora</span><b>{away:.0f}%</b></div>
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
        .gm-real-probbox span{{display:block;font-size:.67rem;color:#94a3b8;text-transform:uppercase;letter-spacing:.05em}}.gm-real-probbox b{{display:block;font-size:1.32rem;margin-top:3px;color:#fff}}
        .gm-real-note{{font-size:.72rem;color:#94a3b8;margin-top:10px;line-height:1.4}}
        @media(max-width:520px){{.gm-real-match{{padding:17px 14px}}.gm-real-probbox{{padding:9px 5px}}.gm-real-probbox b{{font-size:1.12rem}}}}
        </style>
        <section class="gm-real-match">
          <div class="gm-real-kicker">Partida carregada • análise VIP</div>
          <div class="gm-real-title">⚽ {team_a_html} <span style="opacity:.45">×</span> {team_b_html}</div>
          <div class="gm-real-meta">{meta}</div>
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
            text = str(exc).lower()
            if "already registered" in text or "user_already_exists" in text:
                st.warning("Já existe uma conta com este e-mail. Use a opção Entrar.")
            else:
                st.error("Não foi possível criar a conta agora. Tente novamente ou fale com o suporte.")


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
            profile = gm_auth_get_profile(force=True)
        except Exception:
            profile = None
    if profile:
        # Contas antigas precisam registrar um aceite explícito antes de continuar.
        # Administradores ficam isentos desta etapa para evitar travar a gestão do sistema.
        if profile.get("role") != "admin" and not bool(profile.get("terms_accepted")):
            gm_render_terms_acceptance(profile)
            return False

        state = gm_auth_access_state(profile)
        if state in {"admin", "vip"}:
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
                        gm_render_session_conflict(profile, session_reason)
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
                # last_seen_at recente e permite que a janela de 7 minutos no
                # Supabase diferencie uma sessão ativa de uma aba abandonada.
                gm_render_session_heartbeat()

            with st.sidebar:
                st.markdown("### 👤 Minha conta")
                st.caption(str(profile.get("nome") or profile.get("email") or "GM SCORE"))
                st.success("🛠️ Administrador" if state == "admin" else "⭐ VIP ativo")

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
                        "💳 Renovar VIP",
                        use_container_width=True,
                        key="gm_sidebar_renew_vip",
                    ):
                        st.session_state["gm_sidebar_renewal_open"] = not renewal_open
                        st.rerun()

                    if st.session_state.get("gm_sidebar_renewal_open", False):
                        st.markdown("#### 👑 Renovação de planos VIP")
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

                if state == "admin":
                    if st.button("🛠 Painel Administrativo", use_container_width=True, key="gm_sidebar_admin_panel"):
                        st.session_state["gm_admin_panel_open"] = True
                        st.rerun()
                if st.button("🚪 Sair", use_container_width=True, key="gm_sidebar_logout"):
                    gm_auth_sign_out()
                    st.session_state.pop("gm_admin_panel_open", None)
                    st.session_state.pop("gm_news_open", None)
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

# Sino de novidades no canto superior direito da área autenticada.
# O contador vem do Supabase; nenhuma regra de VIP, sessão ou pagamento é alterada.
if _gm_profile_after_gate:
    try:
        _gm_news_unread_top = gm_unread_news_count()
    except Exception:
        _gm_news_unread_top = 0
    gm_render_top_news_bell(_gm_news_unread_top)

if _gm_profile_after_gate and st.session_state.get("gm_news_open"):
    gm_render_news_center()

if (
    _gm_profile_after_gate
    and _gm_profile_after_gate.get("role") == "admin"
    and st.session_state.get("gm_admin_panel_open")
):
    gm_render_admin_panel(_gm_profile_after_gate)
    st.stop()

if _gm_profile_after_gate and _gm_profile_after_gate.get("role") == "admin":
    gm_render_auth_test_console()

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
    r"\byouth\b|\bacadem(?:y|ia)\b|\bjunior(?:es|s)?\b|\breserv(?:e|es|as?)\b|"
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
                sh = _team_similarity(wanted_h, eh)
                sa = _team_similarity(wanted_a, ea)
                if sh >= 0.78 and sa >= 0.78:
                    candidates.append((sh + sa, ev, d.isoformat()))
        except Exception:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    score, ev, event_day = candidates[0]
    # Exige correspondência forte das duas pontas para não trocar equipes homônimas.
    if score < 1.68:
        return None
    return {
        "event_id": ev.get("id"),
        "date": event_day,
        "home": (ev.get("homeTeam") or {}).get("name"),
        "away": (ev.get("awayTeam") or {}).get("name"),
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

    # H2H atual + edições anteriores + histórico público dinâmico do confronto.
    # Para jogos do dia, o SofaScore fornece o event_id exato e o histórico entre
    # as mesmas equipes; isso torna o H2H genérico para qualquer competição.
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
            # ClubElo é a âncora de força global: captura qualidade estrutural e
            # histórico recente em confrontos de níveis diferentes.
            elo_component = max(0.0, min(1.0, (float(elo) - 1350.0) / 700.0))
            return (0.58 * elo_component + 0.20 * league_component +
                    0.14 * prof.get("season_strength", .5) + 0.08 * prof.get("form", .5))
        return 0.48 * league_component + 0.34 * prof.get("season_strength", .5) + 0.18 * prof.get("form", .5)

    ctx = {
        "home_profile": prof_a, "away_profile": prof_b,
        "history": hist, "h2h_games": h2n, "h2h_home": hh, "h2h_away": ah,
        "h2h_home_wins": h2d.get("home_wins", 0), "h2h_away_wins": h2d.get("away_wins", 0),
        "h2h_draws": h2d.get("draws", 0),
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

    # Hierarquia estrutural interligas. O nível do campeonato doméstico é um
    # componente próprio (não apenas um pequeno ajuste do Elo), porque campanhas
    # idênticas em ligas de forças muito diferentes não são equivalentes.
    home_adv = 42.0
    league_component = (hls - als) * 620.0
    attack_component = (hatk - aatk) * 150.0
    season_component = (hseason - aseason) * 75.0
    form_component = (hform - aform) * 42.0

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
                        h2h_component)
    else:
        # Sem Elo, ampliamos a tradução entre ligas mantendo os demais sinais.
        quality_diff = (home_adv + (hls - als) * 980.0 +
                        (hatk - aatk) * 175.0 +
                        (hseason - aseason) * 90.0 +
                        (hform - aform) * 52.0 + h2h_component)

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
        # Em continentais, a amostra inicial do torneio é pequena demais para
        # superar a hierarquia interligas. Ela ganha espaço conforme os jogos chegam.
        w = max(0.48, 0.72 - min(float(sample), 8.0) * 0.030)
    else:
        w = max(0.20, 0.36 - min(float(sample), 12.0) * 0.010)

    # Se a hierarquia estrutural aponta favorito claro e o modelo de poucos
    # jogos aponta o adversário, aumenta o guardrail.
    if qfav != mfav and qgap >= 10:
        w = max(w, 0.76 if contextual else 0.46)
    if qgap >= 20:
        w = max(w, 0.82 if contextual else 0.50)

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
        return {"games": 0, "goal_avg": None, "btts": None, "over25": None}
    totals = [hg + ag for hg, ag in rows]
    return {
        "games": len(rows),
        "goal_avg": sum(totals) / len(totals),
        "btts": sum(1 for hg, ag in rows if hg > 0 and ag > 0) / len(rows),
        "over25": sum(1 for t in totals if t >= 3) / len(rows),
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
    }


def render_data_intelligence_status(competition_name, competition_df):
    """Mostra transparência da base sem alterar o bloco visual de expectativa."""
    profile = competition_learning_profile(competition_name, competition_df)
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
        raw_goal_total = lam_a + lam_b
        lam_total, goal_ctx = competition_adjusted_goal_total(raw_goal_total, a, b, league_name, competition_df)
        add_market(
            "Gols", "⚽", "gols", lam_total, (0.5, 1.5, 2.5, 3.5, 4.5, 5.5),
            f"Projeção ajustada ao perfil da competição: {lam_total:.2f} gols", min_good=65
        )

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


def render_core_markets_dashboard(a, b, team_a, team_b, df, probs=None, sample_games=0):
    """Painel oficial de mercados GM SCORE.

    Todos os mercados principais aparecem sempre. Quando a fonte não oferece base
    suficiente, a resposta é explicitamente inconclusiva em vez de estimada à força.
    """
    ex = match_expectations(a, b, df) or {}
    matches = list(df.attrs.get("matches", []) or []) if isinstance(df, pd.DataFrame) else []
    goal_split = None
    if "Gols" in ex:
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
    @media(max-width:520px){.gm-mkt-lines{grid-template-columns:repeat(3,1fr)}}
    </style>
    ''', unsafe_allow_html=True)

    st.markdown("#### 🧭 Mercados essenciais GM SCORE")
    st.caption("Os campos principais aparecem sempre. Quando a base não sustenta um cálculo, o mercado é marcado como inconclusivo.")
    tabs = st.tabs(["⚽ Resultado e gols", "⛳ Escanteios", "🟨 Cartões", "🎯 Finalizações"])

    with tabs[0]:
        status_result = _gm_market_status(sample_games, available=bool(probs))
        rows = None
        if probs:
            rows = [(f"🏠 {team_a}", f"{float(probs['home']):.0f}%"), ("🤝 Empate", f"{float(probs['draw']):.0f}%"), (f"✈️ {team_b}", f"{float(probs['away']):.0f}%")]
        _gm_market_card("🏆 Resultado final", status_result, compact_rows=rows,
                        note="Distribuição 1X2 final do modelo; casa + empate + fora = 100%." if probs else None)

        g = ex.get("Gols")
        g_status = _gm_market_status(sample_games, available=bool(g))
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
                        note=f"Base específica: {split_n} partidas com intervalo disponível." if goal_split else None)
        _gm_market_card("⏱️ Gols — 2º tempo", second_status,
                        projection=f"{goal_split['second']:.2f}".replace('.', ',') if goal_split else None,
                        lines=_gm_pct_lines(goal_split['second'], [0.5,1.5,2.5]) if goal_split else None,
                        note=f"Base específica: {split_n} partidas com intervalo disponível." if goal_split else None)

        if g:
            rows = [(team_a, f"{g['home']:.2f}".replace('.', ',')), (team_b, f"{g['away']:.2f}".replace('.', ','))]
        else:
            rows = None
        _gm_market_card("👥 Gols por equipe", g_status, compact_rows=rows,
                        note="Projeção ofensiva de cada equipe ajustada ao contexto da competição." if g else None)

        if probs:
            p1x=float(probs['home'])+float(probs['draw']); px2=float(probs['draw'])+float(probs['away']); p12=float(probs['home'])+float(probs['away'])
            dc_rows=[("1X",f"{p1x:.0f}%"),("X2",f"{px2:.0f}%"),("12",f"{p12:.0f}%")]
        else:
            dc_rows=None
        _gm_market_card("🛡️ Dupla chance", status_result, compact_rows=dc_rows,
                        note="Cenários sobrepostos; não devem ser somados entre si." if probs else None)

        if g:
            btts_yes=(1-math.exp(-max(g['home'],0.01)))*(1-math.exp(-max(g['away'],0.01)))*100
            btts_rows=[("Sim",f"{btts_yes:.0f}%"),("Não",f"{100-btts_yes:.0f}%")]
        else:
            btts_rows=None
        _gm_market_card("🤝 Ambas marcam", g_status, compact_rows=btts_rows,
                        note="Estimativa derivada das projeções individuais de gols." if g else None)

    with tabs[1]:
        c = ex.get("Escanteios")
        c_status = _gm_market_status(sample_games, available=bool(c))
        _gm_market_card("⛳ Escanteios na partida", c_status,
                        projection=f"{c['total']:.2f}".replace('.', ',') if c else None,
                        lines=_gm_pct_lines(c['total'], [6.5,7.5,8.5,9.5,10.5]) if c else None)
        inc = _gm_market_status(sample_games, available=False)
        _gm_market_card("⏱️ Escanteios — 1º tempo", inc, note="A fonte atual não fornece separação por tempo com cobertura suficiente.")
        _gm_market_card("⏱️ Escanteios — 2º tempo", inc, note="A fonte atual não fornece separação por tempo com cobertura suficiente.")
        rows=[(team_a,f"{c['home']:.2f}".replace('.',',')),(team_b,f"{c['away']:.2f}".replace('.',','))] if c else None
        _gm_market_card("👥 Escanteios por equipe", c_status, compact_rows=rows,
                        note="Médias/projeções por equipe; probabilidades detalhadas ficam condicionadas à qualidade da base." if c else None)

    with tabs[2]:
        c = ex.get("Cartões")
        c_status = _gm_market_status(sample_games, available=bool(c))
        _gm_market_card("🟨 Cartões totais", c_status,
                        projection=f"{c['total']:.2f}".replace('.', ',') if c else None,
                        lines=_gm_pct_lines(c['total'], [1.5,2.5,3.5,4.5,5.5]) if c else None,
                        note="Cartões são tratados como estimativa estatística; média alta não gera recomendação automaticamente." if c else None)
        rows=[(team_a,f"{c['home']:.2f}".replace('.',',')),(team_b,f"{c['away']:.2f}".replace('.',','))] if c else None
        _gm_market_card("👥 Cartões por equipe", c_status, compact_rows=rows)
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

    with tabs[3]:
        s = ex.get("Finalizações")
        s_status = _gm_market_status(sample_games, available=bool(s))
        _gm_market_card("🎯 Total de finalizações na partida", s_status,
                        projection=f"{s['total']:.2f}".replace('.', ',') if s else None,
                        lines=_gm_pct_lines(s['total'], [19.5,24.5,29.5,34.5]) if s else None)
        t = ex.get("Chutes no alvo")
        t_status = _gm_market_status(sample_games, available=bool(t))
        _gm_market_card("🥅 Finalizações no alvo na partida", t_status,
                        projection=f"{t['total']:.2f}".replace('.', ',') if t else None,
                        lines=_gm_pct_lines(t['total'], [5.5,6.5,7.5,8.5,9.5]) if t else None)
        srows=[(team_a,f"{s['home']:.2f}".replace('.',',')),(team_b,f"{s['away']:.2f}".replace('.',','))] if s else None
        _gm_market_card("👥 Finalizações por equipe", s_status, compact_rows=srows)
        trows=[(team_a,f"{t['home']:.2f}".replace('.',',')),(team_b,f"{t['away']:.2f}".replace('.',','))] if t else None
        _gm_market_card("👥 Finalizações no alvo por equipe", t_status, compact_rows=trows)

    with st.expander("➕ Outros dados gerados", expanded=False):
        extras=[]
        for metric, emoji in [("Faltas","🚫"),("Posse (%)","⚪"),("Impedimentos","🚩")]:
            av,bv=metric_value(a,metric),metric_value(b,metric)
            if av is not None and bv is not None:
                extras.append((emoji,metric,av,bv))
        if extras:
            for emoji,metric,av,bv in extras:
                st.markdown(f"**{emoji} {metric}** — {team_a}: **{av:.2f}** · {team_b}: **{bv:.2f}**")
        else:
            st.caption("Nenhum dado adicional com cobertura suficiente nesta partida.")
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

            br_date = source_date
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
            fixtures.append({
                "competition": comp,
                "home": home,
                "away": away,
                "time": br_time,
                "br_date": br_date,
                "source": "SofaScore",
                "event_id": ev.get("id"),
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
        br_date = target_date
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

        fixtures.append({
            "competition": competition,
            "home": home,
            "away": away,
            "time": br_time,
            "br_date": br_date,
            "source": "ESPN",
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


@st.cache_data(ttl=3600, show_spinner=False)
def load_fixtures_for_date(target_date):
    """Carrega somente jogos das competições suportadas para uma data de Brasília."""
    today = target_date
    fixtures = []

    # Agenda por data: o SofaScore é consultado primeiro porque o próprio GM SCORE
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

    # Mantém todas as fontes anteriores como fallback, mas somente se a consulta
    # focada ainda não encontrou partidas.
    if not fixtures:
        try:
            fixtures.extend([f for f in load_fixtures_for_date(target_date) if f.get("competition") == competition])
        except Exception:
            pass

    best = {}
    for f in fixtures:
        if f.get("competition") != competition or not valid_daily_fixture(f):
            continue
        ff = dict(f)
        if competition in STRICT_OFFICIAL_ROSTERS:
            roster = CURRENT_TEAM_ROSTERS.get(competition, [])
            ff["home"] = resolve_team_name(ff.get("home"), roster) or ff.get("home")
            ff["away"] = resolve_team_name(ff.get("away"), roster) or ff.get("away")
        key = (fixture_team_key(ff.get("home")), fixture_team_key(ff.get("away")))
        if key not in best or _fixture_quality(ff) > _fixture_quality(best[key]):
            best[key] = ff
    return sorted(best.values(), key=lambda f: str(f.get("time") or "99:99"))


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
    """Gera uma arte HD no tema escuro do GM SCORE com os mesmos dados exibidos na análise."""
    import json as _json

    result_lines = []
    if probs:
        result_lines = [
            [f"Vitória {team_a}", f"{probs['home']:.0f}%", float(probs['home'])],
            ["Empate", f"{probs['draw']:.0f}%", float(probs['draw'])],
            [f"Vitória {team_b}", f"{probs['away']:.0f}%", float(probs['away'])],
        ]

    exp_lines = []
    for key, label in [
        ("Gols", "Gols esperados"),
        ("Escanteios", "Escanteios esperados"),
        ("Cartões", "Cartões esperados"),
        ("Finalizações", "Finalizações esperadas"),
        ("Chutes no alvo", "Chutes no alvo"),
    ]:
        item = (expectations or {}).get(key)
        if item:
            exp_lines.append([label, f"{item['total']:.1f}"])

    opp_lines = [
        [x["Mercado"], f"{x['Chance']:.0f}%", float(x["Chance"]), x["Base"]]
        for x in (opportunities or [])
    ]

    metric_labels = [
        "Jogos", "Gols pró", "Gols contra", "Escanteios", "Amarelos",
        "Vermelhos", "Faltas", "Finalizações", "Chutes no alvo",
        "Posse (%)", "Impedimentos",
    ]
    avg_lines = []
    for metric in metric_labels:
        if metric not in a.index or metric not in b.index:
            continue
        av, bv = a[metric], b[metric]
        if pd.isna(av) and pd.isna(bv):
            continue

        def fmt(v):
            if pd.isna(v):
                return "N/D"
            try:
                return str(int(v)) if metric == "Jogos" else f"{float(v):.2f}"
            except Exception:
                return str(v)

        avg_lines.append([metric, fmt(av), fmt(bv)])

    competition = competition_display_name(league_name)
    data = _json.dumps(
        {
            "title": f"{team_a} × {team_b}",
            "league": competition,
            "results": result_lines,
            "expectations": exp_lines,
            "opportunities": opp_lines,
            "averages": avg_lines,
            "home": team_a,
            "away": team_b,
        },
        ensure_ascii=False,
    )

    html = f"""
    <div style='font-family:Inter,Arial,sans-serif'>
      <button id='shareBtn' style='width:100%;padding:12px 16px;border:1px solid #1fe387;border-radius:12px;background:linear-gradient(90deg,#08783f,#10b865);color:white;font-size:16px;font-weight:800;cursor:pointer'>📲 Compartilhar análise em HD</button>
      <div id='msg' style='font-size:12px;color:#94a3b8;margin-top:6px'></div>
    </div>
    <script>
    const D = {data};
    const C = {{
      bg:'#07100f', panel:'#0b1716', panel2:'#101a23', border:'#176e4b',
      green:'#24e58b', green2:'#10b981', text:'#f8fafc', muted:'#a8b4c2',
      soft:'#263845', line:'#1d3132', white:'#ffffff'
    }};

    function rr(ctx,x,y,w,h,r,fill,stroke=null,lw=1) {{
      const q=Math.min(r,w/2,h/2); ctx.beginPath();
      ctx.moveTo(x+q,y);ctx.arcTo(x+w,y,x+w,y+h,q);ctx.arcTo(x+w,y+h,x,y+h,q);
      ctx.arcTo(x,y+h,x,y,q);ctx.arcTo(x,y,x+w,y,q);ctx.closePath();
      if(fill){{ctx.fillStyle=fill;ctx.fill();}} if(stroke){{ctx.lineWidth=lw;ctx.strokeStyle=stroke;ctx.stroke();}}
    }}
    function text(ctx,value,x,y,size=30,weight='400',color=C.text,align='left') {{
      ctx.save(); ctx.fillStyle=color; ctx.font=`${{weight}} ${{size}}px Arial`; ctx.textAlign=align; ctx.textBaseline='alphabetic'; ctx.fillText(String(value),x,y); ctx.restore();
    }}
    function outlinedText(ctx,value,x,y,size=30,weight='900',fill='#ffffff',align='left',stroke='#05070a',strokeWidth=8) {{
      ctx.save(); ctx.font=`${{weight}} ${{size}}px Arial`; ctx.textAlign=align; ctx.textBaseline='alphabetic';
      ctx.lineJoin='round'; ctx.miterLimit=2; ctx.lineWidth=strokeWidth; ctx.strokeStyle=stroke; ctx.strokeText(String(value),x,y);
      ctx.fillStyle=fill; ctx.fillText(String(value),x,y); ctx.restore();
    }}
    function wrap(ctx,value,x,y,maxWidth,lineHeight,size=24,weight='400',color=C.muted) {{
      ctx.save(); ctx.fillStyle=color; ctx.font=`${{weight}} ${{size}}px Arial`; ctx.textAlign='left';
      const words=String(value).split(' '); let line='', yy=y;
      for(const w of words) {{ const t=line+w+' '; if(ctx.measureText(t).width>maxWidth && line) {{ctx.fillText(line.trim(),x,yy); yy+=lineHeight; line=w+' ';}} else line=t; }}
      if(line) ctx.fillText(line.trim(),x,yy); ctx.restore(); return yy;
    }}
    function watermark(ctx,w,h) {{
      ctx.save(); ctx.globalAlpha=.055; ctx.fillStyle='#9ef8ca'; ctx.font='700 34px Arial'; ctx.translate(w/2,h/2); ctx.rotate(-Math.PI/7);
      for(let yy=-h;yy<h;yy+=150) for(let xx=-w;xx<w;xx+=300) ctx.fillText('GM SCORE',xx,yy);
      ctx.restore();
    }}
    function sectionTitle(ctx,label,y) {{
      text(ctx,label,70,y,30,'800',C.text); return y+34;
    }}
    function bar(ctx,x,y,w,h,pct,color=C.green) {{
      rr(ctx,x,y,w,h,h/2,C.soft); rr(ctx,x,y,Math.max(h,Math.min(w,w*pct/100)),h,h/2,color);
    }}

    document.getElementById('shareBtn').onclick = async () => {{
      const avgH = D.averages.length*42;
      const oppH = D.opportunities.length*88;
      const expRows = Math.ceil(D.expectations.length/5);
      const logicalW=1080;
      const logicalH=Math.max(1850, 1250 + avgH + oppH + expRows*120);
      const scale=2; // arquivo final com 2160 px de largura para preservar alta resolução
      const canvas=document.createElement('canvas');
      canvas.width=logicalW*scale; canvas.height=logicalH*scale;
      const ctx=canvas.getContext('2d'); ctx.scale(scale,scale);
      ctx.fillStyle=C.bg; ctx.fillRect(0,0,logicalW,logicalH); watermark(ctx,logicalW,logicalH);

      let y=74;

      // Cabeçalho: mede a largura real de "GM " antes de desenhar "SCORE".
      ctx.save();
      ctx.font='900 50px Arial';
      const gmWidth=ctx.measureText('GM ').width;
      ctx.restore();

      outlinedText(ctx,'GM ',70,y,50,'900','#ffffff','left','#05070a',10);
      outlinedText(ctx,'SCORE',70+gmWidth,y,50,'900',C.green,'left','#05070a',10);

      text(ctx,'DADOS QUE',1015,y-22,16,'800',C.text,'right');
      text(ctx,'TRANSFORMAM',1015,y-2,16,'800',C.text,'right');
      text(ctx,'DADOS EM',1015,y+18,16,'800',C.text,'right');
      text(ctx,'DECISÕES',1015,y+38,16,'800',C.green,'right');

      y+=38;
      text(ctx,'ANÁLISE • ESTATÍSTICAS • PROBABILIDADES',70,y,19,'700','#93e9bc');
      y+=64;

      rr(ctx,60,y,960,190,22,'rgba(8,35,30,.94)',C.border,2);
      text(ctx,D.league.toUpperCase(),88,y+40,22,'800',C.text);
      text(ctx,D.title,992,y+40,18,'700',C.muted,'right');
      text(ctx,D.home,235,y+112,32,'800',C.text,'center');
      text(ctx,'VS',540,y+108,40,'900',C.green,'center');
      text(ctx,D.away,845,y+112,32,'800',C.text,'center');
      text(ctx,'PARTIDA ANALISADA',540,y+150,17,'700',C.muted,'center');
      y+=220;

      if(D.results.length) {{
        rr(ctx,60,y,960,185,20,C.panel,C.border,2);
        sectionTitle(ctx,'🏆 Chance de resultado',y+40);
        const bw=270, gap=25, sx=88;
        D.results.forEach((r,i)=>{{
          const x=sx+i*(bw+gap); text(ctx,r[1],x+bw/2,y+92,34,'900',C.text,'center');
          bar(ctx,x,y+111,bw,15,r[2],i===1?'#dce6eb':C.green);
          text(ctx,r[0],x+bw/2,y+155,18,'600',C.muted,'center');
        }});
        y+=210;
      }}

      if(D.expectations.length) {{
        rr(ctx,60,y,960,170,20,C.panel,C.border,2);
        sectionTitle(ctx,'📈 Expectativa da partida',y+40);
        const n=D.expectations.length, gap=12, w=(900-(n-1)*gap)/n;
        D.expectations.forEach((r,i)=>{{
          const x=90+i*(w+gap); rr(ctx,x,y+62,w,82,14,C.panel2,'#274039',1);
          text(ctx,r[1],x+w/2,y+99,30,'900',C.text,'center');
          wrap(ctx,r[0],x+12,y+127,w-24,18,15,'600',C.muted);
        }});
        y+=195;
      }}

      if(D.opportunities.length) {{
        const h=76 + D.opportunities.length*86;
        rr(ctx,60,y,960,h,20,C.panel,C.border,2);
        sectionTitle(ctx,'⭐ Melhores linhas para observar',y+40);
        let oy=y+72;
        D.opportunities.forEach((r)=>{{
          text(ctx,r[0],88,oy+24,22,'800',C.text);
          wrap(ctx,r[3],88,oy+49,500,20,15,'400',C.muted);
          bar(ctx,610,oy+16,265,14,r[2],C.green);
          text(ctx,r[1],960,oy+30,25,'900',C.green,'right');
          oy+=86;
        }});
        y+=h+25;
      }}

      if(D.averages.length) {{
        const h=92 + D.averages.length*42;
        rr(ctx,60,y,960,h,20,C.panel,C.border,2);
        sectionTitle(ctx,'📊 Médias usadas na análise',y+40);
        let ty=y+74;
        text(ctx,'Dado',92,ty,17,'800',C.muted);
        text(ctx,D.home,635,ty,17,'800',C.muted,'center');
        text(ctx,D.away,870,ty,17,'800',C.muted,'center');
        ty+=18; ctx.strokeStyle=C.line;ctx.beginPath();ctx.moveTo(85,ty);ctx.lineTo(995,ty);ctx.stroke();ty+=28;
        D.averages.forEach(r=>{{
          text(ctx,r[0],92,ty,18,'500',C.text);
          text(ctx,r[1],635,ty,18,'700',C.text,'center');
          text(ctx,r[2],870,ty,18,'700',C.text,'center');
          ty+=42;
        }});
        y+=h+28;
      }}

      text(ctx,'Estimativas estatísticas; não garantem resultado.',65,logicalH-66,17,'400',C.muted);

      ctx.save();
      ctx.font='900 23px Arial';
      const scoreW=ctx.measureText('SCORE').width;
      ctx.restore();
      outlinedText(ctx,'SCORE',1015,logicalH-66,23,'900',C.green,'right','#05070a',5);
      outlinedText(ctx,'GM ',1015-scoreW,logicalH-66,23,'900','#ffffff','right','#05070a',5);

      canvas.toBlob(async blob=>{{
        const safe=(D.home+'-x-'+D.away).replace(/[^a-z0-9áàãâéêíóôõúç_-]+/gi,'-').replace(/-+/g,'-');
        const file=new File([blob],`GM-SCORE-${{safe}}-HD.jpg`,{{type:'image/jpeg'}});
        const shareText=`GM SCORE — ${{D.league}} — ${{D.title}}`;
        try {{
          if(navigator.share && (!navigator.canShare || navigator.canShare({{files:[file]}}))) {{
            await navigator.share({{title:`${{D.league}} — ${{D.title}}`,text:shareText,files:[file]}});
            document.getElementById('msg').innerText='Imagem HD criada. Escolha o WhatsApp para compartilhar.';
          }} else {{
            const dl=document.createElement('a'); dl.href=URL.createObjectURL(blob); dl.download=file.name; dl.click();
            document.getElementById('msg').innerText='Imagem HD salva para compartilhamento.';
          }}
        }} catch(e) {{
          if(e.name!=='AbortError') document.getElementById('msg').innerText='Não foi possível abrir o compartilhamento neste navegador.';
        }}
      }},'image/jpeg',0.98);
    }};
    </script>
    """
    components.html(html, height=82)

def render_analysis():
    global period
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
            st.caption("🕒 Horário de Brasília · toque em **Analisar** para carregar o confronto")
            try:
                today_fixtures = load_competition_fixtures_for_date(league_name, main_fixture_date)
            except Exception:
                today_fixtures = []

            # Barreira final fora do cache: a agenda exibida deve conter somente
            # equipes principais que também existam no elenco profissional carregado
            # para a competição. Isso impede U21/U23/base/reservas.
            safe_fixtures = []
            for f in today_fixtures:
                if not valid_daily_fixture(f):
                    continue
                resolved_fixture_home = resolve_team_name(f.get("home"), teams)
                resolved_fixture_away = resolve_team_name(f.get("away"), teams)
                if not resolved_fixture_home or not resolved_fixture_away:
                    continue
                ff = dict(f)
                ff["home"] = resolved_fixture_home
                ff["away"] = resolved_fixture_away
                safe_fixtures.append(ff)
            today_fixtures = safe_fixtures

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
                        resolved_game_home = resolve_team_name(game_home, teams)
                        resolved_game_away = resolve_team_name(game_away, teams)
                        if not resolved_game_home or not resolved_game_away:
                            st.warning("Não consegui associar este jogo às equipes da competição.")
                        else:
                            st.session_state.selected_home = resolved_game_home
                            st.session_state.selected_away = resolved_game_away
                            st.session_state.loaded_home = resolved_game_home
                            st.session_state.loaded_away = resolved_game_away
                            st.session_state.loaded_competition = league_name
                            st.session_state["_main_games_hidden_competition"] = league_name
                            st.session_state["home_widget"] = resolved_game_home
                            st.session_state["away_widget"] = resolved_game_away
                            st.session_state["_synced_loaded_signature"] = f"{league_name}|{resolved_game_home}|{resolved_game_away}"
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

    loaded_home = resolve_team_name(st.session_state.get("loaded_home"), teams)
    loaded_away = resolve_team_name(st.session_state.get("loaded_away"), teams)

    if loaded_home and loaded_away:
        st.caption(f"✅ Jogo carregado: {loaded_home} × {loaded_away}")
        if st.button("↩️ Escolher outro confronto", use_container_width=True, key="choose_another_match"):
            st.session_state.loaded_home = None
            st.session_state.loaded_away = None
            st.session_state.selected_home = None
            st.session_state.selected_away = None
            st.session_state.pop("_main_games_hidden_competition", None)
            st.session_state.pop("_synced_loaded_signature", None)
            st.rerun()
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
        # O 1X2 principal já está no cabeçalho da partida; evitamos repetir os mesmos números.
        eval_bits = ["força atual", "potencial ofensivo/defensivo", "forma recente", "nível da liga"]
        if analysis_context and analysis_context.get("h2h_games", 0):
            eval_bits.append("confronto direto histórico verificado")
        if moneyline:
            eval_bits.append("mercado público como validação externa")
        st.caption("📌 Leitura GM SCORE considera " + ", ".join(eval_bits) + ". O favoritismo é uma estimativa do cruzamento dos dados, não uma garantia de resultado.")

        p_1x = max(0.0, min(100.0, float(probs['home']) + float(probs['draw'])))
        p_x2 = max(0.0, min(100.0, float(probs['draw']) + float(probs['away'])))
        p_12 = max(0.0, min(100.0, float(probs['home']) + float(probs['away'])))
        with st.expander("🛡️ Dupla chance — ver cenários"):
            st.caption("Os cenários abaixo se sobrepõem e, por isso, não devem ser somados entre si.")
            dc1, dc2, dc3 = st.columns(3)
            dc1.metric(f"{team_a} ou empate", f"{p_1x:.0f}%")
            dc2.metric(f"Empate ou {team_b}", f"{p_x2:.0f}%")
            dc3.metric("Sem empate (12)", f"{p_12:.0f}%")

    if probs:
        # A odd justa é propriedade do modelo e deve aparecer mesmo quando a
        # cotação pública não estiver disponível/validada. Mercado é opcional.
        if moneyline:
            render_market_value_panel(team_a, team_b, probs, moneyline)
        else:
            with st.expander("🎯 Odds justas GM SCORE"):
                st.caption("Derivadas das probabilidades finais do modelo (odd justa = 1 ÷ probabilidade). Não representam cotação disponível em casa de apostas.")
                fair_labels = [("home", f"🏠 {team_a}"), ("draw", "🤝 Empate"), ("away", f"✈️ {team_b}")]
                fair_cols = st.columns(3)
                for col, (key, label) in zip(fair_cols, fair_labels):
                    p_final = max(min(float(probs.get(key, 0.0)), 99.5), 0.5)
                    fair_odd = 100.0 / p_final
                    with col:
                        st.markdown(f"**{label}**")
                        st.markdown(f"{p_final:.1f}% • **{fair_odd:.2f}**")
                st.caption("Mercado público 1X2 não confirmado para o evento exato; valor/edge só é calculado quando houver cotação validada.")

    expectations = render_match_probability_dashboard(a, b, team_a, team_b, df, probs=probs, sample_games=comp_sample)

    opportunities = build_opportunities(a, b, team_a, team_b, df)
    st.markdown("#### ⭐ Oportunidades GM SCORE")
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
        st.info("🔎 Nenhuma oportunidade atingiu os critérios atuais do GM SCORE para destaque nesta partida.")

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


# A agenda da barra lateral é renderizada no início da interface.
# Mantemos apenas o botão de suporte também no final da tela principal.

render_analysis()

st.markdown("---")
st.link_button("✈️ Suporte pelo Telegram", "https://t.me/suport_gm", use_container_width=True)

st.caption("As chances são estimativas estatísticas da temporada vigente e não garantem resultado. Use como apoio à análise e aposte com responsabilidade.")

