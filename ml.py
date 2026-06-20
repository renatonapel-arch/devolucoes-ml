# -*- coding: utf-8 -*-
"""
Motor de leitura das devoluções ML voltando pro galpão (Fase 1 — SÓ LEITURA).
- Token LUCRATIVIDADE com auto-refresh (mesma fonte do refresh.py: ~/.claude/.env).
- Lista ao vivo do que está voltando (reaproveita a lógica do devolucoes_galpao.py).
- Busca por qualquer ID da etiqueta (venda, pack, claim, envio, rastreio, anúncio).
NUNCA escreve no ML. Só GET.
"""
import os, re, json, time, threading
from datetime import datetime, date
import requests

ENV_PATH = os.path.expanduser(r"~/.claude/.env")
INDEX_DIR = os.environ.get("DEVOL_INDEX_DIR", r"C:\Users\Renato\Downloads\demo-vendas-ml")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.environ.get("DEVOL_CACHE_FILE", os.path.join(APP_DIR, "cache_aguardando.json"))
def _today():
    """Em prod usa date.today(). Pra reproduzir o mockup fixe DEVOL_HOJE=2026-06-19."""
    h = os.environ.get("DEVOL_HOJE")
    if h:
        try: return date.fromisoformat(h)
        except Exception: pass
    return date.today()

_lock = threading.RLock()
_sess = requests.Session()
_tok = {"access": None, "refresh": None, "seller": None, "app_id": None, "secret": None}


# ---------------- .env / token ----------------
# Ordem de fonte: (1) env vars do processo (Coolify) — sobrescreve qualquer outra coisa.
# (2) ~/.claude/.env (DEV no PC do Renato). (3) cache no SQLite (token rotacionado em prod).
def _read_env_file():
    env = {}
    try:
        for line in open(ENV_PATH, encoding="utf-8"):
            m = re.match(r"^([A-Z0-9_]+)=(.*)$", line.strip())
            if m:
                env[m.group(1)] = m.group(2).split("#")[0].strip()
    except Exception:
        pass
    return env


def _kv_get(k):
    try:
        if "_db" not in globals(): return None
        r = _db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r["v"] if r else None
    except Exception:
        return None


def _kv_set(k, v):
    try:
        if "_db" not in globals(): return
        _db.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, v))
        _db.commit()
    except Exception:
        pass


def _load_tokens():
    fenv = _read_env_file()
    def pick(name):
        return os.environ.get(name) or _kv_get(name) or fenv.get(name)
    _tok["access"] = pick("LUCRATIVIDADE_ML_ACCESS_TOKEN")
    _tok["refresh"] = pick("LUCRATIVIDADE_ML_REFRESH_TOKEN")
    _tok["seller"] = pick("LUCRATIVIDADE_ML_SELLER_ID")
    _tok["app_id"] = pick("LUCRATIVIDADE_ML_APP_ID")
    _tok["secret"] = pick("LUCRATIVIDADE_ML_CLIENT_SECRET")


def _refresh_token():
    """Renova o access_token. Persiste no SQLite (prod) E no .env (dev, se existir)."""
    _load_tokens()
    j = requests.post("https://api.mercadolibre.com/oauth/token", data={
        "grant_type": "refresh_token",
        "client_id": _tok["app_id"],
        "client_secret": _tok["secret"],
        "refresh_token": _tok["refresh"],
    }, timeout=20).json()
    new = j.get("access_token")
    if not new:
        raise RuntimeError(f"refresh falhou: {j}")
    _tok["access"] = new
    _kv_set("LUCRATIVIDADE_ML_ACCESS_TOKEN", new)
    if j.get("refresh_token"):
        _tok["refresh"] = j["refresh_token"]
        _kv_set("LUCRATIVIDADE_ML_REFRESH_TOKEN", j["refresh_token"])
    # mantém o .env do PC em sincronia (modo dev local)
    if os.path.exists(ENV_PATH):
        try:
            txt = open(ENV_PATH, encoding="utf-8").read()
            txt = re.sub(r"LUCRATIVIDADE_ML_ACCESS_TOKEN=.*", f"LUCRATIVIDADE_ML_ACCESS_TOKEN={new}", txt, count=1)
            if j.get("refresh_token"):
                txt = re.sub(r"LUCRATIVIDADE_ML_REFRESH_TOKEN=.*",
                             f"LUCRATIVIDADE_ML_REFRESH_TOKEN={j['refresh_token']}", txt, count=1)
            open(ENV_PATH, "w", encoding="utf-8").write(txt)
        except Exception:
            pass
    return new


