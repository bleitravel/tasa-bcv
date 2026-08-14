#!/usr/bin/env python3
"""
Tasa BCV + Binance P2P.

Lee la tasa oficial del USD (y EUR) publicada por el Banco Central de Venezuela
y el promedio de los 5 mejores anuncios de Binance P2P (USDT/VES).

Escribe el resultado en data/tasa.json y no toca el archivo si la corrida falla,
para no publicar datos vacios o a medias.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import urllib3
from bs4 import BeautifulSoup

# El BCV sirve una cadena de certificados incompleta; se verifica el contenido,
# no el TLS. Silenciamos el warning para no ensuciar el log del workflow.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BCV_URL = "https://www.bcv.org.ve/"
BINANCE_URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

TOP_N = int(os.environ.get("TOP_N", "5"))
TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "30"))
REINTENTOS = int(os.environ.get("REINTENTOS", "3"))

RAIZ = Path(__file__).resolve().parent.parent
SALIDA = RAIZ / "data" / "tasa.json"

VE_TZ = timezone(timedelta(hours=-4))


# --------------------------------------------------------------------------- #
# utilidades
# --------------------------------------------------------------------------- #
def _a_decimal(texto: str) -> float:
    """'39.181,30' o '39,18130000' -> float. Formato venezolano/es-VE."""
    limpio = re.sub(r"[^\d.,]", "", texto or "")
    if not limpio:
        raise ValueError(f"no hay numero en {texto!r}")
    if "," in limpio:
        # la coma es el separador decimal; el punto es de miles
        limpio = limpio.replace(".", "").replace(",", ".")
    return float(limpio)


def _pedir(metodo: str, url: str, **kwargs) -> requests.Response:
    """Request con reintentos y backoff simple."""
    ultimo: Exception | None = None
    for intento in range(1, REINTENTOS + 1):
        try:
            resp = requests.request(metodo, url, timeout=TIMEOUT, **kwargs)
            resp.raise_for_status()
            return resp
        except Exception as exc:  # noqa: BLE001 - se reintenta cualquier fallo de red
            ultimo = exc
            print(f"  intento {intento}/{REINTENTOS} fallo: {exc}", file=sys.stderr)
            if intento < REINTENTOS:
                import time

                time.sleep(2 * intento)
    raise RuntimeError(f"{metodo} {url} fallo tras {REINTENTOS} intentos") from ultimo


# --------------------------------------------------------------------------- #
# BCV
# --------------------------------------------------------------------------- #
def _normaliza(texto: str) -> str:
    sin_tildes = unicodedata.normalize("NFKD", texto)
    sin_tildes = "".join(c for c in sin_tildes if not unicodedata.combining(c))
    return sin_tildes.strip().upper()


def leer_bcv() -> dict:
    """Devuelve {'usd': float, 'eur': float|None, 'fecha_valor': str|None}."""
    print("BCV: descargando", BCV_URL)
    resp = _pedir(
        "GET",
        BCV_URL,
        verify=False,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "es-VE,es;q=0.9"},
    )
    resp.encoding = resp.apparent_encoding or "utf-8"
    sopa = BeautifulSoup(resp.text, "html.parser")

    monedas: dict[str, float] = {}

    # 1) estructura habitual: <div id="dolar"> ... <strong>39,18130000</strong>
    for clave, ident in (("USD", "dolar"), ("EUR", "euro")):
        nodo = sopa.find(id=ident)
        if nodo:
            fuerte = nodo.find("strong")
            if fuerte:
                try:
                    monedas[clave] = _a_decimal(fuerte.get_text())
                except ValueError:
                    pass

    # 2) respaldo: cualquier bloque que contenga el codigo de la moneda
    if "USD" not in monedas or "EUR" not in monedas:
        for bloque in sopa.find_all(["div", "li", "tr"]):
            texto = _normaliza(bloque.get_text(" ", strip=True))
            for clave in ("USD", "EUR"):
                if clave in monedas:
                    continue
                if re.search(rf"\b{clave}\b", texto):
                    m = re.search(r"\d{1,3}(?:\.\d{3})*,\d+", texto)
                    if m:
                        try:
                            monedas[clave] = _a_decimal(m.group())
                        except ValueError:
                            pass

    if "USD" not in monedas:
        raise RuntimeError("no se pudo extraer la tasa USD del BCV")

    # fecha valor publicada por el BCV
    fecha_valor = None
    nodo_fecha = sopa.find("span", class_="date-display-single")
    if nodo_fecha:
        fecha_valor = nodo_fecha.get("content") or nodo_fecha.get_text(strip=True)

    usd = monedas["USD"]
    if not (0.01 < usd < 1_000_000):
        raise RuntimeError(f"tasa BCV fuera de rango razonable: {usd}")

    print(f"BCV: USD={usd} EUR={monedas.get('EUR')} fecha={fecha_valor}")
    return {"usd": usd, "eur": monedas.get("EUR"), "fecha_valor": fecha_valor}


# --------------------------------------------------------------------------- #
# Binance P2P
# --------------------------------------------------------------------------- #
def _anuncios(trade_type: str, filas: int) -> list[dict]:
    cuerpo = {
        "fiat": "VES",
        "page": 1,
        "rows": filas,
        "tradeType": trade_type,  # BUY = compras USDT, SELL = vendes USDT
        "asset": "USDT",
        "countries": [],
        "payTypes": [],
        "proMerchantAds": False,
        "publisherType": None,
        "shieldMerchantAds": False,
    }
    resp = _pedir(
        "POST",
        BINANCE_URL,
        json=cuerpo,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "clienttype": "web",
        },
    )
    datos = resp.json().get("data") or []
    salida = []
    for item in datos:
        adv = item.get("adv") or {}
        comerciante = item.get("advertiser") or {}
        try:
            precio = float(adv["price"])
        except (KeyError, TypeError, ValueError):
            continue
        salida.append(
            {
                "precio": precio,
                "comerciante": comerciante.get("nickName"),
                "min": adv.get("minSingleTransAmount"),
                "max": adv.get("maxSingleTransAmount"),
                "disponible": adv.get("tradableQuantity"),
            }
        )
    return salida


def leer_binance(trade_type: str) -> dict:
    """Promedio de los TOP_N mejores anuncios para el lado indicado."""
    print(f"Binance P2P: consultando {trade_type} USDT/VES")
    # Se piden mas filas de las necesarias por si alguna viene sin precio valido.
    anuncios = _anuncios(trade_type, max(TOP_N * 2, 10))
    if not anuncios:
        raise RuntimeError(f"Binance P2P no devolvio anuncios para {trade_type}")

    # La API ya viene ordenada, pero lo hacemos explicito:
    # comprando USDT interesa el precio mas bajo; vendiendo, el mas alto.
    anuncios.sort(key=lambda a: a["precio"], reverse=(trade_type == "SELL"))
    mejores = anuncios[:TOP_N]

    precios = [a["precio"] for a in mejores]
    promedio = round(sum(precios) / len(precios), 4)
    print(f"Binance P2P {trade_type}: promedio top {len(precios)} = {promedio}")

    return {
        "promedio": promedio,
        "minimo": min(precios),
        "maximo": max(precios),
        "anuncios_usados": len(precios),
        "anuncios": mejores,
    }


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    ahora = datetime.now(timezone.utc)

    bcv = leer_bcv()
    venta = leer_binance("SELL")   # bolivares que recibes por vender 1 USDT
    compra = leer_binance("BUY")   # bolivares que pagas por comprar 1 USDT

    referencia = venta["promedio"]
    brecha = round((referencia / bcv["usd"] - 1) * 100, 2) if bcv["usd"] else None

    resultado = {
        "actualizado_utc": ahora.isoformat(timespec="seconds"),
        "actualizado_ve": ahora.astimezone(VE_TZ).isoformat(timespec="seconds"),
        "bcv": bcv,
        "binance_p2p": {
            "asset": "USDT",
            "fiat": "VES",
            "top_n": TOP_N,
            "referencia": referencia,
            "venta": venta,
            "compra": compra,
        },
        "brecha_pct": brecha,
    }

    SALIDA.parent.mkdir(parents=True, exist_ok=True)
    SALIDA.write_text(
        json.dumps(resultado, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"OK -> {SALIDA} (BCV {bcv['usd']} | P2P {referencia} | brecha {brecha}%)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
