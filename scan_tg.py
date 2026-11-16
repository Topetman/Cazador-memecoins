#!/usr/bin/env python3
"""scan_tg.py — SOLO LECTURA.

Scan autónomo para GitHub Actions: aplica FILTROS_2026-11-11.md (ratios por tramos,
ventana en carrera, líder del enjambre ≥2x, descartes duros) con fuentes públicas
sin clave (Dexscreener, GeckoTerminal, Rugcheck; LaunchLab y Bags por HTTP).
Sin X y sin PumpPortal websocket (para caber en 5 minutos).

Si un candidato pasa, avisa por Telegram (sendMessage, texto plano) SOLO cuando
corre dentro de GitHub Actions. En local siempre es --dry-run: imprime y no envía.

Uso:
  python scan_tg.py --dry-run          # prueba local, no envía
  python scan_tg.py                    # en Actions: avisa y actualiza avisados.json

Variables: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (Secrets de GitHub).
Nunca firma, compra ni usa claves.
"""
from __future__ import annotations

import argparse, json, os, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import joe_filtros as J
import scan_lanzamientos as SC

AVISADOS_F = HERE / "avisados.json"
MADRID = timezone(timedelta(hours=1))  # Europe/Madrid en invierno; en verano CET/CEST lo da el runner si TZ se ajusta


def ahora():
    # En Actions el runner está en UTC; mostramos Europe/Madrid.
    tz = timezone(timedelta(hours=1))
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Madrid")
    except Exception:
        pass
    return datetime.now(tz)


def hora_txt():
    return ahora().strftime("%Y-%m-%d %H:%M:%S %Z")


def cargar_avisados():
    if not AVISADOS_F.exists():
        return {"actualizado": hora_txt(), "avisados": []}
    try:
        return json.loads(AVISADOS_F.read_text())
    except Exception:
        return {"actualizado": hora_txt(), "avisados": []}


def cas_avisados(doc):
    out = set()
    for x in doc.get("avisados") or []:
        if isinstance(x, dict) and x.get("ca"):
            out.add(x["ca"])
        elif isinstance(x, str):
            out.add(x)
    return out


def guardar_avisados(doc):
    doc["actualizado"] = hora_txt()
    doc["n"] = len(doc.get("avisados") or [])
    tmp = AVISADOS_F.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(AVISADOS_F)


# ---------- refinamientos deterministas sobre el enjambre y los holders ----------
def enjambre_valido(ca, symbol, mcap, liq):
    """Igual que la regla MEMEMAN, pero ignora copias inválidas (liq < 100 o liq > mcap)."""
    key = J._norm(symbol)
    if not key or len(key) < 2:
        return {"copias": 0, "lider": True, "validas": 1}
    toks = {}
    for p in J.ds_search(symbol):
        b = p.get("baseToken") or {}
        if J._norm(b.get("symbol")) != key:
            continue
        a = b.get("address")
        mc = J.fnum(p.get("marketCap")) or J.fnum(p.get("fdv")) or 0
        lq = J.fnum((p.get("liquidity") or {}).get("usd"), 0)
        t = toks.setdefault(a, {"mcap": 0, "liq": 0})
        t["mcap"] = max(t["mcap"], mc)
        t["liq"] = max(t["liq"], lq)
    toks.setdefault(ca, {"mcap": mcap or 0, "liq": liq or 0})

    def valida(v):
        mc, lq = v["mcap"] or 0, v["liq"] or 0
        if lq < 100:
            return False
        if mc and lq > mc:
            return False
        return True

    validos = {k: v for k, v in toks.items() if valida(v)}
    if ca not in validos:
        validos[ca] = toks[ca]
    otros = [v for k, v in validos.items() if k != ca]
    if not otros:
        return {"copias": 0, "lider": True, "validas": 1, "mcap_siguiente": None, "liq_max_otros": 0}
    sig_mc = max(o["mcap"] for o in otros)
    max_liq = max(o["liq"] for o in otros)
    yo = validos[ca]
    lider = (yo["mcap"] or mcap or 0) >= 2 * sig_mc and (yo["liq"] or liq or 0) >= max_liq
    return {
        "copias": len(otros),
        "lider": lider,
        "validas": 1 + len(otros),
        "mcap_siguiente": round(sig_mc),
        "liq_max_otros": round(max_liq),
    }


def bundle_bajo(rc):
    """Patrón de holders casi idénticos por debajo del 2,5 % (caso inuhao), no cubierto por el auto de 2,5–9 %."""
    if not rc:
        return None
    vals = sorted([h["pct"] for h in rc.get("holders_top") or [] if 0.4 <= (h.get("pct") or 0) <= 2.49], reverse=True)
    if len(vals) >= 8 and vals[0] / vals[7] <= 1.15:
        return f"holders casi iguales bajo 2,5 % ({vals[7]:.2f}–{vals[0]:.2f} %)"
    return None


