#!/usr/bin/env python3
"""CENEPRED/SIGRID rain-risk scenarios by district -> static GeoJSON for the web (stdlib only).

SIGRID does not allow browser (CORS) reads, so the Action downloads valid and upcoming
scenarios, validates them like the iOS app and writes:
  data/riesgo-lluvias.json            index: scenarios, validity, status, errors
  data/riesgo-lluvias/{id}.json       districts of one scenario (simplified GeoJSON)
A source failure is reported in the index; it never means "no risk".
"""
import argparse
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import re
import struct
import sys
import urllib.request
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate import PERU  # noqa: E402

LIST_URL = 'https://sigrid.cenepred.gob.pe/sigridv3/escenario-aviso/data-lluvia'
ZIP_URL = 'https://sigrid.cenepred.gob.pe/sigridv3/storage/escenario_aviso/{id}_shape.zip'
DOC_URL = 'https://sigrid.cenepred.gob.pe/sigridv3/documento/{id}'
USER_AGENT = 'AguaCerca-riesgo/1.0 (+https://github.com/0sw2ld0/agua-cerca-aniegos-piloto)'
MAX_LIST = 2_000_000
MAX_ZIP = 80_000_000          # the ZIP also carries a large department layer we do not read
MAX_SHP = 30_000_000
MAX_SCENARIOS = 6
LEVELS = {'MA': 'Muy alto', 'A': 'Alto', 'M': 'Medio', 'B': 'Bajo'}
TOLERANCE = 0.001             # ~110 m: district-scale display, not parcel precision


class FormatError(ValueError):
    pass


def fetch(url, limit, timeout=60):
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if not response.geturl().startswith('https://sigrid.cenepred.gob.pe/'):
            raise FormatError('unexpected redirect')
        data = response.read(limit + 1)
    if len(data) > limit:
        raise FormatError('too_large')
    return data


def day(value):
    if not isinstance(value, str) or not re.match(r'^\d{4}-\d{2}-\d{2}', value):
        raise FormatError('date')
    return datetime.strptime(value[:10], '%Y-%m-%d').replace(tzinfo=PERU)


def scenarios(payload):
    """Validated list, newest first. Returns (rows, omitted)."""
    data = payload.get('data')
    if not isinstance(data, list) or payload.get('recordsTotal') != len(data):
        raise FormatError('list_count')
    rows, seen, omitted = [], set(), 0
    for item in data:
        try:
            ident, doc = str(item['id']), str(item['id_documento'])
            if not re.fullmatch(r'\d{1,8}', ident) or not re.fullmatch(r'\d{1,8}', doc) or ident in seen:
                raise FormatError('id')
            start, end = day(item['inicio_vigencia']), day(item['fin_vigencia'])
            if end < start:
                raise FormatError('range')
            seen.add(ident)
            rows.append({'id': ident, 'aviso': re.sub(r'\s+', ' ', str(item.get('fuente') or '')).strip()[:20],
                         'ambito': re.sub(r'\s+', ' ', str(item.get('ambito') or '')).strip()[:80],
                         'inicio': start.isoformat(), 'fin': (end + timedelta(days=1)).isoformat(),  # whole last day
                         'documento': DOC_URL.format(id=doc)})
        except (KeyError, TypeError, FormatError):
            omitted += 1
    if not rows:
        raise FormatError('no_valid_rows')
    rows.sort(key=lambda r: (r['inicio'], int(r['id'])), reverse=True)
    return rows, omitted


def status(row, now):
    start, end = datetime.fromisoformat(row['inicio']), datetime.fromisoformat(row['fin'])
    return 'vigente' if start <= now < end else 'proximo' if now < start else 'vencido'


def choose(rows, now):
    current = [r for r in rows if status(r, now) in ('vigente', 'proximo')]
    return (current or rows[:1])[:MAX_SCENARIOS]


