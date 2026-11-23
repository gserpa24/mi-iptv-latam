#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
actualizar.py - Generador y actualizador unificado de lista IPTV en español latino.
Descarga fuentes de Perú, Argentina, Latinoamérica y Pluto TV Español,
filtra canales no deseados en otros idiomas, elimina URLs duplicadas y
realiza una validación profunda de streams HLS (verificación de cabecera #EXTM3U
y validación del primer segmento/variante interno) para eliminar canales caídos,
pantallas negras o errores 403/404 antes de generar 'mi_tele_latam.m3u'.
"""

import sys
import re
import urllib.parse
import urllib3
import requests
from concurrent.futures import ThreadPoolExecutor

# Deshabilitar advertencias de certificados SSL no verificados
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

ALLOWED_HTTP_CODES = {200, 206}
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
                    break
                if next_line.startswith("#"):
                    extra_tags.append(next_line)
                    i += 1
                else:
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
    Validación profunda real de streams HLS para eliminar canales con errores 403, 404
    o pantalla negra:
    1. Realiza petición GET con stream=True (timeout=5s) con User-Agent de navegador/IPTV.
    2. Comprueba que el código HTTP sea 200 o 206 (descarta de inmediato 403, 404, 500, etc.).
    3. Lee los primeros 2 KB y comprueba que comience con #EXTM3U (descarta HTML, vacíos o errores).
    4. Si apunta a una variante o segmento interno (.m3u8 o .ts relativo o absoluto),
       toma el primer enlace no comentado, resuelve su URL completa y verifica con
       HEAD o GET (stream=True, timeout=4s) que también devuelva 200 o 206.
    """
    url = channel["url"]

    try:
        # Paso 1: Petición GET con stream=True (timeout=5s)
        with requests.get(
            url,
            headers=HEADERS,
            timeout=5,
            stream=True,
            allow_redirects=True,
            verify=False
        ) as resp:
            # Validar código HTTP (200 o 206)
            if resp.status_code not in ALLOWED_HTTP_CODES:
                return False

            # Paso 2: Leer primeros 2 KB del contenido
            chunk = b""
            for part in resp.iter_content(chunk_size=2048):
                chunk += part
                if len(chunk) >= 2048:
                    break

            text = chunk.decode("utf-8", errors="ignore").lstrip("\ufeff \r\n\t")

            # Comprobar que empiece con #EXTM3U
            if not text.startswith("#EXTM3U"):
                return False

            # Paso 3: Buscar primer enlace interno no comentado
            internal_target = None
            for line in text.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    internal_target = line
                    break

            # Si no hay enlace interno en los primeros 2 KB, el manifest base es válido
            if not internal_target:
                return True

            # Resolver URL completa (relativa o absoluta)
            resolved_url = urllib.parse.urljoin(resp.url, internal_target)

        # Paso 4: Validar enlace interno (variante o segmento) con HEAD o GET (timeout=4s)
        try:
            head_resp = requests.head(
                resolved_url,
                headers=HEADERS,
                timeout=4,
                allow_redirects=True,
                verify=False
            )
            if head_resp.status_code in ALLOWED_HTTP_CODES:
                return True
            if head_resp.status_code in {403, 404, 500}:
                return False
        except Exception:
            pass

        # Fallback a GET con stream=True (timeout=4s)
        try:
            with requests.get(
                resolved_url,
                headers=HEADERS,
                timeout=4,
                stream=True,
                allow_redirects=True,
                verify=False
            ) as sub_resp:
                if sub_resp.status_code in ALLOWED_HTTP_CODES:
                    return True
                return False
        except Exception:
            return False

    except Exception:
        return False


def main():
    print("==================================================", flush=True)
    print("Actualización de lista IPTV con validación profunda HLS", flush=True)
    print("==================================================", flush=True)

    all_channels = []
    seen_urls = set()
    total_downloaded = 0
    total_filtered_language = 0
    total_duplicates = 0

    session = requests.Session()
    session.headers.update(HEADERS)

    for source_url in SOURCES:
        print(f"\nDescargando fuente: {source_url}...", flush=True)
        try:
            resp = session.get(source_url, timeout=15, verify=False)
            if resp.status_code != 200:
                print(f"  [AVISO] No se pudo descargar {source_url} (HTTP {resp.status_code}). Omitiendo.", flush=True)
                continue

            parsed = parse_m3u(resp.text)
            print(f"  Canales encontrados en la fuente: {len(parsed)}", flush=True)
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
            print(f"  [ERROR] Fallo al procesar {source_url}: {e}", flush=True)

    print("\n--------------------------------------------------", flush=True)
    print("Resumen de extracción:", flush=True)
    print(f" - Canales descargados: {total_downloaded}", flush=True)
    print(f" - Canales excluidos por idioma: {total_filtered_language}", flush=True)
    print(f" - URLs duplicadas omitidas: {total_duplicates}", flush=True)
    print(f" - Canales únicos a verificar: {len(all_channels)}", flush=True)
    print("--------------------------------------------------", flush=True)

    print("\nIniciando validación profunda de streams HLS:", flush=True)
    print(f" - Hilos: {MAX_WORKERS}", flush=True)
    print(" - Criterio 1: GET stream=True (5s) -> HTTP 200 o 206", flush=True)
    print(" - Criterio 2: Comprobación de cabecera #EXTM3U en primeros 2 KB", flush=True)
    print(" - Criterio 3: Validación de segmento/variante interno (4s) -> HTTP 200 o 206", flush=True)
    print("Por favor espere mientras se realiza la validación profunda...", flush=True)

    active_channels = []
    processed_count = 0
    total_to_check = len(all_channels)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
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
                print(f"  Progreso: {processed_count}/{total_to_check} canales analizados (Confirmados vivos: {len(active_channels)})", flush=True)

    print(f"\nValidación profunda completada: {len(active_channels)} canales funcionales confirmados.", flush=True)

    # Escribir archivo de salida mi_tele_latam.m3u en UTF-8 con cabecera única #EXTM3U
    print(f"Guardando {OUTPUT_FILE} en UTF-8...", flush=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for ch in active_channels:
            f.write(f"{ch['extinf']}\n")
            for extra in ch.get("extra_tags", []):
                f.write(f"{extra}\n")
            f.write(f"{ch['url']}\n")

    print(f"¡Listo! Se guardaron exitosamente {len(active_channels)} canales en '{OUTPUT_FILE}'.", flush=True)
    return len(active_channels)


if __name__ == "__main__":
    count = main()
    sys.exit(0)
