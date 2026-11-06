#!/usr/bin/env python3
"""COEN-INDECI emergency reports (official) -> review-like incidents for the feed. Rules only, no AI.

Reads https://portal.indeci.gob.pe/emergencias/, keeps water-related reports for districts of Lima
Metropolitana and Callao from the last 48 h (the portal lists one item per emergency, its latest report),
downloads the PDF (cached by URL), converts it with `pdftotext` and extracts only places the report
names explicitly: facts, affected roads, crossings, establishments and residential sectors/groups.
A failure is reported in the output; it never means "no flooding".
"""
import argparse
from datetime import datetime, timedelta
import hashlib
import html
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate import PERU  # noqa: E402
from gpt_incidents import LIMA_DISTRICTS  # noqa: E402

PORTADA = 'https://portal.indeci.gob.pe/emergencias/'
HOST = 'portal.indeci.gob.pe'
USER_AGENT = 'AguaCerca-indeci/1.0 (+https://github.com/0sw2ld0/agua-cerca-aniegos-piloto)'
MAX_PDF = 15_000_000
MAX_REPORTES = 12
AGUA = re.compile(r'INUNDACI|LLUVIA|LLOVIZNA|ANIEGO|DESBORDE|ALCANTARILL|DESAG|TUBER|BUZ[OÓ]N|PRECIPITACI|EROSI[OÓ]N PLUVIAL', re.I)
TITULO = re.compile(
    r'REPORTE\s+(PRELIMINAR|COMPLEMENTARIO)\s+N\.?\s*[°ºo]?\s*(\d+)\s*[–-]\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*/?\s*COEN\s*[–-]?\s*INDECI'
    r'\s*/\s*(\d{1,2}):(\d{2})\s*HORAS.*?\(\s*Reporte\s+N\.?\s*[°ºo]?\s*(\d+)[^)]*\)\s*(.+?)\s+EN\s+(?:EL|LA)\s+DISTRITO\s+DE\s+'
    r'(.+?)\s*[–-]\s*(LIMA|CALLAO)\s*$', re.I | re.S)
PALABRAS_RUIDO = {'md', 'municipalidad', 'cisterna', 'cisternas', 'trabajos', 'total', 'fuente', 'hidrojet', 'hidrojets',
                  'emape', 'sedapal', 'succion', 'evacuacion', 'drenaje', 'maquinaria', 'personal', 'realiza', 'placa',
                  'reporte', 'preliminar', 'complementario', 'coen', 'indeci', 'coer'}
ABREVIATURAS = re.compile(r'\b(Av|av|Jr|jr|Km|km|Mz|mz|Psje|psje|N)\.')


def norm(value):
    value = ''.join(c for c in unicodedata.normalize('NFD', str(value).lower()) if unicodedata.category(c) != 'Mn')
    return re.sub(r'[^a-z0-9]+', ' ', value).strip()


DISTRITOS = {norm(d): d for d in LIMA_DISTRICTS}


def obtener(url, limite=3_000_000, timeout=40):
    if urllib.parse.urlsplit(url).hostname != HOST or not url.startswith('https://'):
        raise ValueError('host no permitido')
    partes = urllib.parse.urlsplit(url)  # PDF names carry accents and "º": percent-encode the path
    url = urllib.parse.urlunsplit((partes.scheme, partes.netloc, urllib.parse.quote(partes.path, safe='/%'), partes.query, ''))
    pedido = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(pedido, timeout=timeout) as r:
        if urllib.parse.urlsplit(r.geturl()).hostname != HOST:
            raise ValueError('redirección fuera del portal')
        datos = r.read(limite + 1)
    if len(datos) > limite:
        raise ValueError('respuesta demasiado grande')
    return datos


def portada(html_texto, ahora):
    """Water-related reports for Lima/Callao districts in the last 48 h, newest first."""
    salida, vistos = [], set()
    for m in re.finditer(r'<a[^>]+href="(https://portal\.indeci\.gob\.pe/emergencias/[^"#?]+)"[^>]*>(.*?)</a>', html_texto, re.S):
        url, titulo = m.group(1), re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', m.group(2)))).strip()
        t = TITULO.search(titulo)
        if not t or url in vistos:
            continue
        tipo, numero, d, mes, anio, hh, mm, n_rep, evento, distrito, region = t.groups()
        canon = DISTRITOS.get(norm(distrito))
        if not canon or not AGUA.search(evento):
            continue
        try:
            fecha = datetime(int(anio), int(mes), int(d), int(hh), int(mm), tzinfo=PERU)
        except ValueError:
            continue
        if not ahora - timedelta(hours=48) <= fecha <= ahora + timedelta(hours=1):
            continue
        vistos.add(url)
        salida.append({'url': url, 'titulo': titulo[:300], 'tipo': tipo.lower(), 'numero': numero, 'reporte_n': int(n_rep),
                       'fecha': fecha.isoformat(), 'evento': re.sub(r'\s+', ' ', evento).strip().capitalize(), 'distrito': canon})
    salida.sort(key=lambda r: r['fecha'], reverse=True)
    return salida[:MAX_REPORTES]


