#!/usr/bin/env python3
"""joe_filtros.py - SOLO LECTURA. Librería común de Joe (11/11/2026).

Fuentes públicas (solo GET / websocket de lectura, sin claves, nunca firma ni compra):
  GeckoTerminal  new_pools, trending_pools (5m/1h/6h), dexes/pump-fun/pools, dexes/pumpswap/pools,
                 dexes/meteora-damm-v2/pools, tokens/<ca>/pools, pools/<pool>/ohlcv
  Dexscreener    tokens/v1, token-boosts latest/top, token-profiles latest, community-takeovers latest, search
  PumpPortal     wss subscribeNewToken + subscribeMigration (subscribeTokenTrade exige API key con SOL: desactivado)
  Rugcheck       api.rugcheck.xyz/v1/tokens/<ca>/report (LP bloqueado, holders, insiders, creador)

Filtros (ver FILTROS_2026-11-11.md): descartes duros SIN CAMBIOS + ratio por tramos + regla líder (MEMEMAN)
+ relectura curva/DAMM v2 + origen débil -> 🔴 (no se calla).
"""
import asyncio, json, re, statistics, threading, time
from datetime import datetime, timezone
import requests

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "application/json"}
S = requests.Session()
ESTADO = {}            # fuente -> "ok" / "HTTP 4xx" / "429 xN"
CONTADOR = {}          # fuente -> nº de llamadas
N429 = {}              # fuente -> nº de 429
LOCK = threading.Lock()
GT = "https://api.geckoterminal.com/api/v2/networks/solana"
DS = "https://api.dexscreener.com"
_last_call = {}        # host -> ts (espaciado mínimo por host)
ESPACIADO = {"api.geckoterminal.com": 3.2, "api.rugcheck.xyz": 0.6, "api.dexscreener.com": 0.25}
ESPACIADO_BASE = dict(ESPACIADO)

CURVA_DEX = {"pumpfun", "pump-fun", "raydium-launchlab", "launchlab", "meteora-dbc", "dynamic-bonding-curve",
             "moonshot", "moonit", "heaven", "bags", "boop"}
DAMM_V2 = {"meteora-damm-v2"}
SOL_USD = None

# ---------------- utilidades ----------------
def fnum(x, d=None):
    try: return float(x)
    except Exception: return d

def ahora_madrid():
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

def iso_ts(s):
    try: return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception: return None

def marca(fuente, msg):
    with LOCK:
        prev = ESTADO.get(fuente)
        if msg == "ok":
            if prev is None: ESTADO[fuente] = "ok"
            elif "429" in prev and not prev.startswith("ok"): ESTADO[fuente] = f"ok (429 x{N429.get(fuente, 0)}, backoff)"
        elif msg.startswith("429"):
            N429[fuente] = N429.get(fuente, 0) + 1
            ESTADO[fuente] = f"ok (429 x{N429[fuente]}, backoff)" if prev in (None, "ok") or "429" in prev else prev
        else:
            ESTADO[fuente] = msg

def get(url, fuente="?", tries=4, timeout=12, headers=UA, max_wait=30):
    """GET con espaciado por host y backoff exponencial en 429/5xx (respeta Retry-After)."""
    host = url.split("/")[2]
    for i in range(tries):
        gap = ESPACIADO.get(host, 0)
        with LOCK:
            w = _last_call.get(host, 0) + gap - time.time()
            _last_call[host] = max(time.time(), _last_call.get(host, 0) + gap)
        if w > 0: time.sleep(w)
        CONTADOR[fuente] = CONTADOR.get(fuente, 0) + 1
        try:
            r = S.get(url, headers=headers, timeout=timeout)
        except Exception as e:
            marca(fuente, f"red {str(e)[:50]}"); time.sleep(1.5 * (i + 1)); continue
        if r.status_code == 429 or r.status_code >= 500:
            if r.status_code == 429 and host in ESPACIADO:   # espaciado adaptativo: +50 % por cada 429 (máx 8 s)
                ESPACIADO[host] = min(8.0, ESPACIADO[host] * 1.5)
            ra = fnum(r.headers.get("retry-after"))
            wait = min(ra if ra else 4 * (2 ** i), max_wait)
            marca(fuente, "429" if r.status_code == 429 else f"HTTP {r.status_code}")
            time.sleep(wait); continue
        if r.status_code != 200:
            marca(fuente, f"HTTP {r.status_code}"); return None
        try:
            j = r.json()
        except Exception:
            marca(fuente, "json inválido"); return None
        if host in ESPACIADO and ESPACIADO[host] > ESPACIADO_BASE.get(host, 0):
            ESPACIADO[host] = max(ESPACIADO_BASE[host], ESPACIADO[host] * 0.97)
        marca(fuente, "ok"); return j
    marca(fuente, f"fallo tras {tries} intentos (429/red)")
    return None

