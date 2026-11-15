#!/usr/bin/env python3
"""scan_lanzamientos.py - SOLO LECTURA. Lanzamientos recientes de Solana en origen + ventana "en carrera".
Versión 11/11/2026 (auditoría): pump.fun API en 403 -> cobertura pump.fun vía GeckoTerminal + PumpPortal 90 s
+ Dexscreener boosts/profiles/CTO. Filtros por tramos en joe_filtros.py (ver FILTROS_2026-11-11.md).

Fuentes (todas públicas, sin clave, solo GET / websocket de lectura):
  pump.fun     frontend-api-v3 (se prueba 1 vez; si da 403 se salta)
  gecko        new_pools p1-5, dexes/pump-fun/pools p1-5 (curvas), dexes/pumpswap/pools p1-3 (migrados),
               dexes/meteora-damm-v2/pools p1, trending_pools 5m (+1h/6h y pumpswap por volumen en --carrera)
  pumpportal   wss subscribeNewToken + subscribeMigration durante --ws s (por defecto 90)
  dexscreener  token-boosts latest/top, token-profiles latest, community-takeovers latest; tokens/v1 (enriquecer)
  raydium      LaunchLab (LetsBonk, ...)      bags   token-launch feed
  rugcheck     solo para los que pasan los números (LP bloqueado, bundle, creador)

Uso:
  python3 scan_lanzamientos.py --min 60                 # pasada completa (con PumpPortal 90 s) + evaluación
  python3 scan_lanzamientos.py --min 60 --carrera       # + ventana en carrera 1–24 h, 100K–3M, vol 1h creciente
  python3 scan_lanzamientos.py --ligero                 # solo Dexscreener/Gecko (sin WS, sin X), incluye carrera
  opciones: --ws 90 --top 40 --min-mcap 5000 --enrich 150 --sin-evaluar --relectura 90 --max-rugcheck 12 --out f.json
Nunca firma, compra ni usa claves.
"""
import argparse, json, sys, time, threading, os
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import joe_filtros as J
from joe_filtros import get, fnum, iso_ts, GT

PF_H = dict(J.UA, Origin="https://pump.fun", Referer="https://pump.fun/")
TOK = {}          # mint -> dict
LIGERO = False
CARRERA = set()   # mints candidatos a la ventana en carrera
LISTADOS = {}     # mint -> etiquetas Dexscreener (boost, perfil, cto)

def add(mint, **kw):
    if not mint: return
    t = TOK.setdefault(mint, {"ca": mint, "fuentes": []})
    src = kw.pop("fuente", None)
    if src and src not in t["fuentes"]: t["fuentes"].append(src)
    for k, v in kw.items():
        if v not in (None, "", [], {}) and t.get(k) in (None, "", [], {}):
            t[k] = v

# ---------------- fuentes ----------------
def src_pumpfun(window_s, now):
    base = "https://frontend-api-v3.pump.fun/coins?limit=50&order=DESC&includeNsfw=true"
    got = 0
    for sort, pages in (("created_timestamp", 25), ("last_trade_timestamp", 3)):
        for p in range(pages):
            j = get(f"{base}&offset={p*50}&sort={sort}", "pump.fun", tries=1, headers=PF_H)
            if not isinstance(j, list) or not j: return got   # 403: no insistimos
            stop = False
            for c in j:
                ct = (c.get("created_timestamp") or 0) / 1000
                if now - ct > window_s:
                    if sort == "created_timestamp": stop = True
                    continue
                got += 1
                add(c["mint"], fuente="pump.fun", launchpad="pump.fun" if not c.get("complete") else "pump.fun (migrado)",
                    symbol=c.get("symbol"), name=c.get("name"), creado=ct, creador=c.get("creator"),
                    mcap_usd=c.get("usd_market_cap"), web=c.get("website"), x=c.get("twitter"), telegram=c.get("telegram"),
                    pool=c.get("pump_swap_pool") or c.get("bonding_curve"))
            if stop: break
    return got

