import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from place_evidence import address_candidates, validate_geocoding
from jev_questions import address_spans

class PlaceTests(unittest.TestCase):
    def test_complete_numbered_address_and_original_offsets(self):
        text = 'Aniego en Av. Pachacútec, cuadra 34 (Pachacútec N° 3470, Urb. Mariano Melgar – VMT). Personal trabaja.'
        values = address_candidates(text)
        self.assertEqual(len(values), 1)
        self.assertTrue(values[0]['span'].endswith('VMT)'))
        self.assertEqual(text[values[0]['start']:values[0]['end']], values[0]['span'])

    def test_intersection_keeps_both_roads_and_not_response_workers(self):
        self.assertEqual(address_spans('En Av. El Sol y la Av. Separadora Industrial, donde trabajan equipos.'), ['Av. El Sol y la Av. Separadora Industrial'])
        self.assertEqual(address_spans('En Av. Pachacútec con la Av. 26 de Noviembre, el equipo entrevistó a obreros.'), ['Av. Pachacútec con la Av. 26 de Noviembre'])

    def test_unseen_places_and_two_distinct_sites(self):
        self.assertEqual(address_spans('Agua en la calle del Río ya fue atendida.'), ['calle del Río'])
        self.assertEqual(address_spans('En Jr. Los Cedros, se reportó agua. También en pasaje Las Flores.'), ['Jr. Los Cedros', 'pasaje Las Flores'])
        self.assertEqual(address_spans('En la Av. La Paz y otro aniego en calle Las Rosas.'), ['Av. La Paz', 'calle Las Rosas'])
        self.assertEqual(address_spans('La calle quedó limpia. Hay calles circundantes y una calle sin identificar.'), [])

    def test_sector_does_not_swallow_news(self):
        self.assertEqual(address_spans('Sector 1, Grupo 1, SEDAPAL ha desplegado equipos.'), ['Sector 1, Grupo 1'])

    def test_geocoder_rejects_wrong_district_number_and_street_centroid(self):
        expected = {'district': 'Villa María del Triunfo', 'roads': ['Pachacútec'], 'house_number': '3470'}
        good = {'district': 'Villa Maria del Triunfo', 'roads': ['Pachacutec'], 'house_number': '3470', 'precision': 'house', 'point': [-76.95,-12.15]}
        self.assertEqual(validate_geocoding(expected, [good])['reason'], 'verified')
        for changes in [{'district':'Villa El Salvador'}, {'house_number':'3400'}, {'precision':'street'}, {'roads':['Industrial']}, {'point':[0,0]}]:
            self.assertIsNone(validate_geocoding(expected, [dict(good, **changes)])['point'])
        self.assertIsNone(validate_geocoding(expected,[good,dict(good,point=[-76.96,-12.15])])['point'])
        self.assertIsNone(validate_geocoding(dict(expected,district=None),[good])['point'])

    def test_intersection_requires_both_roads_and_verified_intersection(self):
        expected = {'district':'Chorrillos','roads':['El Sol','Industrial']}
        base = {'district':'Chorrillos','roads':['El Sol','Industrial'],'precision':'street','point':[-77,-12]}
        self.assertIsNone(validate_geocoding(expected,[base])['point'])
        self.assertIsNotNone(validate_geocoding(expected,[dict(base,precision='intersection')])['point'])
        self.assertIsNone(validate_geocoding(expected,[dict(base,precision='intersection',roads=['El Sol'])])['point'])