# ---------------- GeckoTerminal ----------------
def _gt_rows(j):
    """Convierte respuesta de pools de Gecko en filas planas."""
    out = []
    if not j: return out
    toks = {i["id"]: i["attributes"] for i in j.get("included", []) if i.get("type") == "token"}
    for d in j.get("data", []):
        a = d.get("attributes") or {}; rel = d.get("relationships") or {}
        try:
            tid = rel["base_token"]["data"]["id"]; mint = tid.split("_", 1)[1]
            dex = rel["dex"]["data"]["id"]
        except Exception: continue
        tk = toks.get(tid, {})
        v = a.get("volume_usd") or {}; tx = a.get("transactions") or {}
        name = a.get("name") or ""
        out.append({"ca": mint, "dex": dex, "pool": a.get("address"), "creado": iso_ts(a.get("pool_created_at")),
                    "symbol": tk.get("symbol") or name.split(" / ")[0] or None, "name": tk.get("name"),
                    "mcap": fnum(a.get("market_cap_usd")) or fnum(a.get("fdv_usd")),
                    "liq": fnum(a.get("reserve_in_usd")), "vol": {k: fnum(x, 0) for k, x in v.items()},
                    "tx": tx, "pc": a.get("price_change_percentage") or {},
                    "precio": fnum(a.get("base_token_price_usd")), "sol_usd": fnum(a.get("quote_token_price_usd"))
                    if "SOL" in name.split(" / ")[-1] else None})
    return out

def gt_lista(path, paginas=1, fuente=None, extra=""):
    fuente = fuente or "gecko " + path.split("?")[0].strip("/")
    rows = []
    for p in range(1, paginas + 1):
        sep = "&" if "?" in path else "?"
        j = get(f"{GT}/{path}{sep}page={p}&include=base_token,dex{extra}", fuente)
        r = _gt_rows(j)
        if not r: break
        rows += r
    return rows

def gt_token_pools(ca):
    return gt_lista(f"tokens/{ca}/pools", 1, "gecko token-pools")

