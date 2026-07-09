# -*- coding: utf-8 -*-
"""
Motor de leitura das devoluções ML voltando pro galpão (Fase 1 — SÓ LEITURA).
- Token LUCRATIVIDADE com auto-refresh (mesma fonte do refresh.py: ~/.claude/.env).
- Lista ao vivo do que está voltando (reaproveita a lógica do devolucoes_galpao.py).
- Busca por qualquer ID da etiqueta (venda, pack, claim, envio, rastreio, anúncio).
NUNCA escreve no ML. Só GET.
"""
import os, re, json, time, threading
from datetime import datetime, date, timezone, timedelta
import requests

_BR = timezone(timedelta(hours=-3))   # horário de Brasília (Brasil não tem mais horário de verão)
def _now():
    return datetime.now(_BR)

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
        sub = sh.get("substatus")
        if sub in VOLTA_SUB:
            tipo, fase = "nao_entrega", sub                      # voltando ao remetente (a caminho)
        elif sub in ("returned", "returned_to_sender"):          # já voltou (no ML) — reconhece p/ o
            tipo, fase = "nao_entrega", "chegou"                 # conferente CONSEGUIR conferir AO BIPAR
            o["_chegou_em"] = ((sh.get("status_history") or {}).get("date_returned") or "")[:10] or None  # (não entra na lista automática)

    # 2) devolução via claim/mediação
    if tipo != "nao_entrega" and meds:
        for cid in reversed(meds):
            rt = g(f"/post-purchase/v2/claims/{cid}/returns")
            if isinstance(rt, dict) and rt.get("shipments"):
                claim_id = str(cid)
                status_money = rt.get("status_money")
                fase = rt.get("status")
                dev_aberta = (rt.get("date_created") or "")[:10] or None
                if fase == "delivered":   # chegou no galpão — guarda quando
                    o["_chegou_em"] = (rt.get("last_updated") or rt.get("date_closed") or "")[:10] or None
                shps = rt["shipments"]
                # prefere o envio que vem pro VENDEDOR (seller_address) — é o rótulo que o conferente tem na mão
                shp = next((s for s in shps if (s.get("destination") or {}).get("name") == "seller_address"), shps[0])
                d = shp.get("destination") or {}
                destino = d.get("name")
                addr = d.get("shipping_address") or {}
                city = (addr.get("city") or {}).get("name") or ""
                uf = ((addr.get("state") or {}).get("id") or "").replace("BR-", "")
                local = (city + ("-" + uf if uf else "")) or None
                sh_id = shp.get("shipment_id")
                track = shp.get("tracking_number")
                # TODOS os envios/rastreios da devolução (pode ter 2: seller_address + warehouse) — bipar QUALQUER rótulo acha
                o["_ret_all_ids"] = [str(s.get("shipment_id")) for s in shps if s.get("shipment_id")] + \
                                    [str(s.get("tracking_number")) for s in shps if s.get("tracking_number")]
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
    # TODOS os envios/rastreios da devolução (bipar qualquer rótulo acha) + o envio do pedido
    all_ids = list(o.get("_ret_all_ids") or [])
    if shipid: all_ids.append(str(shipid))
    shipment_ids = sorted({i for i in all_ids if i and i not in ("None", "-")})

    hist = _buyer_hist(buyer.get("id"), oid)

    return {
        "produto": title, "valor": float(valor or 0), "comprador": buyer.get("nickname", ""),
        "data_venda": data_venda, "order_id": str(oid), "pack_id": pack_id,
        "claim_id": claim_id, "shipment_id": shipment_id, "tracking": tracking,
        "shipment_ids": shipment_ids,
        "fase": _norm_fase(fase), "tipo": tipo,
        "claim_type": claim_type, "claim_status": claim_status, "reason_id": reason_id,
        "status_money": status_money, "destino": destino, "item_id": item_id, "qtd": qtd,
        "local": local or "—", "vol_total": 1,
        "compras": hist["compras"], "devolucoes": hist["devolucoes"],
        "dev_aberta": dev_aberta, "dias_aberta": _dias(dev_aberta) if dev_aberta else None,
        "chegou_em": o.get("_chegou_em"), "chegou_dias": _dias(o.get("_chegou_em")) if o.get("_chegou_em") else None,
    }


