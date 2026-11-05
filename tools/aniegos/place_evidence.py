"""Literal place candidates. Offsets refer to the original, unmodified article."""
import re
import unicodedata

START = re.compile(r'\b(?:Av\.|avenida|calle|pasaje|jir[oó]n|Jr\.|hospital|sector\s+\d+\s*,?\s*grupo\s+\d+)\s*', re.I)
STOP = re.compile(r',?\s+(?:donde\b|se\s+(?:encuentra|encuentran|report[oó]|verific[oó])\b|ya\s+fue\b|est[aá]\s+(?:limpia|inundada)\b|personal\s+de\b|y\s+(?:otro|otra|tambi[eé]n)\b)', re.I)


def address_candidates(text):
    # Replace only abbreviation periods, preserving character offsets.
    protected = list(text)
    for match in re.finditer(r'\b(?:Av|Jr|Urb|Nro)\.', text, re.I):
        protected[match.end() - 1] = ' '
    masked = ''.join(protected)
    output = []
    for match in START.finditer(text):
        if any(item['start'] <= match.start() < item['end'] for item in output):
            continue  # second road in an intersection or an address inside parentheses
        head = match.group().strip()
        if head.lower().startswith('sector'):
            label = text[match.start():match.end()].rstrip(' ,')
            output.append({'span': label, 'start': match.start(), 'end': match.start() + len(label)})
            continue
        following_word = re.match(r'[\wÁÉÍÓÚÑáéíóúñ]+', text[match.end():])
        if not following_word:
            continue
        word = following_word.group()
        if not word[0].isupper() and word.lower() not in ('de', 'del', 'la', 'los', 'las', 'el'):
            continue
        tail = masked[match.start():]
        sentence_end = re.search(r'[.;\n]', tail)
        end = match.start() + (sentence_end.start() if sentence_end else len(tail))
        clause = STOP.search(text, match.end(), end)
        if clause:
            end = clause.start()
        descriptive = re.search(
            r',\s*(?:el\b|la\b|se\b|dejando\b|SEDAPAL\b)|\s+(?:podr[ií]a|permanecer[aá]|qued[oó]|ha\s+desplegado|en\s+la\s+zona)\b|[\s\u00a0]{2,}',
            text[match.end():end], re.I)
        if descriptive:
            end = match.end() + descriptive.start()
        # A second named road starts a separate place unless connected as an intersection.
        for following in START.finditer(text, match.end(), end):
            bridge = text[match.end():following.start()]
            connected = re.search(r'(?:\by\s+(?:la\s+)?|\bcon\s+(?:la\s+)?|\bcruce\s+de\s+(?:la\s+)?)$', bridge, re.I)
            if '(' not in bridge and not connected:
                # A parenthetical repeat is already included in the outer address.
                end = following.start()
                break
        label = text[match.start():end].rstrip(' ,')
        if not label or len(label) > 350:
            continue  # do not silently truncate a long or malformed address
        if re.fullmatch(r'(?:calle|pasaje|avenida)\s*(?:sin\s+(?:nombre|identificar))?', label, re.I):
            continue
        output.append({'span': label, 'start': match.start(), 'end': match.start() + len(label)})
    return output


def normalized_place(value):
    value = ''.join(c for c in unicodedata.normalize('NFD', value.lower()) if unicodedata.category(c) != 'Mn')
    return re.sub(r'[^a-z0-9]+', ' ', value).strip()


def validate_geocoding(expected, candidates):
    """Conservative gate for provider adapters. Caller must supply verified precision.

    'expected' is explicit source evidence, never a generated/guessed district.
    Intersection adapters must verify BOTH roads; a street centroid cannot do so.
    """
    accepted = []
    reasons = set()
    if not expected.get('district'):
        return {'point': None, 'reason': 'district_unknown'}
    for candidate in candidates:
        if normalized_place(candidate.get('district', '')) != normalized_place(expected['district']):
            reasons.add('district_mismatch')
            continue
        roads = {normalized_place(road) for road in candidate.get('roads', [])}
        if not {normalized_place(road) for road in expected.get('roads', [])} <= roads:
            reasons.add('road_mismatch')
            continue
        if not expected.get('roads'):
            reasons.add('street_evidence_missing')
            continue
        if expected.get('house_number'):
            if str(candidate.get('house_number')) != str(expected['house_number']) or candidate.get('precision') != 'house':
                reasons.add('house_number_unverified')
                continue
        elif len(expected['roads']) >= 2:
            if candidate.get('precision') != 'intersection':
                reasons.add('intersection_unverified')
                continue
        else:
            reasons.add('insufficient_precision')
            continue
        point = candidate.get('point')
        if (not isinstance(point, list) or len(point) != 2 or
                any(type(x) not in (int, float) for x in point) or
                not (-82 <= point[0] <= -68 and -19 <= point[1] <= 1)):
            reasons.add('invalid_peru_coordinate')
            continue
        accepted.append(candidate)
    unique = {tuple(c['point']) for c in accepted}
    if len(unique) == 1:
        return {'point': list(next(iter(unique))), 'reason': 'verified'}
    return {'point': None, 'reason': 'ambiguous' if unique else ','.join(sorted(reasons)) or 'no_candidates'}