def gt_max_precio(pool, horas=24):
    """Máximo de precio (high) en velas de 15 min de las últimas `horas`. Devuelve (max, ultimo)."""
    j = get(f"{GT}/pools/{pool}/ohlcv/minute?aggregate=15&limit={int(horas*4)}&currency=usd", "gecko ohlcv")
    l = (((j or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    if not l: return None, None
    return max(fnum(c[2], 0) for c in l), fnum(l[0][4])

# ---------------- Dexscreener ----------------
def ds_tokens(mints):
    """Devuelve {mint: [pares]} con TODOS los pares de cada token."""
    res = {}
    mints = [m for m in dict.fromkeys(mints) if m]
    for i in range(0, len(mints), 30):
        j = get(f"{DS}/tokens/v1/solana/" + ",".join(mints[i:i + 30]), "dexscreener")
        if not isinstance(j, list): continue
        for p in j:
            m = (p.get("baseToken") or {}).get("address")
            if m: res.setdefault(m, []).append(p)
    return res

def ds_listados():
    """Boosts latest/top, profiles latest y CTO latest (solo Solana). Devuelve {mint: [etiquetas]}."""
    out = {}
    for path, tag in (("token-boosts/latest/v1", "boost"), ("token-boosts/top/v1", "boost-top"),
                      ("token-profiles/latest/v1", "perfil"), ("community-takeovers/latest/v1", "cto")):
        j = get(f"{DS}/{path}", "dexscreener " + tag)
        for x in j if isinstance(j, list) else []:
            if x.get("chainId") == "solana" and x.get("tokenAddress"):
                out.setdefault(x["tokenAddress"], [])
                if tag not in out[x["tokenAddress"]]: out[x["tokenAddress"]].append(tag)
    return out

def ds_search(q):
    j = get(f"{DS}/latest/dex/search?q={requests.utils.quote(q)}", "dexscreener search")
    return [p for p in (j or {}).get("pairs") or [] if p.get("chainId") == "solana"]

# ---------------- PumpPortal ----------------
class PumpPortal:
    """Escucha wss://pumpportal.fun/api/data: tokens nuevos + migraciones (+ trades de los nuevos si trades=True).
    Lectura pública. Se usa en un hilo: pp = PumpPortal(); pp.start(segundos)."""
    def __init__(self, trades=False, max_trade_subs=300):
        self.nuevos = {}       # mint -> info
        self.migrados = {}     # mint -> info
        self.trades = {}       # mint -> {"b":n,"s":n,"mcap_sol":x,"t":[(ts,tipo,sol)]}
        self.trades_on = trades; self.max_subs = max_trade_subs
        self.err = None; self.stop = False; self.th = None; self.n_msgs = 0

    async def _run(self, secs):
        import websockets
        t0 = time.time()
        while not self.stop and (secs is None or time.time() - t0 < secs):
            try:
                async with websockets.connect("wss://pumpportal.fun/api/data", open_timeout=10, ping_interval=20) as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    await ws.send(json.dumps({"method": "subscribeMigration"}))
                    subs = []
                    while not self.stop and (secs is None or time.time() - t0 < secs):
                        try: m = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                        except asyncio.TimeoutError: continue
                        self.n_msgs += 1
                        mint = m.get("mint")
                        if not mint: continue
                        tx = m.get("txType")
                        if tx == "create":
                            pool = m.get("pool")
                            self.nuevos[mint] = {"ts": time.time(), "symbol": m.get("symbol"), "name": m.get("name"),
                                                 "creador": m.get("traderPublicKey"), "mcap_sol": m.get("marketCapSol"),
                                                 "dev_sol": m.get("solAmount"),
                                                 "launchpad": {"pump": "pump.fun", "bonk": "LaunchLab/LetsBonk"}.get(pool, pool)}
                            if self.trades_on:
                                subs.append(mint)
                                await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [mint]}))
                                if len(subs) > self.max_subs:
                                    old = subs.pop(0)
                                    await ws.send(json.dumps({"method": "unsubscribeTokenTrade", "keys": [old]}))
                        elif tx == "migrate" or m.get("pool") == "pump-amm" and tx not in ("buy", "sell"):
                            self.migrados[mint] = {"ts": time.time(), "pool": m.get("pool"), "sig": m.get("signature")}
                        elif tx in ("buy", "sell"):
                            t = self.trades.setdefault(mint, {"t": []})
                            t["mcap_sol"] = m.get("marketCapSol") or t.get("mcap_sol")
                            t["t"].append((time.time(), tx, fnum(m.get("solAmount"), 0)))
                            t["t"] = [x for x in t["t"] if time.time() - x[0] <= 600]
                self.err = None
            except Exception as e:
                self.err = str(e)[:80]
                if secs is not None and time.time() - t0 >= secs: break
                time.sleep(5)

    def start(self, secs=None):
        def runner():
            try: asyncio.run(self._run(secs))
            except Exception as e: self.err = str(e)[:80]
        self.th = threading.Thread(target=runner, daemon=True); self.th.start(); return self

    def join(self):
        if self.th: self.th.join()

    def m5(self, mint):
        t = self.trades.get(mint) or {}
        now = time.time(); b = s = 0
        for ts, tipo, sol in t.get("t", []):
            if now - ts <= 300:
                if tipo == "buy": b += 1
                else: s += 1
        return b, s, t.get("mcap_sol")

