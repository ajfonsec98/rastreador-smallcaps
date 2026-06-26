"""
Rastreador de movimiento inusual en acciones small-cap
--------------------------------------------------------
Este script hace todo automáticamente:

1. Descarga la lista de todas las acciones de EE.UU. (con su capitalización
   de mercado) desde el buscador público de Nasdaq, y se queda solo con
   las que están en el rango de "small cap".
2. Revisa el volumen y el precio de cada una (en paralelo, para que sea
   rápido), y marca las que se están moviendo de forma inusual.
3. Verifica cada alerta contra la SEC: solo cuenta compras reales de
   insiders o eventos corporativos de alta señal (8-K).
4. (Opcional) Genera una frase explicando qué pasó, usando IA — solo
   para las pocas alertas finales, así que el costo es mínimo.
5. Guarda todo en un archivo (alertas_smallcaps.json), con la hora
   exacta de la revisión.
6. Genera un reporte visual en HTML (oscuro, estilo terminal de trading)
   y lo abre solo en tu navegador apenas termina — el "pop-up" final.

Requisitos (se instalan una sola vez con):
    pip install requests pandas yfinance

El paso 4 (narrativa con IA) es opcional: necesita una clave de API de
Anthropic (se consigue en console.anthropic.com). Si no la configuras,
el script sigue funcionando igual, solo sin esa frase extra.
"""

import json
import os
import re
import time
import webbrowser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import quote

import pandas as pd
import requests
import yfinance as yf

# -----------------------------------------------------------
# PASO 1: Descargar la lista de small caps (gratis, automático)
# -----------------------------------------------------------

URL_LISTA_ACCIONES = (
    "https://api.nasdaq.com/api/screener/stocks"
    "?tableonly=true&limit=10000&offset=0&download=true"
)

# Algunos sitios bloquean pedidos que no parecen venir de un navegador real.
# Con esto, le decimos al sitio que somos un navegador normal de Windows.
ENCABEZADOS_NAVEGADOR = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

# Rango de capitalización de mercado (en dólares) que consideramos "small cap".
SMALLCAP_MINIMO = 300_000_000
SMALLCAP_MAXIMO = 2_000_000_000


def obtener_lista_smallcaps():
    """Descarga todas las acciones de EE.UU. y filtra las que son small-cap."""
    respuesta = requests.get(
        URL_LISTA_ACCIONES, headers=ENCABEZADOS_NAVEGADOR, timeout=30
    )
    respuesta.raise_for_status()

    datos = respuesta.json()
    filas = datos["data"]["rows"]

    df = pd.DataFrame(filas)

    df["marketCap"] = pd.to_numeric(df["marketCap"], errors="coerce")
    df = df.dropna(subset=["marketCap", "symbol"])

    df = df[(df["marketCap"] >= SMALLCAP_MINIMO) & (df["marketCap"] <= SMALLCAP_MAXIMO)]

    df = df.rename(
        columns={
            "symbol": "Ticker",
            "name": "Name",
            "sector": "Sector",
            "industry": "Rubro",
            "exchange": "Bolsa",
            "country": "Pais",
        }
    )
    if "Bolsa" not in df.columns:
        df["Bolsa"] = "No disponible"
    if "Pais" not in df.columns:
        df["Pais"] = ""
    # Nasdaq deja el país en blanco cuando la empresa es de EE.UU.
    df["Pais"] = df["Pais"].fillna("").replace("", "United States")
    df = df[["Ticker", "Name", "Sector", "Rubro", "Bolsa", "Pais"]].dropna(subset=["Ticker"])
    df = df[df["Ticker"].str.match(r"^[A-Z.]{1,6}$", na=False)]

    return df.reset_index(drop=True)


# -----------------------------------------------------------
# PASO 2: Revisar el volumen y precio de cada acción (en paralelo)
# -----------------------------------------------------------
# IMPORTANTE: lo que hace esto lento no es la "potencia" de tu computadora,
# es la espera de cada respuesta por internet. Por eso revisamos varias
# acciones AL MISMO TIEMPO en vez de una por una — como tener 15
# pestañas cargando a la vez en lugar de abrir y cerrar una por una.
MAX_CONSULTAS_EN_PARALELO = 15


def revisar_movimiento_inusual(ticker, dias_promedio=20, umbral_volumen=3.0):
    """
    Compara el volumen y precio de la última sesión de bolsa disponible
    (que puede ser hoy, o el último día hábil si hoy es fin de semana o
    feriado) contra el promedio reciente.
    Devuelve None si no hay nada raro, o un diccionario con los datos
    si el movimiento es inusual (volumen 3x o más sobre lo normal).
    """
    try:
        accion = yf.Ticker(ticker)
        historial = accion.history(period=f"{dias_promedio + 5}d")

        if len(historial) < dias_promedio + 1:
            return None

        # Fecha real de la última sesión con datos (no necesariamente "hoy").
        fecha_sesion = historial.index[-1].strftime("%Y-%m-%d")

        # Estos son totales de TODO el día de bolsa (no de un minuto exacto;
        # con datos diarios gratis no se puede saber la hora precisa del pico).
        volumen_sesion = historial["Volume"].iloc[-1]
        volumen_promedio = historial["Volume"].iloc[-(dias_promedio + 1):-1].mean()

        precio_sesion = historial["Close"].iloc[-1]
        precio_sesion_anterior = historial["Close"].iloc[-2]
        cambio_precio_pct = (
            (precio_sesion - precio_sesion_anterior) / precio_sesion_anterior
        ) * 100

        if volumen_promedio == 0:
            return None

        ratio_volumen = volumen_sesion / volumen_promedio

        if ratio_volumen >= umbral_volumen:
            return {
                "ticker": ticker,
                "fecha_sesion": fecha_sesion,
                "precio": round(float(precio_sesion), 2),
                "cambio_precio_pct": round(float(cambio_precio_pct), 2),
                "volumen_sesion": int(volumen_sesion),
                "volumen_promedio": int(volumen_promedio),
                "ratio_volumen": round(float(ratio_volumen), 2),
            }
        return None

    except Exception:
        return None


