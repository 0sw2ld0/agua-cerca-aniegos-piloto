"""News article -> incident drafts. The model extracts; code decides what is publishable.

The model must quote the article literally for district, place and date. Code checks every
quote, resolves the date, requires an OSM-locatable place and merges duplicates. An empty or
failed extraction is never evidence that no flooding happened.
"""
from datetime import datetime, timedelta
import math
import re
import unicodedata

from osm_places import parse_location, road_core

LIMA_DISTRICTS = [
    'Ancón', 'Ate', 'Barranco', 'Breña', 'Carabayllo', 'Chaclacayo', 'Chorrillos', 'Cieneguilla', 'Comas',
    'El Agustino', 'Independencia', 'Jesús María', 'La Molina', 'La Victoria', 'Lima', 'Lince',
    'Los Olivos', 'Lurigancho', 'Lurín', 'Magdalena del Mar', 'Miraflores', 'Pachacámac', 'Pucusana',
    'Pueblo Libre', 'Puente Piedra', 'Punta Hermosa', 'Punta Negra', 'Rímac', 'San Bartolo', 'San Borja',
    'San Isidro', 'San Juan de Lurigancho', 'San Juan de Miraflores', 'San Luis', 'San Martín de Porres',
    'San Miguel', 'Santa Anita', 'Santa María del Mar', 'Santa Rosa', 'Santiago de Surco', 'Surquillo',
    'Villa El Salvador', 'Villa María del Triunfo', 'Callao', 'Bellavista', 'Carmen de la Legua Reynoso',
    'La Perla', 'La Punta', 'Mi Perú', 'Ventanilla']
ABBREVIATIONS = {'Villa El Salvador': ['VES'], 'Villa María del Triunfo': ['VMT'],
                 'San Juan de Miraflores': ['SJM'], 'San Juan de Lurigancho': ['SJL'],
                 'San Martín de Porres': ['SMP'], 'Santiago de Surco': ['Surco']}

SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['incidents'],
    'properties': {'incidents': {'type': 'array', 'items': {
        'type': 'object', 'additionalProperties': False,
        'required': ['district', 'district_quote', 'location', 'location_quote', 'timing', 'water_reported',
                     'date_relation', 'explicit_date', 'date_quote', 'summary'],
        'properties': {
            'district': {'type': 'string', 'enum': LIMA_DISTRICTS + ['']},
            'district_quote': {'type': 'string'},
            'location': {'type': 'string'},
            'location_quote': {'type': 'string'},
            'timing': {'type': 'string', 'enum': ['current', 'background', 'forecast_or_risk', 'unclear']},
            'water_reported': {'type': 'string', 'enum': ['yes', 'no', 'unclear']},
            'date_relation': {'type': 'string', 'enum': ['same_day_as_publication', 'previous_day',
                                                         'explicit_date', 'unknown']},
            'explicit_date': {'type': 'string'},
            'date_quote': {'type': 'string'},
            'summary': {'type': 'string'},
        }}}}}

INSTRUCTIONS = """Extraes aniegos (agua o desagüe acumulado en calles, pasos a desnivel, viviendas o accesos) de UNA nota periodística sobre Lima y Callao.

Devuelve un elemento por cada LUGAR afectado. Si la nota menciona varios lugares, sepáralos. Si un lugar se repite, inclúyelo una vez.

Reglas estrictas:
- district_quote, location_quote y date_quote deben ser copias LITERALES de un fragmento de la nota (mismas palabras y tildes). Si la nota no lo dice, deja "".
- district: el distrito donde la nota sitúa ESE lugar, solo si la nota lo dice. No lo deduzcas por conocimiento propio ni uses el distrito de una autoridad que atendió.
- location: el lugar en forma corta para un mapa, construida SOLO con palabras de location_quote. Formatos: "Av. X con Av. Y" para un cruce; "Bypass de Av. X con Av. Y" si es un paso a desnivel en un cruce; "Sector N, Grupo M"; "Frente al Hospital ..." para un establecimiento; o la vía sola si no hay más precisión. Nunca inventes una vía, número o cruce.
- timing: current si el agua se reporta en el episodio que cubre la nota; background si es un antecedente de otro episodio; forecast_or_risk si es pronóstico, riesgo o zona vulnerable sin agua reportada.
- water_reported: yes solo si la nota afirma agua acumulada, inundación o desborde en ese lugar.
- date_relation: same_day_as_publication si la nota dice "hoy", "esta mañana", "este jueves" y coincide con su publicación; previous_day si dice "ayer" o "la noche anterior"; explicit_date si da día y mes; unknown si no lo dice. explicit_date en formato AAAA-MM-DD solo con explicit_date.
- summary: máximo 240 caracteres, con tus palabras, sin copiar frases, sin afirmar que sigue activo.
- Ignora notas de otras regiones fuera de Lima y Callao, recomendaciones generales y estadísticas sin lugar.
"""


