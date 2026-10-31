"""Operational aggregate-activity review states, never personal isolation risk."""
import math

STATES = {
    'verify_model': ('자료·모형 확인', ['보정월과 다른 거래 움직임·휴일·계절 영향을 먼저 확인',
                                    '모형 경보를 개인 고립 위험이나 지원 자격으로 확정하지 않음',
                                    '현재 원하는 지원은 별도로 선택해 탐색']),
    'verify_data': ('관측 자료 확인', ['격자 누락·관측범위를 먼저 확인', '현재 지원 희망은 지역 지표와 별도로 확인']),
    'follow_up': ('활동 동반 신호 확인', ['거래 지속감소와 유동량 변화를 함께 검토', '원할 때 현재 관계·지원 필요 확인', '확정 조건을 지키는 자원·첫 행동 검토']),
    'check_context': ('추가 맥락 확인', ['휴일·계절·지역 공통 변화 가능성 검토', '원할 때 현재 관계·지원 필요 확인', '선택한 지원 목표에 맞는 자원 검토']),
    'routine': ('관측 신호 제한', ['현재 자료에서 뚜렷한 동반 신호는 제한적', '지원 필요가 없다는 뜻은 아니며 선택한 목표로 자원 탐색']),
}


def classify_activity(row, policy):
    required = ['cell_coverage', 'baseline_population_mass_coverage', 'longest_below_streak', 'population_change_pct']
    if any(not isinstance(row.get(k), (int, float)) or isinstance(row.get(k), bool)
           or not math.isfinite(row[k]) for k in required):
        return 'verify_data'
    if (row['cell_coverage'] < policy['min_cell_coverage'] or
            row['baseline_population_mass_coverage'] < policy['min_population_mass_coverage']):
        return 'verify_data'
    card = row['longest_below_streak'] >= policy['persistent_days']
    population = row['population_change_pct'] <= policy['population_decline_pct']
    return 'follow_up' if card and population else ('check_context' if card or population else 'routine')


def assign_review_order(rows):
    # This orders additional data/context checks. No welfare allocation or person ranking.
    queue = sorted([r for r in rows if r['state'] in {'follow_up', 'check_context'}],
                   key=lambda r: (0 if r['state'] == 'follow_up' else 1,
                                  -r['longest_below_streak'], r['population_change_pct'], r['region'], r['age_min']))
    for row in rows:
        row['review_order'] = None
    for order, row in enumerate(queue, 1):
        row['review_order'] = order
    return queue
