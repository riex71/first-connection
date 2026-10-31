"""Optional self-reported support needs; no diagnostic scale or risk classification."""

PREFERENCES = {
    'social_activity': {'label': '관계 활동을 알아보고 싶어요', 'goal': '관계 취미 문화'},
    'daily_support': {'label': '안부 확인·일상 지원을 알아보고 싶어요', 'goal': '안부 일상 생활'},
    'information_only': {'label': '지금은 정보만 살펴볼게요', 'goal': None},
}
BOOLEAN_FIELDS = {
    'recent_contact': '최근 2주 동안 가족·지인과 이야기를 주고받을 기회',
    'support_available': '힘들거나 일상에 도움이 필요할 때 부탁할 사람',
}


def normalize_support(data):
    if data is None:
        data = {}
    if not isinstance(data, dict) or set(data) - {*BOOLEAN_FIELDS, 'preferred_next_step'}:
        raise ValueError('지원 필요 확인의 입력 항목을 확인하세요.')
    normalized = {}
    for field in BOOLEAN_FIELDS:
        value = data.get(field)
        if value is not None and type(value) is not bool:
            raise ValueError('지원 필요 응답은 예·아니오·미응답 중 선택하세요.')
        normalized[field] = value
    value = data.get('preferred_next_step')
    if value is not None and (not isinstance(value, str) or value not in PREFERENCES):
        raise ValueError('지금 준비하고 싶은 행동을 선택하거나 미응답으로 남겨 주세요.')
    normalized['preferred_next_step'] = value
    return normalized


def build_support_context(profile):
    answers = normalize_support(profile.get('support_check'))
    findings, unanswered = [], []
    for field, label in BOOLEAN_FIELDS.items():
        value = answers[field]
        if value is None:
            unanswered.append(label)
        else:
            findings.append({'field': field, 'label': label, 'response': value,
                             'text': label + (': 있다고 답했어요.' if value else ': 없다고 답했어요.'),
                             'basis': 'self_report'})
    preference = answers['preferred_next_step']
    if preference is None:
        unanswered.append('지금 준비하고 싶은 행동')
    else:
        findings.append({'field': 'preferred_next_step', 'label': '지금 준비하고 싶은 행동',
                         'response': preference, 'text': PREFERENCES[preference]['label'], 'basis': 'self_report'})
    guidance = []
    if answers['support_available'] is False:
        guidance.append('도움이 필요할 때 이용할 수 있는 기관·문의 경로를 후보별로 확인해 보세요. 본인 응답만으로 돌봄 자격을 충족하지는 않아요.')
    if answers['recent_contact'] is False:
        guidance.append('최근 교류 기회가 없다고 답했어요. 관계 활동을 원하는지는 별도로 선택할 수 있어요.')
    if preference == 'information_only':
        guidance.append('이번에는 공식 안내와 조건을 살펴보는 단계까지만 준비해요. 문의·신청 준비는 다음에 선택할 수 있어요.')
    return {'version': 'support-check/0.4', 'status': 'self_report_only' if findings else 'unanswered',
            'answers': answers, 'findings': findings, 'unanswered': unanswered, 'guidance': guidance,
            'risk_assessment': 'not_assessed', 'causal_assessment': 'not_assessed', 'priority_rank': None,
            'assurance': '선택한 본인 응답을 정리한 지원 필요 확인입니다. 검증된 고립 척도·위험 등급·진단이 아닙니다.'}
