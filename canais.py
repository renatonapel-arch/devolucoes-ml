"""Outros canais de venda (Shopee e loja própria) — conferência de devolução.

Regras (aprovadas pelo Renato em 17/11/2026):
- O Mercado Livre é procurado SEMPRE primeiro; este módulo só entra quando o ML não achou.
- Desligado por padrão: só liga com env DEVOL_CANAIS (ex.: "shopee" ou "shopee,loja").
- Nunca escreve em lugar nenhum fora do app (Tiny e Catálogo: só leitura).
- Venda de outro canal é gravada com prefixo próprio (SHP-… / LOJA-…) — nunca colide
  com os números do ML (2000…).
- "O que deveria voltar" vem do pedido no Tiny; composição de kit SÓ do Catálogo Mestre.
- Tiny: o limite de acessos é da CONTA (vale pra todas as integrações) — 1 chamada por vez,
  timeout curto e cache de 10 min.
"""
import os, re, json, time, threading
import requests

CANAIS = {
    "shopee": {"prefixo": "SHP-", "nome": "Shopee", "ecommerce_ids": {"11194", "14065"}},
    "loja":   {"prefixo": "LOJA-", "nome": "Loja Napel", "ecommerce_ids": {"14209"}},
}
_RE_SHOPEE = re.compile(r"^(?:SHP-)?(\d{6}(?=[0-9A-Z]*[A-Z])[0-9A-Z]{8})$")   # ex.: 2611111TYE9B1T
_RE_LOJA = re.compile(r"^LOJA-?(\d{3,7})$")                                   # ex.: LOJA-9007
TIMEOUT = 5          # segundos por chamada — se demorar, desiste (não segura a tela)
CACHE_SEG = 600
_cache, _cache_lock = {}, threading.Lock()
_tiny_lock = threading.Lock()
_DE_PARA = {}
try:
    _DE_PARA = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "de_para_sku.json"), encoding="utf-8"))
except Exception:
    pass


def ativos():
    return {c.strip().lower() for c in (os.environ.get("DEVOL_CANAIS") or "").split(",") if c.strip()}


def eh_outro(oid):
    """True se o order_id é de outro canal (prefixo SHP-/LOJA-). Usado pra isolar o ML."""
    s = str(oid or "")
    return any(s.startswith(c["prefixo"]) for c in CANAIS.values())


def detectar(code):
    """-> (canal, nº do pedido no canal) ou (None, None). Só reconhece formatos que o ML não usa."""
    up = (code or "").strip().upper().replace(" ", "")
    m = _RE_SHOPEE.match(up)
    if m:
        return "shopee", m.group(1)
    m = _RE_LOJA.match(up)
    if m:
        return "loja", m.group(1)
    return None, None


def _tiny(ep, **p):
    tok = os.environ.get("TINY_API_TOKEN")
    if not tok:
        raise RuntimeError("TINY_API_TOKEN ausente")
    p.update(token=tok, formato="json")
    with _tiny_lock:
        r = requests.post("https://api.tiny.com.br/api2/" + ep, data=p, timeout=TIMEOUT)
    ret = (r.json() or {}).get("retorno") or {}
    if ret.get("status") != "OK":
        raise RuntimeError(f"Tiny {ep}: {json.dumps(ret.get('erros') or ret.get('codigo_erro') or '', ensure_ascii=False)[:160]}")
    return ret


_clavis_tok = {"t": None}


def _kit(id_produto):
    """Composição do kit pelo Catálogo Mestre. None = não é kit. {'erro':...} = não deu pra conferir."""
    base = (os.environ.get("CLAVIS_API_URL") or "https://clavis.napel.com.br/api/v1").rstrip("/")
    email, senha = os.environ.get("DEVOL_CLAVIS_EMAIL"), os.environ.get("DEVOL_CLAVIS_SENHA")
    if not (email and senha):
        return {"erro": "Catálogo sem credencial"}
    try:
        for tentativa in range(2):
            if not _clavis_tok["t"]:
                j = requests.post(base + "/auth/login", json={"email": email, "password": senha}, timeout=TIMEOUT).json()
                _clavis_tok["t"] = j.get("access_token")
            r = requests.get(f"{base}/catalogo-kits/kits/{id_produto}",
                             headers={"Authorization": "Bearer " + (_clavis_tok["t"] or "")}, timeout=TIMEOUT)
            if r.status_code == 401 and tentativa == 0:
                _clavis_tok["t"] = None
                continue
            if r.status_code == 404:
                return None
            if r.status_code == 200:
                return r.json()
            return {"erro": f"Catálogo respondeu {r.status_code}"}
    except Exception as e:
        return {"erro": f"Catálogo indisponível ({type(e).__name__})"}
    return {"erro": "Catálogo sem resposta"}