def g(path):
    """GET autenticado. Renova token uma vez se 401. Retorna json ou None."""
    if not _tok["access"]:
        _load_tokens()
    for attempt in range(5):
        try:
            r = _sess.get("https://api.mercadolibre.com" + path,
                          headers={"Authorization": "Bearer " + (_tok["access"] or "")}, timeout=30)
        except Exception:
            time.sleep(1); continue
        if r.status_code in (200, 206):
            return r.json()
        if r.status_code == 401:
            with _lock:
                _refresh_token()
            continue
        if r.status_code == 429:
            time.sleep(2 ** attempt); continue
        if r.status_code == 404:
            return None
        return None
    return None


def token_status():
    if not _tok["access"]:
        _load_tokens()
    me = g("/users/me") or {}
    return {"ok": bool(me.get("id")), "seller_id": me.get("id"), "nickname": me.get("nickname")}


# ---------------- índices ----------------
def _loadj(name):
    p = os.path.join(INDEX_DIR, name)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}


VOLTA_SUB = {"returning_to_sender", "returning_to_hub", "returned_to_hub"}
DEVOLU_VIVA = {"shipped", "ready_to_ship", "label_generated", "in_route", "in_hub",
               "handling", "pending", "not_delivered"}


def _norm_fase(s):
    s = (s or "").lower()
    if s in ("shipped", "in_route", "in_hub", "out_for_delivery", "delivering"):
        return "shipped"
    if s in ("label_generated", "ready_to_ship", "handling", "pending"):
        return "label_generated"
    if s in ("returning_to_sender",):
        return "returning_to_sender"
    if s in ("returning_to_hub", "returned_to_hub"):
        return "returning_to_hub"
    return s or "shipped"


def _dias(dstr):
    try:
        return (_today() - date.fromisoformat(dstr[:10])).days
    except Exception:
        return None


# conjunto de orders que já foram/estão sendo devolvidos (p/ reincidência)
_RET_ORDERS = None
def _ret_orders():
    global _RET_ORDERS
    if _RET_ORDERS is None:
        idx = _loadj("claim_return_index.json")
        _RET_ORDERS = {str(k) for k, v in idx.items()
                       if str(k).startswith("2000") and (v.get("returned") or v.get("returning"))}
    return _RET_ORDERS


def _buyer_hist(buyer_id, current_oid):
    """compras totais na Napel + nº de devoluções (real)."""
    out = {"compras": None, "devolucoes": 1}
    if not buyer_id:
        return out
    s = g(f"/orders/search?seller={_tok['seller']}&buyer={buyer_id}&sort=date_desc")
    if isinstance(s, dict) and "paging" in s:
        out["compras"] = s["paging"]["total"]
        oids = {str(e["id"]) for e in s.get("results", [])}
        dev = (oids & _ret_orders()) | {str(current_oid)}
        out["devolucoes"] = len(dev)
    return out


