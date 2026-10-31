"""Verified retrospective explanatory context, without replacing the detector."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_drift_context(root=ROOT):
    folder = root/'analysis/drift'
    if not (folder/'manifest.json').exists():
        return {'available': False, 'reason': '보정 이탈 맥락 분석이 없습니다.'}
    try:
        manifest = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
        sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
        if sha(root/'analysis/drift_diagnosis.py') != manifest['script_sha256']:
            raise ValueError('맥락 분석 코드 변경')
        for name, expected in manifest['input_sha256'].items():
            if sha(root/'analysis'/name) != expected: raise ValueError('맥락 입력 변경')
        name = 'latest_context.json'
        if sha(folder/name) != manifest['output_sha256'][name]: raise ValueError('맥락 출력 변경')
        payload = json.loads((folder/name).read_text(encoding='utf-8'))
        if payload['challenger_deployed'] is not False or len(payload['cohorts']) != 10:
            raise ValueError('후보 모형·집단 범위 확인 실패')
        return {'available': True, 'generated_at': manifest['generated_at'], **payload}
    except (OSError, ValueError, KeyError, TypeError):
        return {'available': False, 'reason': '맥락 분석의 코드·입출력 일관성을 확인하지 못했습니다.'}


def attach_drift_context(assessment, payload):
    if not payload.get('available') or assessment['status'] != 'assessed':
        return False
    cohort = assessment['cohort']
    if payload['period_end'] != assessment['period_end']:
        return False
    rows = [r for r in payload['cohorts'] if r['region'] == cohort['region'] and r['age_min'] == cohort['age_min']]
    if len(rows) != 1:
        return False
    row = dict(rows[0])
    outside = [r for r in payload['calendar'] if r['region'] == cohort['region'] and r['stratum'] == 'outside_window']
    notes = [f"월초 거래 예측 대비 해당 집단 {row['cohort_vs_expected_pct']:+.2f}%, 같은 지역 다른 4개 연령집단 합계 {row['other_ages_vs_expected_pct']:+.2f}%.",
             f"다른 연령집단 대비 상대 변화 {row['relative_to_other_ages_pct']:+.2f}%. 지역 공통 변화와 집단 차이를 함께 확인해야 해요."]
    if len(outside) == 1:
        c = outside[0]
        notes.append(f"12/25 하루를 제외해도 해당 지역의 거래 하한 포함률은 {c['lower_coverage']:.1%} ({c['cohort_days']}개 집단·일)예요. 단일 날짜만으로 보정 이탈을 설명할 수 없어요.")
    assessment['drift_context'] = {'version': payload['version'], 'cohort': row, 'notes': notes,
                                  'challenger_deployed': False, 'assurance': payload['assurance']}
    return True