def _claims_por_order():
    """Varre TODOS os claims (opened+closed) do seller -> {order_id: claim_id}. Usado por
    _candidatos() (lista visível) e pelo watcher (_watch_scan) que monitora além da lista."""
    if not _tok["seller"]:
        _load_tokens()
    seller = _tok["seller"]
    out = {}
    if not seller:
        return out
    for status, max_pages in (("opened", 40), ("closed", 20)):
        offset = 0
        for _ in range(max_pages):
            r = g(f"/post-purchase/v1/claims/search?status={status}&limit=50&offset={offset}")
            data = (r or {}).get("data") or []
            total = ((r or {}).get("paging") or {}).get("total", 0)
            for c in data:
                oid = str(c.get("resource_id") or "")
                cid = c.get("id")
                if oid.startswith("2000") and cid:
                    out.setdefault(oid, cid)
            offset += 50
            if not data or offset >= total:
                break
    return out


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
    # A LISTA = só o que está A CAMINHO (comprador postou, ainda em trânsito pra Napel).
    # NÃO usar 'delivered' do ML como "chegou no galpão": o ML marca delivered quando a
    # devolução chega na LOGÍSTICA/CD do ML, não na porta da Napel (mostrava 42 "chegou"
    # quando só havia 2 pacotes físicos). Quando o pacote chega de fato, o conferente BIPA
    # (a busca acha qualquer envio, mesmo delivered) — esse é o sinal real de chegada.
    A_CAMINHO = {"shipped"}
    claims_por_order = _claims_por_order()
    for oid, cid in claims_por_order.items():
        rt = g(f"/post-purchase/v2/claims/{cid}/returns") or {}
        if not (rt.get("shipments")):
            continue
        if rt.get("status") in A_CAMINHO:   # só 'shipped' (a caminho, ainda não chegou)
            cand[str(oid)] = "devolucao"
    # não-entregas: ENVIO que falhou e está VOLTANDO ao remetente (ainda não chegou).
    # shipping.status=not_delivered pega venda antiga voltando agora; mantém só os EM TRÂNSITO
    #  só returning_* = AINDA voltando (a caminho). NÃO usar 'returned': o ML marca returned
    #  quando o pacote chega no hub/logística dele, não na doca da Napel — quem confirma a
    #  chegada física é o conferente bipando (a busca acha a devolução mesmo fora da lista).
    NE_A_CAMINHO = {"returning_to_sender", "returning_to_hub"}
    offset = 0
    for _ in range(8):
        r = g(f"/orders/search?seller={seller}&shipping.status=not_delivered&limit=50&offset={offset}&sort=date_desc")
        results = (r or {}).get("results", [])
        total = ((r or {}).get("paging") or {}).get("total", 0)
        for e in results:
            oid = str(e.get("id"))
            shp = (e.get("shipping") or {}).get("id")
            if shp:
                sh = g(f"/shipments/{shp}") or {}
                if sh.get("substatus") in NE_A_CAMINHO:
                    cand.setdefault(oid, "nao_entrega")
        offset += 50
        if offset >= total or not results:
            break
    return cand


_build_lock = threading.Lock()
_building = False


def _ler_cache():
    if os.path.exists(CACHE_FILE):
        try:
            return json.load(open(CACHE_FILE, encoding="utf-8"))
        except Exception:
            return None
    return None


