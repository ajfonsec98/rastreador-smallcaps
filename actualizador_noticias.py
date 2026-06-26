"""
Actualizador de noticias de small caps — usando Finnhub
---------------------------------------------------------
Usa la API gratuita de Finnhub (60 llamadas/minuto, sin costo)
para traer noticias reales de empresas: fusiones, cambios
ejecutivos, resultados, acuerdos, FDA, etc.

Cómo correrlo:
    python actualizador_noticias.py

Se queda corriendo solo, actualizando cada 5 minutos.
Para detener: Ctrl+C en la ventana negra.
"""

import json
import os
import time
from datetime import datetime, timedelta

import requests
from deep_translator import GoogleTranslator

# -----------------------------------------------------------
# CONFIGURACIÓN — pega aquí tu clave de Finnhub (gratis)
# -----------------------------------------------------------
CLAVE_FINNHUB = "d8u4kf9r01qinhufosi0d8u4kf9r01qinhufosig"
INTERVALO_MINUTOS = 5
RUTA_SALIDA = "noticias_smallcaps.json"
MAXIMO_NOTICIAS = 30

ENCABEZADOS = {
    "X-Finnhub-Token": CLAVE_FINNHUB,
    "Content-Type": "application/json",
}

ENCABEZADOS_NAVEGADOR = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

# Categorías de noticias corporativas que más interesan
CATEGORIAS_FINNHUB = [
    "merger",
    "analyst-rating",
    "insider-trading",
    "earnings",
    "ipo",
    "clinical-trials",
]

MESES_ES = {
    1: "ene", 2: "feb", 3: "mar", 4: "abr", 5: "may", 6: "jun",
    7: "jul", 8: "ago", 9: "sep", 10: "oct", 11: "nov", 12: "dic",
}

# Emojis por categoría para el panel
EMOJI_CATEGORIA = {
    "merger":          "🤝 Fusión/Adquisición",
    "analyst-rating":  "🏦 Calificación analista",
    "insider-trading": "📈 Insider trading",
    "earnings":        "📊 Resultados",
    "ipo":             "🚀 IPO / Salida a bolsa",
    "clinical-trials": "💊 Ensayos clínicos",
    "general":         "📋 Noticia general",
}

# Instancia del traductor — se reutiliza en cada llamada
_traductor = GoogleTranslator(source="en", target="es")


def traducir_al_espanol(texto):
    """
    Traduce un texto al español usando deep-translator (Google Translate).
    Si falla, devuelve el texto original sin interrumpir nada.
    """
    if not texto or not texto.strip():
        return texto
    try:
        return _traductor.translate(texto.strip()) or texto
    except Exception:
        return texto


def formatear_fecha(timestamp_unix):
    """Convierte timestamp Unix a fecha legible en español."""
    try:
        dt = datetime.fromtimestamp(timestamp_unix)
        return f"{dt.day} {MESES_ES.get(dt.month,'')} {dt.year} {dt.hour:02d}:{dt.minute:02d}"
    except Exception:
        return ""


def es_reciente(timestamp_unix, horas=48):
    """Devuelve True si la noticia tiene menos de N horas."""
    try:
        dt = datetime.fromtimestamp(timestamp_unix)
        return datetime.now() - dt < timedelta(hours=horas)
    except Exception:
        return False