def _montar(canal, numero):
    ret = _tiny("pedidos.pesquisa.php", numeroEcommerce=numero)
    peds = [p["pedido"] for p in (ret.get("pedidos") or [])]
    if not peds:
        return None
    # a mesma venda pode ter um pedido Cancelado + o válido (refeito) — prefere o não cancelado
    peds.sort(key=lambda p: p.get("situacao") == "Cancelado")
    p = _tiny("pedido.obter.php", id=peds[0]["id"])["pedido"]
    if str((p.get("ecommerce") or {}).get("id")) not in CANAIS[canal]["ecommerce_ids"]:
        return None      # número existe no Tiny mas é de outro canal — não chuta
    nf = None
    if p.get("id_nota_fiscal") not in (None, "", "0"):
        try:
            nf = (_tiny("nota.fiscal.obter.php", id=p["id_nota_fiscal"]).get("nota_fiscal") or {}).get("numero")
        except Exception:
            nf = None
    itens, kit_pendente = [], False
    for x in p.get("itens") or []:
        it = x.get("item") or {}
        sku = str(it.get("codigo") or "")
        kit = None   # kit no Catálogo pode estar pelo código do produto ou pelo id do Tiny
        for chave in dict.fromkeys(x for x in (sku, str(it.get("id_produto") or "")) if x):
            kit = _kit(chave)
            if kit is not None:
                break
        if isinstance(kit, dict) and kit.get("erro"):
            kit_pendente = True
        itens.append({"sku": sku, "sku_sige": _DE_PARA.get(sku, sku), "descricao": it.get("descricao"),
                      "qtd": float(it.get("quantidade") or 0), "valor_unit": float(it.get("valor_unitario") or 0),
                      "kit": kit})
    prod = itens[0]["descricao"] if itens else "(sem itens)"
    if len(itens) > 1:
        prod += f" + {len(itens) - 1} item(ns)"
    qtd = sum(i["qtd"] for i in itens) or 1
    oid = CANAIS[canal]["prefixo"] + numero
    return {"order_id": oid, "pack_id": oid, "canal": canal, "canal_nome": CANAIS[canal]["nome"],
            "pedido_canal": numero, "pedido_tiny": p.get("numero"), "nf": nf,
            "situacao_tiny": p.get("situacao"), "produto": prod,
            "comprador": (p.get("cliente") or {}).get("nome") or "—",
            "valor": float(p.get("total_pedido") or 0), "qtd": str(int(qtd)) if qtd == int(qtd) else str(qtd),
            "itens": itens, "kit_pendente": kit_pendente,
            "fase": "chegou", "tipo": canal, "destino": None, "claim_id": None, "claim_status": None,
            "status_money": None, "dias_aberta": None, "tracking": "-", "shipment_id": None,
            "shipment_ids": [], "item_id": None, "vol_total": 1, "local": "—"}


def buscar(code):
    """Chamado só DEPOIS que o ML não achou. Retorna o mesmo formato de ml.buscar, ou None."""
    canal, numero = detectar(code)
    if not canal or canal not in ativos():
        return None
    chave = canal + ":" + numero
    with _cache_lock:
        hit = _cache.get(chave)
        if hit and time.time() - hit[0] < CACHE_SEG:
            return {"found": True, "in_list": False, "by": "order_id", "item": hit[1]}
    try:
        it = _montar(canal, numero)
    except Exception as e:
        return {"found": False, "in_list": False, "code": code, "canal": canal,
                "hint": f"Não consegui consultar a {CANAIS[canal]['nome']} agora ({str(e)[:80]}). Tente de novo em 1 minuto."}
    if not it:
        return {"found": False, "in_list": False, "code": code, "canal": canal,
                "hint": f"Não achei o pedido {numero} da {CANAIS[canal]['nome']}. Confira o número."}
    with _cache_lock:
        _cache[chave] = (time.time(), it)
    return {"found": True, "in_list": False, "by": "order_id", "item": it}