def src_raydium(window_s, now):
    got = 0
    for url, lp in (("https://launch-mint-v1.raydium.io/get/list?sort=new&size=100&mintType=default&includeNsfw=true", None),
                    ("https://launch-mint-v1.raydium.io/get/list?platformId=FfYek5vEz23cMkWsdJwG2oa6EphsvXSHrGpdALN4g6W1&sort=new&size=50&mintType=default&includeNsfw=true", "LetsBonk")):
        j = get(url, "raydium-launchlab")
        for x in (((j or {}).get("data") or {}).get("rows")) or []:
            ct = (x.get("createAt") or 0) / 1000
            if now - ct > window_s: continue
            got += 1
            plat = lp or ((x.get("platformInfo") or {}).get("name")) or "Raydium LaunchLab"
            add(x["mint"], fuente="raydium-launchlab", launchpad=f"LaunchLab/{plat}", symbol=x.get("symbol"),
                name=x.get("name"), creado=ct, creador=x.get("creator"), mcap_usd=x.get("marketCap"),
                web=x.get("website"), x=x.get("twitter"), telegram=x.get("telegram"), pool=x.get("poolId"))
    return got

def src_bags():
    j = get("https://public-api-v2.bags.fm/api/v1/token-launch/feed", "bags", tries=2)
    got = 0
    for x in (j or {}).get("response") or []:
        if x.get("status") not in (None, "PRE_GRAD", "PRE_LAUNCH"): continue
        got += 1
        add(x.get("tokenMint"), fuente="bags", launchpad="Bags", symbol=x.get("symbol"), name=x.get("name"),
            web=x.get("website"), x=x.get("twitter"), pool=x.get("dbcPoolKey"))
    return got

def _add_gt(r, fuente):
    add(r["ca"], fuente=fuente, launchpad=r["dex"], symbol=r["symbol"], name=r["name"], creado=r["creado"],
        mcap_usd=r["mcap"], pool=r["pool"], gt_liq=r["liq"], gt_vol=r["vol"], gt_tx=r["tx"], gt_pc=r["pc"])
    t = TOK[r["ca"]]
    t.setdefault("gt_dexes", [])
    if r["dex"] not in t["gt_dexes"]: t["gt_dexes"].append(r["dex"])

def src_gecko(window_s, now, carrera):
    """Devuelve contadores por lista."""
    c = {}
    k = 1 if LIGERO else 2   # ligero: menos páginas (Gecko limita fuerte; ~20 llamadas/min sin 429)
    listas = [("new_pools", 2 * k, None), ("dexes/pump-fun/pools?sort=h24_tx_count_desc", 2 * k, "pump-fun"),
              ("dexes/pumpswap/pools?sort=h24_tx_count_desc", k, "pumpswap"),
              ("dexes/meteora-damm-v2/pools?sort=h24_tx_count_desc", 1, "meteora-damm-v2"),
              ("trending_pools?duration=5m", 1, None)]
    if carrera:
        listas += [("trending_pools?duration=1h", k, None), ("trending_pools?duration=6h", 1, None),
                   ("dexes/pumpswap/pools?sort=h24_volume_usd_desc", k, "pumpswap")]
    for path, pag, dexf in listas:
        rows = J.gt_lista(path, pag, "geckoterminal")
        key = path.split("?")[0].replace("dexes/", "").replace("/pools", "") + ("" if "?" not in path else "?" + path.split("?")[1].split("=")[-1])
        n_win = 0
        for r in rows:
            if r.get("sol_usd"): J.SOL_USD = r["sol_usd"]
            edad = now - r["creado"] if r["creado"] else None
            en_ventana = edad is not None and edad <= window_s
            es_carrera = carrera and edad is not None and 3600 <= edad <= 24 * 3600 and \
                J.UMBRAL["momentum_min"] <= (r["mcap"] or 0) <= J.UMBRAL["carrera_max"]
            if r["dex"] == "pumpswap" and carrera and (r["mcap"] or 0) >= J.UMBRAL["momentum_min"] \
                    and (r["mcap"] or 0) <= J.UMBRAL["carrera_max"]:
                es_carrera = True   # pool migrado; la edad real se mide en Dexscreener
            if en_ventana or es_carrera or path.startswith("trending_pools?duration=5m"):
                _add_gt(r, "geckoterminal")
                n_win += 1
                if es_carrera: CARRERA.add(r["ca"])
            if r["dex"] in ("pump-fun",):
                PF_CURVAS.add(r["ca"])
                if en_ventana: PF_CURVAS_VENTANA.add(r["ca"])
            if r["dex"] == "pumpswap": PSWAP.add(r["ca"])
        c[key] = {"pools": len(rows), "en_ventana_o_carrera": n_win}
    return c

