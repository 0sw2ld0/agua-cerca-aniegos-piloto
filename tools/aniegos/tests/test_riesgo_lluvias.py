from datetime import datetime
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from generate import PERU
import riesgo_lluvias as rl

NOW = datetime(2026, 11, 6, 13, 0, tzinfo=PERU)
WGS84 = 'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]]]'


def shp(polygons):
    """polygons: list of shapes; each shape a list of rings [(x, y), ...] (closed)."""
    records = b''
    for n, rings in enumerate(polygons, 1):
        points = [p for r in rings for p in r]
        parts, start = [], 0
        for r in rings:
            parts.append(start)
            start += len(r)
        body = struct.pack('<i4d', 5, -77, -13, -76, -12) + struct.pack('<ii', len(rings), len(points))
        body += struct.pack('<%di' % len(parts), *parts) + b''.join(struct.pack('<2d', *p) for p in points)
        records += struct.pack('>ii', n, len(body) // 2) + body
    header = struct.pack('>i20xi', 9994, (100 + len(records)) // 2) + struct.pack('<ii4d32x', 1000, 5, -77, -13, -76, -12)
    return header + records


def dbf(rows, fields=(('UBIGEO', 6), ('N_RIESGO', 2), ('NOMBDIST', 20), ('NOMBPROV', 20), ('NOMBDEP', 20))):
    head_len = 32 + 32 * len(fields) + 1
    size = 1 + sum(n for _, n in fields)
    out = struct.pack('<B3xIHH20x', 3, len(rows), head_len, size)
    for name, n in fields:
        out += name.encode().ljust(11, b'\0') + b'C' + b'\0' * 4 + bytes([n]) + b'\0' * 15
    out += b'\x0d'
    for deleted, row in rows:
        out += (b'*' if deleted else b' ') + b''.join(str(row.get(name, '')).encode().ljust(n)[:n] for name, n in fields)
    return out


def archive(shapes, rows, prj=WGS84):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as z:
        z.writestr('Shape/dist_NRiesgo.shp', shp(shapes))
        z.writestr('Shape/dist_NRiesgo.dbf', dbf(rows))
        z.writestr('Shape/dist_NRiesgo.prj', prj)
        z.writestr('Shape/smm_dptal.dbf', b'ignored')
    return buffer.getvalue()


# Clockwise exterior (Esri) and a counter-clockwise hole.
OUTER = [(-77.0, -12.0), (-76.9, -12.0), (-76.9, -12.1), (-77.0, -12.1), (-77.0, -12.0)]
HOLE = [(-76.97, -12.03), (-76.97, -12.07), (-76.93, -12.07), (-76.93, -12.03), (-76.97, -12.03)]
ROW = {'UBIGEO': '150142', 'N_RIESGO': 'MA', 'NOMBDIST': 'VILLA EL SALVADOR', 'NOMBPROV': 'LIMA', 'NOMBDEP': 'LIMA'}


class ListTests(unittest.TestCase):
    def test_validates_sorts_and_makes_end_exclusive(self):
        payload = {'recordsTotal': 3, 'data': [
            {'id': '10', 'id_documento': '5', 'inicio_vigencia': '2026-11-05 00:00:01', 'fin_vigencia': '2026-11-06 23:59:01', 'ambito': 'COSTA', 'fuente': '454'},
            {'id': 'x', 'id_documento': '5', 'inicio_vigencia': '2026-11-05', 'fin_vigencia': '2026-11-06'},
            {'id': '11', 'id_documento': '6', 'inicio_vigencia': '2026-11-08 00:00:01', 'fin_vigencia': '2026-11-09 23:59:01', 'ambito': 'SELVA', 'fuente': '457'}]}
        rows, omitted = rl.scenarios(payload)
        self.assertEqual([r['id'] for r in rows], ['11', '10'])
        self.assertEqual(omitted, 1)
        self.assertEqual(rows[1]['fin'], '2026-11-07T00:00:00-05:00')
        self.assertEqual([rl.status(r, NOW) for r in rows], ['proximo', 'vigente'])
        self.assertEqual([r['id'] for r in rl.choose(rows, NOW)], ['11', '10'])

    def test_count_mismatch_fails(self):
        with self.assertRaises(rl.FormatError):
            rl.scenarios({'recordsTotal': 2, 'data': []})


class ShapefileTests(unittest.TestCase):
    def test_district_with_hole(self):
        fc = rl.districts(archive([[OUTER, HOLE]], [(False, ROW)]))
        feature, = fc['features']
        self.assertEqual(feature['properties']['nivel'], 'Muy alto')
        polygon, = feature['geometry']['coordinates']
        self.assertEqual(len(polygon), 2)  # exterior + its hole

    def test_deleted_rows_keep_alignment(self):
        other = dict(ROW, UBIGEO='150143', N_RIESGO='B')
        fc = rl.districts(archive([[OUTER], [OUTER]], [(True, ROW), (False, other)]))
        self.assertEqual([f['id'] for f in fc['features']], ['150143'])

    def test_projected_or_invalid_rows_fail(self):
        with self.assertRaisesRegex(rl.FormatError, 'proyeccion'):
            rl.districts(archive([[OUTER]], [(False, ROW)], prj='PROJCS["UTM",' + WGS84 + ']'))
        with self.assertRaisesRegex(rl.FormatError, 'row'):
            rl.districts(archive([[OUTER]], [(False, dict(ROW, N_RIESGO='X'))]))

    def test_simplify_reduces_closed_ring(self):
        ring = [(-77 + i * 0.00001, -12.0) for i in range(100)] + [(-76.99, -12.01), (-77, -12.0)]
        self.assertLess(len(rl.simplify(ring)), 10)


class RunTests(unittest.TestCase):
    def setUp(self):
        folder = Path(tempfile.mkdtemp())
        self.index, self.folder = folder / 'riesgo.json', folder / 'riesgo'
        self.list = {'recordsTotal': 1, 'data': [{'id': '10', 'id_documento': '5', 'inicio_vigencia': '2026-11-05 00:00:01',
                                                  'fin_vigencia': '2026-11-06 23:59:01', 'ambito': 'COSTA', 'fuente': '454'}]}
        self.calls = []

    def get(self, url, limit, timeout=60):
        self.calls.append(url)
        if url == rl.LIST_URL:
            return json.dumps(self.list).encode()
        return archive([[OUTER]], [(False, ROW)])

    def test_downloads_once_and_reuses(self):
        rl.run(self.index, self.folder, NOW, self.get)
        rl.run(self.index, self.folder, NOW, self.get)
        self.assertEqual(sum('_shape.zip' in c for c in self.calls), 1)
        index = json.loads(self.index.read_text())
        self.assertEqual(index['escenarios'][0]['distritos'], 1)
        self.assertTrue((self.folder / '10.json').exists())

    def test_list_failure_keeps_previous_index(self):
        rl.run(self.index, self.folder, NOW, self.get)

        def broken(url, limit, timeout=60):
            raise OSError('down')
        rl.run(self.index, self.folder, NOW, broken)
        index = json.loads(self.index.read_text())
        self.assertEqual(index['escenarios'][0]['id'], '10')
        self.assertIn('error_lista', index)


if __name__ == '__main__':
    unittest.main()