def concentracion(rc):
    """Una sola wallet (ya sin LP) con una parte muy grande del supply."""
    if not rc:
        return None
    top = (rc.get("holders_top") or [None])[0]
    if top and (top.get("pct") or 0) >= 25:
        return f"una wallet tiene {top['pct']:.1f} % del supply"
    return None


def fmt_num(n, dec=0):
    if n is None:
        return "?"
    try:
        n = float(n)
    except Exception:
        return "?"
    if dec == 0:
        return f"{n:,.0f}".replace(",", ".")
    s = f"{n:,.{dec}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def mensaje(e, color):
    m = e.get("metricas") or {}
    nombre = e.get("name") or e.get("symbol") or "token"
    ticker = e.get("symbol") or "?"
    tramo = e.get("etiqueta") or e.get("tramo") or ""
    notas = e.get("notas") or []
    sw = e.get("enjambre") or {}
    rc = e.get("rugcheck") or {}
    lp = rc.get("lp") or []
    lp_txt = "LP no verificado"
    if lp:
        tipo, pct, liq = lp[0][0], lp[0][1], lp[0][2]
        if pct is not None:
            lp_txt = f"LP {pct:.0f} % bloqueado ({tipo}, ~{fmt_num(liq)} $)"
    lider = "Líder del enjambre." if sw.get("lider") and (sw.get("copias") or 0) >= 5 else (
        "Sin enjambre relevante." if (sw.get("copias") or 0) < 5 else "Enjambre revisado."
    )
    riesgo = "Riesgo alto. El origen no está comprobado en X; esto no es un consejo."
    lineas = [
        f"{color} ${ticker} ({nombre}) — {tramo}. {riesgo}",
        "",
        f"Mercado: ~{fmt_num(m.get('mcap'))} $, liq ~{fmt_num(m.get('liq'))} $, "
        f"r5 {m.get('r5') if m.get('r5') is not None else '?'}, "
        f"r1 {m.get('r1') if m.get('r1') is not None else '?'}, "
        f"vol 5m ~{fmt_num(m.get('v5'))} $, ticket ~{m.get('ticket5') if m.get('ticket5') is not None else '?'} $. "
        f"{lp_txt}. {lider}",
    ]
    extra = []
    if e.get("frac_max_24h") is not None:
        extra.append(f"mcap al {e['frac_max_24h']*100:.0f} % de su máximo 24 h")
    if m.get("pc5") is not None:
        extra.append(f"cambio 5m {m['pc5']}%")
    if m.get("edad_h") is not None:
        extra.append(f"edad {m['edad_h']} h")
    tops = rc.get("top_holders_pct") or []
    if tops:
        extra.append("holders " + " / ".join(f"{x:.1f}" for x in tops[:5]) + " %")
    if notas:
        extra.append("; ".join(notas[:3]))
    if extra:
        lineas.append("Detalle: " + ". ".join(extra) + ".")
    lineas += ["", "CA:", e["ca"], ""]
    link = e.get("link") or (f"https://dexscreener.com/solana/{m['pair']}" if m.get("pair") else "")
    if link:
        lineas.append(link)
    return "\n".join(lineas)


def enviar_telegram(texto):
    """Solo se llama desde GitHub Actions. Nunca imprimas el token."""
    tok = os.environ.get("TELEGRAM_BOT_TOKEN") or ""
    chat = os.environ.get("TELEGRAM_CHAT_ID") or ""
    if not tok or not chat:
        raise SystemExit("Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID (Secrets de GitHub).")
    import requests
    r = requests.post(
        f"https://api.telegram.org/bot{tok}/sendMessage",
        json={"chat_id": chat, "text": texto, "disable_web_page_preview": True},
        timeout=20,
    )
    if r.status_code != 200:
        raise SystemExit(f"Telegram ha contestado HTTP {r.status_code}.")
    return True