def build_item(oid, origem="devolucao"):
    """Monta o registro enriquecido de uma devolução (mesmos campos do mockup)."""
    o = g(f"/orders/{oid}")
    if not o:
        return None
    try:
        it = o["order_items"][0]
        title = it["item"]["title"]
        item_id = it["item"]["id"]
        qtd = str(it.get("quantity") or 1)
    except Exception:
        title, item_id, qtd = "", None, "1"
    buyer = o.get("buyer") or {}
    valor = o.get("total_amount") or o.get("paid_amount") or 0
    data_venda = (o.get("date_created") or "")[:10]
    shipid = (o.get("shipping") or {}).get("id")
    meds = [m.get("id") for m in (o.get("mediations") or [])]

    tipo = "comprador"
    fase = None
    claim_id = None
    claim_type = claim_status = reason_id = status_money = destino = local = dev_aberta = None

    # 1) não-entrega (shipment voltando)?
    if shipid:
        sh = g(f"/shipments/{shipid}") or {}
        if sh.get("substatus") in VOLTA_SUB:
            tipo, fase = "nao_entrega", sh.get("substatus")

    # 2) devolução via claim/mediação
    if tipo != "nao_entrega" and meds:
        for cid in reversed(meds):
            rt = g(f"/post-purchase/v2/claims/{cid}/returns")
            if isinstance(rt, dict) and rt.get("shipments"):
                claim_id = str(cid)
                status_money = rt.get("status_money")
                fase = rt.get("status")
                dev_aberta = (rt.get("date_created") or "")[:10] or None
                shp = rt["shipments"][0]
                d = shp.get("destination") or {}
                destino = d.get("name")
                addr = d.get("shipping_address") or {}
                city = (addr.get("city") or {}).get("name") or ""
                uf = ((addr.get("state") or {}).get("id") or "").replace("BR-", "")
                local = (city + ("-" + uf if uf else "")) or None
                sh_id = shp.get("shipment_id")
                track = shp.get("tracking_number")
                # detalhe do claim (tipo/motivo/status)
                c = g(f"/post-purchase/v1/claims/{cid}") or {}
                claim_type = c.get("type")
                claim_status = c.get("status")
                reason_id = c.get("reason_id")
                o["_ret_shipment"] = sh_id
                o["_ret_track"] = track
                break

    if fase is None:
        return None  # não está voltando (já chegou / encerrado)

    # ids da etiqueta
    dc = _loadj("delivery_cache.json")
    pack_id = str((dc.get(str(oid)) or {}).get("pack_id") or oid)
    shipment_id = str(o.get("_ret_shipment") or shipid or "")
    tracking = o.get("_ret_track") or "-"

    hist = _buyer_hist(buyer.get("id"), oid)

    return {
        "produto": title, "valor": float(valor or 0), "comprador": buyer.get("nickname", ""),
        "data_venda": data_venda, "order_id": str(oid), "pack_id": pack_id,
        "claim_id": claim_id, "shipment_id": shipment_id, "tracking": tracking,
        "fase": _norm_fase(fase), "tipo": tipo,
        "claim_type": claim_type, "claim_status": claim_status, "reason_id": reason_id,
        "status_money": status_money, "destino": destino, "item_id": item_id, "qtd": qtd,
        "local": local or "—", "vol_total": 1,
        "compras": hist["compras"], "devolucoes": hist["devolucoes"],
        "dev_aberta": dev_aberta, "dias_aberta": _dias(dev_aberta) if dev_aberta else None,
    }