def _do_build():
    """Recomputa a lista DE FATO (lento, ~3min). Grava no cache. NÃO chamar direto no request."""
    global _building
    try:
        cand = _candidatos()
        itens = []
        for oid, origem in sorted(cand.items()):
            try:
                it = build_item(oid, origem)
                if it:
                    itens.append(it)
            except Exception:
                continue
        # SÓ a caminho (shipped/returning_*). Nada de "chegou" derivado do ML (não é confiável p/
        # a doca da Napel). A chegada física é registrada pelo conferente ao bipar.
        ACEITA_FASE = {"shipped", "returning_to_sender", "returning_to_hub"}
        itens = [it for it in itens if it.get("fase") in ACEITA_FASE]
        rank = {"shipped": 0, "label_generated": 1, "returning_to_sender": 2, "returning_to_hub": 2}
        itens.sort(key=lambda x: (rank.get(x["fase"], 3), -x["valor"]))
        out = {"ts": time.time(), "atualizado": _now().strftime("%d/%m/%Y %H:%M"),
               "total": len(itens), "itens": itens, "construindo": False}
        try:
            json.dump(out, open(CACHE_FILE, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception:
            pass
        return out
    finally:
        with _build_lock:
            _building = False


def _start_bg_build():
    """Dispara o rebuild em thread, se já não houver um rodando."""
    global _building
    with _build_lock:
        if _building:
            return
        _building = True
    threading.Thread(target=_do_build, daemon=True).start()


def build_aguardando(force=False, max_idade_min=30):
    """NUNCA bloqueia o request. Serve cache (mesmo velho) na hora e revalida em background.
    - cache fresco               -> devolve na hora
    - cache velho/inexistente    -> dispara rebuild em background e devolve o que tem
                                    (stale com construindo=True, ou vazio+construindo se 1ª vez)
    - force=True                 -> idem, mas força o rebuild mesmo com cache fresco
    """
    if os.environ.get("DEVOL_EMPTY") == "1":
        return _with_test({"ts": time.time(), "atualizado": _now().strftime("%d/%m/%Y %H:%M"),
                           "total": 0, "itens": [], "construindo": False})
    cached = _ler_cache()
    idade = (time.time() - cached.get("ts", 0)) / 60 if cached else 1e9
    fresca = cached is not None and idade < max_idade_min
    if fresca and not force:
        return _with_test(cached)
    # precisa revalidar -> dispara em background (não trava o HTTP)
    _start_bg_build()
    if cached:                      # tem algo (mesmo velho): serve na hora
        c = dict(cached); c["construindo"] = True
        return _with_test(c)
    # 1ª carga, nada em cache ainda: devolve vazio sinalizando que está construindo
    return _with_test({"ts": 0, "atualizado": "atualizando ao vivo…",
                       "total": 0, "itens": [], "construindo": True})


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
            for sid in (it.get("shipment_ids") or []):   # todos os envios da devolução
                if str(sid).upper() == up:
                    return {"found": True, "in_list": True, "by": "shipment_id", "item": it}
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


MOTIVOS_SRF = {
    "SRF2": "produto chegou avariado", "SRF3": "devolução incompleta",
    "SRF4": "produto devolvido é diferente do enviado", "SRF5": "produto não está no pacote",
}


def enviar_avaria(payload):
    """Envia reclamação de devolução com problema (SRF2/3/4/5, conforme o desfecho marcado
    na etapa 'abertura'). Em modo teste (default) faz DRY-RUN. Em modo real (DEVOL_AVARIA_OK=1),
    fluxo validado ao vivo em 2026-07-06 (venda 2000017088170988, claim 5535822835, motivo
    SRF2 — resultado: seller_status='claimed', claim foi p/ stage='dispute'):
      1) GET /post-purchase/v1/returns/reasons?flow=seller_return_failed&claim_id={cid} — confere reason
      2) POST /post-purchase/v1/claims/{cid}/returns/attachments (multipart 'file') por foto -> file_name
      3) POST /post-purchase/v1/returns/{return_id}/return-review
         corpo = LISTA: [{"reason":MOTIVO,"message":msg,"attachments":[file_names]}]
    (endpoint antigo /claims/{id}/returns/review e /claims/{id}/attachments NÃO EXISTEM —
    eram chute de antes de validar; corrigido aqui). MOTIVO vem do front (mapeado do desfecho
    avariado/trocado/faltando/vazio); só SRF2 foi validado ao vivo até agora — SRF3/4/5 usam
    o mesmo endpoint e formato, então a mesma lógica se aplica, mas ainda sem teste real."""
    import base64
    claim_id = str(payload.get("claim_id") or "")
    msg = (payload.get("mensagem") or "").strip()
    fotos = payload.get("fotos") or []
    motivo = payload.get("motivo") or "SRF2"
    if motivo not in MOTIVOS_SRF:
        motivo = "SRF2"
    n = len(fotos)
    write = os.environ.get("DEVOL_AVARIA_OK") == "1"
    is_test = (not write) or claim_id.startswith("TESTE") or not claim_id
    if is_test:
        try:
            with open(DRYRUN_LOG, "a", encoding="utf-8") as f:
                f.write(f"{_now().isoformat()} | claim={claim_id} | motivo={motivo} | anexos={n} | msg={msg[:120]}\n")
        except Exception:
            pass
        return {"ok": True, "mode": "teste", "claim_id": claim_id,
                "resumo": {"motivo": f"{motivo} ({MOTIVOS_SRF[motivo]})", "anexos": n, "mensagem": msg},
                "aviso": "Modo teste — NADA foi enviado ao Mercado Livre."}
    # ---- modo REAL (gated por DEVOL_AVARIA_OK) ----
    c = g(f"/post-purchase/v1/claims/{claim_id}") or {}
    acts = []
    for p in (c.get("players") or []):
        if p.get("type") == "seller":
            acts = [a.get("action") for a in (p.get("available_actions") or [])]
    if "return_review_fail" not in acts:
        dbg(f"AVARIA claim={claim_id} SKIP (acao indisponivel; acts={acts})")
        return {"ok": False, "mode": "real", "skip": True,
                "nota": f"ML não oferece return_review_fail agora (ações: {acts}). Nada enviado."}
    rt = g(f"/post-purchase/v2/claims/{claim_id}/returns") or {}
    return_id = rt.get("id")
    if not return_id:
        return {"ok": False, "mode": "real", "erro": "return_id não encontrado"}
    enviados = []
    for i, durl in enumerate(fotos):
        try:
            raw = durl.split(",", 1)[1] if "," in durl else durl
            b = base64.b64decode(raw)
        except Exception:
            continue
        r = _post(f"/post-purchase/v1/claims/{claim_id}/returns/attachments",
                  files={"file": (f"avaria_{i+1}.jpg", b, "image/jpeg")})
        if isinstance(r, dict) and r.get("file_name"):
            enviados.append(r["file_name"])
        else:
            dbg(f"AVARIA claim={claim_id} upload_anexo FAIL resp={str(r)[:200]}")
            return {"ok": False, "mode": "real", "etapa": "upload_anexo", "erro": r}
    body = [{"reason": motivo, "message": msg, "attachments": enviados}]
    review = _post(f"/post-purchase/v1/returns/{return_id}/return-review", json_body=body)
    ok = not (isinstance(review, dict) and review.get("_err"))
    dbg(f"AVARIA claim={claim_id} motivo={motivo} return={return_id} anexos={len(enviados)} -> ok={ok} resp={str(review)[:200]}")
    return {"ok": ok, "mode": "real", "motivo": motivo, "return_id": return_id, "anexos_enviados": enviados, "resp": review}


def confirmar_revisao_ok(claim_id):
    """Desfecho 'certo/perfeito': confirma ao ML que a devolução chegou como esperado
    (= botão 'Já revisei' → ação return_review_ok), pra não ficar dias em 'Para sua revisão'.
    Endpoint oficial: POST /post-purchase/v1/returns/{RETURN_ID}/return-review com corpo {}.
    Não mexe no dinheiro (reembolso já sai na entrega/envio). Gate próprio: DEVOL_REVIEW_OK=1
    liga SÓ esta escrita (avaria continua atrás de DEVOL_WRITE)."""
    claim_id = str(claim_id or "")
    write = WRITE_ENABLED or os.environ.get("DEVOL_REVIEW_OK") == "1"
    if (not write) or claim_id.startswith("TESTE") or not claim_id:
        try:
            with open(DRYRUN_LOG, "a", encoding="utf-8") as f:
                f.write(f"{_now().isoformat()} | REVISAO_OK claim={claim_id} (dry-run)\n")
        except Exception:
            pass
        return {"ok": True, "mode": "teste", "claim_id": claim_id,
                "aviso": "Modo teste — confirmação NÃO enviada ao ML."}
    # ---- modo REAL ----
    # 1) valida que a ação está disponível pro seller (não chutar escrita fora de hora)
    c = g(f"/post-purchase/v1/claims/{claim_id}") or {}
    acts = []
    for p in (c.get("players") or []):
        if p.get("type") == "seller":
            acts = [a.get("action") for a in (p.get("available_actions") or [])]
    if "return_review_ok" not in acts:
        dbg(f"REVISAO_OK claim={claim_id} SKIP (acao indisponivel; acts={acts})")
        return {"ok": False, "mode": "real", "skip": True,
                "nota": f"ML não oferece return_review_ok agora (ações: {acts}). Nada enviado."}
    # 2) resolve o RETURN_ID e confirma
    rt = g(f"/post-purchase/v2/claims/{claim_id}/returns") or {}
    rid = rt.get("id")
    if not rid:
        return {"ok": False, "mode": "real", "erro": "return_id não encontrado"}
    # corpo = LISTA de problemas; lista vazia [] = "chegou como esperado" (validado ao vivo: 201 completed)
    r = _post(f"/post-purchase/v1/returns/{rid}/return-review", json_body=[])
    ok = not (isinstance(r, dict) and r.get("_err"))
    dbg(f"REVISAO_OK claim={claim_id} return={rid} -> ok={ok} resp={str(r)[:200]}")
    return {"ok": ok, "mode": "real", "return_id": rid, "resp": r}


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
# entradas = "deu entrada na doca" (fase 1). Tabela SEPARADA das conferências de propósito:
# não interage com etapas/compute_status — impossível mudar o comportamento atual.
_db.execute("CREATE TABLE IF NOT EXISTS entradas (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, codigo TEXT, produto TEXT, comprador TEXT, nome TEXT, em TEXT, ts REAL)")
# ml_watch = detecção automática de chegada via API do ML (fase de validação, roda em
# paralelo à bipagem manual da Natalia por alguns dias até provar 100% de match).
_db.execute("""CREATE TABLE IF NOT EXISTS ml_watch (
    claim_id TEXT PRIMARY KEY, order_id TEXT, produto TEXT, comprador TEXT,
    primeiro_visto_ts REAL, chegada_ts REAL, chegada_fonte TEXT,
    notificado_ts REAL, bip_ts REAL, bip_nome TEXT, match TEXT, finalizado_ts REAL)""")
_db.execute("CREATE INDEX IF NOT EXISTS ix_anexos_oid ON anexos(order_id)")
_db.execute("CREATE INDEX IF NOT EXISTS ix_entradas_oid ON entradas(order_id)")
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
    em = _now().strftime("%d/%m/%Y %H:%M")
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


# ---- log de debug persistente (vive no volume /data) p/ capturar falhas reais do conferente ----
DEBUG_LOG = os.path.join(os.path.dirname(DB_FILE) or ".", "debug_conferente.log")
def dbg(msg):
    try:
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"{_now().strftime('%d/%m %H:%M:%S')} | {msg}\n")
    except Exception:
        pass


def _req_etapas(reg):
    desf = (reg.get("etapas", {}).get("abertura") or {}).get("desfecho")
    base = ["chegada", "abertura", "nf_devolucao", "compensacao", "estoque"]
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
        e["em"] = _now().strftime("%d/%m %H:%M")
        reg["etapas"][etapa] = e
        if not reg.get("iniciada_por") and nome:
            reg["iniciada_por"] = f"{nome} · {perfil}"
        reg["atualizado_em"] = _now().strftime("%d/%m/%Y %H:%M")
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
            "construindo": bool(base.get("construindo")),
            "contagem": {k: cont.get(k, 0) for k in ["nao_iniciada", "em_andamento", "aguardando_ml", "concluida"]},
            "modo": mode()["mode"]}


