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


@app.on_event("startup")
def _aquecer_cache():
    # Após um redeploy o container é novo (cache frio). Sem aquecer, o 1º conferente
    # a abrir esperaria ~3min de tela vazia. Dispara o build em background no boot.
    try:
        ml._start_bg_build()
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
