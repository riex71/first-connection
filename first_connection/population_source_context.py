"""Show verified source-quality facts without assigning marginal data to an age."""
import copy
import hashlib
import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_population_source_context(root=ROOT):
    folder = root/'analysis/population_context'
    try:
        manifest = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
        sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
        if sha(root/'analysis/population_context.py') != manifest['script_sha256']:
            raise ValueError('추가 통신 분석 코드 변경')
        for name,expected in manifest['input_sha256'].items():
            if sha(root/'analysis'/name) != expected: raise ValueError('추가 통신 입력 기록 변경')
        if sha(folder/'latest.json') != manifest['output_sha256']['latest.json']:
            raise ValueError('추가 통신 품질 기록 변경')
        payload = json.loads((folder/'latest.json').read_text(encoding='utf-8'))
        if (payload['scope'] != 'region_age_unstratified' or payload['changes_response'] is not False
                or payload['mode'] != 'source_quality_context_only'
                or len(payload['cohorts']) != 2 or {r['region'] for r in payload['cohorts']} != {'강남구','춘천시'}):
            raise ValueError('지역 출처 품질 기록 범위 불일치')
        for row in payload['cohorts']:
            if (row['age_scope'] != 'not_stratified' or row['sensitivity_values_attached'] is not False
                    or row['status'] != 'source_review_required' or len(row['sources']) != 2
                    or {s['view'] for s in row['sources']} != {'time','wkdy'}):
                raise ValueError('자료 해석 보류 범위 불일치')
            for s in row['sources']:
                if (s['primary_eligible'] is not False or s['use_status'] != 'deduplicated_sensitivity_only'
                        or s['month'] != payload['month'] or s['duplicate_key_rows'] <= 0
                        or s['duplicate_key_rows'] != s['identical_extra_rows']
                        or s['raw_rows'] != s['unique_rows']+s['identical_extra_rows']):
                    raise ValueError('중복 원본과 해석 보류 기록 불일치')
        return {'available':True,**payload}
    except (OSError,ValueError,KeyError,TypeError):
        return {'available':False,'reason':'추가 통신자료의 코드·입출력·해석 범위를 확인하지 못했습니다.'}


def attach_population_source_context(assessment,payload):
    if not payload.get('available') or assessment['status'] != 'assessed': return False
    month = date.fromisoformat(assessment['period_end']).strftime('%Y%m')
    if payload['month'] != month: return False
    rows = [r for r in payload['cohorts'] if r['region'] == assessment['cohort']['region']]
    if len(rows) != 1: return False
    assessment['population_source_context'] = {'version':payload['version'],'month':month,
        'scope':payload['scope'],'mode':payload['mode'],'changes_response':False,
        'assurance':payload['assurance'],**copy.deepcopy(rows[0])}
    return True