# ---------------- Rugcheck ----------------
def rugcheck(ca):
    j = get(f"https://api.rugcheck.xyz/v1/tokens/{ca}/report", "rugcheck", tries=3)
    if not j or "error" in j: return None
    mk = j.get("markets") or []
    pools = {m.get("pubkey") for m in mk} | {m.get("liquidityA") for m in mk} | {m.get("liquidityB") for m in mk}
    known = set((j.get("knownAccounts") or {}).keys())
    lp = []
    for m in mk:
        l = m.get("lp") or {}
        lp.append({"tipo": m.get("marketType"), "pool": m.get("pubkey"), "lp_bloqueado_pct": fnum(l.get("lpLockedPct")),
                   "liq_usd": fnum(l.get("quoteUSD"), 0) + fnum(l.get("baseUSD"), 0) if l else None,
                   "lp_bloqueado_usd": fnum(l.get("lpLockedUSD"))})
    holders = []
    for h in j.get("topHolders") or []:
        own = h.get("owner"); acc = h.get("address")
        if own in pools or acc in pools or own in known or acc in known: continue
        holders.append({"owner": own, "pct": fnum(h.get("pct"), 0), "insider": bool(h.get("insider"))})
    ct = j.get("creatorTokens") or []
    return {"score": j.get("score_normalised"), "rugged": j.get("rugged"), "creador": j.get("creator"),
            "creador_pct": fnum(j.get("creatorBalance"), 0) / max(fnum((j.get("token") or {}).get("supply"), 1), 1) * 100
            if j.get("creatorBalance") else 0,
            "mint_auth": j.get("mintAuthority"), "freeze_auth": j.get("freezeAuthority"),
            "holders_total": j.get("totalHolders"), "insiders_grafo": j.get("graphInsidersDetected"),
            "creador_tokens_previos": len(ct), "creador_tokens": ct[:20], "markets": lp, "holders_top": holders[:15],
            "riesgos": [r.get("name") for r in j.get("risks") or []], "launchpad": (j.get("launchpad") or {}).get("name")
            if isinstance(j.get("launchpad"), dict) else j.get("launchpad")}

def bundle_holders(rc):
    """Holders casi iguales / insiders. Devuelve texto de motivo o None."""
    if not rc: return None
    hs = [h for h in rc["holders_top"] if h["pct"] >= 1.0][:12]
    ins = sum(h["pct"] for h in rc["holders_top"] if h["insider"])
    if ins >= 20: return f"insiders con {ins:.0f}% del supply"
    # ventana deslizante: >=5 holders de 2,5–9 % cada uno con max/min <= 1,3 (patrón BORLY/OnlyCats 3–6 %).
    # Calibrado para no marcar a X/apeonx (3,6/3,4/3,1/2,6/2,2 %, holders orgánicos).
    vals = sorted([h["pct"] for h in hs if 2.5 <= h["pct"] <= 9], reverse=True)
    for i in range(len(vals) - 4):
        w = vals[i:i + 5]
        if w[0] / w[-1] <= 1.3:
            return f"holders casi iguales ({', '.join(f'{x:.1f}' for x in w)} %)"
    return None

def creador_rugs(rc):
    """Creador con rugs previos (tokens anteriores muertos con mcap ínfimo). Devuelve motivo o None."""
    if not rc: return None
    ct = rc.get("creador_tokens") or []
    muertos = [t for t in ct if isinstance(t, dict) and fnum(t.get("marketCap"), 1e9) < 5000]
    if rc.get("rugged"): return "Rugcheck: token marcado rugged"
    if len(ct) >= 3 and len(muertos) >= 3: return f"creador con {len(muertos)} tokens previos muertos (relanzamientos)"
    return None