def _candidatos():
    """Lista candidatos a 'devolução voltando'. Tenta primeiro índices locais (PC do Renato).
    Se não houver, puxa direto da API ML — funciona standalone na VPS."""
    cand = {}
    cret = _loadj("claim_return_index.json")
    sv = _loadj("status_vivo_index.json")
    if cret or sv:
        for oid, v in cret.items():
            if v.get("returning") and not v.get("returned"):
                cand[str(oid)] = "devolucao"
        for oid, s in sv.items():
            sub = str(s).split("|")[-1]
            if sub in VOLTA_SUB:
                cand.setdefault(str(oid), "nao_entrega")
        return cand
    # ---- fallback API ML (sem dependência de índice no disco) ----
    if not _tok["seller"]:
        _load_tokens()
    seller = _tok["seller"]
    if not seller:
        return cand
    # devoluções com produto em trânsito de volta (claims do tipo return)
    offset = 0
    for _ in range(20):
        r = g(f"/post-purchase/v1/claims/search?seller={seller}&stage=claim&type=return&limit=50&offset={offset}")
        results = (r or {}).get("data") or (r or {}).get("results") or []
        if not results:
            break
        for c in results:
            oid = str((c.get("resource_id") or c.get("resource") or ""))
            if oid.startswith("2000"):
                cand.setdefault(oid, "devolucao")
        if len(results) < 50:
            break
        offset += 50
    # não-entregas (pedidos com shipment voltando ao remetente, últimos 60 dias)
    from datetime import timedelta
    desde = (_today() - timedelta(days=60)).isoformat()
    r = g(f"/orders/search?seller={seller}&order.date_created.from={desde}T00:00:00.000-03:00&limit=50&sort=date_desc")
    for e in (r or {}).get("results", []):
        oid = str(e.get("id"))
        shp = (e.get("shipping") or {}).get("id")
        if shp:
            sh = g(f"/shipments/{shp}") or {}
            if sh.get("substatus") in VOLTA_SUB:
                cand.setdefault(oid, "nao_entrega")
    return cand