PF_CURVAS, PF_CURVAS_VENTANA, PSWAP = set(), set(), set()
CARRERA_DESC = {}

def src_listados(window_s, now, carrera):
    global LISTADOS
    LISTADOS = J.ds_listados()
    for m, tags in LISTADOS.items():
        add(m, fuente="dexscreener-" + "/".join(tags))
        if carrera: CARRERA.add(m)
    return len(LISTADOS)

# ---------------- enriquecimiento ----------------
def enrich_dexscreener(mints):
    res = J.ds_tokens(mints)
    for m, pares in res.items():
        if m not in TOK: continue
        mt = J.metricas_ds(pares)
        TOK[m]["ds"] = mt
        t = TOK[m]
        if not t.get("creado") and mt.get("edad_h") is not None: t["creado"] = time.time() - mt["edad_h"] * 3600
        for k in ("symbol", "name", "x", "web"):
            if not t.get(k) and mt.get(k): t[k] = mt[k]
    return len(res)

def metricas_gecko(t):
    """Métricas mínimas con datos de Gecko si Dexscreener no tiene el par."""
    tx = t.get("gt_tx") or {}; v = t.get("gt_vol") or {}
    m5 = tx.get("m5") or {}; h1 = tx.get("h1") or {}
    b5, s5, b1, s1 = m5.get("buys", 0), m5.get("sells", 0), h1.get("buys", 0), h1.get("sells", 0)
    v5, v1 = v.get("m5", 0), v.get("h1", 0)
    dex = t.get("launchpad") or ""
    if dex == "pump-fun": dex = "pumpfun"
    return {"symbol": t.get("symbol"), "name": t.get("name"), "dex": dex, "labels": [], "pair": t.get("pool"),
            "link": f"https://www.geckoterminal.com/solana/pools/{t.get('pool')}" if t.get("pool") else None,
            "mcap": t.get("mcap_usd"), "liq": t.get("gt_liq"), "liq_total": t.get("gt_liq") or 0, "n_pares": 1,
            "b5": b5, "s5": s5, "b1": b1, "s1": s1, "r5": round(b5 / s5, 2) if s5 else (float(b5) if b5 else None),
            "r1": round(b1 / s1, 2) if s1 else (float(b1) if b1 else None), "v5": v5, "v1": v1, "v6": v.get("h6", 0),
            "v24": v.get("h24", 0), "ticket5": round(v5 / (b5 + s5), 1) if (b5 + s5) else None,
            "ticket1": round(v1 / (b1 + s1), 1) if (b1 + s1) else None, "pc5": fnum((t.get("gt_pc") or {}).get("m5")),
            "pc1": fnum((t.get("gt_pc") or {}).get("h1")), "pc6": fnum((t.get("gt_pc") or {}).get("h6")),
            "edad_h": round((time.time() - t["creado"]) / 3600, 2) if t.get("creado") else None,
            "x": t.get("x"), "web": t.get("web"), "perfil_pagado": None, "fuente_metricas": "geckoterminal"}