# ---------------- métricas de un token ----------------
def metricas_ds(pares, now=None):
    """Métricas del par principal (más volumen 24h) + totales de todos los pares."""
    now = now or time.time()
    if not pares: return None
    vol = lambda p: ((p.get("volume") or {}).get("h24") or 0)
    p = max(pares, key=vol)
    tx = p.get("txns") or {}; v = p.get("volume") or {}
    b5, s5 = (tx.get("m5") or {}).get("buys", 0), (tx.get("m5") or {}).get("sells", 0)
    b1, s1 = (tx.get("h1") or {}).get("buys", 0), (tx.get("h1") or {}).get("sells", 0)
    v5, v1, v6 = fnum(v.get("m5"), 0), fnum(v.get("h1"), 0), fnum(v.get("h6"), 0)
    creados = [x.get("pairCreatedAt") for x in pares if x.get("pairCreatedAt")]
    labels = p.get("labels") or []
    dex = p.get("dexId")
    if dex == "meteora" and "DYN2" in labels: dex = "meteora-damm-v2"
    liq_total = sum(fnum((x.get("liquidity") or {}).get("usd"), 0) for x in pares)
    info = p.get("info") or {}
    return {"symbol": (p.get("baseToken") or {}).get("symbol"), "name": (p.get("baseToken") or {}).get("name"),
            "dex": dex, "labels": labels, "pair": p.get("pairAddress"), "link": p.get("url"),
            "mcap": fnum(p.get("marketCap")) or fnum(p.get("fdv")), "liq": fnum((p.get("liquidity") or {}).get("usd")),
            "liq_total": liq_total, "n_pares": len(pares),
            "b5": b5, "s5": s5, "b1": b1, "s1": s1,
            "r5": round(b5 / s5, 2) if s5 else (float(b5) if b5 else None),
            "r1": round(b1 / s1, 2) if s1 else (float(b1) if b1 else None),
            "v5": v5, "v1": v1, "v6": v6, "v24": fnum(v.get("h24"), 0),
            "ticket5": round(v5 / (b5 + s5), 1) if (b5 + s5) else None,
            "ticket1": round(v1 / (b1 + s1), 1) if (b1 + s1) else None,
            "pc5": fnum((p.get("priceChange") or {}).get("m5")), "pc1": fnum((p.get("priceChange") or {}).get("h1")),
            "pc6": fnum((p.get("priceChange") or {}).get("h6")),
            "edad_h": round((now - min(creados) / 1000) / 3600, 2) if creados else None,
            "precio": fnum(p.get("priceUsd")),
            "x": next((s.get("url") for s in info.get("socials") or [] if s.get("type") == "twitter"), None),
            "web": next((w.get("url") for w in info.get("websites") or []), None),
            "perfil_pagado": bool(info)}

def es_curva(dex):
    return (dex or "").lower() in CURVA_DEX

# ---------------- enjambre / líder (regla MEMEMAN afinada) ----------------
def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower().replace("$", ""))

def enjambre(ca, symbol, mcap, liq):
    """Busca copias por ticker en Dexscreener. Devuelve dict con nº de copias y si es líder claro
    (>=2x mcap de la siguiente y la mayor liquidez del tema)."""
    key = _norm(symbol)
    if not key or len(key) < 2: return {"copias": 0, "lider": True}
    toks = {}
    for p in ds_search(symbol):
        b = p.get("baseToken") or {}
        if _norm(b.get("symbol")) != key: continue
        a = b.get("address"); mc = fnum(p.get("marketCap")) or fnum(p.get("fdv")) or 0
        lq = fnum((p.get("liquidity") or {}).get("usd"), 0)
        t = toks.setdefault(a, {"mcap": 0, "liq": 0})
        t["mcap"] = max(t["mcap"], mc); t["liq"] = max(t["liq"], lq)
    toks.setdefault(ca, {"mcap": mcap or 0, "liq": liq or 0})
    otros = [v for k, v in toks.items() if k != ca]
    copias = len(otros)
    if not otros: return {"copias": 0, "lider": True}
    sig_mc = max(o["mcap"] for o in otros); max_liq = max(o["liq"] for o in otros)
    yo = toks[ca]
    lider = (yo["mcap"] or mcap or 0) >= 2 * sig_mc and (yo["liq"] or liq or 0) >= max_liq
    return {"copias": copias, "lider": lider, "mcap_siguiente": round(sig_mc), "liq_max_otros": round(max_liq)}

