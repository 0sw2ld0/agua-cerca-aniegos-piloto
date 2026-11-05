"""Auditable questions and publication rules; no free-form model generation."""
import re
from datetime import datetime, timedelta
from generate import PERU, normalized

YES = 0.7
NO = 0.3
MODEL = 'jev-latest'
VERSION = 'blind-state-3'


def questions_for(fragment_key):
    target = f'`fragments.{fragment_key}.text`'
    return {
        'observed': {'type': 'noul', 'instructions': f'¿{target} describe un aniego que efectivamente ocurrió?',
                     'criteria': {'true': 'Relata agua acumulada o un aniego observado, incluso si después fue atendido.',
                                  'false': 'Solo pronostica riesgo, recomienda prevención o describe limpieza sin afirmar un aniego ocurrido.'}},
        'historical': {'type': 'noul', 'instructions': f'¿El episodio de {target} se presenta como antecedente de otro episodio?',
                       'criteria': {'true': 'Recuerda un episodio distinto del principal como contexto o comparación.',
                                    'false': 'Informa el episodio principal o su seguimiento, atención o limpieza, aunque haya empezado antes. Un seguimiento de la resolución no es otro episodio.'}},
        'water_present': {'type': 'noul', 'instructions': f'¿{target} afirma que hay agua acumulada en el lugar al momento de la observación reportada?',
                          'criteria': {'true': 'Describe un aniego presente o agua que sigue acumulada en el lugar.',
                                       'false': 'Dice que se retiró el agua, solo recuerda un episodio o no informa presencia de agua durante esa observación.'}},
        'attended': {'type': 'noul', 'instructions': f'¿{target} informa que el lugar ya fue atendido?',
                     'criteria': {'true': 'Declara finalizada la atención, removida el agua o el lugar limpio.',
                                  'false': 'Solo anuncia equipos, trabajos aún en curso, ausencia de equipos o atención solicitada.'}},
        'removal_work': {'type': 'noul', 'instructions': f'¿{target} informa trabajos de extracción de agua en curso?',
                         'criteria': {'true': 'Describe bombas, hidrojet o personal retirando agua durante la observación.',
                                      'false': 'Los trabajos ya terminaron, solo son una propuesta, el equipo está averiado o no se informa extracción de agua.'}},
    }


def batch_payload(document, fragments):
    fragments = {'passage_' + str(i): fragment for i, fragment in enumerate(fragments.values())}
    questions = {}
    for key in fragments:
        questions.update({key + '__' + name: question for name, question in questions_for(key).items()})
    return {'model': MODEL, 'state': {'document': document, 'fragments': fragments}, 'questions': questions}


def verdict(answers, key):
    result = {}
    for name in questions_for(key):
        answer = answers.get(key + '__' + name, {})
        p = answer.get('noul')
        if answer.get('type') != 'noul' or type(p) not in (int, float) or not 0 <= p <= 1:
            raise ValueError('Missing or invalid typed answer; not classified as negative.')
        result[name] = {'probability': p, 'value': True if p >= YES else False if p <= NO else None}
    return result


def route(values, day, now):
    """Dates and action policy belong to code, never a model's calendar math."""
    try:
        start = datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=PERU)
    except (TypeError, ValueError):
        return 'review_date'
    if not start <= now < start + timedelta(hours=48):
        return 'exclude_expired_or_future'
    if values['historical']['value'] is True:
        return 'exclude_background'
    if values['historical']['value'] is None:
        return 'review'
    if values['attended']['value'] is True:
        return 'attended_according_to_source'
    if values['observed']['value'] is False:
        return 'exclude_not_observed'
    if values['observed']['value'] is None or values['attended']['value'] is None:
        return 'review'
    if values['water_present']['value'] is True and values['removal_work']['value'] is True:
        return 'in_attention_according_to_source'
    if values['water_present']['value'] is True:
        return 'water_reported_according_to_source'
    return 'review'


def address_spans(text):
    from place_evidence import address_candidates
    return [item['span'] for item in address_candidates(text)]


def date_spans(text, published_day):
    months = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre']
    base = datetime.strptime(published_day, '%Y-%m-%d').replace(tzinfo=PERU)
    dates = []
    pattern = r'\b(\d{1,2})\s+de\s+(' + '|'.join(months) + r')(?:\s+de\s+(\d{4}))?\b'
    for match in re.finditer(pattern, text, flags=re.IGNORECASE):
        try:
            day = base.replace(year=int(match[3]) if match[3] else base.year,
                               month=months.index(match[2].lower()) + 1, day=int(match[1]))
        except ValueError:
            continue
        dates.append({'span': match[0], 'date': day.strftime('%Y-%m-%d'),
                      'year_inferred': not bool(match[3])})
    for match in re.finditer(r'\b(hoy|ayer|mañana)\b', text, flags=re.IGNORECASE):
        day = base + timedelta(days={'hoy': 0, 'ayer': -1, 'mañana': 1}[match[0].lower()])
        dates.append({'span': match[0], 'date': day.strftime('%Y-%m-%d'), 'year_inferred': False})
    return dates


def extractive_payload(document, fragment):
    places = address_spans(fragment)
    options = {'place_' + str(i): label for i, label in enumerate(places)}
    dates = date_spans(fragment, document['published'][:10])
    date_options = {'date_' + str(i): item['span'] for i, item in enumerate(dates)}
    questions = {
        'has_place': {'type': 'noul', 'instructions': '¿Alguna referencia de `places` identifica el lugar principal descrito en `fragment`?'},
        'has_explicit_date': {'type': 'noul', 'instructions': '¿`fragment` indica la fecha en que ocurrió el aniego?',
                              'criteria': {'true': 'El fragmento asigna un día absoluto o relativo al episodio.',
                                           'false': 'Solo conocemos cuándo se publicó el documento o el fragmento no indica un día del episodio.'}},
    }
    if options:
        questions['place'] = {'type': 'choice', 'instructions': '¿Qué referencia de `places` ubica con más detalle el lugar principal descrito en `fragment`?', 'criteria': options}
    if date_options:
        questions['date'] = {'type': 'choice', 'instructions': '¿Qué expresión de `dates` indica cuándo ocurrió el aniego de `fragment`?', 'criteria': date_options}
    return {'model': MODEL, 'state': {'document': document, 'fragment': fragment, 'places': options, 'dates': date_options}, 'questions': questions}
