"""Escáner de mercados con DATOS REALES de Polymarket. SOLO LECTURA.
No opera, no usa wallet ni claves. Aplica los filtros de liquidez,
volumen y spread y muestra qué mercados los pasan.
Pasar los filtros NO es una oportunidad: aún no hay modelo de ventaja (edge)."""
import json
import os
import sys
import time
from collections import Counter

import requests

GAMMA_URL = os.environ.get("GAMMA_URL", "https://gamma-api.polymarket.com")
CLOB_URL = os.environ.get("CLOB_URL", "https://clob.polymarket.com")
GEOBLOCK_URL = os.environ.get("GEOBLOCK_URL", "https://polymarket.com/api/geoblock")

MIN_LIQUIDEZ = 1000.0
MIN_VOLUME = 5000.0
MAX_SPREAD = 0.03

PAGINAS = 3
POR_PAGINA = 100
MAX_LIBROS = 100
MOSTRAR = 15

session = requests.Session()
session.headers.update({"User-Agent": "polymarket-paper-bot/0.1"})


class ApiError(Exception):
    pass


def llamar(metodo, url, params=None, cuerpo=None, retries=3, backoff=1.0):
    ultimo = None
    for intento in range(1, retries + 1):
        try:
            r = session.request(metodo, url, params=params, json=cuerpo, timeout=15)
            if r.status_code == 429 or r.status_code >= 500:
                raise ApiError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError, ApiError) as e:
            ultimo = e
            if intento < retries:
                time.sleep(backoff * (2 ** (intento - 1)))
    raise ApiError(f"{metodo} {url} falló tras {retries} intentos: {ultimo}")


def numero(m, *claves):
    for k in claves:
        try:
            return float(m[k])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def token_ids(m):
    raw = m.get("clobTokenIds")
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return [str(t) for t in raw]


def evaluar_mercado(m):
    if m.get("closed") is True:
        return None, "cerrado"
    if m.get("active") is False:
        return None, "inactivo"
    if m.get("acceptingOrders") is False:
        return None, "no acepta órdenes"
    ids = token_ids(m)
    if len(ids) < 2:
        return None, "sin token IDs"
    liq = numero(m, "liquidityNum", "liquidity", "liquidityClob")
    vol = numero(m, "volumeNum", "volume")
    if liq is None:
        return None, "sin dato de liquidez"
    if vol is None:
        return None, "sin dato de volumen"
    if liq < MIN_LIQUIDEZ:
        return None, "liquidez baja"
    if vol < MIN_VOLUME:
        return None, "volumen bajo"
    return {"pregunta": str(m.get("question")), "yes": ids[0], "liq": liq, "vol": vol}, None


def mejor_bid_ask(libro):
    bids = [float(x["price"]) for x in libro.get("bids", [])]
    asks = [float(x["price"]) for x in libro.get("asks", [])]
    return (max(bids) if bids else None, min(asks) if asks else None)


def main():
    fallos_red = 0
    print("=== ESCÁNER (solo lectura, datos reales) ===")
    print(f"Filtros: liquidez>={MIN_LIQUIDEZ} volumen>={MIN_VOLUME} spread<={MAX_SPREAD}\n")

    try:
        g = llamar("GET", GEOBLOCK_URL)
        print(f"[geoblock] blocked={g.get('blocked')} país={g.get('country')} región={g.get('region')}")
    except ApiError as e:
        print(f"[geoblock] no disponible: {e}")

    mercados, cursor = [], None
    for pagina in range(PAGINAS):
        params = {"closed": "false", "limit": POR_PAGINA}
        if cursor:
            params["after_cursor"] = cursor
        try:
            datos = llamar("GET", f"{GAMMA_URL}/markets/keyset", params=params)
        except ApiError as e:
            fallos_red += 1
            print(f"[gamma] FALLO página {pagina + 1}: {e}")
            break
        mercados += datos.get("markets", [])
        cursor = datos.get("next_cursor")
        if not cursor:
            break
    print(f"[gamma] mercados leídos: {len(mercados)}")
    if mercados:
        print("[gamma] campos:", sorted(mercados[0].keys()))

    candidatos, descartes = [], Counter()
    for m in mercados:
        c, motivo = evaluar_mercado(m)
        if c:
            candidatos.append(c)
        else:
            descartes[motivo] += 1
    print(f"[filtros] pasan liquidez/volumen: {len(candidatos)} de {len(mercados)}")
    for motivo, n in descartes.most_common():
        print(f"          descartados por {motivo}: {n}")

    candidatos = candidatos[:MAX_LIBROS]
    finales, sin_libro = [], Counter()
    if candidatos:
        try:
            cuerpo = [{"token_id": c["yes"]} for c in candidatos]
            libros = llamar("POST", f"{CLOB_URL}/books", cuerpo=cuerpo)
            por_token = {str(b.get("asset_id")): b for b in libros}
            for c in candidatos:
                libro = por_token.get(c["yes"])
                if not libro:
                    sin_libro["sin libro"] += 1
                    continue
                bid, ask = mejor_bid_ask(libro)
                if bid is None or ask is None:
                    sin_libro["libro incompleto"] += 1
                    continue
                spread = round(ask - bid, 4)
                if spread > MAX_SPREAD:
                    sin_libro["spread alto"] += 1
                    continue
                c.update(bid=bid, ask=ask, spread=spread, min5=round(5 * ask, 2))
                finales.append(c)
        except ApiError as e:
            fallos_red += 1
            print(f"[clob] FALLO libros: {e}")
    for motivo, n in sin_libro.most_common():
        print(f"[libros] descartados por {motivo}: {n}")

    finales.sort(key=lambda c: (c["spread"], -c["liq"]))
    print(f"\n[resultado] mercados que pasan TODOS los filtros: {len(finales)}")
    for i, c in enumerate(finales[:MOSTRAR], 1):
        extremo = " (precio extremo)" if c["ask"] >= 0.97 or c["bid"] <= 0.03 else ""
        print(f"{i}. {c['pregunta'][:70]}")
        print(f"   bid={c['bid']} ask={c['ask']} spread={c['spread']} "
              f"liq={c['liq']:.0f} vol={c['vol']:.0f} 5 shares≈${c['min5']}{extremo}")
    print("\nNOTA: pasar filtros NO es una oportunidad. Falta el modelo de ventaja.")
    print("NO HAY OPORTUNIDAD confirmada. No se operó (modo solo lectura).")
    return 1 if (fallos_red and not mercados) else 0


if __name__ == "__main__":
    sys.exit(main())
