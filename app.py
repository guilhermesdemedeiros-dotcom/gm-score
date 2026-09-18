# ============================================================
# CONFIGURAÇÃO
# ============================================================
GM_BUILD = "2026-09-18-v133-admin-fixture-rebuild-diagnostics"
GM_BUILD = "2026-09-18-v134-news-publication-hub"
GM_DAILY_PICK_RESET_DATE = date(2026, 9, 16)  # novo ciclo: Matadeira, Dica Principal e Bingo

# IDs auditados das 21 competições.
@@ -1498,6 +1498,37 @@ def gm_list_news():
return [row for row in rows if isinstance(row, dict)]


def gm_publish_system_news(title, message, category="novidade", featured=True):
    """Publica uma Novidade automática usando a mesma central das publicações manuais.

    V134: Dicas do Dia e Apostas do ADM passam pelo mesmo ponto de entrada,
    preparando a Central de Novidades para notificações push sem duplicar regras.
    """
    data = gm_admin_rpc("gm_admin_publish_news", {
        "p_title": str(title or "").strip(),
        "p_message": str(message or "").strip(),
        "p_category": str(category or "novidade"),
        "p_is_featured": bool(featured),
    })
    gm_invalidate_unread_news_cache()
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
    )


def gm_invalidate_unread_news_cache():
st.session_state.pop("_gm_unread_news_count_cache", None)
st.session_state.pop("_gm_unread_news_count_tick", None)
@@ -2316,7 +2347,14 @@ def published_for(kind):
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
@@ -2684,12 +2722,12 @@ def gm_render_admin_bets_manager():
gm_admin_bet_publish(title, description, odd, clean_url, valid_until)
news_ok = True
try:
                        gm_admin_rpc("gm_admin_publish_news", {
                            "p_title": "⭐ Nova Aposta do ADM disponível",
                            "p_message": f"{str(title).strip()} · Odd {float(odd):.2f}. Confira a publicação na página Início.",
                            "p_category": "novidade",
                            "p_is_featured": True,
                        })
                        gm_publish_system_news(
                            "⭐ Nova Aposta do ADM disponível",
                            f"{str(title).strip()} · Odd {float(odd):.2f}. Confira a publicação na página Início.",
                            category="novidade",
                            featured=True,
                        )
except Exception:
news_ok = False
st.success("Aposta do ADM publicada com sucesso.")