# ---------------- evaluación por tramos ----------------
UMBRAL = {
    "suelo_curva": 25_000, "temprano_min": 30_000, "r_bajo": 1.4,           # < 100K
    "momentum_min": 100_000, "momentum_max": 1_000_000, "carrera_max": 3_000_000,
    "r5_alto": 1.15, "r5_alto_pref": 1.2, "r1_alto": 1.1, "v5_alto": 15_000, "v5_alto_pref": 20_000,
    "frac_max": 0.70, "lp_min": 90.0, "ticket_wash": 5.0, "damm_liq_min": 20_000, "swarm_min": 5,
}

def descartes_duros(m, rc=None):
    """Descartes duros SIN CAMBIOS. Devuelve lista de motivos (vacía = pasa)."""
    d = []
    curva = es_curva(m.get("dex"))
    for k, n in (("ticket5", m["b5"] + m["s5"]), ("ticket1", m["b1"] + m["s1"])):
        if m.get(k) is not None and n >= 20 and m[k] <= UMBRAL["ticket_wash"]:
            d.append(f"wash: ticket {k[-1]} {m[k]} $ <= 5 $")
    if m.get("liq") and m.get("mcap") and m["liq"] > m["mcap"]:
        d.append("liq > mcap")
    if not curva and m.get("mcap", 0) and m["mcap"] > 10_000 and (m.get("liq_total") or 0) < 500:
        d.append("liquidez drenada en TODAS las pools")
    if rc:
        if not curva:
            ml = [x for x in rc["markets"] if x["lp_bloqueado_pct"] is not None and (x.get("liq_usd") or 0) > 1000]
            if ml:
                best = max(ml, key=lambda x: x.get("liq_usd") or 0)
                if best["lp_bloqueado_pct"] < UMBRAL["lp_min"]:
                    d.append(f"LP desbloqueado ({best['lp_bloqueado_pct']:.0f}% bloqueado en {best['tipo']})")
        b = bundle_holders(rc)
        if b: d.append("bundle: " + b)
        cr = creador_rugs(rc)
        if cr: d.append(cr)
        if m.get("dex") in DAMM_V2:
            ml = [x for x in rc["markets"] if x["tipo"] and "damm" in x["tipo"].lower()]
            if not ml or max(x["lp_bloqueado_pct"] or 0 for x in ml) < UMBRAL["lp_min"]:
                d.append("Meteora DAMM v2 sin LP bloqueado verificado")
    return d

def tramo(m, carrera=False):
    mc = m.get("mcap") or 0
    if es_curva(m.get("dex")): return "curva"
    if mc < UMBRAL["momentum_min"]: return "temprano"
    if mc <= UMBRAL["momentum_max"]: return "momentum"
    if carrera and mc <= UMBRAL["carrera_max"]: return "carrera"
    return "fuera"

def vol_creciente(m):
    """Volumen 1h por encima de la media horaria de 6h (x1,3) o token <2 h con volumen 1h fuerte."""
    if (m.get("edad_h") or 99) < 2: return (m.get("v1") or 0) >= 50_000
    base = (m.get("v6") or 0) / 6
    return base > 0 and (m.get("v1") or 0) >= 1.3 * base