# =====================================================================
# ENTRADA NA DOCA (fase 1) — bipada rápida de "deu entrada", sem conferir.
# Cria o carimbo REAL de chegada física. Não toca em conferências/etapas.
# =====================================================================
def registrar_entrada(code, nome):
    code = (code or "").strip()
    if not code:
        return {"ok": False, "erro": "código vazio"}
    # identifica a devolução (lista primeiro, ML depois — mesma busca já testada)
    r = buscar(code)
    if not r.get("found"):
        r = buscar(code, force_ml=True)
    it = r.get("item") or {}
    oid = it.get("order_id")
    if not oid:
        # falha de bipagem não é chegada — não grava (Renato 2026-07-02)
        dbg(f"ENTRADA nao identificada (nao gravada) code={code} nome={nome}")
        return {"ok": False, "nao_identificado": True,
                "erro": "Não achei essa devolução. Tente outra foto do código ou digite o nº da venda (2000…)."}
    with _lock:
        if oid:
            ja = _db.execute("SELECT em, nome FROM entradas WHERE order_id=? ORDER BY id DESC LIMIT 1",
                             (str(oid),)).fetchone()
            if ja:
                return {"ok": True, "ja_registrada": True, "em": ja["em"], "por": ja["nome"],
                        "produto": it.get("produto"), "comprador": it.get("comprador"), "order_id": str(oid)}
        em = _now().strftime("%d/%m/%Y %H:%M")
        _db.execute("INSERT INTO entradas(order_id,codigo,produto,comprador,nome,em,ts) VALUES(?,?,?,?,?,?,?)",
                    (str(oid) if oid else None, code, it.get("produto") or "(não identificado)",
                     it.get("comprador"), nome, em, time.time()))
        _db.commit()
    dbg(f"ENTRADA code={code} oid={oid} nome={nome}")
    return {"ok": True, "order_id": str(oid) if oid else None, "identificado": bool(oid),
            "produto": it.get("produto") or "(não identificado)", "comprador": it.get("comprador"), "em": em}