# ---------------- evaluación ----------------
def evaluar(cands, a):
    """Paso 1 números; paso 2 (los que pasan) rugcheck + enjambre + máximo; paso 3 relectura curva/DAMM."""
    pre = []
    for t in cands:
        m = t.get("ds") or metricas_gecko(t)
        if not m or not m.get("mcap"): continue
        en_carrera = t["ca"] in CARRERA
        pasa, tr, no, notas = J.evaluar_numeros(m, carrera=en_carrera)
        duros_previos = J.descartes_duros(m)
        blandos = [x for x in no if not x.startswith(("mcap al", "enjambre"))]
        t["_m"] = m; t["_tramo"] = tr
        if tr == "fuera" or duros_previos: continue
        # "cerca": como mucho 1 motivo blando y ratios razonables
        if not blandos or (len(blandos) == 1 and (m.get("r5") or 0) >= 1.1 and (m.get("mcap") or 0) >= 25000):
            pre.append((t, m, en_carrera, blandos))
    pre.sort(key=lambda x: (len(x[3]), -(x[1].get("v5") or 0)))
    evaluados = []
    for t, m, en_carrera, blandos in pre[: a.max_rugcheck]:
        rc = J.rugcheck(t["ca"])
        sw = J.enjambre(t["ca"], m.get("symbol"), m.get("mcap"), m.get("liq"))
        frac = None
        if J.tramo(m, en_carrera) in ("momentum", "carrera") and m.get("pair"):
            mx, _ = J.gt_max_precio(m["pair"], 24)
            if mx and m.get("precio"): frac = round(min(1.0, m["precio"] / mx), 2)
        pasa, tr, no, notas = J.evaluar_numeros(m, carrera=en_carrera, frac_max=frac, swarm=sw)
        duros = J.descartes_duros(m, rc)
        if rc is None and not J.es_curva(m.get("dex")):
            notas.append("Rugcheck sin informe: LP/holders SIN verificar (comprobar a mano antes de avisar)")
        e = {"ca": t["ca"], "symbol": m.get("symbol") or t.get("symbol"), "name": m.get("name") or t.get("name"),
             "tramo": tr, "etiqueta": J.etiqueta(tr), "pasa": pasa and not duros,
             "descartes_duros": duros or None, "motivos_no": no or None, "notas": notas or None,
             "metricas": {k: m.get(k) for k in ("dex", "mcap", "liq", "liq_total", "r5", "r1", "v5", "v1", "v6",
                                                "ticket5", "ticket1", "pc5", "pc1", "edad_h", "b5", "s5", "b1", "s1")},
             "frac_max_24h": frac, "enjambre": sw,
             "rugcheck": {k: rc.get(k) for k in ("score", "holders_total", "insiders_grafo", "creador", "creador_tokens_previos",
                                                 "mint_auth", "freeze_auth", "riesgos")} | {
                 "lp": [(x["tipo"], x["lp_bloqueado_pct"], round(x["liq_usd"] or 0)) for x in rc["markets"][:4]],
                 "top_holders_pct": [round(h["pct"], 2) for h in rc["holders_top"][:8]]} if rc else None,
             "x": t.get("x") or m.get("x"), "web": t.get("web") or m.get("web"), "listado_dexscreener": LISTADOS.get(t["ca"]),
             "link": m.get("link") or (f"https://dexscreener.com/solana/{m['pair']}" if m.get("pair") else None),
             "launchpad": t.get("launchpad"), "fuentes": t["fuentes"]}
        e["color_sugerido"] = J.color_sugerido(tr, notas) if e["pasa"] else None
        e["necesita_relectura"] = e["pasa"] and (tr == "curva" or m.get("dex") in J.DAMM_V2)
        evaluados.append(e)
    # relectura conjunta (curva 60–120 s, DAMM v2 120 s)
    rel = [e for e in evaluados if e["necesita_relectura"]]
    if rel and a.relectura > 0:
        espera = max(a.relectura, 120 if any(e["metricas"]["dex"] in J.DAMM_V2 for e in rel) else 0)
        time.sleep(espera)
        nuevos = J.ds_tokens([e["ca"] for e in rel])
        for e in rel:
            m1 = J.metricas_ds(nuevos.get(e["ca"]) or []) if nuevos.get(e["ca"]) else None
            m0 = e["metricas"]
            if not m1:
                e["pasa"] = False; e["relectura"] = "sin datos en la relectura"; continue
            umb = J.UMBRAL["r_bajo"] if J.tramo(m1) in ("curva", "temprano") else J.UMBRAL["r5_alto"]
            ok = (m1.get("r5") or 0) >= umb and (m1.get("mcap") or 0) >= 0.97 * (m0.get("mcap") or 0) \
                and not J.descartes_duros(m1)
            e["relectura"] = (f"+{espera:.0f}s: mcap {m0['mcap'] or 0:,.0f}→{m1.get('mcap') or 0:,.0f}, "
                              f"r5 {m0['r5']}→{m1.get('r5')} → {'CONFIRMA' if ok else 'NO confirma'}")
            e["pasa"] = ok
            if not ok: e["color_sugerido"] = None
    elif rel:
        for e in rel: e["relectura"] = "PENDIENTE (lanzar --relectura 90 o releer a mano antes de avisar)"
    evaluados.sort(key=lambda e: (not e["pasa"], e["tramo"] != "momentum", -(e["metricas"].get("v5") or 0)))
    return evaluados