def postfiltrar(e):
    """Rehace líder con copias válidas y aplica bundle bajo / concentración. Devuelve (ok, motivos)."""
    motivos = []
    m = e.get("metricas") or {}
    sw = enjambre_valido(e["ca"], e.get("symbol"), m.get("mcap"), m.get("liq"))
    e["enjambre"] = {**(e.get("enjambre") or {}), **sw}
    if sw.get("copias", 0) >= J.UMBRAL["swarm_min"] and not sw.get("lider"):
        motivos.append(
            f"enjambre de {sw['copias']} copias válidas y NO es líder claro "
            f"(siguiente ~{fmt_num(sw.get('mcap_siguiente'))} $)"
        )
    # rugcheck ya viene en e; reconstruimos un rc mínimo para bundle_bajo / concentración
    rc = None
    raw = e.get("rugcheck")
    if raw and raw.get("top_holders_pct") is not None:
        rc = {"holders_top": [{"pct": p} for p in (raw.get("top_holders_pct") or [])]}
    # Si hay informe, usamos también los descartes ya calculados por joe_filtros
    if e.get("descartes_duros"):
        motivos.extend(e["descartes_duros"])
    b = bundle_bajo(rc) if rc else None
    if b:
        motivos.append("bundle: " + b)
    c = concentracion(rc) if rc else None
    if c:
        motivos.append(c)
    return (not motivos), motivos


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="imprime avisos, no envía ni escribe avisados.json")
    ap.add_argument("--min", type=float, default=60)
    ap.add_argument("--relectura", type=float, default=90)
    args = ap.parse_args()

    en_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    dry = args.dry_run or not en_actions
    if not en_actions and not args.dry_run:
        print("Fuera de GitHub Actions no se envía nada a Telegram (modo dry-run).")

    t0 = time.time()
    doc = cargar_avisados()
    ya = cas_avisados(doc)
    print(f"[{hora_txt()}] inicio dry_run={dry} avisados_previos={len(ya)}")

    # Dexscreener, Gecko, Rugcheck, LaunchLab y Bags. Sin websocket de PumpPortal
    # (no hay HTTP público equivalente) para no pasarnos de 5 minutos.
    scan = SC.main(
        ["--min", str(args.min), "--carrera", "--ws", "0", "--relectura", str(args.relectura)],
        imprimir=False,
    )

    vistos = (scan.get("cobertura") or {}).get("tokens_totales") or 0
    enriquecidos = (scan.get("cobertura") or {}).get("enriquecidos_dexscreener") or 0
    evaluados = scan.get("evaluados") or []
    avisables = [e for e in (scan.get("avisables") or []) if e.get("pasa")]

    enviaria = []
    descartes_extra = []
    for e in avisables:
        ca = e.get("ca")
        if not ca or ca in ya:
            if ca in ya:
                descartes_extra.append(f"{e.get('symbol')} {ca[:8]}… ya avisado")
            continue
        ok, motivos = postfiltrar(e)
        if not ok:
            e["pasa"] = False
            e["descartes_post"] = motivos
            descartes_extra.append(f"{e.get('symbol')} {ca[:8]}… " + "; ".join(motivos))
            continue
        color = e.get("color_sugerido") or "🟠"
        if color == "🟢":
            color = "🟠"  # sin X no hay origen verificado
        e["_color"] = color
        e["_mensaje"] = mensaje(e, color)
        enviaria.append(e)

    print(f"tokens vistos: {vistos}")
    print(f"enriquecidos en Dexscreener: {enriquecidos}")
    print(f"evaluados: {len(evaluados)}")
    print(f"pasan el scan: {len(avisables)}")
    print(f"pasarían a aviso (nuevos, tras postfiltro): {len(enviaria)}")
    print(f"duración scan: {scan.get('duracion_s')} s  fuentes: {json.dumps(scan.get('estado_fuentes') or {}, ensure_ascii=False)}")
    if descartes_extra:
        print("descartes extra / ya avisados:")
        for d in descartes_extra:
            print(" -", d)

    n_antes = len(cas_avisados(doc))
    if not enviaria:
        print("Ningún aviso nuevo.")
    for e in enviaria:
        print("-" * 40)
        print(e["_mensaje"])
        print("-" * 40)
        if not dry:
            enviar_telegram(e["_mensaje"])
            doc.setdefault("avisados", []).append({
                "ca": e["ca"],
                "symbol": e.get("symbol"),
                "hora": hora_txt(),
                "color": e["_color"],
                "origen": "scan_tg",
                "mcap": (e.get("metricas") or {}).get("mcap"),
            })
            ya.add(e["ca"])
            print(f"enviado {e.get('symbol')} {e['ca'][:8]}…")
            # Tras cada envío, no al final: si Telegram falla a mitad, lo ya enviado queda grabado.
            guardar_avisados(doc)
            print(f"avisados.json actualizado ({len(cas_avisados(doc))} CA)")
            time.sleep(0.4)

    if not dry and len(cas_avisados(doc)) == n_antes:
        guardar_avisados(doc)
        print(f"avisados.json actualizado ({len(cas_avisados(doc))} CA)")

    print(f"fin en {time.time() - t0:.1f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
