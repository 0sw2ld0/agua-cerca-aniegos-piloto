from datetime import datetime
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from generate import PERU
from gpt_incidents import gate, merge, same_place

NOW = datetime(2026, 11, 6, 13, 0, tzinfo=PERU)
ARTICLE = {'url': 'https://rpp.pe/n', 'source': 'rpp.pe', 'title': 'Aniego',
           'published_at': '2026-11-05T09:00:00-05:00',
           'text': 'Este jueves, en el Sector uno, grupo uno de Villa El Salvador el desagüe colapsó. '
                   'También se inundó el acceso al Hospital de Emergencias Villa El Salvador. '
                   'Vecinos colocaron sacos en su vivienda.'}


def item(**changes):
    base = {'district': 'Villa El Salvador', 'district_quote': 'Villa El Salvador',
            'location': 'Sector 1, Grupo 1', 'location_quote': 'Sector uno, grupo uno',
            'timing': 'current', 'water_reported': 'yes', 'date_relation': 'same_day_as_publication',
            'explicit_date': '', 'date_quote': 'Este jueves', 'summary': 'Colapso del desagüe.'}
    base.update(changes)
    return base


def run(*items):
    return gate(ARTICLE, {'incidents': list(items)}, NOW, None)


class GateTests(unittest.TestCase):
    def test_literal_place_with_number_words_is_accepted(self):
        ok, bad = run(item())
        self.assertEqual((len(ok), bad), (1, []))
        self.assertEqual(ok[0]['day'], '2026-11-05')
        self.assertEqual(ok[0]['date_status'], 'stated')

    def test_invented_place_is_rejected(self):
        ok, bad = run(item(location='Av. Los Héroes', location_quote='avenida Los Héroes'))
        self.assertEqual(ok, [])
        self.assertEqual(bad[0]['reason'], 'location_not_literal')
        ok, bad = run(item(location='Sector 1, Grupo 6'))
        self.assertEqual(bad[0]['reason'], 'location_label_not_from_quote')

    def test_vague_place_background_and_no_water_are_rejected(self):
        reasons = [run(x)[1][0]['reason'] for x in (
            item(location='su vivienda', location_quote='su vivienda'),
            item(timing='background'), item(water_reported='unclear'))]
        self.assertEqual(reasons, ['location_too_vague', 'not_current:background', 'water_not_reported'])

    def test_district_can_come_from_the_literal_place_name(self):
        ok, _ = run(item(district='', district_quote='', location='Hospital de Emergencias Villa El Salvador',
                         location_quote='Hospital de Emergencias Villa El Salvador'))
        self.assertEqual(ok[0]['district'], 'Villa El Salvador')

    def test_unknown_date_is_flagged_not_invented(self):
        ok, _ = run(item(date_relation='unknown', date_quote=''))
        self.assertEqual(ok[0]['date_status'], 'publication_day')

    def test_unsupported_explicit_date_is_refused_not_replaced(self):
        ok, bad = run(item(date_relation='explicit_date', explicit_date='2026-08-16', date_quote='Este jueves'))
        self.assertEqual(ok, [])
        self.assertEqual(bad[0]['reason'], 'date_unverified')

    def test_old_episode_is_outside_window(self):
        article = dict(ARTICLE, text=ARTICLE['text'] + ' Ocurrió igual el 16 de agosto.')
        ok, bad = gate(article, {'incidents': [item(date_relation='explicit_date', explicit_date='2026-08-16',
                                                     date_quote='el 16 de agosto')]}, NOW, None)
        self.assertEqual(ok, [])
        self.assertEqual(bad[0]['reason'], 'outside_48h')


class MergeTests(unittest.TestCase):
    def test_same_crossing_merges_but_single_road_does_not(self):
        a = {'district': 'X', 'location': 'Bypass de Av. Pachacútec con Av. 26 de Noviembre', 'point': None,
             'day': '2026-11-05', 'url': 'u1', 'source': 's1'}
        b = dict(a, location='Av. 26 de Noviembre con Av. Pachacútec', url='u2', source='s2')
        c = dict(a, location='Av. Pachacútec, cuadra 34', url='u3', source='s3')
        self.assertTrue(same_place(a, b))
        self.assertFalse(same_place(a, c))
        self.assertEqual([len(g['urls']) for g in merge([a, b, c])], [2, 1])

    def test_leading_words_and_plural_do_not_split_a_place(self):
        a = {'district': 'X', 'location': 'Frente al Hospital de Emergencia de Villa El Salvador', 'point': None}
        b = dict(a, location='Hospital de Emergencias de Villa El Salvador')
        self.assertTrue(same_place(a, b))


if __name__ == '__main__':
    unittest.main()