def enlace_pdf(html_texto):
    for m in re.finditer(r'href="(https://portal\.indeci\.gob\.pe/wp-content/uploads/[^"]+\.pdf)"', html_texto, re.I):
        if 'REPORTE' in urllib.parse.unquote(m.group(1)).upper():
            return m.group(1)
    return None


def pdf_a_texto(datos):
    # pdftotext (poppler-utils) in the Action; on macOS, PDFKit through a small Swift script for local runs.
    if shutil.which('pdftotext'):
        orden = ['pdftotext', '-enc', 'UTF-8', '{ruta}', '-']
    elif shutil.which('swift') and (Path(__file__).with_name('pdftexto.swift')).exists():
        orden = ['swift', str(Path(__file__).with_name('pdftexto.swift')), '{ruta}']
    else:
        raise RuntimeError('pdftotext no está instalado (poppler-utils)')
    with tempfile.TemporaryDirectory() as carpeta:
        ruta = Path(carpeta) / 'reporte.pdf'
        ruta.write_bytes(datos)
        r = subprocess.run([x.replace('{ruta}', str(ruta)) for x in orden], capture_output=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError('pdftotext falló')
        return r.stdout.decode('utf-8', 'replace')


# ---- extracción de lugares ------------------------------------------------
def via(texto):
    """'avenida pumacahua' / 'av. Sol' -> 'Av. Pumacahua' / 'Av. Sol'; keeps other road types."""
    texto = re.sub(r'\s+', ' ', texto).strip(' .,;-')
    texto = re.sub(r'^(?:la|el|las|los)\s+', '', texto, flags=re.I)
    texto = re.sub(r'^(?:av\.?|avenida)\s+', 'Av. ', texto, flags=re.I)
    texto = re.sub(r'^(?:jr\.?|jir[oó]n)\s+', 'Jr. ', texto, flags=re.I)
    texto = re.sub(r'^calle\s+', 'Calle ', texto, flags=re.I)
    return texto[:1].upper() + texto[1:]


def limpia(etiqueta):
    palabras = set(norm(etiqueta).split())
    return 3 <= len(etiqueta) <= 110 and not (palabras & PALABRAS_RUIDO)


def seccion(texto, inicio, fin):
    m = re.search(inicio + r'(.*?)' + fin, texto, re.S | re.I)
    return m.group(1) if m else ''


def lugares(texto):
    """[(etiqueta, origen)] in order, without duplicates. Only explicit mentions."""
    plano = re.sub(r'\s+', ' ', texto)
    # Mask abbreviation dots ("Av.") so a sentence end is not detected inside a road name.
    hechos = ABREVIATURAS.sub(lambda m: m.group(1) + '§', re.sub(r'\s+', ' ', seccion(texto, r'1\.\s*HECHOS', r'2\.\s*UBICACI')))
    encontrados = []

    def agregar(etiqueta, origen):
        etiqueta = re.sub(r'\s+', ' ', etiqueta).strip(' .,;')
        if limpia(etiqueta) and norm(etiqueta) not in {norm(e) for e, _ in encontrados}:
            encontrados.append((etiqueta, origen))

    for m in re.finditer(r'sector\s+(\d{1,2})\s*[,–-]?\s*grupo\s*(\d{1,2})', plano, re.I):
        agregar(f'Sector {m.group(1)}, Grupo {m.group(2)}', 'sector')
    # Facts: "en la Av. Pachacútec con la Av. 26 de noviembre, distrito de ..."
    for m in re.finditer(r'\ben\s+(?:la|el)?\s*((?:Av§|Avenida|Jr§|Jir[oó]n|Calle|Panamericana)\s[^,;]+?)(?=,|;|\.\s|\s+distrito|\s+provincia)', hechos, re.I):
        partes = re.split(r'\s+con\s+(?:la\s+)?', m.group(1).replace('§', '.'), maxsplit=1, flags=re.I)
        agregar(' con '.join(via(p) for p in partes), 'hechos')
    # Crossings named in actions or resources.
    for m in re.finditer(r'intersecci[oó]n\s+de\s+(?:las?\s+)?((?:Av\.?|avenida)\s+[^,;]+?)\s+con\s+((?:Av\.?|avenida)\s+[\wÁÉÍÓÚÑáéíóúñ ]+?)(?=[.,;]|\s+(?:MD|Trabajos|Cisterna|TOTAL|Fuente|EMAPE)\b)', plano, re.I):
        agregar(f'{via(m.group(1))} con {via(m.group(2))}', 'cruce')
    for m in re.finditer(r'((?i:av\.?|avenida)\s+[\wÁÉÍÓÚÑáéíóúñ ]+?)\s+cruce\s+con\s+((?i:av\.?\s+|avenida\s+)?[A-ZÁÉÍÓÚÑ0-9][\wÁÉÍÓÚÑáéíóúñ]*(?:\s+[A-ZÁÉÍÓÚÑ0-9][\wÁÉÍÓÚÑáéíóúñ]*){0,3})', plano):
        # Tables interleave columns: cut the second road at the first word that belongs to another column.
        palabras = []
        for w in re.sub(r'(?i)^(?:av\.?|avenida)\s+', '', m.group(2)).split():
            if norm(w) in PALABRAS_RUIDO:
                break
            palabras.append(w)
        if palabras:
            agregar(f'{via(m.group(1))} con Av. {" ".join(palabras)}', 'cruce')
    for m in re.finditer(r'altura de (?:la\s+)?((?:avenida|av\.)\s+[\wÁÉÍÓÚÑáéíóúñ ]+?)\s+y\s+((?:avenida|av\.)\s+[\wÁÉÍÓÚÑáéíóúñ ]+?)(?=\s+se\b|[.,;])', plano, re.I):
        agregar(f'{via(m.group(1))} con {via(m.group(2))}', 'cruce')
    # Establishments.
    for m in re.finditer(r'(?:frontis|exteriores|inmediaciones|frente)\s+del?\s+(Hospital|Centro de Salud|Mercado)\s+([A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ]*(?:\s+(?:de\s+)?[A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ]*){0,4})', plano):
        agregar(f'Frente al {m.group(1)} {m.group(2)}', 'establecimiento')
    for m in re.finditer(r'instituci[oó]n educativa\s+([A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ]*(?:\s+[A-ZÁÉÍÓÚÑIVX][\wÁÉÍÓÚÑáéíóúñ]*){0,4})', plano):
        agregar(f'Colegio {m.group(1)}', 'establecimiento')
    return encontrados


def vias_afectadas(texto):
    bloque = seccion(texto, r'V[ií]as afectadas', r'(?:4\.\s*ACCIONES|$)')
    items = [re.sub(r'\s+', ' ', x).strip(' .') for x in re.findall(r'-\s*((?:Av\.|Jr\.|Calle|Psje\.|Pasaje|Prolongaci[oó]n)[^\n]+)', bloque)]
    return [i for i in items if 3 <= len(i) <= 80][:20]


def afectados(texto):
    bloque = seccion(texto, r'3\.1\s*Evaluaci[oó]n preliminar', r'(?:Nota:|3\.2|4\.\s*ACCIONES)')
    return [re.sub(r'\s+', ' ', x).strip() for x in re.findall(r'•\s*(\d+\s+[a-záéíóúñ ]+afectad[oa]s?)', bloque, re.I)][:6]


def incidentes(reporte, texto):
    """Review-like entries for publish.py (same fields as reviewed.json)."""
    fecha = datetime.fromisoformat(reporte['fecha'])
    cuando = fecha.strftime('%d/%m %H:%M')
    fuente = {'name': f"COEN-INDECI · Reporte {reporte['numero']}, {cuando}", 'url': reporte['url']}
    base = f"Reporte {reporte['tipo']} N.º {reporte['numero']} del COEN-INDECI ({cuando}): {reporte['evento'].lower()} en {reporte['distrito']}."
    dano = afectados(texto)
    if dano:
        base += ' Evaluación preliminar: ' + ', '.join(dano) + '.'
    salida = []
    clave = 'indeci-' + hashlib.sha256(reporte['url'].encode()).hexdigest()[:10]
    for etiqueta, origen in lugares(texto):
        texto_origen = {'hechos': 'en los hechos', 'cruce': 'en las acciones de respuesta', 'sector': 'como zona afectada',
                        'establecimiento': 'en las acciones de respuesta'}[origen]
        salida.append({'key': f"{clave}-{norm(etiqueta).replace(' ', '-')[:50]}", 'district': reporte['distrito'],
                       'location': etiqueta, 'summary': (base + f' El reporte menciona este lugar {texto_origen}.')[:560],
                       'day': fecha.date().isoformat(), 'coordinates': None, 'precision': 'Sin punto verificado',
                       'sources': [fuente], 'origen': 'indeci', 'seccion': origen})
    vias = vias_afectadas(texto)
    if vias:
        etiqueta = vias[0] + (f', y otras {len(vias) - 1} vías del distrito' if len(vias) > 1 else '')
        salida.append({'key': f'{clave}-vias', 'district': reporte['distrito'], 'location': etiqueta[:110],
                       'summary': (base + ' Vías afectadas: ' + '; '.join(vias) + '.')[:560], 'day': fecha.date().isoformat(),
                       'coordinates': None, 'precision': 'Vías por cuadra; sin punto verificado', 'sources': [fuente],
                       'origen': 'indeci', 'seccion': 'vias'})
    if not salida:  # the report gives no usable place: keep the emergency visible at district level
        salida.append({'key': f'{clave}-distrito', 'district': reporte['distrito'], 'location': f"{reporte['distrito']} · ver reporte",
                       'summary': base[:560], 'day': fecha.date().isoformat(), 'coordinates': None,
                       'precision': 'Sin lugar específico en el reporte', 'sources': [fuente], 'origen': 'indeci', 'seccion': 'distrito'})
    return salida


def run(salida_path, estado_path, ahora, get=obtener, a_texto=pdf_a_texto):
    estado = json.loads(estado_path.read_text()) if estado_path.exists() else {}
    cache = estado.get('pdfs', {})
    resultado = {'schema_version': 1, 'fuente': PORTADA, 'generated_at': ahora.isoformat(), 'reportes': [], 'incidentes': [], 'errores': []}
    try:
        # The portal lists every region; a busy day pushes Lima reports to page 2 or 3.
        reportes, vistos = [], set()
        for pagina in range(1, 4):
            url = PORTADA if pagina == 1 else f'{PORTADA}page/{pagina}/'
            texto = get(url).decode('utf-8', 'replace')
            for r in portada(texto, ahora):
                if r['url'] not in vistos:
                    vistos.add(r['url']); reportes.append(r)
        reportes = sorted(reportes, key=lambda r: r['fecha'], reverse=True)[:MAX_REPORTES]
    except Exception as error:
        resultado['errores'].append({'etapa': 'portada', 'error': type(error).__name__ + ': ' + str(error)[:120]})
        previo = json.loads(salida_path.read_text()) if salida_path.exists() else None
        if previo:  # keep the last good list, flagged
            previo.update({'checked_at': ahora.isoformat(), 'errores': resultado['errores']})
            resultado = previo
        salida_path.write_text(json.dumps(resultado, ensure_ascii=False, indent=1) + '\n')
        print(json.dumps({'indeci': 'portada_fallida'}))
        return 0
    for rep in reportes:
        fila = dict(rep)
        try:
            pdf = enlace_pdf(get(rep['url']).decode('utf-8', 'replace'))
            if not pdf:
                raise ValueError('sin PDF')
            fila['pdf'] = pdf
            if pdf not in cache:
                texto = a_texto(get(pdf, MAX_PDF, timeout=90))
                cache[pdf] = {'texto_sha': hashlib.sha256(texto.encode()).hexdigest(), 'leido': ahora.isoformat(),
                              'incidentes': incidentes(rep, texto)}
            fila['lugares'] = len(cache[pdf]['incidentes'])
            resultado['incidentes'] += cache[pdf]['incidentes']
        except Exception as error:
            fila['error'] = type(error).__name__ + ': ' + str(error)[:120]
            resultado['errores'].append({'url': rep['url'], 'error': fila['error']})
        resultado['reportes'].append(fila)
    vigentes = {f.get('pdf') for f in resultado['reportes']}
    estado = {'schema_version': 1, 'pdfs': {k: v for k, v in cache.items() if k in vigentes}}
    salida_path.parent.mkdir(parents=True, exist_ok=True)
    salida_path.write_text(json.dumps(resultado, ensure_ascii=False, indent=1) + '\n')
    estado_path.write_text(json.dumps(estado, ensure_ascii=False, indent=1, sort_keys=True) + '\n')
    print(json.dumps({'indeci_reportes': len(resultado['reportes']), 'incidentes': len(resultado['incidentes']),
                      'errores': len(resultado['errores'])}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('data/indeci.json'))
    parser.add_argument('--state', type=Path, default=Path('data/indeci-estado.json'))
    args = parser.parse_args()
    raise SystemExit(run(args.output, args.state, datetime.now(PERU)))