# ---------------- main ----------------
def main(argv=None, imprimir=True):
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=float, default=60, help="ventana en minutos (edad máxima)")
    ap.add_argument("--ws", type=float, default=90, help="segundos escuchando PumpPortal (0 = off; recomendado 60–120)")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--min-mcap", type=float, default=5000)
    ap.add_argument("--enrich", type=int, default=150, help="máx. tokens a enriquecer con Dexscreener")
    ap.add_argument("--carrera", action="store_true", help="añade ventana en carrera 1–24 h, 100K–3M, vol 1h creciente")
    ap.add_argument("--ligero", action="store_true", help="solo Dexscreener/Gecko (sin WS, sin LaunchLab/Bags/pump.fun), incluye carrera")
    ap.add_argument("--sin-evaluar", action="store_true", help="no aplica filtros (solo datos crudos)")
    ap.add_argument("--relectura", type=float, default=None, help="s para la 2ª lectura de curva/DAMM (0=no; ligero 90)")
    ap.add_argument("--max-rugcheck", type=int, default=12)
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    global LIGERO
    if a.ligero:
        a.carrera = True; a.ws = 0; LIGERO = True
    if a.relectura is None: a.relectura = 90 if a.ligero else 0
    t0 = time.time(); now = t0; W = a.min * 60
    counts = {}
    pp = None
    if a.ws > 0:
        pp = J.PumpPortal(trades=False).start(a.ws)
    if not a.ligero:
        counts["pump.fun"] = src_pumpfun(W, now)
        counts["raydium-launchlab"] = src_raydium(W, now)
        counts["bags"] = src_bags()
    counts["geckoterminal"] = src_gecko(W, now, a.carrera)
    counts["dexscreener_listados"] = src_listados(W, now, a.carrera)
    if pp:
        pp.join()
        for m, i in pp.nuevos.items():
            add(m, fuente="pumpportal", launchpad=i["launchpad"], symbol=i["symbol"], name=i["name"], creado=i["ts"],
                creador=i["creador"], compra_inicial_dev_sol=i["dev_sol"])
            if i["launchpad"] == "pump.fun": PF_CURVAS.add(m); PF_CURVAS_VENTANA.add(m)
        for m, i in pp.migrados.items():
            add(m, fuente="pumpportal-migracion", migrado_ts=i["ts"])
        counts["pumpportal"] = {"nuevos": len(pp.nuevos), "migraciones": len(pp.migrados), "mensajes": pp.n_msgs,
                                "segundos": a.ws, "error": pp.err}
        J.marca("pumpportal", "ok" if not pp.err else f"error {pp.err}")
    now = time.time()
    cands = [t for t in TOK.values() if (t.get("mcap_usd") or 0) >= a.min_mcap or "bags" in t["fuentes"]
             or any(f.startswith(("pumpportal", "dexscreener-")) for f in t["fuentes"]) or t["ca"] in CARRERA]
    cands.sort(key=lambda t: (t["ca"] in CARRERA, (t.get("gt_vol") or {}).get("h1", 0), t.get("mcap_usd") or 0), reverse=True)
    cands = cands[: a.enrich]
    n_ds = enrich_dexscreener([t["ca"] for t in cands])
    # filtrar por ventana (o carrera)
    final = []
    for t in cands:
        en_v = not t.get("creado") or now - t["creado"] <= W or (t.get("migrado_ts") and now - t["migrado_ts"] <= W)
        en_c = False
        if t["ca"] in CARRERA:
            m = t.get("ds") or metricas_gecko(t)
            eh = m.get("edad_h"); mc = m.get("mcap") or 0
            en_c = eh is not None and 1 <= eh <= 24 and J.UMBRAL["momentum_min"] <= mc <= J.UMBRAL["carrera_max"] and J.vol_creciente(m)
            if not en_c:
                CARRERA.discard(t["ca"])
                why = ("sin edad" if eh is None else "edad>24h" if eh > 24 else "edad<1h" if eh < 1 else
                       "mcap fuera 100K–3M" if not (J.UMBRAL["momentum_min"] <= mc <= J.UMBRAL["carrera_max"]) else "vol 1h no creciente")
                CARRERA_DESC[why] = CARRERA_DESC.get(why, 0) + 1
        if en_v or en_c:
            t["ventana"] = "carrera" if en_c and not (t.get("creado") and now - t["creado"] <= W) else "lanzamiento"
            final.append(t)
    out = []
    for t in final:
        m = t.get("ds") or metricas_gecko(t)
        r = {"ventana": t["ventana"], "launchpad": t.get("launchpad") or m.get("dex"), "symbol": t.get("symbol") or m.get("symbol"),
             "name": t.get("name"), "ca": t["ca"], "edad_min": round((now - t["creado"]) / 60, 1) if t.get("creado") else None,
             "mcap_usd": round(m.get("mcap") or t.get("mcap_usd") or 0) or None,
             "mcap_fuente": "dexscreener" if t.get("ds") else "geckoterminal/origen",
             "liquidez_usd": m.get("liq"), "vol_m5_usd": m.get("v5"), "vol_h1_usd": m.get("v1"), "vol_h6_usd": m.get("v6"),
             "txns_m5": {"buys": m.get("b5"), "sells": m.get("s5")}, "txns_h1": {"buys": m.get("b1"), "sells": m.get("s1")},
             "r5": m.get("r5"), "r1h": m.get("r1"), "ticket5": m.get("ticket5"), "cambio_precio": {"m5": m.get("pc5"), "h1": m.get("pc1"), "h6": m.get("pc6")},
             "creador": t.get("creador"), "compra_inicial_dev_sol": t.get("compra_inicial_dev_sol"),
             "web": t.get("web"), "x": t.get("x"), "telegram": t.get("telegram"), "perfil_dexscreener_pagado": m.get("perfil_pagado"),
             "listado_dexscreener": LISTADOS.get(t["ca"]), "pair": m.get("pair"), "link": m.get("link"),
             "pool_origen": t.get("pool"), "fuentes": t["fuentes"]}
        flags = []
        tx_h1 = (m.get("b1") or 0) + (m.get("s1") or 0)
        if tx_h1 and (m.get("v1") or 0) / tx_h1 > 1500: flags.append("volumen/txn muy alto: posible wash trading")
        if tx_h1 <= 3 and (m.get("v1") or 0) > 20000: flags.append("volumen sin transacciones: dato poco fiable")
        if (m.get("r1") or 0) > 6 and (m.get("s1") or 0) >= 3: flags.append(f"compras/ventas {m['r1']} > 6: posible bots")
        r["avisos_datos"] = flags or None
        out.append(r)
    out.sort(key=lambda r: (not any("wash" in f or "poco fiable" in f for f in (r["avisos_datos"] or [])),
                            (r["txns_h1"]["buys"] or 0) + (r["txns_h1"]["sells"] or 0), r.get("vol_h1_usd") or 0), reverse=True)
    evaluados = [] if a.sin_evaluar else evaluar(final, a)
    pf_ventana = {t["ca"] for t in final if t["ca"] in PF_CURVAS or (t.get("launchpad") or "").startswith("pump")
                  or (t.get("ds") or {}).get("dex") == "pumpfun"}
    res = {"generado": J.ahora_madrid(), "modo": "ligero" if a.ligero else "completo", "ventana_min": a.min,
           "carrera": a.carrera, "duracion_s": round(time.time() - t0, 1), "estado_fuentes": J.ESTADO,
           "llamadas_por_fuente": J.CONTADOR, "tokens_vistos_por_fuente": counts,
           "cobertura": {"pumpfun_curvas_vistas": len(PF_CURVAS), "pumpfun_curvas_en_ventana": len(PF_CURVAS_VENTANA),
                         "pumpfun_en_salida": len(pf_ventana), "pumpswap_vistos": len(PSWAP),
                         "tokens_totales": len(TOK), "enriquecidos_dexscreener": n_ds,
                         "en_ventana_lanzamiento": sum(1 for t in final if t["ventana"] == "lanzamiento"),
                         "en_ventana_carrera": sum(1 for t in final if t["ventana"] == "carrera"),
                         "carrera_descartes": CARRERA_DESC},
           "avisables": [e for e in evaluados if e["pasa"]],
           "evaluados": evaluados, "candidatos_enriquecidos": len(cands), "tokens": out[: a.top]}
    s = json.dumps(res, ensure_ascii=False, indent=1, default=str)
    if a.out: open(a.out, "w").write(s)
    if imprimir: print(s)
    return res

if __name__ == "__main__":
    main()
