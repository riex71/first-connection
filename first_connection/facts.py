"""Validate mapped dates and prices against their specific evidence strings."""
import re
from datetime import date


def parse_date_range(text, year):
    text = re.sub(r"\([^)]*\)", "", text)
    text = re.sub(r"\s+", "", text)
    explicit_year = re.search(r"(20\d{2})년", text)
    if explicit_year:
        year = int(explicit_year[1])
        text = text[explicit_year.end():]
    parts = text.split("~")
    dates = []
    month = None
    for part in parts:
        match = re.search(r"(?P<month>\d{1,2})[월./](?P<day>\d{1,2})(?:일|\.)?", part)
        if match:
            month, day = int(match['month']), int(match['day'])
        else:
            match = re.search(r"(\d{1,2})일", part)
            if match and month:
                day = int(match[1])
            else:
                raise ValueError("모집일을 구조화할 수 없는 원문 형식")
        dates.append(date(year, month, day).isoformat())
    if len(dates) == 1:
        return None, dates[0]
    if len(dates) != 2 or dates[0] > dates[1]:
        raise ValueError("지원하지 않는 모집일 범위")
    return tuple(dates)


def validate_field_checks(service, normalized_source):
    for check in service.get('field_checks', []):
        evidence = check['evidence']
        if re.sub(r'\s+', '', evidence) not in normalized_source:
            raise ValueError('필드별 원문 근거 누락')
        if check['kind'] == 'date_range':
            start, end = parse_date_range(evidence, check['year'])
            if (start, end) != (service['application_start'], service['application_end']):
                raise ValueError('모집기간 매핑과 원문 날짜 불일치')
        elif check['kind'] == 'cost':
            match = re.search(r'(\d[\d,]*)\s*원', evidence)
            amount = int(match[1].replace(',', '')) if match else (0 if '무료' in evidence else None)
            if amount is None or amount != service['cost_krw']:
                raise ValueError('참가비 매핑과 원문 금액 불일치')
        else:
            raise ValueError('지원하지 않는 필드 검증 형식')