def norm(value):
    value = ''.join(c for c in unicodedata.normalize('NFD', value.lower()) if unicodedata.category(c) != 'Mn')
    return re.sub(r'[^a-z0-9]+', ' ', value).strip()


NUMBER_WORDS = {'uno': '1', 'dos': '2', 'tres': '3', 'cuatro': '4', 'cinco': '5', 'seis': '6', 'siete': '7',
                'ocho': '8', 'nueve': '9', 'diez': '10', 'primer': '1', 'primero': '1', 'segundo': '2', 'tercer': '3'}
PLACE_WORD = re.compile(r'\b(?:av|avenida|calle|jiron|jr|pasaje|psje|bypass|baipas|desnivel|ovalo|puente|intercambio|'
                        r'hospital|clinica|colegio|mercado|sector|grupo|urbanizacion|asentamiento|panamericana|'
                        r'carretera|prolongacion|alameda|malecon|via)\b')


def canonical(value):
    """Spell numbers as digits so "Sector uno, grupo uno" == "Sector 1, Grupo 1"."""
    return re.sub(r'\b(%s)\b' % '|'.join(NUMBER_WORDS), lambda m: NUMBER_WORDS[m[1].lower()], value, flags=re.I)


def district_in(quote):
    for district in sorted(LIMA_DISTRICTS, key=len, reverse=True):
        if district != 'Lima' and district_supported(district, quote):
            return district
    return None


NEIGHBOURS = {  # Lima Sur: media often name the neighbouring district of a border crossing.
    'Villa El Salvador': ['Villa María del Triunfo', 'San Juan de Miraflores', 'Lurín', 'Chorrillos'],
    'Villa María del Triunfo': ['Villa El Salvador', 'San Juan de Miraflores', 'Pachacámac', 'La Molina', 'Ate'],
    'San Juan de Miraflores': ['Villa María del Triunfo', 'Villa El Salvador', 'Santiago de Surco', 'Chorrillos'],
    'Chorrillos': ['Santiago de Surco', 'San Juan de Miraflores', 'Villa El Salvador', 'Barranco'],
}
LEAD = re.compile(r'^(?:frente\s+a(?:l)?|exteriores\s+del?|inmediaciones\s+del?|accesos?\s+al?|cerca\s+del?)\s+', re.I)


def core(location):
    """Comparable label: text before '·', without 'frente al'/'exteriores del', singularised words."""
    head = LEAD.sub('', canonical(location).split('·')[0].strip())
    return ' '.join(t[:-1] if len(t) > 4 and t.endswith('s') else t for t in road_core(head).split())


def request(article):
    text = article['text'][:14000]
    return {'instructions': INSTRUCTIONS,
            'input': 'Publicación: %s\nTítulo: %s\n\nNota:\n%s' % (article['published_at'], article['title'], text),
            'text': {'format': {'type': 'json_schema', 'name': 'aniegos', 'schema': SCHEMA, 'strict': True}}}


def quoted(quote, text):
    return bool(quote) and norm(quote) in norm(text)


def district_supported(district, quote):
    if not district or not quote:
        return False
    names = [district] + ABBREVIATIONS.get(district, [])
    return any(re.search(r'\b%s\b' % re.escape(norm(n)), norm(quote)) for n in names)


FORMAT_WORDS = {'av', 'avenida', 'con', 'y', 'de', 'del', 'la', 'el', 'los', 'las', 'bypass', 'baipas', 'frente',
                'al', 'a', 'en', 'calle', 'jr', 'jiron', 'pasaje', 'ovalo', 'cruce', 'exteriores', 'paso', 'desnivel'}


def location_from_quote(location, quote):
    """Every content word of the map label must come from the literal quote (no invented roads)."""
    words = set(norm(canonical(quote)).split())
    return bool(norm(location)) and set(norm(canonical(location)).split()) - FORMAT_WORDS <= words


def resolve_day(item, published):
    day = published.date()
    if item['date_relation'] == 'same_day_as_publication':
        return day
    if item['date_relation'] == 'previous_day':
        return day - timedelta(days=1)
    if item['date_relation'] == 'explicit_date' and item['date_quote']:
        try:
            stated = datetime.strptime(item['explicit_date'], '%Y-%m-%d').date()
        except ValueError:
            return None
        if str(stated.day) in re.findall(r'\d+', item['date_quote']) and stated <= day:
            return stated
    return None


