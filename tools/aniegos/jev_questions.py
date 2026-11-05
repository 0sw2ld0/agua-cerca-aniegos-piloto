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
    relative = {'hoy': 0, 'ayer': -1, 'anteayer': -2,
                'esta mañana': 0, 'esta madrugada': 0, 'anoche': -1}
    weekdays = ('lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado', 'domingo')
    # "El día anterior" has no fixed anchor and bare "mañana" can mean morning.
    expressions = '|'.join((*relative, *weekdays))
    for match in re.finditer(r'\b(?:' + expressions + r')\b', text, flags=re.IGNORECASE):
        span = match[0].lower()
        if span in relative:
            day = base + timedelta(days=relative[span])
        else:
            if re.search(r'\b(?:próximo|próxima|siguiente)\s*$', text[:match.start()], re.I):
                continue
            if re.match(r'\s+\d{1,2}\b', text[match.end():]):
                continue  # Never read a weekday separately from its explicit calendar date.
            day = base - timedelta(days=(base.weekday() - weekdays.index(span)) % 7)
        dates.append({'span': match[0], 'date': day.strftime('%Y-%m-%d'), 'year_inferred': False})
    return dates


def date_extraction(document, fragment, context=()):
    places = address_spans(fragment)
    options = {'place_' + str(i): label for i, label in enumerate(places)}
    questions = {
        'has_place': {'type': 'noul', 'instructions': '¿Alguna referencia de `places` identifica el lugar principal descrito en `fragment`?'},
    }
    if options:
        questions['place'] = {'type': 'choice', 'instructions': '¿Qué referencia de `places` ubica con más detalle el lugar principal descrito en `fragment`?', 'criteria': options}
    dates = {}
    for segment, text in [('fragment', fragment), *context]:
        for item in date_spans(text, document['published'][:10]):
            if len(dates) >= 8:
                break
            key = 'date_' + str(len(dates))
            dates[key] = {**item, 'segment': segment}
            target = 'fragment' if segment == 'fragment' else 'context.' + segment
            questions[key] = {
                'type': 'noul',
                'instructions': f'¿La expresión literal «{item["span"]}» en `{target}` fecha la ocurrencia o la observación del mismo aniego descrito en `fragment`?',
                'criteria': {
                    'true': 'El texto vincula esa expresión con el agua acumulada del mismo episodio y lugar. Si solo indica un día de semana, se refiere a esa semana respecto de la publicación original.',
                    'false': 'Es una fecha de publicación, una fecha de limpieza o visita sin observación del aniego, un pronóstico, parte de una dirección, otro lugar, otro episodio o no se puede establecer el vínculo.',
                },
            }
    state = {'document': document, 'fragment': fragment, 'context': dict(context), 'places': options}
    return {'model': MODEL, 'state': state, 'questions': questions}, dates


def confirmed_date(answers, dates):
    supported = []
    for key, date in dates.items():
        answer = answers.get(key, {})
        p = answer.get('noul')
        if answer.get('type') != 'noul' or type(p) not in (int, float) or not 0 <= p <= 1:
            raise ValueError('Invalid typed date answer')
        if p >= YES:
            supported.append(date)
    if len({date['date'] for date in supported}) == 1:
        return next((date for date in supported if not date['year_inferred']), None)
    return None