def build_aguardando(force=False, max_idade_min=30):
    """Lista ao vivo. Usa cache em arquivo; recomputa se velho ou force."""
    if os.environ.get("DEVOL_EMPTY") == "1":   # sem devoluções reais (só itens de teste, se ligados)
        return _with_test({"ts": time.time(), "atualizado": datetime.now().strftime("%d/%m/%Y %H:%M"),
                           "total": 0, "itens": []})
    if not force and os.path.exists(CACHE_FILE):
        try:
            c = json.load(open(CACHE_FILE, encoding="utf-8"))
            idade = (time.time() - c.get("ts", 0)) / 60
            if idade < max_idade_min:
                return _with_test(c)
        except Exception:
            pass
    cand = _candidatos()
    itens = []
    for oid, origem in sorted(cand.items()):
        try:
            it = build_item(oid, origem)
            if it:
                itens.append(it)
        except Exception:
            continue
    rank = {"delivered": -1, "shipped": 0, "label_generated": 1, "returning_to_sender": 2, "returning_to_hub": 2}
    itens.sort(key=lambda x: (rank.get(x["fase"], 3), -x["valor"]))
    out = {"ts": time.time(), "atualizado": datetime.now().strftime("%d/%m/%Y %H:%M"),
           "total": len(itens), "itens": itens}
    try:
        json.dump(out, open(CACHE_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass
    return _with_test(out)


def buscar(code, force_ml=False):
    """Acha a devolução por qualquer ID. Primeiro na lista, depois ao vivo no ML."""
    code = (code or "").strip()
    if not code:
        return {"found": False, "reason": "vazio"}
    cache = build_aguardando()
    up = code.upper()
    if not force_ml:
        for it in cache["itens"]:
            for f in ("order_id", "pack_id", "claim_id", "shipment_id", "tracking", "item_id"):
                v = str(it.get(f) or "").upper()
                if v and v == up:
                    return {"found": True, "in_list": True, "by": f, "item": it}
        return {"found": False, "in_list": False, "code": code}

    # ---- busca AO VIVO no ML (fora da lista) ----
    oid = None
    if re.fullmatch(r"2000\d{10,}", code):           # nº da venda
        oid = code
    elif re.fullmatch(r"\d{8,}", code):              # tenta shipment -> order
        sh = g(f"/shipments/{code}")
        if sh and sh.get("order_id"):
            oid = str(sh["order_id"])
        elif re.fullmatch(r"5\d{9}", code):          # parece claim
            rt = g(f"/post-purchase/v2/claims/{code}/returns")
            if isinstance(rt, dict) and rt.get("resource_id"):
                oid = str(rt["resource_id"])
    if oid:
        it = build_item(oid)
        if it:
            return {"found": True, "in_list": False, "by": "ml", "item": it}
    return {"found": False, "in_list": False, "code": code,
            "hint": "Sem resultado no ML por esse código. Tente o nº da venda (2000…) ou o cód. de envio (47…)."}


# =====================================================================
# Fase 2 — modo teste / envio de avaria (TRAVA DE ESCRITA)
# Por padrão: modo TESTE (nada vai pro ML). Real só com env DEVOL_WRITE=1.
# =====================================================================
WRITE_ENABLED = os.environ.get("DEVOL_WRITE") == "1"


def mode():
    return {"mode": "real" if WRITE_ENABLED else "teste", "write_enabled": WRITE_ENABLED}


def test_items():
    """12 devoluções FALSAS — uma por cenário ponta a ponta. O nome diz o que fazer.
    Só aparecem com DEVOL_TEST_ITEMS=1. Códigos começam com TESTE."""
    def it(oid, nome, valor, **kw):
        d = {"produto": nome, "valor": valor, "comprador": "COMPRADOR_TESTE",
             "data_venda": "2026-06-15", "order_id": oid, "pack_id": oid,
             "claim_id": "CLAIM-" + oid, "shipment_id": "SH-" + oid, "tracking": "BR-" + oid,
             "item_id": "MLB-" + oid, "fase": "delivered", "tipo": "comprador",
             "claim_type": "returns", "claim_status": "closed", "reason_id": "TESTE",
             "status_money": "refunded", "destino": "seller_address", "qtd": "1",
             "local": "Maringá-PR", "vol_total": 1, "compras": 1, "devolucoes": 1,
             "dev_aberta": "2026-06-17", "dias_aberta": 2, "teste": True}
        d.update(kw)
        return d
    return [
        it("TESTE-T01", "[T1] Certo → volta ao estoque", 120.00, status_money="retained"),
        it("TESTE-T02", "[T2] Avariado → ganhamos → volta", 199.90, claim_type="mediations", claim_status="opened"),
        it("TESTE-T03", "[T3] Avariado → perdemos → descarte", 250.00, claim_type="mediations", claim_status="opened"),
        it("TESTE-T04", "[T4] Trocado/golpe → ganhamos → retido", 380.00, claim_type="mediations", claim_status="opened"),
        it("TESTE-T05", "[T5] Faltando peças → perdemos → volta", 175.00, claim_type="mediations", claim_status="opened"),
        it("TESTE-T06", "[T6] Caixa vazia → ganhamos → descarte", 90.00, claim_type="mediations", claim_status="opened"),
        it("TESTE-T07", "[T7] Multi-volume (3 caixas) — parcial", 612.00, qtd="3", vol_total=3),
        it("TESTE-T08", "[T8] Com compensação SIM", 140.00),
        it("TESTE-T09", "[T9] Sem compensação (não se aplica)", 60.00, status_money="retained"),
        it("TESTE-T10", "[T10] CD do ML (warehouse) — não confere", 300.00, destino="warehouse", local="Cajamar-SP"),
        it("TESTE-T11", "[T11] Não-entrega → volta ao estoque", 130.00, tipo="nao_entrega", claim_id=None, fase="returning_to_sender", status_money=None),
        it("TESTE-T12", "[T12] Trava de concorrência (abra em 2)", 210.00),
    ]


def _with_test(out):
    # Devoluções FALSAS de treino só aparecem se DEVOL_TEST_ITEMS=1 (padrão: OFF).
    if WRITE_ENABLED or os.environ.get("DEVOL_TEST_ITEMS") != "1":
        return out
    ti = test_items()
    return {**out, "itens": ti + out.get("itens", []),
            "total": len(ti) + out.get("total", 0), "modo": "teste"}


def _post(path, files=None, json_body=None):
    if not _tok["access"]:
        _load_tokens()
    for attempt in range(3):
        try:
            r = _sess.post("https://api.mercadolibre.com" + path,
                           headers={"Authorization": "Bearer " + (_tok["access"] or "")},
                           files=files, json=json_body, timeout=60)
        except Exception as e:
            return {"_err": "net", "_b": str(e)}
        if r.status_code in (200, 201):
            try:
                return r.json()
            except Exception:
                return {"ok": True}
        if r.status_code == 401:
            with _lock:
                _refresh_token()
            continue
        return {"_err": r.status_code, "_b": r.text[:400]}
    return {"_err": "retry"}


DRYRUN_LOG = os.path.join(APP_DIR, "avaria_dryrun.log")


def enviar_avaria(payload):
    """Envia reclamação de avaria. Em modo teste (default) faz DRY-RUN: registra o que
    SERIA enviado e não toca no ML. Em modo real (DEVOL_WRITE=1) sobe anexos + abre revisão SRF2."""
    import base64
    claim_id = str(payload.get("claim_id") or "")
    msg = (payload.get("mensagem") or "").strip()
    fotos = payload.get("fotos") or []
    n = len(fotos)
    is_test = (not WRITE_ENABLED) or claim_id.startswith("TESTE") or not claim_id
    if is_test:
        try:
            with open(DRYRUN_LOG, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().isoformat()} | claim={claim_id} | anexos={n} | msg={msg[:120]}\n")
        except Exception:
            pass
        return {"ok": True, "mode": "teste", "claim_id": claim_id,
                "resumo": {"motivo": "SRF2 (produto chegou avariado)", "anexos": n,
                           "endpoint_anexo": f"POST /post-purchase/v1/claims/{claim_id}/attachments",
                           "endpoint_revisao": f"POST /post-purchase/v1/claims/{claim_id}/returns/review",
                           "mensagem": msg},
                "aviso": "Modo teste — NADA foi enviado ao Mercado Livre."}
    # ---- modo REAL (gated) ----
    enviados = []
    for i, durl in enumerate(fotos):
        try:
            raw = durl.split(",", 1)[1] if "," in durl else durl
            b = base64.b64decode(raw)
        except Exception:
            continue
        r = _post(f"/post-purchase/v1/claims/{claim_id}/attachments",
                  files={"file": (f"avaria_{i+1}.jpg", b, "image/jpeg")})
        if isinstance(r, dict) and not r.get("_err"):
            enviados.append(r.get("filename") or r.get("original_filename") or f"avaria_{i+1}.jpg")
        else:
            return {"ok": False, "mode": "real", "etapa": "upload_anexo", "erro": r}
    review = _post(f"/post-purchase/v1/claims/{claim_id}/returns/review",
                   json_body={"reason": "SRF2", "message": msg, "attachments": enviados})
    ok = not (isinstance(review, dict) and review.get("_err"))
    return {"ok": ok, "mode": "real", "anexos_enviados": enviados, "review": review}


# =====================================================================
# Persistência da CONFERÊNCIA (manual-first) — etapas salvam sozinhas,
# com quem fez e quando. Status derivado: nao_iniciada / em_andamento /
# aguardando_ml / concluida. SIGE é sempre MANUAL (botão "feito").
# =====================================================================
CONF_FILE = os.path.join(APP_DIR, "conferencias.json")   # legado (migrado p/ SQLite)
DB_FILE = os.environ.get("DEVOL_DB_PATH", os.path.join(APP_DIR, "devolucoes.db"))
RUINS = ("avariado", "trocado", "faltando", "vazio")

import sqlite3
# garante diretório (caso esteja em /data e o volume seja novo)
try: os.makedirs(os.path.dirname(DB_FILE) or ".", exist_ok=True)
except Exception: pass
_db = sqlite3.connect(DB_FILE, check_same_thread=False)
_db.row_factory = sqlite3.Row
_db.execute("CREATE TABLE IF NOT EXISTS conferencias (order_id TEXT PRIMARY KEY, reg TEXT, updated_at REAL)")
_db.execute("CREATE TABLE IF NOT EXISTS anexos (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, etapa TEXT, tipo TEXT, data TEXT, criado_em TEXT)")
_db.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
_db.execute("CREATE INDEX IF NOT EXISTS ix_anexos_oid ON anexos(order_id)")
_db.commit()


def _migrar_json():
    """Importa o conferencias.json antigo (uma vez), se o banco estiver vazio."""
    try:
        with _lock:
            n = _db.execute("SELECT COUNT(*) c FROM conferencias").fetchone()["c"]
            if n == 0 and os.path.exists(CONF_FILE):
                d = json.load(open(CONF_FILE, encoding="utf-8"))
                for oid, reg in d.items():
                    _db.execute("INSERT OR REPLACE INTO conferencias(order_id,reg,updated_at) VALUES(?,?,?)",
                                (str(oid), json.dumps(reg, ensure_ascii=False), time.time()))
                _db.commit()
    except Exception:
        pass


_migrar_json()


def _get_reg(oid):
    with _lock:
        r = _db.execute("SELECT reg FROM conferencias WHERE order_id=?", (str(oid),)).fetchone()
    return json.loads(r["reg"]) if r else None


def _put_reg(oid, reg):
    with _lock:
        _db.execute("INSERT OR REPLACE INTO conferencias(order_id,reg,updated_at) VALUES(?,?,?)",
                    (str(oid), json.dumps(reg, ensure_ascii=False), time.time()))
        _db.commit()


def _all_regs():
    with _lock:
        rows = _db.execute("SELECT order_id,reg FROM conferencias").fetchall()
    return {row["order_id"]: json.loads(row["reg"]) for row in rows}


def save_anexos(oid, etapa, tipo, fotos):
    if not fotos:
        return 0
    em = datetime.now().strftime("%d/%m/%Y %H:%M")
    n = 0
    with _lock:
        for durl in fotos:
            if not durl:
                continue
            _db.execute("INSERT INTO anexos(order_id,etapa,tipo,data,criado_em) VALUES(?,?,?,?,?)",
                        (str(oid), etapa, tipo, durl, em))
            n += 1
        _db.commit()
        tot = _db.execute("SELECT COUNT(*) c FROM anexos WHERE order_id=? AND etapa=?", (str(oid), etapa)).fetchone()["c"]
    return tot


def get_anexos(oid):
    with _lock:
        rows = _db.execute("SELECT id,etapa,tipo,data,criado_em FROM anexos WHERE order_id=? ORDER BY id",
                           (str(oid),)).fetchall()
    return [{"id": r["id"], "etapa": r["etapa"], "tipo": r["tipo"], "data": r["data"], "em": r["criado_em"]} for r in rows]


def _req_etapas(reg):
    desf = (reg.get("etapas", {}).get("abertura") or {}).get("desfecho")
    base = ["chegada", "abertura", "nf_devolucao", "financeiro", "compensacao", "estoque"]
    if desf in RUINS:
        base.insert(2, "reclamacao_ml")
    return base


def compute_status(reg):
    et = reg.get("etapas", {})
    rc = et.get("reclamacao_ml") or {}
    if rc.get("enviado") and (rc.get("resultado") or "aguardando") == "aguardando":
        return "aguardando_ml"
    req = _req_etapas(reg)
    if all((et.get(k) or {}).get("feito") for k in req):
        return "concluida"
    if et:   # qualquer etapa registrada (inclusive chegada parcial) = iniciada
        return "em_andamento"
    return "nao_iniciada"


def _progresso(reg):
    et = reg.get("etapas", {})
    req = _req_etapas(reg)
    return {"feito": sum(1 for k in req if (et.get(k) or {}).get("feito")), "total": len(req)}


def _novo(item):
    return {"order_id": str(item["order_id"]), "claim_id": item.get("claim_id"),
            "produto": item.get("produto"), "valor": item.get("valor"), "comprador": item.get("comprador"),
            "fase": item.get("fase"), "destino": item.get("destino"), "status_money": item.get("status_money"),
            "dias_aberta": item.get("dias_aberta"), "vol_total": item.get("vol_total", 1), "snapshot": item,
            "etapas": {}, "iniciada_por": None, "atualizado_em": None, "ativo": None}


def get_conferencia(oid, item=None):
    reg = _get_reg(oid)
    if not reg:
        reg = _novo(item) if item else None
    if reg:
        reg = dict(reg)
        reg["status"] = compute_status(reg)
        reg["progresso"] = _progresso(reg)
    return reg


def save_etapa(oid, etapa, dados, perfil, nome, item=None):
    with _lock:
        reg = _get_reg(oid) or (_novo(item) if item else None)
        if reg is None:
            return {"erro": "item desconhecido"}
        dados = dict(dados or {})
        anexos = dados.pop("anexos", None)
        tipo_anexo = dados.pop("anexo_tipo", etapa)
        if anexos:
            dados["fotos"] = save_anexos(oid, etapa, tipo_anexo, anexos)
        e = reg["etapas"].get(etapa, {})
        if dados:
            e.update(dados)
        e["feito"] = dados.get("feito", True) if ("feito" in dados) else True
        e["perfil"] = perfil
        e["nome"] = nome
        e["em"] = datetime.now().strftime("%d/%m %H:%M")
        reg["etapas"][etapa] = e
        if not reg.get("iniciada_por") and nome:
            reg["iniciada_por"] = f"{nome} · {perfil}"
        reg["atualizado_em"] = datetime.now().strftime("%d/%m/%Y %H:%M")
        reg["ativo"] = {"nome": nome, "perfil": perfil, "ts": time.time()}
        _put_reg(oid, reg)
        out = dict(reg)
        out["status"] = compute_status(reg)
        out["progresso"] = _progresso(reg)
        return out


# trava de concorrência (soft): marca quem está ativo no card AGORA (<120s)
def claim_lock(oid, perfil, nome, item=None):
    with _lock:
        reg = _get_reg(oid) or (_novo(item) if item else None)
        if reg is None:
            return {"anterior": None}
        ant = reg.get("ativo")
        outro_fresco = bool(ant and ant.get("nome") != nome and (time.time() - ant.get("ts", 0)) < 120)
        reg["ativo"] = {"nome": nome, "perfil": perfil, "ts": time.time()}
        _put_reg(oid, reg)
        if outro_fresco:
            ha = int(time.time() - ant.get("ts", 0))
            return {"anterior": {"nome": ant.get("nome"), "perfil": ant.get("perfil"), "ha_seg": ha}}
        return {"anterior": None}


def lista():
    base = build_aguardando()
    confs = _all_regs()
    itens, vistos = [], set()
    for it in base["itens"]:
        oid = str(it["order_id"])
        vistos.add(oid)
        reg = confs.get(oid)
        it = dict(it)
        it["conf_status"] = compute_status(reg) if reg else "nao_iniciada"
        it["conf_prog"] = _progresso(reg) if reg else None
        it["vol_recebidos"] = ((reg or {}).get("etapas", {}).get("chegada") or {}).get("recebidos", 0)
        itens.append(it)
    # conferências já iniciadas que sumiram da lista ao vivo NÃO podem desaparecer
    for oid, reg in confs.items():
        if oid in vistos:
            continue
        st = compute_status(reg)
        if st == "nao_iniciada":
            continue
        snap = dict(reg.get("snapshot") or {"order_id": oid, "produto": reg.get("produto"),
                "valor": reg.get("valor"), "comprador": reg.get("comprador"), "fase": reg.get("fase"),
                "destino": reg.get("destino"), "status_money": reg.get("status_money"),
                "claim_id": reg.get("claim_id"), "qtd": "1", "dias_aberta": reg.get("dias_aberta")})
        snap["conf_status"] = st
        snap["conf_prog"] = _progresso(reg)
        itens.append(snap)
    from collections import Counter
    cont = Counter(i.get("conf_status", "nao_iniciada") for i in itens)
    return {"atualizado": base.get("atualizado"), "total": len(itens), "itens": itens,
            "contagem": {k: cont.get(k, 0) for k in ["nao_iniciada", "em_andamento", "aguardando_ml", "concluida"]},
            "modo": mode()["mode"]}
