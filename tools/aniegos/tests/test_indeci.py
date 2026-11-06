from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from generate import PERU
import indeci
from publish import merge_official, place_key

FIX = Path(__file__).parent / 'fixtures'
AHORA = datetime(2026, 11, 6, 17, 0, tzinfo=PERU)
PORTADA = (FIX / 'indeci_portada.html').read_text(errors='ignore')
TEXTOS = {n: (FIX / f'indeci_{n}.txt').read_text() for n in ('vmt_15558', 'ves_15559', 'sjm_15563')}


class PortadaTests(unittest.TestCase):
    def test_only_water_reports_in_lima_and_callao(self):
        reportes = indeci.portada(PORTADA, AHORA)
        self.assertEqual([(r['numero'], r['distrito']) for r in reportes],
                         [('15563', 'San Juan de Miraflores'), ('15559', 'Villa El Salvador'), ('15558', 'Villa María del Triunfo')])
        self.assertTrue(all(r['url'].startswith('https://portal.indeci.gob.pe/emergencias/') for r in reportes))

    def test_old_reports_are_ignored(self):
        self.assertEqual(indeci.portada(PORTADA, datetime(2026, 11, 10, 12, 0, tzinfo=PERU)), [])


class LugaresTests(unittest.TestCase):
    def test_places_named_explicitly(self):
        self.assertEqual([e for e, _ in indeci.lugares(TEXTOS['vmt_15558'])], ['Av. Pachacútec con Av. 26 de noviembre'])
        self.assertEqual([e for e, _ in indeci.lugares(TEXTOS['ves_15559'])],
                         ['Sector 1, Grupo 1', 'Panamericana Sur Km 17.5', 'Av. Sol con Av. Separadora Industrial',
                          'Av. Pumacahua con Av. La Unión', 'Colegio Juan Pablo II'])
        self.assertEqual([e for e, _ in indeci.lugares(TEXTOS['sjm_15563'])], ['Av. Los Héroes', 'Frente al Hospital María Auxiliadora'])

    def test_pdftotext_column_order(self):
        # pdftotext puts the next table cell right after the crossing.
        texto = 'ANEXO 1 Av. Sol cruce con av. Separadora Industrial Av. 200 millas cruce con MD Villa El Salvador Cisterna (P)'
        self.assertEqual([e for e, _ in indeci.lugares(texto)], ['Av. Sol con Av. Separadora Industrial'])

    def test_table_noise_is_not_a_place(self):
        for texto in TEXTOS.values():
            for etiqueta, _ in indeci.lugares(texto):
                self.assertTrue(indeci.limpia(etiqueta), etiqueta)
                self.assertNotIn('REPORTE', etiqueta.upper())

    def test_affected_roads_and_damage(self):
        self.assertEqual(indeci.vias_afectadas(TEXTOS['sjm_15563'])[:2], ['Av. Pastor Sevilla cuadra 6', 'Av. Los Héroes cuadra 8'])
        self.assertEqual(indeci.afectados(TEXTOS['sjm_15563'])[0], '5 personas afectadas')

    def test_incidents_use_report_date_and_official_source(self):
        rep = indeci.portada(PORTADA, AHORA)[0]
        filas = indeci.incidentes(rep, TEXTOS['sjm_15563'])
        self.assertEqual({f['day'] for f in filas}, {'2026-11-06'})
        self.assertTrue(all(f['sources'][0]['url'] == rep['url'] for f in filas))
        self.assertTrue(any(f['seccion'] == 'vias' for f in filas))
        self.assertTrue(all(len(f['summary']) <= 600 for f in filas))


class RunTests(unittest.TestCase):
    def setUp(self):
        carpeta = Path(tempfile.mkdtemp())
        self.salida, self.estado = carpeta / 'indeci.json', carpeta / 'estado.json'
        self.pdfs = 0

    def get(self, url, limite=0, timeout=0):
        if url == indeci.PORTADA:
            return PORTADA.encode()
        if url.endswith('.pdf'):
            self.pdfs += 1
            return url.encode()
        return f'<a href="https://portal.indeci.gob.pe/wp-content/uploads/2026/11/REPORTE-{abs(hash(url)) % 9999}.pdf">pdf</a>'.encode()

    def texto(self, datos):
        return TEXTOS['ves_15559']

    def test_pdf_is_read_once(self):
        indeci.run(self.salida, self.estado, AHORA, self.get, self.texto)
        indeci.run(self.salida, self.estado, AHORA, self.get, self.texto)
        self.assertEqual(self.pdfs, 3)
        datos = json.loads(self.salida.read_text())
        self.assertEqual(len(datos['reportes']), 3)
        self.assertTrue(datos['incidentes'])

    def test_portal_failure_keeps_last_list(self):
        indeci.run(self.salida, self.estado, AHORA, self.get, self.texto)

        def caido(url, limite=0, timeout=0):
            raise OSError('caído')
        indeci.run(self.salida, self.estado, AHORA, caido, self.texto)
        datos = json.loads(self.salida.read_text())
        self.assertTrue(datos['incidentes'])
        self.assertEqual(datos['errores'][0]['etapa'], 'portada')


class MergeTests(unittest.TestCase):
    def test_manual_review_has_priority_for_same_place_and_day(self):
        manual = [{'key': 'm', 'day': '2026-11-06', 'district': 'Villa El Salvador', 'location': 'Av. El Sol con Av. Separadora Industrial'}]
        auto = [{'key': 'a1', 'day': '2026-11-06', 'district': 'Villa El Salvador', 'location': 'Av. Sol con Av. Separadora Industrial'},
                {'key': 'a2', 'day': '2026-11-06', 'district': 'Villa El Salvador', 'location': 'Av. Pumacahua con Av. La Unión'}]
        self.assertEqual([r['key'] for r in merge_official(manual, auto)], ['m', 'a2'])
        self.assertEqual(place_key('VES', 'Bypass de Av. Pachacútec con Av. 26 de Noviembre'), place_key('VES', 'Av. Pachacútec con Av. 26 de noviembre'))
        vias_manual = [{'key': 'v', 'day': '2026-11-06', 'district': 'SJM', 'location': 'Av. Los Héroes, cuadras 1, 4 y 8, y otras vías del distrito'}]
        vias_auto = [{'key': 'x', 'day': '2026-11-06', 'district': 'SJM', 'location': 'Av. Pastor Sevilla cuadra 6, y otras 9 vías', 'seccion': 'vias'}]
        self.assertEqual([r['key'] for r in merge_official(vias_manual, vias_auto)], ['v'])


if __name__ == '__main__':
    unittest.main()