def listar_entradas(dia=None):
    """Entradas de um dia (default hoje, dd/mm/aaaa)."""
    dia = dia or _now().strftime("%d/%m/%Y")
    with _lock:
        rows = _db.execute("SELECT id,order_id,codigo,produto,comprador,nome,em,ts FROM entradas WHERE em LIKE ? ORDER BY id DESC",
                           (dia + "%",)).fetchall()
    return {"dia": dia, "total": len(rows), "itens": [dict(r) for r in rows]}


def visao_entradas(max_itens=500):
    """Visão do gestor: entradas por dia + status da conferência + horas sem iniciar/em
    conferência/até concluir. Início = entrada.ts (chegada real bipada). Fim = conferencias.
    updated_at (epoch do último save_etapa — quando concluída, é o instante da conclusão)."""
    agora = time.time()
    with _lock:
        rows = _db.execute("SELECT order_id,codigo,produto,comprador,nome,em,ts FROM entradas ORDER BY id DESC LIMIT ?",
                           (max_itens,)).fetchall()
    dias = {}
    for r in rows:
        d = dict(r)
        oid = d.get("order_id")
        ts_entrada = d.get("ts") or agora
        if oid:
            reg = _get_reg(oid)
            if reg:
                d["conf_status"] = compute_status(reg)
                d["conf_prog"] = _progresso(reg)
                d["conf_atualizada"] = reg.get("atualizado_em")
                with _lock:
                    ur = _db.execute("SELECT updated_at FROM conferencias WHERE order_id=?", (str(oid),)).fetchone()
                ts_fim = (ur["updated_at"] if ur else None) or agora
                if d["conf_status"] == "concluida":
                    d["horas_para_concluir"] = round((ts_fim - ts_entrada) / 3600, 1)
                elif d["conf_status"] in ("em_andamento", "aguardando_ml"):
                    d["horas_em_conferencia"] = round((agora - ts_entrada) / 3600, 1)
            else:
                d["conf_status"] = "nao_iniciada"
        else:
            d["conf_status"] = "nao_identificada"
        if d["conf_status"] in ("nao_iniciada", "nao_identificada"):
            d["horas_sem_iniciar"] = round((agora - ts_entrada) / 3600, 1)
        dia = (d.get("em") or "")[:10]
        dias.setdefault(dia, []).append(d)
    return {"dias": [{"dia": k, "total": len(v), "itens": v} for k, v in dias.items()]}


