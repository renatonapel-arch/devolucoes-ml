import sys; sys.path.insert(0,".")
import ml; ml._load_tokens()
from datetime import date
seller=ml._tok["seller"]
print("=== varredura profunda: acha JK Travessa + lista todos os returns por status ===", flush=True)
from collections import Counter
status_count=Counter()
travessa=[]
delivered=[]
total_claims=0
for status,maxp in (("opened",40),("closed",40)):
    off=0
    for _ in range(maxp):
        r=ml.g(f"/post-purchase/v1/claims/search?status={status}&limit=50&offset={off}") or {}
        data=r.get("data") or []; tot=(r.get("paging") or {}).get("total",0)
        for c in data:
            oid=str(c.get("resource_id") or ""); cid=c.get("id")
            if not oid.startswith("2000") or not cid: continue
            total_claims+=1
            rt=ml.g(f"/post-purchase/v2/claims/{cid}/returns") or {}
            if not rt.get("shipments"): continue
            st=rt.get("status"); status_count[st]+=1
            shids=[str(s.get("shipment_id")) for s in rt["shipments"]]
            # produto?
            o=ml.g(f"/orders/{oid}") or {}
            prod=""
            try: prod=o["order_items"][0]["item"]["title"]
            except: pass
            if "travessa" in prod.lower() or "47307911010" in shids:
                last=(rt.get("last_updated") or rt.get("date_closed") or "")[:10]
                travessa.append((oid,cid,st,shids,prod[:40],last))
            if st=="delivered":
                last=(rt.get("last_updated") or rt.get("date_closed") or "")[:10]
                try: d=(date.today()-date.fromisoformat(last)).days if last else 999
                except: d=999
                if d<=30: delivered.append((oid,prod[:34],last,d,shids))
        off+=50
        if not data or off>=tot: break
print("claims com return:", total_claims, flush=True)
print("status dos returns:", dict(status_count), flush=True)
print("\n=== JK TRAVESSA encontrada? ===", flush=True)
for t in travessa: print("  ", t, flush=True)
if not travessa: print("  NAO achei nenhum 'travessa' nem shipment 47307911010", flush=True)
print("\n=== DELIVERED recentes (<=30d) — o que deveria estar como CHEGOU ===", flush=True)
for d in delivered: print("  ", d, flush=True)
print("total delivered<=30d:", len(delivered), flush=True)
