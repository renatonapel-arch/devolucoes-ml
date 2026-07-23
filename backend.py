# -*- coding: utf-8 -*-
"""
Backend Fase 1 — Devoluções ML (SÓ LEITURA).
Serve o front mobile + 3 endpoints ao vivo. Nenhuma escrita no ML.
Rodar:  uvicorn backend:app --host 127.0.0.1 --port 8077
"""
import os, json
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import ml

APP_DIR = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="Devoluções ML — Recebimento (Fase 1, leitura)")


@app.middleware("http")
async def redirect_demos_to_prod(request: Request, call_next):
    """Redirect 301 permanente: *.devolucoes.demos.napel.com.br -> *.devolucoes.napel.com.br
    Bookmark antigo abre a URL nova sozinho, mantendo path+query. Migração invisível pro
    usuário; depois de X semanas o domínio demo pode ser removido do Coolify sem quebrar
    ninguém. Ativa/desativa via env DEVOL_REDIRECT_DEMOS (default '1' = ligado)."""
    from fastapi.responses import RedirectResponse
    import os as _os
    if _os.environ.get("DEVOL_REDIRECT_DEMOS", "1") == "1":
        host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").lower()
        if ".demos.napel.com.br" in host:
            novo = host.replace(".demos.napel.com.br", ".napel.com.br")
            url = str(request.url).replace(f"://{host}", f"://{novo}", 1)
            return RedirectResponse(url, status_code=301)
    return await call_next(request)


@app.on_event("startup")
def _aquecer_cache():
    # Após um redeploy o container é novo (cache frio). Sem aquecer, o 1º conferente
    # a abrir esperaria ~3min de tela vazia. Dispara o build em background no boot.
    try:
        ml._start_bg_build()
    except Exception:
        pass
    # watcher de validação: compara sinal do ML x bipagem da Natalia por alguns dias
    try:
        ml.start_watch()
    except Exception:
        pass


@app.get("/api/health")
def health():
    return ml.token_status()


@app.get("/api/aguardando")
def aguardando(refresh: int = 0):
    # nunca bloqueia: refresh apenas dispara rebuild em background (stale-while-revalidate)
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
    raw = await req.body()
    try:
        b = json.loads(raw or b"{}")
    except Exception as e:
        ml.dbg(f"ETAPA {oid} JSON-FAIL bytes={len(raw)} err={e}")
        return JSONResponse({"erro": "payload inválido", "detalhe": str(e)}, status_code=400)
    dados = b.get("dados") or {}
    nfotos = len(dados.get("anexos") or [])
    # item: tenta a lista ao vivo; se não achar (devolução achada pela BUSCA, fora da lista),
    # usa o snapshot que o próprio front mandou. Sem isso = "item desconhecido".
    item = _achar_item(oid) or b.get("item")
    ml.dbg(f"ETAPA {oid} etapa={b.get('etapa')} bytes={len(raw)} fotos={nfotos} nome={b.get('nome')} item={'sim' if item else 'NAO'}")
    try:
        out = ml.save_etapa(oid, b.get("etapa"), dados, b.get("perfil", "?"), b.get("nome", "?"), item)
        ml.dbg(f"ETAPA {oid} OK status={out.get('status')} erro={out.get('erro')}")
        return JSONResponse(out)
    except Exception as e:
        import traceback
        ml.dbg(f"ETAPA {oid} SAVE-FAIL {e}\n{traceback.format_exc()[:500]}")
        return JSONResponse({"erro": "falha ao salvar", "detalhe": str(e)}, status_code=500)


@app.post("/api/conferencia/{oid}/anexo")
async def post_anexo(oid: str, req: Request):
    """Sobe UMA foto na hora (POST pequeno) — desacopla o upload do 'Salvar e avançar'."""
    raw = await req.body()
    try:
        b = json.loads(raw or b"{}")
    except Exception as e:
        ml.dbg(f"ANEXO {oid} JSON-FAIL bytes={len(raw)} err={e}")
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=400)
    foto = b.get("foto")
    ml.dbg(f"ANEXO {oid} etapa={b.get('etapa')} tipo={b.get('tipo')} bytes={len(raw)}")
    try:
        tot = ml.save_anexos(oid, b.get("etapa", "chegada"), b.get("tipo", "chegada"), [foto] if foto else [])
        return JSONResponse({"ok": True, "total": tot})
    except Exception as e:
        ml.dbg(f"ANEXO {oid} FAIL {e}")
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=500)


