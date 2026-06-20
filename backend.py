# -*- coding: utf-8 -*-
"""
Backend Fase 1 — Devoluções ML (SÓ LEITURA).
Serve o front mobile + 3 endpoints ao vivo. Nenhuma escrita no ML.
Rodar:  uvicorn backend:app --host 127.0.0.1 --port 8077
"""
import os
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import ml

APP_DIR = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="Devoluções ML — Recebimento (Fase 1, leitura)")


@app.get("/api/health")
def health():
    return ml.token_status()


@app.get("/api/aguardando")
def aguardando(refresh: int = 0):
    if refresh:
        ml.build_aguardando(force=True)
    return ml.lista()


def _achar_item(oid):
    for it in ml.lista()["itens"]:
        if str(it["order_id"]) == str(oid):
            return it
    return None


@app.get("/api/conferencia/{oid}")
def get_conf(oid: str):
    return JSONResponse(ml.get_conferencia(oid, _achar_item(oid)) or {"erro": "não encontrado"})


@app.post("/api/conferencia/{oid}/etapa")
async def post_etapa(oid: str, req: Request):
    b = await req.json()
    return JSONResponse(ml.save_etapa(oid, b.get("etapa"), b.get("dados") or {},
                                      b.get("perfil", "?"), b.get("nome", "?"), _achar_item(oid)))


@app.post("/api/conferencia/{oid}/lock")
async def post_lock(oid: str, req: Request):
    b = await req.json()
    return JSONResponse(ml.claim_lock(oid, b.get("perfil", "?"), b.get("nome", "?"), _achar_item(oid)))


@app.get("/api/conferencia/{oid}/anexos")
def get_anexos(oid: str):
    return JSONResponse(ml.get_anexos(oid))


@app.get("/api/buscar")
def buscar(code: str = Query(...), ml_fallback: int = 0):
    return JSONResponse(ml.buscar(code, force_ml=bool(ml_fallback)))


@app.get("/api/mode")
def get_mode():
    return ml.mode()


@app.post("/api/avaria")
async def avaria(req: Request):
    payload = await req.json()
    return JSONResponse(ml.enviar_avaria(payload))


@app.get("/")
def index(request: Request):
    # roteamento por host (2 URLs distintas, mesmo backend):
    # gestor.devolucoes.demos.napel.com.br -> serve a tela do gestor
    # conferente.devolucoes.demos.napel.com.br -> serve a tela do conferente
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").lower()
    arquivo = "gestor.html" if host.startswith("gestor.") else "index.html"
    return FileResponse(os.path.join(APP_DIR, "static", arquivo))


@app.get("/gestor")
def gestor():
    return FileResponse(os.path.join(APP_DIR, "static", "gestor.html"))


@app.get("/conferente")
def conferente():
    return FileResponse(os.path.join(APP_DIR, "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(APP_DIR, "static")), name="static")