def revisar_lista_en_paralelo(lista):
    """Revisa todas las acciones de la lista al mismo tiempo (en paralelo)."""
    alertas = []
    fecha_sesion_detectada = None
    total = len(lista)
    completados = 0

    with ThreadPoolExecutor(max_workers=MAX_CONSULTAS_EN_PARALELO) as ejecutor:
        futuros = {
            ejecutor.submit(revisar_movimiento_inusual, fila["Ticker"]): fila
            for _, fila in lista.iterrows()
        }
        for futuro in as_completed(futuros):
            fila = futuros[futuro]
            completados += 1
            print(f"Revisando... ({completados}/{total})", end="\r")
            resultado = futuro.result()
            if resultado:
                resultado["nombre"] = fila["Name"]
                resultado["sector"] = fila["Sector"]
                resultado["rubro"] = fila["Rubro"]
                resultado["bolsa"] = fila["Bolsa"]
                resultado["pais"] = fila["Pais"]
                alertas.append(resultado)
                if fecha_sesion_detectada is None:
                    fecha_sesion_detectada = resultado["fecha_sesion"]

    return alertas, fecha_sesion_detectada


# -----------------------------------------------------------
# PASO 3: Verificar las alertas contra la SEC (gratis, oficial)
# -----------------------------------------------------------
# La SEC (el regulador de la bolsa en EE.UU.) publica gratis y en tiempo
# real todo lo que las empresas le reportan: eventos importantes
# (formulario "8-K") y compras/ventas de insiders (formulario "4").

# La SEC pide que cada programa que use su sitio se identifique con un
# nombre y un correo de contacto (es su política oficial). Puedes
# cambiar esto por tu propio correo si quieres.
ENCABEZADOS_SEC = {"User-Agent": "RastreadorSmallCaps contacto@ejemplo.com"}

URL_MAPA_TICKERS_SEC = "https://www.sec.gov/files/company_tickers.json"

# Para no saturar el servidor de la SEC, revisamos menos acciones a la vez
# aquí que en el Paso 2 (la SEC pide un uso razonable de su sitio).
MAX_CONSULTAS_EN_PARALELO_SEC = 6

# Tipos de evento dentro de un 8-K que de verdad suelen mover el precio
# de una acción. Dejamos fuera lo puramente administrativo.
ITEMS_8K_ALTA_SENAL = {
    "1.01",  # Entrada a un acuerdo importante
    "1.02",  # Terminación de un acuerdo importante
    "2.01",  # Adquisición o venta de activos ya completada
    "3.02",  # Venta de acciones no registrada
    "5.01",  # Cambio de quién controla la empresa
}

DESCRIPCION_ITEMS_8K = {
    "1.01": "acuerdo importante firmado",
    "1.02": "acuerdo importante terminado",
    "2.01": "compra/venta de activos completada",
    "3.02": "venta de acciones fuera del proceso normal",
    "5.01": "cambio de quién controla la empresa",
}


def obtener_mapa_tickers_a_cik():
    """Descarga el mapa oficial de la SEC: ticker -> número de identificación (CIK)."""
    respuesta = requests.get(URL_MAPA_TICKERS_SEC, headers=ENCABEZADOS_SEC, timeout=30)
    respuesta.raise_for_status()
    datos = respuesta.json()
    return {entrada["ticker"]: entrada["cik_str"] for entrada in datos.values()}


def construir_url_documento(cik, numero_acceso, documento):
    numero_acceso_sin_guiones = numero_acceso.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
        f"{numero_acceso_sin_guiones}/{documento}"
    )


def es_compra_real_de_insider(url_documento):
    """
    Abre el Formulario 4 real y revisa si alguna transacción fue una COMPRA
    hecha con dinero propio del insider (código "P"), y no una venta ni
    acciones recibidas como parte de su sueldo o bono.
    """
    try:
        respuesta = requests.get(url_documento, headers=ENCABEZADOS_SEC, timeout=30)
        respuesta.raise_for_status()
        return "<transactionCode>P</transactionCode>" in respuesta.text
    except Exception:
        return False


def verificar_con_sec(cik, dias_recientes=3):
    """
    Revisa si la empresa reportó algo realmente importante a la SEC en los
    últimos días. Devuelve (razones, fuentes): una lista de razones en
    texto, y una lista de enlaces a los documentos reales (para que
    puedas verificarlos tú mismo, o para generar la narrativa con IA).
    """
    url = f"https://data.sec.gov/submissions/CIK{str(cik).zfill(10)}.json"

    try:
        respuesta = requests.get(url, headers=ENCABEZADOS_SEC, timeout=30)
        respuesta.raise_for_status()
        datos = respuesta.json()
    except Exception:
        return [], []

    recientes = datos.get("filings", {}).get("recent", {})
    formularios = recientes.get("form", [])
    fechas = recientes.get("filingDate", [])
    items_8k = recientes.get("items", [""] * len(formularios))
    numeros_acceso = recientes.get("accessionNumber", [])
    documentos = recientes.get("primaryDocument", [])

    hoy = datetime.now().date()
    razones = []
    fuentes = []

    for i, (formulario, fecha_texto) in enumerate(zip(formularios, fechas)):
        try:
            fecha = datetime.strptime(fecha_texto, "%Y-%m-%d").date()
        except ValueError:
            continue

        if (hoy - fecha).days > dias_recientes:
            continue

        numero_acceso = numeros_acceso[i] if i < len(numeros_acceso) else None
        documento = documentos[i] if i < len(documentos) else None
        url_documento = (
            construir_url_documento(cik, numero_acceso, documento)
            if numero_acceso and documento
            else None
        )

        if formulario == "8-K":
            items_de_este_reporte = set(
                items_8k[i].split(",") if i < len(items_8k) and items_8k[i] else []
            )
            coincidencias = items_de_este_reporte & ITEMS_8K_ALTA_SENAL
            for codigo in sorted(coincidencias):
                descripcion = DESCRIPCION_ITEMS_8K.get(codigo, "evento importante")
                razones.append(f"8-K Item {codigo} ({descripcion}), {fecha_texto}")
                if url_documento:
                    fuentes.append(url_documento)

        elif formulario == "4" and url_documento:
            if es_compra_real_de_insider(url_documento):
                razones.append(
                    f"Un insider compró acciones con su propio dinero (Formulario 4, {fecha_texto})"
                )
                fuentes.append(url_documento)
            time.sleep(0.1)

    razones_unicas = list(dict.fromkeys(razones))
    fuentes_unicas = list(dict.fromkeys(fuentes))
    return razones_unicas, fuentes_unicas