@app.post("/api/decode-barcode")
async def decode_barcode(req: Request):
    """Recebe a FOTO de um código de barras / QR (dataURL) e decodifica no servidor com zxing.
    Foto parada = nítida = leitura confiável (resolve a instabilidade do leitor ao vivo no iOS)."""
    raw = await req.body()
    try:
        b = json.loads(raw or b"{}")
    except Exception as e:
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=400)
    durl = b.get("foto") or ""
    ml.dbg(f"DECODE bytes={len(raw)}")
    try:
        import base64, io
        from PIL import Image, ImageOps
        import zxingcpp
        data = durl.split(",", 1)[-1]
        img = Image.open(io.BytesIO(base64.b64decode(data)))
        img = ImageOps.exif_transpose(img)            # respeita a orientação do iPhone
        codes = []
        for variant in (img, img.convert("L")):       # tenta colorido e cinza
            for r in zxingcpp.read_barcodes(variant):
                if r.text and r.text not in codes:
                    codes.append(r.text)
            if codes:
                break
        ml.dbg(f"DECODE -> {codes}")
        return JSONResponse({"ok": True, "codes": codes})
    except Exception as e:
        ml.dbg(f"DECODE FAIL {e}")
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=500)


@app.post("/api/clientlog")
async def clientlog(req: Request):
    """Recebe erros/eventos do app no celular do conferente (pra eu ver a falha real)."""
    try:
        b = await req.json()
        ml.dbg(f"CLIENT {b.get('nome','?')} | {b.get('msg','')[:400]}")
    except Exception:
        pass
    return JSONResponse({"ok": True})


@app.post("/api/conferencia/{oid}/lock")
async def post_lock(oid: str, req: Request):
    b = await req.json()
    return JSONResponse(ml.claim_lock(oid, b.get("perfil", "?"), b.get("nome", "?"), _achar_item(oid) or b.get("item")))


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


@app.post("/api/revisao-ok")
async def revisao_ok(req: Request):
    b = await req.json()
    return JSONResponse(ml.confirmar_revisao_ok(b.get("claim_id")))


# ===== ENTRADA NA DOCA (fase 1 — aditivo, não toca no fluxo de conferência) =====
@app.post("/api/entrada")
async def post_entrada(req: Request):
    b = await req.json()
    return JSONResponse(ml.registrar_entrada(b.get("code"), b.get("nome", "?")))


@app.get("/api/entradas")
def get_entradas(dia: str = None):
    return JSONResponse(ml.listar_entradas(dia))


@app.get("/api/entradas-visao")
def entradas_visao():
    return JSONResponse(ml.visao_entradas())


@app.get("/api/watch-status")
def watch_status():
    return JSONResponse(ml.watch_status())


@app.get("/entrada")
def entrada_page():
    return FileResponse(os.path.join(APP_DIR, "static", "entrada.html"), headers=NO_CACHE)


CLAVIS_API_URL = os.environ.get("CLAVIS_API_URL", "https://clavis.napel.com.br/api/v1")


@app.get("/sso/clavis")
def sso_clavis(token: str = "", request: Request = None):
    """Recebe token SSO do Clavis (padrão docuseal): troca por {user_id, email, name, role}
    via GET {CLAVIS_API_URL}/sso/verify/{token}, e redireciona pra '/' com o nome/role em
    query string — o front (index.html / gestor.html) detecta e grava em localStorage
    ('conf_user'), sem precisar de cookie/sessão."""
    from fastapi.responses import RedirectResponse
    import urllib.parse, urllib.request, urllib.error, json as _json
    if not token:
        return RedirectResponse("/?sso_err=sem_token", status_code=302)
    try:
        with urllib.request.urlopen(f"{CLAVIS_API_URL}/sso/verify/{token}", timeout=15) as r:
            data = _json.loads(r.read())
    except urllib.error.HTTPError as e:
        return RedirectResponse(f"/?sso_err=verify_{e.code}", status_code=302)
    except Exception:
        return RedirectResponse("/?sso_err=verify_net", status_code=302)
    nome = (data.get("name") or "").strip() or (data.get("email") or "Clavis")
    role = (data.get("role") or "").strip() or "conferente"
    perfil = "gestor" if role in ("admin", "gerente", "gestor") else "conferente"
    qs = urllib.parse.urlencode({"sso_nome": nome, "sso_perfil": perfil})
    return RedirectResponse(f"/?{qs}", status_code=302)


NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}


@app.get("/")
def index(request: Request):
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").lower()
    arquivo = "gestor.html" if host.startswith("gestor.") else "index.html"
    return FileResponse(os.path.join(APP_DIR, "static", arquivo), headers=NO_CACHE)


@app.get("/gestor")
def gestor():
    return FileResponse(os.path.join(APP_DIR, "static", "gestor.html"), headers=NO_CACHE)


@app.get("/conferente")
def conferente():
    return FileResponse(os.path.join(APP_DIR, "static", "index.html"), headers=NO_CACHE)


app.mount("/static", StaticFiles(directory=os.path.join(APP_DIR, "static")), name="static")