# =====================================================================
# WATCHER — deteccao automatica de chegada via ML (fase de VALIDACAO)
# Roda em paralelo a bipagem manual da Natalia por alguns dias, so pra provar
# se o sinal do ML bate 100% com a chegada real, antes de decidir abandonar
# a bipagem manual. NAO substitui a bipagem sozinho — so avisa e confere.
#
# Sinal de "chegou" = players[seller].available_actions contem return_review_ok
# OU return_review_fail (validado ao vivo em 2026-07-06/07 contra o painel real
# do ML: e exatamente quando o botao muda de "Ir para detalhe" pra "Ja revisei").
# So watch a devolucoes cujo shipments[] tem uma perna com destination.name==
# "seller_address" (a que termina no endereco fisico da Napel) — as que so vao
# pro "warehouse" (CD ML) NUNCA chegam fisicamente aqui.
# =====================================================================
WATCH_POLL_SECONDS = int(os.environ.get("DEVOL_WATCH_POLL_SEG", "180"))
WATCH_QUIET_NOTIFY_MIN = float(os.environ.get("DEVOL_WATCH_QUIET_NOTIFY_MIN", "12"))
WATCH_QUIET_RECON_MIN = float(os.environ.get("DEVOL_WATCH_QUIET_RECON_MIN", "10"))
WATCH_RECON_TIMEOUT_H = float(os.environ.get("DEVOL_WATCH_RECON_TIMEOUT_H", "3"))


def _whatsapp(msg):
    fenv = _read_env_file()
    def pick(k):
        return os.environ.get(k) or fenv.get(k)
    url, token, inst, numero = (pick("EVOLUTION_API_URL"), pick("EVOLUTION_API_TOKEN"),
                                 pick("EVOLUTION_INSTANCE"), pick("RENATO_WHATSAPP"))
    if not all([url, token, inst, numero]):
        dbg("WHATSAPP creds ausentes, nao enviado")
        return False
    try:
        r = requests.post(url + "/message/sendText/" + inst,
                          headers={"apikey": token, "Content-Type": "application/json"},
                          json={"number": numero, "text": msg}, timeout=20)
        dbg(f"WHATSAPP status={r.status_code}")
        return r.status_code in (200, 201)
    except Exception as e:
        dbg(f"WHATSAPP FAIL {e}")
        return False


def _watch_scan():
    agora = time.time()
    with _lock:
        rows = {r["claim_id"]: dict(r) for r in _db.execute("SELECT * FROM ml_watch").fetchall()}

    # 1) registra qualquer claim novo com perna pro endereco da Napel (mesmo fora da lista visivel)
    claims = _claims_por_order()
    novos = 0
    for oid, cid in claims.items():
        cid = str(cid)
        if cid in rows:
            continue
        rt = g(f"/post-purchase/v2/claims/{cid}/returns") or {}
        sel = [s for s in (rt.get("shipments") or []) if (s.get("destination") or {}).get("name") == "seller_address"]
        if not sel:
            continue
        o = g(f"/orders/{oid}") or {}
        try:
            produto = o["order_items"][0]["item"]["title"]
        except Exception:
            produto = ""
        comprador = (o.get("buyer") or {}).get("nickname") or ""
        with _lock:
            _db.execute("INSERT OR IGNORE INTO ml_watch(claim_id,order_id,produto,comprador,primeiro_visto_ts) VALUES(?,?,?,?,?)",
                        (cid, oid, produto, comprador, agora))
            _db.commit()
        rows[cid] = {"claim_id": cid, "order_id": oid, "produto": produto, "comprador": comprador, "chegada_ts": None}
        novos += 1
    if novos:
        dbg(f"WATCH scan: {len(claims)} claims vistos, {novos} novos no watch")

    # 2) checa quem ainda nao tem chegada confirmada — sinal = return_review_ok/fail disponivel
    for r in [r for r in rows.values() if not r.get("chegada_ts")]:
        cid = r["claim_id"]
        c = g(f"/post-purchase/v1/claims/{cid}") or {}
        acts = []
        for p in (c.get("players") or []):
            if p.get("type") == "seller":
                acts = [a.get("action") for a in (p.get("available_actions") or [])]
        if "return_review_ok" not in acts and "return_review_fail" not in acts:
            continue
        chegada_ts, fonte = agora, "available_actions"
        try:
            rt = g(f"/post-purchase/v2/claims/{cid}/returns") or {}
            sel = [s for s in (rt.get("shipments") or []) if (s.get("destination") or {}).get("name") == "seller_address"]
            if sel:
                h = g(f"/shipments/{sel[0]['shipment_id']}/history") or {}
                dd = (h.get("date_history") or {}).get("date_delivered")
                if dd:
                    chegada_ts, fonte = datetime.fromisoformat(dd).timestamp(), "shipment_delivered"
        except Exception:
            pass
        with _lock:
            _db.execute("UPDATE ml_watch SET chegada_ts=?, chegada_fonte=? WHERE claim_id=?", (chegada_ts, fonte, cid))
            _db.commit()
        dbg(f"WATCH chegada detectada claim={cid} order={r['order_id']} fonte={fonte}")

    # NOTA (2026-07-09): os avisos de "chegou" + conferência com a bipagem NÃO usam mais esse
    # sinal (return_review_ok/fail) como gatilho — provado que o e-mail "Detalhe da entrega" do
    # ML é mais rápido (~14min vs 6-43h de atraso) e cobre também devoluções "por problema na
    # entrega" (que nunca ganham return_review_ok). Ver _watch_email_scan() abaixo. chegada_ts
    # aqui continua sendo calculado só como registro informativo (prazo de revisão do vendedor).


