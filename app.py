"""GM SCORE — launcher ASGI/Streamlit (V135).

Este arquivo é o entrypoint do Streamlit Community Cloud. A interface completa
permanece em ``gm_score_app.py``. O launcher usa ``st.App`` para manter a UI
Streamlit em ``/`` e expor o Service Worker do OneSignal na raiz da mesma origem.
"""
from pathlib import Path

import streamlit as st
from starlette.responses import Response
from starlette.routing import Route

GM_BUILD = "2026-09-18-v135-onesignal-worker-route"
_BASE_DIR = Path(__file__).resolve().parent
_WORKER_PATH = _BASE_DIR / "OneSignalSDKWorker.js"


async def onesignal_service_worker(request):
    """Entrega o worker oficial do OneSignal com MIME e escopo corretos."""
    try:
        worker_source = _WORKER_PATH.read_text(encoding="utf-8")
    except OSError:
        return Response(
            "OneSignalSDKWorker.js não encontrado no deploy.",
            status_code=404,
            media_type="text/plain",
            headers={"Cache-Control": "no-store"},
        )

    return Response(
        worker_source,
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Service-Worker-Allowed": "/",
            "X-Content-Type-Options": "nosniff",
        },
    )


app = st.App(
    "gm_score_app.py",
    routes=[
        Route(
            "/OneSignalSDKWorker.js",
            endpoint=onesignal_service_worker,
            methods=["GET"],
            name="onesignal-service-worker",
        )
    ],
)