# ---- shapefile -------------------------------------------------------------
def read_dbf(data):
    if len(data) < 32:
        raise FormatError('dbf')
    count, header, size = struct.unpack('<IHH', data[4:12])
    fields, offset = [], 32
    while offset < header - 1 and data[offset] != 0x0D:
        name = data[offset:offset + 11].split(b'\0')[0].decode('ascii', 'replace').upper()
        fields.append((name, data[offset + 16]))
        offset += 32
    rows = []
    for index in range(count):
        record = data[header + index * size: header + (index + 1) * size]
        if len(record) < size:
            raise FormatError('dbf_truncated')
        if record[:1] == b'*':
            rows.append(None)  # deleted: keep alignment with the .shp records
            continue
        values, pos = {}, 1
        for name, length in fields:
            raw = record[pos:pos + length]
            try:
                values[name] = raw.decode('utf-8').strip()
            except UnicodeDecodeError:
                values[name] = raw.decode('cp1252', 'replace').strip()
            pos += length
        rows.append(values)
    return rows


def signed_area(ring):
    return sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1])) / 2


def point_in(ring, x, y):
    hit = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            hit = not hit
    return hit


def read_shp(data):
    if len(data) < 100 or struct.unpack('>i', data[:4])[0] != 9994:
        raise FormatError('shp')
    if struct.unpack('<i', data[32:36])[0] != 5:
        raise FormatError('not_polygon')
    shapes, offset = [], 100
    while offset + 8 <= len(data):
        _, words = struct.unpack('>ii', data[offset:offset + 8])
        body = data[offset + 8: offset + 8 + words * 2]
        offset += 8 + words * 2
        kind = struct.unpack('<i', body[:4])[0]
        if kind == 0:
            shapes.append([])
            continue
        if kind != 5:
            raise FormatError('not_polygon')
        parts_n, points_n = struct.unpack('<ii', body[36:44])
        parts = list(struct.unpack('<%di' % parts_n, body[44:44 + 4 * parts_n])) + [points_n]
        coords = struct.unpack('<%dd' % (2 * points_n), body[44 + 4 * parts_n: 44 + 4 * parts_n + 16 * points_n])
        points = list(zip(coords[0::2], coords[1::2]))
        if any(not (-82 <= x <= -68 and -19 <= y <= 1) for x, y in points):
            raise FormatError('coordinates')
        rings = [points[parts[i]:parts[i + 1]] for i in range(parts_n)]
        outers = [r for r in rings if signed_area(r) < 0]   # Esri: exterior clockwise
        holes = [r for r in rings if signed_area(r) >= 0]
        polygons = [[r] for r in outers]
        for hole in holes:
            owners = [p for p in polygons if point_in(p[0], *hole[0])]
            if owners:
                min(owners, key=lambda p: abs(signed_area(p[0]))).append(hole)
        shapes.append(polygons)
    return shapes