# =====================================================================
# WATCHER — e-mail "Detalhe da entrega" (gatilho PRINCIPAL de chegada)
# O ML manda esse e-mail pra ecommerce@napel.com.br minutos depois de cada visita de entrega
# de devolução na Napel — é o registro primário (motorista assinou), não um sinal derivado.
# Lido via IMAP direto (GMAIL_USER/GMAIL_APP_PASS) — não depende de sessão de navegador nem
# de mim. Cobre os 2 tipos ("revisão do vendedor" e "problema na entrega"), o watcher da API
# só cobria o primeiro.
# =====================================================================
import imaplib, email as _email_lib, re as _re

_db.execute("""CREATE TABLE IF NOT EXISTS email_lotes (
    msg_id TEXT PRIMARY KEY, assunto TEXT, chegada_ts REAL, total INT,
    revisao_n INT, revisao_prazo TEXT, problema_n INT, problema_prazo TEXT,
    notificado_ts REAL, finalizado_ts REAL)""")
_db.commit()


def _imap_conn():
    fenv = _read_env_file()
    user = os.environ.get("GMAIL_USER") or fenv.get("GMAIL_USER")
    pw = os.environ.get("GMAIL_APP_PASS") or fenv.get("GMAIL_APP_PASS")
    if not (user and pw):
        dbg("EMAIL_ROMANEIO creds ausentes (GMAIL_USER/GMAIL_APP_PASS)")
        return None
    conn = imaplib.IMAP4_SSL("imap.gmail.com")
    conn.login(user, pw)
    conn.select("INBOX")
    return conn


def _parse_email_romaneio(raw_bytes):
    msg = _email_lib.message_from_bytes(raw_bytes)
    html = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                html = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="ignore")
                break
    else:
        html = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", errors="ignore")
    if not html:
        return None
    m = _re.search(r"Entregamos\s+(\d+)\s+pacotes", html)
    total = int(m.group(1)) if m else (1 if _re.search(r"Entregamos\s+o\s+pacote", html) else None)
    if total is None:
        return None

    def bloco(rotulo):
        mm = _re.search(r"(\d+)\s*pacotes?\s*de\s*devoluções\s*" + rotulo +
                        r".*?antes\s*de\s*([^.<]+)\.", html, _re.S)
        return (int(mm.group(1)), mm.group(2).strip()) if mm else (0, None)

    rev_n, rev_prazo = bloco(r"com\s*revisão\s*do\s*vendedor")
    prob_n, prob_prazo = bloco(r"por\s*problemas\s*na\s*entrega")
    from email.utils import parsedate_to_datetime
    dt = parsedate_to_datetime(msg.get("Date"))
    return {"msg_id": msg.get("Message-ID") or msg.get("Date"), "assunto": msg.get("Subject") or "",
            "chegada_ts": dt.timestamp(), "total": total,
            "revisao_n": rev_n, "revisao_prazo": rev_prazo, "problema_n": prob_n, "problema_prazo": prob_prazo}


