#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
actualizar.py - Generador y actualizador unificado de lista IPTV en español latino.
Descarga fuentes de Perú, Argentina, Latinoamérica y Pluto TV Español,
filtra canales no deseados en otros idiomas, elimina URLs duplicadas y
verifica de manera concurrente con 20 hilos y timeout de 4 segundos
que los streams estén activos antes de generar 'mi_tele_latam.m3u'.
"""

import sys
import re
import urllib3
import requests
from concurrent.futures import ThreadPoolExecutor

# Deshabilitar advertencias de certificados no verificados para evitar ruido en consola
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Fuentes oficiales
SOURCES = [
    "https://iptv-org.github.io/iptv/countries/pe.m3u",
    "https://iptv-org.github.io/iptv/countries/ar.m3u",
    "https://iptv-org.github.io/iptv/regions/latam.m3u",
    "https://i.mjh.nz/PlutoTV/es.m3u8",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "*/*",
}

ALLOWED_HTTP_CODES = {200, 206, 301, 302}
TIMEOUT = 4
MAX_WORKERS = 20
OUTPUT_FILE = "mi_tele_latam.m3u"

# Patrones para detección de idiomas
NON_ES_REGEX = re.compile(
    r"\b(english|ingl[eé]s|portuguese|portugu[eê]s|french|franc[eé]s|deutsch|alem[aá]n|italian|italiano|russian|ruso)\b|"
    r"(\[(?:en|eng|pt|por|fr|fra|de|deu|it|ita|ru|rus)\])|"
    r"(\((?:en|eng|pt|por|fr|fra|de|deu|it|ita|ru|rus|france)\))",
    re.IGNORECASE,
)

ES_REGEX = re.compile(
    r"\b(spanish|español|castellano)\b|(\[(?:es|spa)\])|(\((?:es|spa)\))",
    re.IGNORECASE,
)

NON_ES_LANG_CODES = {
    "en", "eng", "english", "pt", "por", "portuguese",
    "fr", "fra", "fre", "french", "de", "deu", "ger",
    "german", "it", "ita", "italian", "ru", "rus",
    "russian", "ar", "ara", "arabic", "zh", "zho", "chi", "chinese"
}

ES_LANG_CODES = {"es", "spa", "spanish", "espanol", "español", "castellano"}


def is_non_spanish(extinf_line: str, title: str) -> bool:
    """
    Retorna True si el canal explícitamente está en un idioma distinto al español.
    Si no especifica idioma o es ambiguo, se conserva (retorna False).
    """
    # 1. Verificar atributo de idioma explícito (tvg-language o language)
    lang_match = re.search(r'(?:tvg-language|language)="([^"]+)"', extinf_line, re.IGNORECASE)
    if lang_match:
        langs = [l.strip().lower() for l in lang_match.group(1).split(";") if l.strip()]
        has_es = any(l in ES_LANG_CODES for l in langs)
        has_non_es = any(l in NON_ES_LANG_CODES for l in langs)
        if has_non_es and not has_es:
            return True

    # 2. Si el título menciona español explícitamente, se conserva siempre
    if ES_REGEX.search(title):
        return False

    # 3. Si el título indica otro idioma distinto
    if NON_ES_REGEX.search(title):
        return True

    return False


def parse_m3u(content: str):
    """
    Parsea una lista M3U extrayendo cada canal conservando sus líneas #EXTINF
    originales, etiquetas adicionales y su URL correspondiente.
    """
    channels = []
    lines = content.splitlines()
    i = 0
    total_lines = len(lines)
    while i < total_lines:
        line = lines[i].strip()
        if line.startswith("#EXTINF"):
            extinf = line
            extra_tags = []
            i += 1
            while i < total_lines:
                next_line = lines[i].strip()
                if not next_line:
                    i += 1
                    continue
                if next_line.startswith("#EXTINF"):
                    # Se encontró otro EXTINF antes de URL
                    break
                if next_line.startswith("#"):
                    extra_tags.append(next_line)
                    i += 1
                else:
                    # Línea de la URL del stream
                    channels.append({
                        "extinf": extinf,
                        "extra_tags": extra_tags,
                        "url": next_line
                    })
                    break
        i += 1
    return channels


def check_stream(channel: dict) -> bool:
    """
    Verifica si el stream está vivo aceptando códigos HTTP 200, 206, 301 y 302
    con User-Agent de navegador y timeout de 4 segundos.
    """
    url = channel["url"]

    # Intentar primero HEAD
    try:
        r = requests.head(
            url,
            headers=HEADERS,
            timeout=TIMEOUT,
            allow_redirects=True,
            verify=False
        )
        if r.status_code in ALLOWED_HTTP_CODES or any(res.status_code in ALLOWED_HTTP_CODES for res in r.history):
            return True
    except Exception:
        pass

    # Si HEAD falla o no está soportado (e.g. 405 Method Not Allowed), intentar GET stream=True
    try:
        r = requests.get(
            url,
            headers=HEADERS,
            timeout=TIMEOUT,
            stream=True,
            allow_redirects=True,
            verify=False
        )
        code = r.status_code
        has_allowed_history = any(res.status_code in ALLOWED_HTTP_CODES for res in r.history)
        r.close()
        if code in ALLOWED_HTTP_CODES or has_allowed_history:
            return True
    except Exception:
        pass

    return False


def main():
    print("==================================================")
    print("Iniciando actualizacion de lista IPTV unificada")
    print("==================================================")

    all_channels = []
    seen_urls = set()
    total_downloaded = 0
    total_filtered_language = 0
    total_duplicates = 0

    session = requests.Session()
    session.headers.update(HEADERS)

    for source_url in SOURCES:
        print(f"\nDescargando fuente: {source_url}...")
        try:
            resp = session.get(source_url, timeout=15, verify=False)
            if resp.status_code != 200:
                print(f"  [AVISO] No se pudo descargar {source_url} (HTTP {resp.status_code}). Omitiendo.")
                continue

            parsed = parse_m3u(resp.text)
            print(f"  Canales encontrados en la fuente: {len(parsed)}")
            total_downloaded += len(parsed)

            for ch in parsed:
                url_clean = ch["url"].strip()
                extinf = ch["extinf"]
                title = extinf.split(",", 1)[-1].strip() if "," in extinf else ""

                # Filtrar idioma no español
                if is_non_spanish(extinf, title):
                    total_filtered_language += 1
                    continue

                # Eliminar URLs duplicadas
                if url_clean in seen_urls:
                    total_duplicates += 1
                    continue

                seen_urls.add(url_clean)
                all_channels.append(ch)

        except Exception as e:
            print(f"  [ERROR] Fallo al procesar {source_url}: {e}")

    print("\n--------------------------------------------------")
    print(f"Resumen de extraccion:")
    print(f" - Canales descargados: {total_downloaded}")
    print(f" - Canales excluidos por idioma: {total_filtered_language}")
    print(f" - URLs duplicadas omitidas: {total_duplicates}")
    print(f" - Canales unicos a verificar: {len(all_channels)}")
    print(f"--------------------------------------------------")

    print(f"\nIniciando verificacion concurrente de streams:")
    print(f" - Hilos: {MAX_WORKERS}")
    print(f" - Timeout: {TIMEOUT}s")
    print(f" - Codigos HTTP validos: {ALLOWED_HTTP_CODES}")
    print(f"Por favor espere mientras se comprueba la disponibilidad...")

    active_channels = []
    processed_count = 0
    total_to_check = len(all_channels)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Enviar todas las tareas
        futures = [executor.submit(check_stream, ch) for ch in all_channels]

        for ch, future in zip(all_channels, futures):
            try:
                is_alive = future.result()
            except Exception:
                is_alive = False

            processed_count += 1
            if is_alive:
                active_channels.append(ch)

            if processed_count % 100 == 0 or processed_count == total_to_check:
                print(f"  Progreso: {processed_count}/{total_to_check} canales verificados (Activos: {len(active_channels)})")

    print(f"\nVerificacion completada: {len(active_channels)} canales activos confirmados.")

    # Escribir archivo de salida mi_tele_latam.m3u en UTF-8 con cabecera unica #EXTM3U
    print(f"Guardando {OUTPUT_FILE} en UTF-8...")
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for ch in active_channels:
            f.write(f"{ch['extinf']}\n")
            for extra in ch.get("extra_tags", []):
                f.write(f"{extra}\n")
            f.write(f"{ch['url']}\n")

    print(f"¡Listo! Se guardaron exitosamente {len(active_channels)} canales en '{OUTPUT_FILE}'.")
    return len(active_channels)


if __name__ == "__main__":
    count = main()
    sys.exit(0)