def buscar_noticias_generales():
    """Busca noticias generales del mercado en Finnhub."""
    url = "https://finnhub.io/api/v1/news"
    try:
        resp = requests.get(
            url,
            headers=ENCABEZADOS,
            params={"category": "general", "minId": 0},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json() or []
    except Exception as e:
        print(f"  Error noticias generales: {e}")
        return []


def buscar_noticias_empresa(ticker):
    """Busca noticias recientes de una empresa específica."""
    hoy = datetime.now().strftime("%Y-%m-%d")
    hace_7_dias = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    url = "https://finnhub.io/api/v1/company-news"
    try:
        resp = requests.get(
            url,
            headers=ENCABEZADOS,
            params={"symbol": ticker, "from": hace_7_dias, "to": hoy},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json() or []
    except Exception as e:
        print(f"  Error noticias {ticker}: {e}")
        return []


def actualizar_noticias(tickers_verificados=None):
    """
    Busca noticias en Finnhub y guarda las más relevantes.
    Si se pasan tickers verificados del día, busca también
    noticias específicas de esas empresas.
    """
    print(f"\n[{datetime.now().strftime('%H:%M')}] Buscando noticias en Finnhub...")
    noticias = []

    # Noticias generales del mercado
    generales = buscar_noticias_generales()
    for n in generales:
        if not es_reciente(n.get("datetime", 0), horas=48):
            continue
        categoria = n.get("category", "general")
        titulo_es = traducir_al_espanol(n.get("headline", "").strip())
        time.sleep(0.3)
        noticias.append({
            "etiqueta": EMOJI_CATEGORIA.get(categoria, "📋 " + categoria),
            "ticker": n.get("related", "").split(",")[0].strip() or "MERCADO",
            "texto": titulo_es,
            "fuente": n.get("source", ""),
            "fecha": formatear_fecha(n.get("datetime", 0)),
            "enlace": n.get("url", ""),
        })
        if len(noticias) >= 15:
            break

    time.sleep(1)

    # Noticias específicas de los tickers verificados hoy
    if tickers_verificados:
        for ticker in tickers_verificados[:5]:  # máximo 5 tickers
            empresa_noticias = buscar_noticias_empresa(ticker)
            for n in empresa_noticias[:3]:
                if not es_reciente(n.get("datetime", 0), horas=72):
                    continue
                titulo_es = traducir_al_espanol(n.get("headline", "").strip())
                time.sleep(0.3)
                noticias.append({
                    "etiqueta": f"🔔 {ticker}",
                    "ticker": ticker,
                    "texto": titulo_es,
                    "fuente": n.get("source", ""),
                    "fecha": formatear_fecha(n.get("datetime", 0)),
                    "enlace": n.get("url", ""),
                })
            time.sleep(0.5)

    # Guardar solo las primeras MAXIMO_NOTICIAS
    noticias = [n for n in noticias if n["texto"]][:MAXIMO_NOTICIAS]

    with open(RUTA_SALIDA, "w", encoding="utf-8") as f:
        json.dump(
            {
                "ultima_actualizacion": datetime.now().strftime("%d %b %Y %H:%M"),
                "noticias": noticias,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )
    print(f"  → {len(noticias)} noticias guardadas en {RUTA_SALIDA}")


# -----------------------------------------------------------
# Intentar leer los tickers verificados del día desde
# alertas_smallcaps.json, para buscar noticias específicas
# -----------------------------------------------------------
def leer_tickers_verificados():
    try:
        with open("alertas_smallcaps.json", encoding="utf-8") as f:
            datos = json.load(f)
        return [a["ticker"] for a in datos.get("alertas_verificadas", [])]
    except Exception:
        return []


# -----------------------------------------------------------
# BUCLE PRINCIPAL — corre para siempre, cada 5 minutos
# -----------------------------------------------------------
if __name__ == "__main__":
    if CLAVE_FINNHUB == "PEGA_AQUI_TU_CLAVE_FINNHUB":
        print("ERROR: Pega tu clave de Finnhub en la variable CLAVE_FINNHUB")
        exit(1)

    print("Actualizador de noticias Finnhub iniciado.")
    print(f"Actualizará cada {INTERVALO_MINUTOS} minutos.")
    print("Para detener: Ctrl+C\n")

    while True:
        try:
            tickers = leer_tickers_verificados()
            actualizar_noticias(tickers)
        except Exception as e:
            print(f"  Error inesperado: {e}")
        print(f"  Próxima actualización en {INTERVALO_MINUTOS} minutos...")
        time.sleep(INTERVALO_MINUTOS * 60)