def _watch_email_scan():
    conn = _imap_conn()
    if not conn:
        return
    try:
        typ, data = conn.search(None, '(FROM "no-reply@mercadolivre.com.br" SUBJECT "Detalhe da entrega")')
        ids = data[0].split() if data and data[0] else []
        with _lock:
            ja_visto = {r["msg_id"] for r in _db.execute("SELECT msg_id FROM email_lotes").fetchall()}
        primeira_carga = len(ja_visto) == 0
        for mid in ids:
            typ, msgdata = conn.fetch(mid, "(RFC822)")
            if not msgdata or not msgdata[0]:
                continue
            info = _parse_email_romaneio(msgdata[0][1])
            if not info or info["msg_id"] in ja_visto:
                continue
            with _lock:
                if primeira_carga:
                    # semeia o historico sem notificar — só passa a avisar dali pra frente
                    _db.execute("""INSERT OR IGNORE INTO email_lotes
                        (msg_id,assunto,chegada_ts,total,revisao_n,revisao_prazo,problema_n,problema_prazo,notificado_ts,finalizado_ts)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (info["msg_id"], info["assunto"], info["chegada_ts"], info["total"],
                         info["revisao_n"], info["revisao_prazo"], info["problema_n"], info["problema_prazo"],
                         time.time(), time.time()))
                else:
                    _db.execute("""INSERT OR IGNORE INTO email_lotes
                        (msg_id,assunto,chegada_ts,total,revisao_n,revisao_prazo,problema_n,problema_prazo)
                        VALUES (?,?,?,?,?,?,?,?)""",
                        (info["msg_id"], info["assunto"], info["chegada_ts"], info["total"],
                         info["revisao_n"], info["revisao_prazo"], info["problema_n"], info["problema_prazo"]))
                _db.commit()
            if not primeira_carga:
                dbg(f"WATCH_EMAIL novo lote msg={info['msg_id'][:30]} total={info['total']}")
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    agora = time.time()
    # notifica lotes novos (o proprio e-mail ja e o "fim da visita" — sem precisar de quiet-period)
    with _lock:
        pend = _db.execute("SELECT * FROM email_lotes WHERE notificado_ts IS NULL").fetchall()
    for lote in pend:
        partes = []
        if lote["revisao_n"]:
            partes.append(f"- {lote['revisao_n']} de devolução com revisão do vendedor (informar como chegou antes de {lote['revisao_prazo']})")
        if lote["problema_n"]:
            partes.append(f"- {lote['problema_n']} de devolução por problema na entrega (prazo {lote['problema_prazo']})")
        msg = (f"📦 ML entregou {lote['total']} devolução(ões) na Napel agora:\n" + "\n".join(partes) +
               "\n\nPeça pra Natalia bipar a chegada (aba Entrada) pra conferirmos o match.")
        if _whatsapp(msg):
            with _lock:
                _db.execute("UPDATE email_lotes SET notificado_ts=? WHERE msg_id=?", (agora, lote["msg_id"]))
                _db.commit()
            dbg(f"WATCH_EMAIL notificado lote {lote['msg_id'][:30]} total={lote['total']}")

    # reconcilia lotes notificados: compara total do e-mail com quantas bipagens vieram depois
    with _lock:
        lotes = _db.execute("SELECT * FROM email_lotes WHERE notificado_ts IS NOT NULL AND finalizado_ts IS NULL").fetchall()
    for lote in lotes:
        nts = lote["notificado_ts"]
        with _lock:
            bips = _db.execute("SELECT * FROM entradas WHERE ts > ? AND ts < ?", (nts, nts + 4 * 3600)).fetchall()
        ultimo_bip = max([b["ts"] for b in bips], default=None)
        quiet_ok = bool(ultimo_bip) and (agora - ultimo_bip) / 60 >= WATCH_QUIET_RECON_MIN
        timeout_ok = (agora - nts) / 3600 >= WATCH_RECON_TIMEOUT_H
        if not bips and not timeout_ok:
            continue
        if bips and not quiet_ok and not timeout_ok:
            continue
        ok = len(bips)
        total = lote["total"]
        if ok == total:
            msg = f"✅ Bipagem conferida: {ok}/{total} bateram 100% com o e-mail do ML."
        else:
            msg = (f"⚠️ Bipagem conferida: {ok}/{total} bateram com o e-mail do ML.\n"
                   f"Confira a aba Entrada — pode ter pacote não bipado ou bipagem a mais.")
        if _whatsapp(msg):
            with _lock:
                _db.execute("UPDATE email_lotes SET finalizado_ts=? WHERE msg_id=?", (agora, lote["msg_id"]))
                _db.commit()
            dbg(f"WATCH_EMAIL lote {lote['msg_id'][:30]} finalizado: {ok}/{total}")


WATCH_EMAIL_POLL_SECONDS = int(os.environ.get("DEVOL_WATCH_EMAIL_POLL_SEG", "60"))


def _watch_email_loop():
    """Loop RÁPIDO e independente, só pro e-mail — o horário da entrega varia todo dia,
    então checa a cada 1min pra saber assim que chegar. Não espera a varredura da API
    (lenta, minutos) porque ela não é mais time-critical (virou só registro de prazo)."""
    while True:
        try:
            _watch_email_scan()
        except Exception as e:
            dbg(f"WATCH_EMAIL loop erro: {e}")
        time.sleep(WATCH_EMAIL_POLL_SECONDS)


def _watch_api_loop():
    """Loop lento — só mantém o registro informativo do prazo de revisão do vendedor."""
    while True:
        try:
            _watch_scan()
        except Exception as e:
            dbg(f"WATCH loop erro: {e}")
        time.sleep(WATCH_POLL_SECONDS)


def start_watch():
    threading.Thread(target=_watch_email_loop, daemon=True).start()
    threading.Thread(target=_watch_api_loop, daemon=True).start()


def watch_status():
    """Painel de acompanhamento do match ML x bipagem (fase de validacao)."""
    with _lock:
        rows = _db.execute("SELECT * FROM ml_watch ORDER BY primeiro_visto_ts DESC LIMIT 200").fetchall()
    itens = [dict(r) for r in rows]
    finalizados = [i for i in itens if i.get("finalizado_ts")]
    ok = sum(1 for i in finalizados if i.get("match") == "ok")
    return {"total": len(itens), "finalizados": len(finalizados), "match_ok": ok,
            "match_pct": round(100 * ok / len(finalizados), 1) if finalizados else None, "itens": itens}