def gate(article, extraction, now, locator=None, check_window=True):
    """Return (incidents, rejected) with a reason for every rejection."""
    accepted, rejected = [], []
    published = datetime.fromisoformat(article['published_at'].replace('Z', '+00:00')).astimezone(now.tzinfo)
    for item in extraction.get('incidents', []):
        reason = None
        if item['timing'] != 'current':
            reason = 'not_current:' + item['timing']
        elif item['water_reported'] != 'yes':
            reason = 'water_not_reported'
        elif not quoted(item['location_quote'], article['text']):
            reason = 'location_not_literal'
        elif not PLACE_WORD.search(norm(item['location'])):
            reason = 'location_too_vague'
        if not reason and not (item['district'] and quoted(item['district_quote'], article['text'])
                               and district_supported(item['district'], item['district_quote'])):
            # The place name itself may carry the district ("Hospital de Emergencias Villa El Salvador").
            inferred = district_in(item['location_quote'])
            if inferred:
                item = {**item, 'district': inferred, 'district_quote': item['location_quote']}
            else:
                reason = 'district_not_literal'
        if not reason and item['date_quote'] and not quoted(item['date_quote'], article['text']):
            reason = 'date_not_literal'
        day = resolve_day(item, published) if not reason else None
        date_status = 'stated'
        if not reason and day is None and item['date_relation'] == 'explicit_date':
            reason = 'date_unverified'  # a stated date the quote does not support is never replaced
        if not reason and day is None:
            # Not invented: shown as "Fecha del hecho por verificar", dated by the publication.
            day, date_status = published.date(), 'publication_day'
        if not reason and check_window:
            start = datetime(day.year, day.month, day.day, tzinfo=now.tzinfo)
            if not start <= now < start + timedelta(hours=48):
                reason = 'outside_48h'
        if not reason and not location_from_quote(item['location'], item['location_quote']):
            reason = 'location_label_not_from_quote'
        if reason:
            rejected.append({**item, 'reason': reason, 'url': article['url']})
            continue
        item = {**item, 'location': canonical(item['location'])}
        review = {'district': item['district'], 'location': item['location']}
        found = locator.locate(review) if locator else None
        last = locator.reason_for(review) if locator else None
        if not found and last in ('no_junction_in_district', 'outside_district'):
            # The roads meet, but not in the district the article named. Accept a neighbour only
            # if exactly one has that crossing (geography, not a guess); otherwise a human decides.
            options = []
            for other in NEIGHBOURS.get(item['district'], []):
                candidate = locator.locate({'district': other, 'location': item['location']})
                if candidate:
                    options.append((other, candidate))
            if len(options) != 1:
                rejected.append({**item, 'reason': 'district_conflict', 'url': article['url']})
                continue
            item = {**item, 'district_stated': item['district'], 'district': options[0][0]}
            found = options[0][1]
        accepted.append({**item, 'day': day.isoformat(), 'date_status': date_status,
                         'url': article['url'], 'source': article['source'],
                         'rule': (parse_location(item['location']) or {}).get('kind'),
                         'point': found['point'] if found else None,
                         'precision': found['precision'] if found else 'Sin punto verificado'})
    return accepted, rejected


def distance_m(a, b):
    dy = (a[1] - b[1]) * 111320
    dx = (a[0] - b[0]) * 111320 * math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot(dx, dy)


def same_place(a, b):
    if a['district'] != b['district']:
        return False
    if a['point'] and b['point']:
        return distance_m(a['point'], b['point']) <= 150
    pa, pb = parse_location(canonical(a['location'])), parse_location(canonical(b['location']))
    if pa and pb and pa['kind'] == pb['kind'] == 'intersection':
        return set(pa['roads']) == set(pb['roads'])
    if (pa and pa['kind'] == 'intersection') != (pb and pb['kind'] == 'intersection'):
        return False  # a crossing is never the same place as a single road
    return bool(core(a['location'])) and core(a['location']) == core(b['location'])


def merge(incidents):
    """One incident per (day, place); sources accumulate."""
    groups = []
    for item in incidents:
        for group in groups:
            if group['day'] == item['day'] and same_place(group, item):
                if item['url'] not in group['urls']:
                    group['urls'].append(item['url'])
                    group['sources'].append(item['source'])
                break
        else:
            groups.append({**item, 'urls': [item['url']], 'sources': [item['source']]})
    return groups