def evaluar_numeros(m, carrera=False, frac_max=None, swarm=None):
    """Filtros blandos/por tramos. Devuelve (pasa, tramo, motivos_no, notas)."""
    t = tramo(m, carrera); no = []; notas = []
    r5, r1 = m.get("r5") or 0, m.get("r1") or 0
    mc = m.get("mcap") or 0
    if t == "fuera": return False, t, [f"mcap {mc:,.0f} fuera de rango"], notas
    if t in ("curva", "temprano"):
        suelo = UMBRAL["suelo_curva"] if t == "curva" else UMBRAL["suelo_curva"]
        if mc < suelo: no.append(f"mcap {mc:,.0f} < suelo {suelo:,}")
        if r5 < UMBRAL["r_bajo"]: no.append(f"r5 {r5} < 1,4")
        if r1 < UMBRAL["r_bajo"]: no.append(f"r1h {r1} < 1,4")
        if (m.get("pc5") or 0) < -20: no.append(f"caída 5m {m['pc5']}%")
        if (m.get("ticket5") or 0) < 12 and m["b5"] + m["s5"] >= 10: notas.append(f"ticket5 bajo {m.get('ticket5')} $")
        if t == "temprano" and mc < UMBRAL["temprano_min"]: notas.append("25–30K: gema muy temprana")
        if t == "curva": notas.append("curva: exige 2ª lectura a +60–120 s (ratio ≥1,4 y mcap sin caer)")
    else:  # momentum / carrera
        if r5 < UMBRAL["r5_alto"]: no.append(f"r5 {r5} < 1,15")
        elif r5 < UMBRAL["r5_alto_pref"]: notas.append(f"r5 {r5} justo (1,15–1,2)")
        if r1 < UMBRAL["r1_alto"]: no.append(f"r1h {r1} < 1,1")
        if (m.get("v5") or 0) < UMBRAL["v5_alto"]: no.append(f"vol5m {m.get('v5',0):,.0f} < 15K")
        elif (m.get("v5") or 0) < UMBRAL["v5_alto_pref"]: notas.append("vol5m 15–20K (justo)")
        if (m.get("pc5") or 0) < -10: no.append(f"5m {m['pc5']}% peor que -10%")
        if frac_max is not None:
            if frac_max < UMBRAL["frac_max"]: no.append(f"mcap al {frac_max*100:.0f}% de su máximo (<70%)")
        else: notas.append("máximo sin medir")
        if t == "carrera":
            eh = m.get("edad_h")
            if eh is not None and not (1 <= eh <= 24): no.append(f"edad {eh} h fuera de 1–24 h")
            if not vol_creciente(m): no.append("volumen 1h no creciente")
    if m.get("dex") in DAMM_V2:
        if (m.get("liq") or 0) < UMBRAL["damm_liq_min"]: no.append(f"DAMM v2 liq {m.get('liq') or 0:,.0f} < 20K")
        notas.append("DAMM v2: exige relectura a +2 min")
    if (m.get("r5") or 0) > 6 and m["s5"] >= 3: notas.append("compras/ventas >6: posibles bots")
    if swarm:
        if swarm.get("copias", 0) >= UMBRAL["swarm_min"] and not swarm.get("lider"):
            no.append(f"enjambre de {swarm['copias']} copias y NO es líder claro (regla MEMEMAN)")
        elif swarm.get("copias", 0) >= UMBRAL["swarm_min"]:
            notas.append(f"líder claro de un enjambre de {swarm['copias']} copias")
    return not no, t, no, notas

def color_sugerido(t, notas, origen_verificado=False):
    """🟠 por defecto (origen sin comprobar). 🔴 si hay notas de riesgo extremo. 🟢 solo con origen verificado por el agente."""
    rojo = any(k in " ".join(notas) for k in ("justo", "bots", "gema muy temprana", "DAMM", "máximo sin medir"))
    if origen_verificado and not rojo: return "🟢"
    return "🔴" if rojo else "🟠"

def etiqueta(t):
    return {"curva": "💎 curva", "temprano": "🟠 temprano", "momentum": "🔼 momentum 100K–1M",
            "carrera": "🔼 en carrera (1–24 h)"}.get(t, t)

def releer_confirma(ca, m0, espera):
    """Relectura a +espera s: confirma si ratio 5m sigue >= umbral y mcap no cae (>=97 %)."""
    time.sleep(espera)
    pares = ds_tokens([ca]).get(ca)
    m1 = metricas_ds(pares) if pares else None
    if not m1: return False, None, "sin datos en la relectura"
    umb = UMBRAL["r_bajo"] if tramo(m1) in ("curva", "temprano") else UMBRAL["r5_alto"]
    ok = (m1.get("r5") or 0) >= umb and (m1.get("mcap") or 0) >= 0.97 * (m0.get("mcap") or 0)
    return ok, m1, f"relectura +{espera:.0f}s: mcap {m0.get('mcap') or 0:,.0f}→{m1.get('mcap') or 0:,.0f}, r5 {m0.get('r5')}→{m1.get('r5')}"