def simplify(ring, tol=TOLERANCE):
    """Douglas-Peucker; keeps rings closed and at least 4 points."""
    if len(ring) < 5:
        return ring
    keep = [False] * len(ring)
    keep[0] = keep[-1] = True
    stack = [(0, len(ring) - 1)]
    while stack:
        a, b = stack.pop()
        (x1, y1), (x2, y2) = ring[a], ring[b]
        dx, dy = x2 - x1, y2 - y1
        norm = (dx * dx + dy * dy) ** 0.5
        far, index = 0.0, None
        for i in range(a + 1, b):
            px, py = ring[i][0] - x1, ring[i][1] - y1
            # A closed ring starts and ends on the same point: use plain distance to it.
            d = abs(dy * px - dx * py) / norm if norm else (px * px + py * py) ** 0.5
            if d > far:
                far, index = d, i
        if index is not None and far > tol:
            keep[index] = True
            stack += [(a, index), (index, b)]
    if sum(keep) < 4:  # a valid ring needs 4 positions: keep one more vertex instead of the whole ring
        keep[next(i for i in range(len(ring) // 2, len(ring) - 1) if not keep[i])] = True
    return [p for p, k in zip(ring, keep) if k]


def districts(zip_bytes):
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        names = archive.namelist()
        dbf = next((n for n in names if n.lower().endswith('.dbf') and 'dist' in n.lower().rsplit('/', 1)[-1]
                    and 'riesgo' in n.lower().rsplit('/', 1)[-1]), None)
        if not dbf:
            raise FormatError('sin_mapa')
        base = dbf[:-4]
        need = {ext: next((n for n in names if n.lower() == (base + ext).lower()), None) for ext in ('.shp', '.prj')}
        if not all(need.values()):
            raise FormatError('sin_mapa')
        prj = archive.read(need['.prj']).decode('latin-1')
        if 'GEOGCS' not in prj or 'WGS_1984' not in prj or 'PROJCS' in prj:
            raise FormatError('proyeccion')
        if archive.getinfo(need['.shp']).file_size > MAX_SHP:
            raise FormatError('too_large')
        shapes, rows = read_shp(archive.read(need['.shp'])), read_dbf(archive.read(dbf))
    if len(shapes) != len(rows):
        raise FormatError('dbf_shp_mismatch')
    features, ids = [], set()
    for shape, row in zip(shapes, rows):
        if row is None or not shape:
            continue
        ident = row.get('UBIGEO') or row.get('IDDIST') or ''
        level = (row.get('N_RIESGO') or '').upper()
        if not re.fullmatch(r'\d{6}', ident) or ident in ids or level not in LEVELS:
            raise FormatError('row')
        ids.add(ident)
        geometry = [[[[round(x, 5), round(y, 5)] for x, y in simplify(ring)] for ring in polygon] for polygon in shape]
        features.append({'type': 'Feature', 'id': ident, 'geometry': {'type': 'MultiPolygon', 'coordinates': geometry},
                         'properties': {'ubigeo': ident, 'nivel': LEVELS[level],
                                        'distrito': row.get('NOMBDIST') or 'Distrito ' + ident,
                                        'provincia': row.get('NOMBPROV') or '',
                                        'departamento': row.get('NOMBDEP') or 'No indicado'}})
    if not features:
        raise FormatError('sin_mapa')
    return {'type': 'FeatureCollection', 'features': features}


def run(index_path, folder, now, get=fetch):
    previous = json.loads(index_path.read_text()) if index_path.exists() else {}
    known = {s['id']: s for s in previous.get('escenarios', []) if s.get('distritos')}
    index = {'schema_version': 1, 'fuente': 'CENEPRED / SIGRID', 'generated_at': now.isoformat(),
             'lista': 'https://sigrid.cenepred.gob.pe/sigridv3/escenarios/lbam', 'escenarios': [], 'errores': []}
    try:
        rows, omitted = scenarios(json.loads(get(LIST_URL, MAX_LIST)))
    except Exception as error:  # keep the last good index; report the failure
        previous.update({'checked_at': now.isoformat(), 'error_lista': type(error).__name__ + ': ' + str(error)[:120]})
        index_path.write_text(json.dumps(previous or index, ensure_ascii=False, indent=1) + '\n')
        print(json.dumps({'riesgo_lluvias': 'list_failed'}))
        return 0
    index['omitidos'] = omitted
    folder.mkdir(parents=True, exist_ok=True)
    for row in choose(rows, now):
        entry = {**row, 'estado': status(row, now)}
        target = folder / (row['id'] + '.json')
        if row['id'] in known and target.exists():
            entry.update({k: known[row['id']][k] for k in ('distritos', 'niveles', 'descargado') if k in known[row['id']]})
        else:
            try:
                collection = districts(get(ZIP_URL.format(id=row['id']), MAX_ZIP, timeout=120))
                target.write_text(json.dumps(collection, ensure_ascii=False, separators=(',', ':')) + '\n')
                levels = {}
                for f in collection['features']:
                    levels[f['properties']['nivel']] = levels.get(f['properties']['nivel'], 0) + 1
                entry.update({'distritos': len(collection['features']), 'niveles': levels, 'descargado': now.isoformat()})
            except Exception as error:
                code = str(error) if isinstance(error, FormatError) else 'fuente'
                entry['error'] = code
                index['errores'].append({'id': row['id'], 'error': code})
        index['escenarios'].append(entry)
    keep = {s['id'] + '.json' for s in index['escenarios'] if s.get('distritos')}
    for old in folder.glob('*.json'):
        if old.name not in keep:
            old.unlink()
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=1) + '\n')
    print(json.dumps({'riesgo_lluvias': [(s['id'], s['estado'], s.get('distritos'), s.get('error')) for s in index['escenarios']]},
                     ensure_ascii=False))
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index', type=Path, default=Path('data/riesgo-lluvias.json'))
    parser.add_argument('--folder', type=Path, default=Path('data/riesgo-lluvias'))
    args = parser.parse_args()
    raise SystemExit(run(args.index, args.folder, datetime.now(PERU)))