def verificar_alertas(alertas):
    """Revisa cada alerta de volumen contra la SEC (en paralelo) y separa verificadas de no verificadas."""
    print("\nDescargando el directorio de empresas de la SEC...")
    mapa_cik = obtener_mapa_tickers_a_cik()

    verificadas = []
    descartadas = []
    total = len(alertas)
    completados = 0

    def _verificar_una(alerta):
        cik = mapa_cik.get(alerta["ticker"])
        if not cik:
            return alerta, [], []
        razones, fuentes = verificar_con_sec(cik)
        return alerta, razones, fuentes

    with ThreadPoolExecutor(max_workers=MAX_CONSULTAS_EN_PARALELO_SEC) as ejecutor:
        futuros = [ejecutor.submit(_verificar_una, alerta) for alerta in alertas]
        for futuro in as_completed(futuros):
            completados += 1
            print(f"Verificando con la SEC... ({completados}/{total})", end="\r")
            alerta, razones, fuentes = futuro.result()
            if razones:
                alerta["razones_verificacion"] = razones
                alerta["fuentes_sec"] = fuentes
                verificadas.append(alerta)
            else:
                descartadas.append(alerta)

    return verificadas, descartadas


# -----------------------------------------------------------
# PASO 4: Narrativa con IA (OPCIONAL — esto sí tiene un costo mínimo)
# -----------------------------------------------------------
# Para usar esto, necesitas una clave de API de Anthropic.
# Se consigue en https://console.anthropic.com -> API Keys.
# Pega tu clave abajo. Si la dejas como está, este paso se omite solo
# y el script sigue funcionando igual de bien sin él.
CLAVE_API_ANTHROPIC = "PEGA_AQUI_TU_CLAVE"
MODELO_IA = "claude-haiku-4-5-20251001"


def obtener_texto_documento(url, limite_caracteres=6000):
    """Descarga un documento de la SEC y deja solo el texto (sin HTML)."""
    try:
        respuesta = requests.get(url, headers=ENCABEZADOS_SEC, timeout=30)
        respuesta.raise_for_status()
        texto_limpio = re.sub(r"<[^>]+>", " ", respuesta.text)
        texto_limpio = re.sub(r"\s+", " ", texto_limpio).strip()
        return texto_limpio[:limite_caracteres]
    except Exception:
        return ""


def generar_narrativa(alerta):
    """Le pide a la IA que resuma en una frase simple qué pasó, usando el documento real como fuente."""
    if not CLAVE_API_ANTHROPIC or CLAVE_API_ANTHROPIC == "PEGA_AQUI_TU_CLAVE":
        return None

    fuentes = alerta.get("fuentes_sec", [])
    if not fuentes:
        return None

    texto_documento = obtener_texto_documento(fuentes[0])
    if not texto_documento:
        return None

    pregunta = (
        f"Este es el texto de un reporte oficial ante la SEC de la empresa "
        f"{alerta.get('nombre', alerta['ticker'])} ({alerta['ticker']}):\n\n"
        f"{texto_documento}\n\n"
        "Resume en una sola frase, en español sencillo y sin tecnicismos "
        "(máximo 30 palabras), qué pasó y por qué le importaría a alguien "
        "que sigue esta acción. Responde solo con la frase, nada más."
    )

    try:
        respuesta = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": CLAVE_API_ANTHROPIC,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODELO_IA,
                "max_tokens": 150,
                "messages": [{"role": "user", "content": pregunta}],
            },
            timeout=30,
        )
        respuesta.raise_for_status()
        datos = respuesta.json()
        return datos["content"][0]["text"].strip()
    except Exception as error:
        print(f"  No se pudo generar narrativa para {alerta['ticker']}: {error}")
        return None


# -----------------------------------------------------------
# PASO 6: Reporte visual en HTML (el "pop-up" estilo terminal)
# -----------------------------------------------------------

# Bandera de cada país, para la tira de rueda bursátil. Si un país no está
# en esta lista, se usa una bandera genérica en su lugar.
# Emoji por sector, para el tooltip de la rueda bursátil (de qué se trata
# la empresa, a simple vista, al pasar el mouse).
SECTOR_A_EMOJI = {
    "health care": "💊", "healthcare": "💊", "technology": "💻",
    "finance": "💰", "consumer discretionary": "🛍️",
    "consumer staples": "🛒", "consumer services": "🛍️",
    "industrials": "🏭", "energy": "⚡", "basic materials": "⛏️",
    "real estate": "🏢", "utilities": "💡", "telecommunications": "📡",
    "miscellaneous": "📦",
}


def obtener_emoji_sector(sector):
    if not sector:
        return "📦"
    return SECTOR_A_EMOJI.get(sector.strip().lower(), "📦")


PAIS_A_BANDERA = {
    "united states": "🇺🇸", "usa": "🇺🇸", "u.s.": "🇺🇸",
    "china": "🇨🇳", "canada": "🇨🇦", "israel": "🇮🇱",
    "united kingdom": "🇬🇧", "bermuda": "🇧🇲", "cayman islands": "🇰🇾",
    "ireland": "🇮🇪", "switzerland": "🇨🇭", "germany": "🇩🇪",
    "france": "🇫🇷", "japan": "🇯🇵", "south korea": "🇰🇷",
    "india": "🇮🇳", "brazil": "🇧🇷", "australia": "🇦🇺",
    "singapore": "🇸🇬", "hong kong": "🇭🇰", "netherlands": "🇳🇱",
    "luxembourg": "🇱🇺", "peru": "🇵🇪", "mexico": "🇲🇽",
    "chile": "🇨🇱", "colombia": "🇨🇴", "argentina": "🇦🇷",
    "spain": "🇪🇸", "italy": "🇮🇹", "belgium": "🇧🇪",
    "denmark": "🇩🇰", "sweden": "🇸🇪", "norway": "🇳🇴",
    "finland": "🇫🇮", "taiwan": "🇹🇼", "indonesia": "🇮🇩",
    "south africa": "🇿🇦", "monaco": "🇲🇨", "greece": "🇬🇷",
    "turkey": "🇹🇷", "united arab emirates": "🇦🇪", "panama": "🇵🇦",
    "jersey": "🇯🇪", "guernsey": "🇬🇬", "isle of man": "🇮🇲",
    "marshall islands": "🇲🇭", "british virgin islands": "🇻🇬",
}


def obtener_bandera(pais):
    if not pais:
        return "🏳️"
    return PAIS_A_BANDERA.get(pais.strip().lower(), "🏳️")


