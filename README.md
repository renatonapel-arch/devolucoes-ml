# Devoluções ML — Recebimento · Fase 1 (SÓ LEITURA)

App de teste ponta a ponta do **caminho de recebimento**. Lista ao vivo o que está
voltando pro galpão e acha a devolução por **qualquer ID da etiqueta**. **Nunca escreve no ML.**

## Rodar

```powershell
cd C:\Users\Renato\teste\devolucoes-ml
.\run.ps1
```

Abra http://127.0.0.1:8077 no Chrome.

## O que dá pra testar

1. **Lista ao vivo** — vem da API do ML (não é dado fixo). Botão 🔄 atualiza.
2. **Bipar / digitar** — toque na barra de scan e:
   - digite/escaneie o **nº da venda (2000…)**, **cód. de envio (47…)**, **claim** ou **rastreio**;
   - bate contra a lista → abre a ficha.
   - leitor USB de código de barras funciona (ele "digita" + Enter).
3. **Buscar no ML** — se o código não está na lista, o botão azul consulta a API do ML ao vivo.
4. **Ficha completa** + automações 1 (cronômetro), 4 (guardião) e 7 (reincidência), tudo com dado real.

## Endpoints (leitura)

| Rota | O que faz |
|---|---|
| `GET /api/health` | valida o token do ML (seller_id) |
| `GET /api/aguardando?refresh=1` | lista ao vivo (cache 30 min; `refresh=1` força) |
| `GET /api/buscar?code=XXX&ml_fallback=1` | acha por qualquer ID; `ml_fallback=1` consulta o ML |

## Notas

- Token `LUCRATIVIDADE_ML_*` lido de `~/.claude/.env`, com **auto-refresh** (persiste no .env, igual ao `refresh.py`).
- Índices ao vivo lidos de `C:\Users\Renato\Downloads\demo-vendas-ml` (mantidos pelo `refresh.py`); mude com a env `DEVOL_INDEX_DIR`.
- `cache_aguardando.json` é cache local — não versionar.
- Câmera (bipar pela foto) e envio de avaria ao ML são **Fase 2** (exigem HTTPS e escrita).
