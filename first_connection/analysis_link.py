"""Carry aggregate analysis context into a plan without inventing risk or ranking."""
import hashlib
from .engine import read_regional_signals


def build_analysis_context(csv_text, profile):
    if not isinstance(csv_text, str) or len(csv_text.encode('utf-8')) > 180000:
        raise ValueError('분석 CSV는 UTF-8 180KB 이내 문자열이어야 합니다.')
    rows = read_regional_signals(csv_text) if csv_text else []
    matched = [row for row in rows if row['region'] == profile['region'] and
               (profile['age'] is None or row['age_min'] <= profile['age'] <= row['age_max'])]
    stages = [
        ('risk_signal', '위험 검토용 활동 신호 입력', lambda r: bool(r['signal_label'] and r['evidence'])),
        ('risk_level', '위험 수준 입력', lambda r: bool(r['risk_level'])),
        ('cause_analysis', '위험 원인 해석 입력', lambda r: bool(r['cause_summary'])),
        ('priority_target', '우선 지원 대상 입력', lambda r: bool(r['priority_rank'] and r['priority_basis']))
    ]
    coverage = []
    for key, label, available in stages:
        actual = any(r['data_kind'] == 'analysis' and available(r) for r in matched)
        synthetic = any(r['data_kind'] == 'synthetic' and available(r) for r in matched)
        status = 'provided_unverified' if actual else ('synthetic_only' if synthetic else 'not_connected')
        coverage.append({'stage': key, 'label': label, 'status': status})
    return {'schema_version': 'analysis-context/0.2.1', 'input_present': bool(csv_text),
            'input_sha256': hashlib.sha256(csv_text.encode('utf-8')).hexdigest() if csv_text else None,
            'input_row_count': len(rows), 'matched_row_count': len(matched), 'matched_rows': matched,
            'coverage': coverage, 'age_match': 'not_filtered' if profile['age'] is None else 'filtered',
            'use': '지역·계층 맥락과 누락 단계 기록. 개인 위험·자격·추천 점수·지역 지원 순위를 산출하지 않음.',
            'input_assurance': 'analysis는 제공자의 구분값이며 데이터 진위·모델 성능·대회 요건 충족을 인증하지 않음.'}


def attach_analysis_context(plan, context):
    plan['analysis_context'] = context
    plan['workflow_status'] = context['coverage'][:3] + [
        {'stage': 'resource_recommendation', 'label': '복지자원 조건 검토·계획',
         'status': 'executed', 'mode': plan['mode'], 'candidate_count': len(plan['recommendations'])}
    ] + context['coverage'][3:]
    plan['trace'].insert(0, {'tool': 'read_analysis_context', 'result': {
        'input_sha256': context['input_sha256'], 'matched_row_count': context['matched_row_count'],
        'coverage': context['coverage'], 'changes_recommendation_score': False}})
    return plan