def generar_dashboard_html(salida, ruta_salida="dashboard_smallcaps.html"):
    """
    Genera un reporte visual oscuro, estilo terminal de trading, y lo abre
    automáticamente en tu navegador. No necesita instalar nada extra.
    """
    verificadas = salida.get("alertas_verificadas", [])
    hay_alertas = len(verificadas) > 0

    # Si hoy la bolsa no abrió (fin de semana o feriado), lo dejamos bien
    # claro arriba Y abajo, para que no haya duda de a qué día corresponde
    # todo el análisis.
    fecha_mercado = salida.get("fecha_datos_mercado", "")
    hoy_texto = datetime.now().strftime("%Y-%m-%d")
    mercado_cerrado_hoy = fecha_mercado and fecha_mercado != hoy_texto

    if hay_alertas:
        banner_color = "#3fb950"
        banner_bg = "rgba(63,185,80,0.12)"
        banner_texto = f"{len(verificadas)} ALERTA{'S' if len(verificadas) != 1 else ''} VERIFICADA{'S' if len(verificadas) != 1 else ''}"
        if mercado_cerrado_hoy:
            banner_texto += f" — ÚLTIMA SESIÓN: {fecha_mercado}"
    else:
        banner_color = "#d29922"
        banner_bg = "rgba(210,153,34,0.12)"
        if mercado_cerrado_hoy:
            banner_texto = f"SIN ALERTAS — ÚLTIMA SESIÓN: {fecha_mercado}"
        else:
            banner_texto = "SIN ALERTAS VERIFICADAS HOY — ESPERANDO"

    if mercado_cerrado_hoy:
        nota_mercado = (
            f"⚠ Hoy la bolsa no operó (fin de semana o feriado). Todo este "
            f"análisis — las {salida.get('total_acciones_revisadas',0):,} acciones revisadas, "
            f"las {salida.get('total_alertas_volumen',0)} con volumen raro, y las "
            f"{salida.get('total_verificadas',0)} verificadas — corresponde al "
            f"último día que SÍ operó: {fecha_mercado}."
        )
        nota_color = "#d29922"
        nota_bg = "rgba(210,153,34,0.12)"
    else:
        nota_mercado = f"Este análisis corresponde a la sesión de bolsa de hoy: {fecha_mercado}."
        nota_color = "#8b949e"
        nota_bg = "#11161d"

    ultimo_hallazgo = salida.get("ultimo_hallazgo")
    usando_hallazgo_anterior = not verificadas and ultimo_hallazgo
    alertas_a_mostrar = verificadas if verificadas else (ultimo_hallazgo["alertas"] if usando_hallazgo_anterior else [])

    tarjetas_html = ""
    if usando_hallazgo_anterior:
        tarjetas_html += (
            f'<p style="font-size:11px; color:#d29922; margin:0 0 12px; font-weight:bold;">'
            f'⚠ No hay nada nuevo verificado desde entonces. Mostrando el último '
            f'hallazgo real que sí hubo, del {ultimo_hallazgo.get("fecha","")}:</p>'
        )
    for a in alertas_a_mostrar:
        cambio = a.get("cambio_precio_pct", 0)
        color_cambio = "#3fb950" if cambio >= 0 else "#f85149"
        signo = "+" if cambio >= 0 else ""
        razones = a.get("razones_verificacion", [])
        razon_principal = razones[0] if razones else ""
        narrativa = a.get("narrativa")
        fuentes = a.get("fuentes_sec", [])
        enlace = fuentes[0] if fuentes else None

        narrativa_html = ""
        if narrativa:
            narrativa_html = (
                f'<p style="margin:10px 0 0; font-size:13px; color:#c9d1d9; '
                f'line-height:1.5;">{narrativa}</p>'
            )

        enlace_html = ""
        if enlace:
            enlace_html = (
                f'<a href="{enlace}" target="_blank" style="font-size:11px; '
                f'color:#58a6ff; text-decoration:none;">Ver documento original en la SEC &rarr;</a>'
            )

        menciones_html = ""
        menciones = a.get("menciones_noticias", [])
        if menciones:
            filas_menciones = ""
            for m in menciones:
                fecha_m = m.get("fecha", "")
                texto_mostrado = m.get("resumen_verificado") or m["titulo"]
                filas_menciones += (
                    f'<a href="{m["enlace"]}" target="_blank" style="display:block; '
                    f'text-decoration:none; padding:6px 0; border-top:1px solid #21262d;">'
                    f'<div style="display:flex; justify-content:space-between; font-size:10px; '
                    f'color:#8b949e; margin-bottom:2px;">'
                    f'<span style="font-weight:bold; color:#58a6ff;">{m["fuente"]}</span>'
                    f'<span>{fecha_m}</span>'
                    f'</div>'
                    f'<span style="font-size:11px; color:#c9d1d9; line-height:1.4;">{texto_mostrado}</span>'
                    f'</a>'
                )
            menciones_html = (
                f'<div style="margin-top:10px;">'
                f'<p style="font-size:10px; color:#8b949e; margin:0 0 2px; letter-spacing:0.5px;">MENCIONES VERIFICADAS EN PRENSA/BLOGS</p>'
                f'{filas_menciones}'
                f'</div>'
            )

        tarjetas_html += f"""
        <div style="background:#11161d; border:1px solid #21262d; border-radius:6px; padding:16px 18px; margin-bottom:12px;">
          <div style="display:flex; justify-content:space-between; align-items:baseline; margin-bottom:6px;">
            <span style="font-size:18px; font-weight:bold; color:#e6edf3; letter-spacing:0.5px;">{a['ticker']}</span>
            <div style="text-align:right;">
              <span style="font-size:16px; font-weight:bold; color:{color_cambio};">{signo}{cambio}%</span>
              <span style="font-size:12px; color:#8b949e; margin-left:10px;">{a['ratio_volumen']}x volumen normal</span>
            </div>
          </div>
          <p style="margin:2px 0 8px; font-size:12px; color:#8b949e;">{a.get('nombre','')} &middot; {a.get('rubro', a.get('sector',''))}</p>
          <div style="background:#0a0e14; border-left:3px solid {banner_color}; padding:8px 12px; border-radius:4px;">
            <span style="font-size:12px; color:#c9d1d9;">{razon_principal}</span>
          </div>
          {narrativa_html}
          <div style="margin-top:10px;">{enlace_html}</div>
          {menciones_html}
        </div>
        """

    if not tarjetas_html:
        tarjetas_html = '<p style="color:#8b949e; font-size:13px;">Ninguna acción ha tenido una razón oficial confirmada todavía.</p>'

    minimo_m = salida.get("smallcap_minimo", 300_000_000) // 1_000_000
    maximo_m = salida.get("smallcap_maximo", 2_000_000_000) // 1_000_000

    # Tira de rueda bursátil: usamos TODAS las acciones con volumen raro
    # (verificadas + descartadas), no solo las verificadas, para que se
    # vea con movimiento de verdad, como una pantalla de bolsa. En vez de
    # banderas, el símbolo de la empresa es el protagonista.
    todas_las_acciones = verificadas + salida.get("alertas_descartadas_sin_verificar", [])
    item_tira_html = ""
    for a in todas_las_acciones:
        cambio = a.get("cambio_precio_pct", 0)
        color = "#3fb950" if cambio >= 0 else "#f85149"
        signo = "+" if cambio >= 0 else ""
        emoji_sector = obtener_emoji_sector(a.get("sector", ""))
        nombre_empresa = a.get("nombre", "") or a["ticker"]
        item_tira_html += (
            f'<span class="tira-item" style="display:inline-block; padding:0 26px; font-size:14px; cursor:default;">'
            f'<span class="tira-normal">'
            f'<span style="color:#e6edf3; font-weight:bold; letter-spacing:0.5px;">{a["ticker"]}</span> '
            f'<span style="color:{color}; margin-left:4px;">{signo}{cambio}%</span>'
            f'</span>'
            f'<span class="tira-hover" style="display:none; color:#c9d1d9;">{emoji_sector} {nombre_empresa}</span>'
            f'</span>'
        )
    if not item_tira_html:
        item_tira_html = '<span style="padding:0 22px; font-size:13px; color:#8b949e;">Sin movimiento inusual hoy</span>'

    # Velocidad constante sin importar cuántas acciones haya: más acciones
    # = tira más larga = más segundos, para que siempre se sienta igual
    # de pausada (antes, con más acciones se veía más rápida).
    duracion_tira = max(140, len(todas_las_acciones) * 3)

    # Datos para el buscador con autocompletado (solo busca dentro de las
    # acciones que tuvieron volumen raro hoy, no en las 1,555 small caps
    # completas — eso requeriría guardar una lista mucho más grande).
    empresas_buscador = [
        {"ticker": a["ticker"], "nombre": a.get("nombre", "")}
        for a in todas_las_acciones
    ]
    empresas_buscador_json = json.dumps(empresas_buscador, ensure_ascii=False)

    # Noticias para los popups rotantes — primero las del actualizador
    # en tiempo real (si existe), luego las menciones de alertas del día.
    noticias_popup = []

    ruta_noticias_live = "noticias_smallcaps.json"
    if os.path.exists(ruta_noticias_live):
        try:
            with open(ruta_noticias_live, encoding="utf-8") as f:
                datos_live = json.load(f)
            for n in datos_live.get("noticias", []):
                noticias_popup.append({
                    "ticker": n.get("etiqueta", ""),
                    "fuente": n.get("fuente", ""),
                    "fecha": n.get("fecha", ""),
                    "texto": n.get("texto", ""),
                    "enlace": n.get("enlace", ""),
                })
        except Exception:
            pass

    # Completar con menciones verificadas de alertas del día
    for a in alertas_a_mostrar:
        for m in a.get("menciones_noticias", []):
            resumen = m.get("resumen_verificado") or m.get("titulo", "")
            if resumen:
                noticias_popup.append({
                    "ticker": a["ticker"],
                    "fuente": m.get("fuente", ""),
                    "fecha": m.get("fecha", ""),
                    "texto": resumen,
                    "enlace": m.get("enlace", ""),
                })

    # Si no hay menciones verificadas, usar las razones de la SEC
    if not noticias_popup:
        for a in alertas_a_mostrar:
            razones = a.get("razones_verificacion", [])
            if razones:
                noticias_popup.append({
                    "ticker": a["ticker"],
                    "fuente": "SEC EDGAR",
                    "fecha": a.get("fecha_sesion", ""),
                    "texto": razones[0],
                    "enlace": "",
                })
    noticias_popup_json = json.dumps(noticias_popup, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<title>Rastreador Small Caps - Monitor</title>
<style>
@media (max-width: 980px) {{
  .contenedor-flex {{ flex-direction: column !important; align-items: center !important; }}
  .burbuja-lateral {{ width: 100% !important; margin-top: 20px !important; }}
}}
</style>
</head>
<body style="margin:0; padding:24px; background:#0a0e14; font-family:'Consolas','Courier New',monospace;">
  <div class="contenedor-flex" style="max-width:1040px; margin:0 auto; display:flex; justify-content:center; gap:36px; align-items:flex-start;">

    <div class="burbuja-lateral" style="flex-shrink:0; width:160px; display:flex; flex-direction:column; align-items:center; margin-top:24px;">
      <div style="width:100%; margin-bottom:50px;">
        <input type="text" id="buscador" placeholder="Buscar empresa..." style="width:100%; box-sizing:border-box; background:#11161d; border:1px solid #21262d; border-radius:6px; padding:9px 10px; font-family:'Consolas','Courier New',monospace; font-size:11px; color:#e6edf3; outline:none;">
        <div id="resultados-busqueda" style="margin-top:4px; background:#11161d; border-radius:6px; overflow:hidden;"></div>
      </div>
      <div class="burbuja-flotante-izq" style="width:120px; height:120px; border-radius:50%; border:1.5px solid #58a6ff; display:flex; align-items:center; justify-content:center; background:rgba(88,166,255,0.07); transform:rotate(-4deg);">
        <span style="font-size:22px; font-weight:bold; color:#58a6ff; transform:rotate(4deg); display:inline-block;">{salida.get('total_acciones_revisadas',0):,}</span>
      </div>
      <p style="margin-top:14px; font-size:10px; color:#8b949e; text-align:center; letter-spacing:0.5px; line-height:1.5; transform:rotate(-3deg);">ACCIONES DE EE.UU.<br>COTIZADAS HOY EN<br>ESTE RANGO</p>
    </div>

    <div style="max-width:640px; width:100%;">

      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
        <span style="font-size:15px; font-weight:bold; color:#e6edf3; letter-spacing:0.5px;">RASTREADOR SMALL CAPS — MONITOR EN VIVO</span>
        <span style="font-size:11px; color:#8b949e;">{salida.get('revision_inicio','')} &rarr; {salida.get('revision_fin','')}</span>
      </div>
      <p style="margin:0 0 16px; font-size:11px; color:#8b949e;">Sesion de bolsa: {fecha_mercado}</p>

      <div style="background:{banner_bg}; border:1px solid {banner_color}; border-radius:4px; padding:10px 14px; margin-bottom:18px;">
        <span style="color:{banner_color}; font-weight:bold; font-size:13px; letter-spacing:0.5px;">{banner_texto}</span>
      </div>

      <div style="display:flex; gap:10px; margin-bottom:20px;">
        <div style="flex:1; background:#11161d; border:1px solid #21262d; border-radius:6px; padding:10px 14px;">
          <p style="margin:0 0 2px; font-size:10px; color:#8b949e;">REVISADAS</p>
          <p style="margin:0; font-size:18px; color:#e6edf3; font-weight:bold;">{salida.get('total_acciones_revisadas',0):,}</p>
        </div>
        <div style="flex:1; background:#11161d; border:1px solid #21262d; border-radius:6px; padding:10px 14px;">
          <p style="margin:0 0 2px; font-size:10px; color:#8b949e;">VOLUMEN RARO</p>
          <p style="margin:0; font-size:18px; color:#e6edf3; font-weight:bold;">{salida.get('total_alertas_volumen',0)}</p>
        </div>
        <div style="flex:1; background:#11161d; border:1px solid #21262d; border-radius:6px; padding:10px 14px;">
          <p style="margin:0 0 2px; font-size:10px; color:#8b949e;">VERIFICADAS</p>
          <p style="margin:0; font-size:18px; color:{banner_color}; font-weight:bold;">{salida.get('total_verificadas',0)}</p>
        </div>
      </div>

      {tarjetas_html}

      <div style="margin-top:18px; padding:12px 16px; background:{nota_bg}; border:1px solid {nota_color}; border-left:4px solid {nota_color}; border-radius:4px;">
        <span style="font-size:12px; color:{nota_color}; font-weight:bold; line-height:1.6;">{nota_mercado}</span>
      </div>

      <p style="margin-top:20px; font-size:10px; color:#484f58; text-align:center;">Generado automaticamente &middot; {salida.get('fecha_revision','')}</p>

    </div>

    <div class="burbuja-lateral" style="flex-shrink:0; width:150px; display:flex; flex-direction:column; align-items:center; margin-top:24px;">

      <div id="panel-noticias" style="width:150px; margin-bottom:24px;">
        <p style="font-size:9px; color:#8b949e; letter-spacing:0.5px; margin:0 0 6px; text-align:center;">NOTICIAS DEL DÍA</p>
        <div id="popup-noticia" style="background:#11161d; border:1px solid #21262d; border-left:3px solid #58a6ff; border-radius:6px; padding:10px; font-size:10px; line-height:1.5; color:#c9d1d9; min-height:100px; transition: opacity 0.6s ease;">
          <span id="popup-ticker" style="color:#58a6ff; font-weight:bold; display:block; margin-bottom:4px;"></span>
          <span id="popup-texto"></span>
          <span id="popup-meta" style="display:block; margin-top:6px; color:#8b949e; font-size:9px;"></span>
        </div>
        <div style="display:flex; justify-content:center; gap:4px; margin-top:6px;" id="popup-dots"></div>
      </div>

      <div style="margin-top:120px;">
        <div class="burbuja-flotante-der" style="width:140px; height:140px; border-radius:50%; border:1.5px solid #d29922; display:flex; flex-direction:column; align-items:center; justify-content:center; background:rgba(210,153,34,0.07); transform:rotate(3deg);">
          <span style="font-size:15px; font-weight:bold; color:#d29922; transform:rotate(-3deg);">${minimo_m}M</span>
          <span style="font-size:10px; color:#d29922; margin:2px 0; transform:rotate(-3deg);">a</span>
          <span style="font-size:15px; font-weight:bold; color:#d29922; transform:rotate(-3deg);">${maximo_m}M</span>
        </div>
        <p style="margin-top:14px; font-size:10px; color:#8b949e; text-align:center; letter-spacing:0.5px; line-height:1.5; transform:rotate(2deg);">RANGO CONSIDERADO<br>"SMALL CAP" EN<br>ESTE REPORTE</p>
      </div>
    </div>

  </div>

  <div style="overflow:hidden; white-space:nowrap; margin-top:30px; padding:14px 0; background:#05070a; border-top:1px solid #21262d;">
    <div style="display:inline-block; animation: rueda-bursatil {duracion_tira}s linear infinite;">
      {item_tira_html}{item_tira_html}
    </div>
  </div>
  <style>
    @keyframes rueda-bursatil {{ from {{ transform: translateX(0); }} to {{ transform: translateX(-50%); }} }}
    .tira-item:hover .tira-normal {{ display: none; }}
    .tira-item:hover .tira-hover {{ display: inline-block !important; }}
    @keyframes flotar-izq {{ 0%, 100% {{ transform: rotate(-4deg) translateY(0px); }} 50% {{ transform: rotate(-4deg) translateY(-14px); }} }}
    @keyframes flotar-der {{ 0%, 100% {{ transform: rotate(3deg) translateY(0px); }} 50% {{ transform: rotate(3deg) translateY(12px); }} }}
    .burbuja-flotante-izq {{ animation: flotar-izq 7s ease-in-out infinite; }}
    .burbuja-flotante-der {{ animation: flotar-der 9s ease-in-out infinite; }}
    @media (prefers-reduced-motion: reduce) {{
      .burbuja-flotante-izq, .burbuja-flotante-der {{ animation: none; }}
    }}
  </style>

  <script>
    const noticiasPopup = {noticias_popup_json};
    let popupActual = 0;
    let todasNoticias = [...noticiasPopup];

    function mostrarNoticia(idx) {{
      const popup = document.getElementById('popup-noticia');
      popup.style.opacity = '0';
      setTimeout(() => {{
        const n = todasNoticias[idx];
        if (!n) return;
        const ticker = document.getElementById('popup-ticker');
        ticker.textContent = n.ticker + (n.fuente ? ' — ' + n.fuente : '');
        if (n.enlace) {{
          ticker.style.cursor = 'pointer';
          ticker.onclick = () => window.open(n.enlace, '_blank');
        }}
        document.getElementById('popup-texto').textContent = n.texto;
        document.getElementById('popup-meta').textContent = n.fecha;
        popup.style.opacity = '1';
        const dots = document.getElementById('popup-dots');
        dots.innerHTML = '';
        const total = Math.min(todasNoticias.length, 10);
        for (let i = 0; i < total; i++) {{
          const d = document.createElement('span');
          d.style.cssText = 'width:5px;height:5px;border-radius:50%;background:'
            + (i === idx % total ? '#58a6ff' : '#21262d') + ';display:inline-block;';
          dots.appendChild(d);
        }}
      }}, 400);
    }}

    if (todasNoticias.length > 0) {{
      mostrarNoticia(0);
      setInterval(() => {{
        popupActual = (popupActual + 1) % todasNoticias.length;
        mostrarNoticia(popupActual);
      }}, 5000);
    }} else {{
      document.getElementById('panel-noticias').style.display = 'none';
    }}

    // Cada 5 minutos, recarga las noticias del actualizador en vivo
    // sin recargar la página entera — solo el archivo JSON.
    setInterval(async () => {{
      try {{
        const r = await fetch('noticias_smallcaps.json?t=' + Date.now());
        if (r.ok) {{
          const datos = await r.json();
          const nuevas = datos.noticias.map(n => ({{
            ticker: n.etiqueta || '',
            fuente: n.fuente || '',
            fecha: n.fecha || '',
            texto: n.texto || '',
            enlace: n.enlace || '',
          }}));
          if (nuevas.length > 0) todasNoticias = nuevas;
        }}
      }} catch(e) {{}}
    }}, 5 * 60 * 1000);
    const campoBuscador = document.getElementById('buscador');
    const resultadosBusqueda = document.getElementById('resultados-busqueda');

    campoBuscador.addEventListener('input', function () {{
      const texto = this.value.trim().toLowerCase();
      resultadosBusqueda.innerHTML = '';
      if (!texto) return;
      const coincidencias = empresasBuscador.filter(function (e) {{
        return e.ticker.toLowerCase().includes(texto) || e.nombre.toLowerCase().includes(texto);
      }}).slice(0, 6);
      coincidencias.forEach(function (e) {{
        const fila = document.createElement('div');
        fila.style.cssText = 'padding:7px 9px; font-size:10px; color:#c9d1d9; border-bottom:1px solid #21262d; cursor:default; line-height:1.4;';
        fila.innerHTML = '<span style="color:#58a6ff; font-weight:bold;">' + e.ticker + '</span><br>' + e.nombre;
        resultadosBusqueda.appendChild(fila);
      }});
      if (coincidencias.length === 0) {{
        const vacio = document.createElement('div');
        vacio.style.cssText = 'padding:7px 9px; font-size:10px; color:#8b949e;';
        vacio.textContent = 'Sin resultados hoy';
        resultadosBusqueda.appendChild(vacio);
      }}
    }});
  </script>

</body>
</html>"""

    with open(ruta_salida, "w", encoding="utf-8") as archivo:
        archivo.write(html)

    webbrowser.open("file://" + os.path.abspath(ruta_salida))
    return ruta_salida


# -----------------------------------------------------------
# PASO 6.5: Buscar y VERIFICAR menciones recientes en noticias (gratis)
# -----------------------------------------------------------
# Usa la búsqueda de Google Noticias en formato RSS — no necesita clave,
# no hay que registrar nada de antemano (a diferencia de Google Alerts),
# y por eso sirve para CUALQUIER ticker que aparezca, incluso uno nuevo
# cada día. No es Zacks directamente, pero puede traer artículos donde
# sí se mencione una calificación de analistas, opiniones, etc.
# IMPORTANTE: Google no documenta oficialmente esta puerta, así que la
# usamos con moderación (solo en las pocas alertas verificadas, con
# pausas entre cada una) para no abusar ni arriesgarnos a que nos bloqueen.

MESES_ES = {
    1: "ene", 2: "feb", 3: "mar", 4: "abr", 5: "may", 6: "jun",
    7: "jul", 8: "ago", 9: "sep", 10: "oct", 11: "nov", 12: "dic",
}


def buscar_menciones_noticias(ticker, maximo=3):
    """Busca hasta `maximo` artículos recientes que mencionen este ticker, con fecha."""
    consulta = quote(f"{ticker} stock analyst rating")
    url = f"https://news.google.com/rss/search?q={consulta}&hl=en-US&gl=US&ceid=US:en"

    try:
        respuesta = requests.get(url, headers=ENCABEZADOS_NAVEGADOR, timeout=15)
        respuesta.raise_for_status()
        raiz = ET.fromstring(respuesta.content)

        menciones = []
        for item in raiz.findall(".//item")[:maximo]:
            titulo = item.findtext("title", default="").strip()
            enlace = item.findtext("link", default="").strip()
            fuente_elemento = item.find("source")
            fuente = fuente_elemento.text if fuente_elemento is not None else ""

            fecha_texto = item.findtext("pubDate", default="")
            fecha_legible = ""
            if fecha_texto:
                try:
                    from email.utils import parsedate_to_datetime
                    fecha_dt = parsedate_to_datetime(fecha_texto)
                    fecha_legible = f"{fecha_dt.day} {MESES_ES.get(fecha_dt.month, '')} {fecha_dt.year}"
                except Exception:
                    fecha_legible = ""

            if titulo and enlace:
                menciones.append({
                    "titulo": titulo,
                    "fuente": fuente,
                    "enlace": enlace,
                    "fecha": fecha_legible,
                })
        return menciones
    except Exception:
        return []


def obtener_texto_pagina(url, limite_caracteres=4000):
    """Descarga una página de noticias/blog y deja solo el texto (sin HTML)."""
    try:
        respuesta = requests.get(url, headers=ENCABEZADOS_NAVEGADOR, timeout=15)
        respuesta.raise_for_status()
        texto_limpio = re.sub(r"<script.*?</script>", " ", respuesta.text, flags=re.DOTALL)
        texto_limpio = re.sub(r"<style.*?</style>", " ", texto_limpio, flags=re.DOTALL)
        texto_limpio = re.sub(r"<[^>]+>", " ", texto_limpio)
        texto_limpio = re.sub(r"\s+", " ", texto_limpio).strip()
        return texto_limpio[:limite_caracteres]
    except Exception:
        return ""


def verificar_mencion_con_ia(mencion, ticker, nombre_empresa):
    """
    Lee el artículo completo (no solo el título) y le pide a la IA que
    decida si es contenido real y relevante, o solo ruido (preguntas
    vagas, foros sin sustancia, contenido genérico). Si no hay clave de
    Anthropic configurada, esta verificación se omite — no es obligatoria
    para que el resto del script funcione.
    Devuelve la mención con un resumen agregado si es relevante, o None
    si la IA decide que es ruido y no vale la pena mostrarla.
    """
    if not CLAVE_API_ANTHROPIC or CLAVE_API_ANTHROPIC == "PEGA_AQUI_TU_CLAVE":
        return mencion

    texto_articulo = obtener_texto_pagina(mencion["enlace"])
    if not texto_articulo or len(texto_articulo) < 200:
        return None

    pregunta = (
        f"Este es el título y el contenido de un artículo que menciona a la "
        f"empresa {nombre_empresa} ({ticker}):\n\n"
        f"Título: {mencion['titulo']}\n\n"
        f"Contenido: {texto_articulo}\n\n"
        "Evalúa esto en dos pasos:\n"
        "1. ¿Es contenido real y relevante sobre la empresa (una noticia, un "
        "análisis, una opinión de un analista, un dato concreto), o es solo "
        "ruido (una pregunta vaga de foro, contenido genérico no relacionado, "
        "una conversación sin sustancia real)?\n"
        "2. Si es relevante, resume en una sola frase simple en español qué "
        "se dijo exactamente.\n\n"
        "Responde SOLO en este formato exacto, sin nada más:\n"
        "RELEVANTE: si/no\n"
        "RESUMEN: (la frase, o N/A si no es relevante)"
    )

    try:
        respuesta = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": CLAVE_API_ANTHROPIC,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODELO_IA,
                "max_tokens": 150,
                "messages": [{"role": "user", "content": pregunta}],
            },
            timeout=30,
        )
        respuesta.raise_for_status()
        texto_respuesta = respuesta.json()["content"][0]["text"].strip()

        relevante = "si" in texto_respuesta.lower().split("relevante:")[1][:10].lower()
        if not relevante:
            return None

        resumen = ""
        if "resumen:" in texto_respuesta.lower():
            resumen = texto_respuesta.split("RESUMEN:")[-1].strip()

        mencion["resumen_verificado"] = resumen if resumen and resumen != "N/A" else ""
        return mencion
    except Exception as error:
        print(f"  No se pudo verificar una mención: {error}")
        return mencion


# -----------------------------------------------------------
# PASO 7: Juntar todo y guardar el resultado
# -----------------------------------------------------------

def main():
    hora_inicio = datetime.now()
    print(f"Iniciando revisión a las {hora_inicio.strftime('%H:%M')}...")
    print("Descargando la lista actual de small caps...")
    lista = obtener_lista_smallcaps()
    total = len(lista)
    print(f"Se encontraron {total} acciones para revisar.\n")

    alertas, fecha_sesion_detectada = revisar_lista_en_paralelo(lista)

    print(f"\n\nSe encontraron {len(alertas)} acciones con volumen inusual.")
    print("Ahora verificando cuáles tienen una razón real detrás...\n")

    verificadas, descartadas = verificar_alertas(alertas)

    if verificadas and CLAVE_API_ANTHROPIC and CLAVE_API_ANTHROPIC != "PEGA_AQUI_TU_CLAVE":
        print(f"\nGenerando resúmenes con IA para las {len(verificadas)} alertas finales...")
        for alerta in verificadas:
            narrativa = generar_narrativa(alerta)
            if narrativa:
                alerta["narrativa"] = narrativa

    if verificadas:
        print(f"\nBuscando menciones recientes en noticias para las {len(verificadas)} alertas finales...")
        for alerta in verificadas:
            menciones_crudas = buscar_menciones_noticias(alerta["ticker"])
            time.sleep(0.5)

            menciones_verificadas = []
            for mencion in menciones_crudas:
                resultado = verificar_mencion_con_ia(mencion, alerta["ticker"], alerta.get("nombre", ""))
                if resultado:
                    menciones_verificadas.append(resultado)
                time.sleep(0.5)

            alerta["menciones_noticias"] = menciones_verificadas

    hora_fin = datetime.now()

    salida = {
        "revision_inicio": hora_inicio.strftime("%H:%M"),
        "revision_fin": hora_fin.strftime("%H:%M"),
        "fecha_revision": hora_inicio.isoformat(),
        "fecha_datos_mercado": fecha_sesion_detectada,
        "nota_horario": (
            "Los volúmenes son del DÍA COMPLETO de bolsa de la fecha indicada "
            "en fecha_datos_mercado, no de un minuto específico."
        ),
        "total_acciones_revisadas": total,
        "total_alertas_volumen": len(alertas),
        "total_verificadas": len(verificadas),
        "smallcap_minimo": SMALLCAP_MINIMO,
        "smallcap_maximo": SMALLCAP_MAXIMO,
        "alertas_verificadas": verificadas,
        "alertas_descartadas_sin_verificar": descartadas,
    }

    with open("alertas_smallcaps.json", "w", encoding="utf-8") as archivo:
        json.dump(salida, archivo, indent=2, ensure_ascii=False)

    # Guardamos por separado el último hallazgo verificado que SÍ hubo, para
    # poder mostrarlo en días donde no se encuentre nada nuevo, en vez de
    # dejar el dashboard vacío.
    ruta_ultimo_hallazgo = "ultimo_hallazgo_verificado.json"
    if verificadas:
        with open(ruta_ultimo_hallazgo, "w", encoding="utf-8") as archivo:
            json.dump(
                {"fecha": fecha_sesion_detectada, "alertas": verificadas},
                archivo, indent=2, ensure_ascii=False,
            )
    elif os.path.exists(ruta_ultimo_hallazgo):
        with open(ruta_ultimo_hallazgo, "r", encoding="utf-8") as archivo:
            salida["ultimo_hallazgo"] = json.load(archivo)

    generar_dashboard_html(salida)

    print(f"\n\nListo. {len(verificadas)} de {len(alertas)} alertas tienen una razón verificada.")
    print(f"Revisión: {hora_inicio.strftime('%H:%M')} a {hora_fin.strftime('%H:%M')}.")
    if fecha_sesion_detectada:
        hoy = datetime.now().strftime("%Y-%m-%d")
        if fecha_sesion_detectada == hoy:
            print(f"Estos datos son de HOY ({fecha_sesion_detectada}).")
        else:
            print(
                f"AVISO: la bolsa no operó hoy. Estos datos son del último "
                f"cierre disponible: {fecha_sesion_detectada}."
            )
    print("Resultado guardado en alertas_smallcaps.json")


if __name__ == "__main__":
    main()
