"""Load a completed aggregate detector and choose a transparent review response."""
import copy
import hashlib
import json
from pathlib import Path
from .activity_policy import STATES, classify_activity
from .drift_context import load_drift_context, attach_drift_context
from .episode_review import load_episode_review, attach_episode_review
from .population_source_context import load_population_source_context, attach_population_source_context

ROOT = Path(__file__).resolve().parents[1]


def load_activity_model(root=ROOT):
    folder = root/'analysis/activity'
    if not (folder/'manifest.json').exists():
        return {'available': False, 'reason': '완료된 활동 신호 모형이 없습니다.'}
    try:
        manifest = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
        sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
        if sha(root/'analysis/activity_model.py') != manifest['script_sha256']:
            raise ValueError('모형 코드 변경 후 재실행 필요')
        if sha(root/'first_connection/activity_policy.py') != manifest['policy_code_sha256']:
            raise ValueError('판단 정책 변경 후 재실행 필요')
        for name, expected in manifest['input_sha256'].items():
            if sha(root/'analysis'/name) != expected:
                raise ValueError('모형 입력 해시 불일치')
        for name in ['latest.json', 'summary.json']:
            if sha(folder/name) != manifest['output_sha256'][name]:
                raise ValueError('모형 결과 해시 불일치')
        payload = json.loads((folder/'latest.json').read_text(encoding='utf-8'))
        if len(payload['cohorts']) != 10 or payload['policy']['deployment_mode'] != 'review_only':
            raise ValueError('집단·모드 불일치')
        if any(classify_activity(r, payload['policy']) != r['state'] for r in payload['cohorts']):
            raise ValueError('저장된 판단과 현재 정책 불일치')
        return {'available': True, 'generated_at': manifest['generated_at'], **payload}
    except (OSError, ValueError, KeyError, TypeError):
        return {'available': False, 'reason': '활동 모형의 코드·입력·출력 일관성을 확인하지 못했습니다.'}


def build_activity_assessment(profile, model):
    base = {'version': model.get('version'), 'scope': 'aggregate_activity_not_personal_risk',
            'personal_isolation_risk': None, 'welfare_priority_rank': None,
            'deployment_mode': 'review_only', 'generated_at': model.get('generated_at'),
            'period_start': model.get('period_start'), 'period_end': model.get('period_end')}
    if profile['age'] is None:
        return {**base, 'status': 'age_required', 'message': '나이를 입력하면 해당 연령집단의 과거 활동 신호를 확인할 수 있어요.'}
    matched = [r for r in model['cohorts'] if r['region'] == profile['region'] and r['age_min'] <= profile['age'] <= r['age_max']]
    if len(matched) != 1:
        return {**base, 'status': 'not_covered', 'message': '제공 분석에 일치하는 지역·연령집단이 없어 판단하지 않았어요.'}
    row = copy.deepcopy(matched[0])
    # Empirical undercoverage can reflect distribution drift or real changes.
    # Resolve neither by assumption; a human reviews it before treating alerts as reliable.
    response_key = ('verify_data' if row['state'] == 'verify_data' else
                    ('verify_model' if row['calibration_watch'] else row['state']))
    label, steps = STATES[response_key]
    information_only = profile['support_check']['preferred_next_step'] == 'information_only'
    if information_only:
        steps = ['공식 분석의 관측 기간·근거와 모형 보정 상태 살펴보기', '문의·신청 준비는 원할 때 선택하기']
    return {**base, 'status': 'assessed', 'cohort': row, 'observations': row['observations'],
            'interpretation': '관측값이 정책 분기에 기여한 근거이며 고립의 인과 원인이 아닙니다.',
            'response': {'key': response_key, 'label': label, 'steps': list(steps),
                'suggest_support_check': not information_only and response_key in {'follow_up', 'check_context'},
                'preparation_scope': 'information_only' if information_only else 'selected_goal'},
            'review_queue': [r for r in model['review_queue'] if r['region'] == profile['region']],
            'review_order_meaning': '과거 집단 자료·맥락의 추가 확인 순서. 주민 개인의 지원 우선순위가 아님.',
            'assurance': model['assurance']}


def attach_activity_assessment(plan, model):
    assessment = build_activity_assessment(plan['profile'], model)
    context_attached = attach_drift_context(assessment, load_drift_context())
    episode_attached = attach_episode_review(assessment, load_episode_review(), plan['profile'])
    population_attached = attach_population_source_context(assessment, load_population_source_context())
    plan['activity_assessment'] = assessment
    plan['trace'].insert(0, {'tool': 'assess_aggregate_activity', 'result': {
        'status': assessment['status'], 'version': assessment['version'], 'scope': assessment['scope'],
        'changes_eligibility': False, 'changes_recommendation_score': False}})
    if assessment['status'] == 'assessed':
        plan['trace'].insert(1, {'tool': 'select_activity_response', 'result': {
            'observed_state': assessment['cohort']['state'], 'response': assessment['response']['key'],
            'calibration_watch': assessment['cohort']['calibration_watch'],
            'review_order': assessment['cohort']['review_order'], 'deployment_mode': 'review_only'}})
    if context_attached:
        plan['trace'].insert(2, {'tool': 'read_retrospective_activity_context', 'result': {
            'version': assessment['drift_context']['version'], 'challenger_deployed': False,
            'changes_response_key': False, 'causal_inference': False}})
    if episode_attached:
        review = assessment['episode_review']
        plan['trace'].insert(3 if context_attached else 2, {'tool': 'prepare_observation_review', 'result': {
            'version': review['version'], 'record_id': review['record_id'], 'mode': review['mode'],
            'state': review['state'], 'memo_prepared': bool(review['draft_memo']),
            'active_detector_replaced': False, 'actual_support_status': 'not_tracked'}})
    if population_attached:
        plan['trace'].insert(2+int(context_attached)+int(episode_attached), {
            'tool':'read_population_source_quality','result':{
                'version':assessment['population_source_context']['version'],
                'status':'source_review_required','scope':'region_age_unstratified',
                'changes_response':False,'changes_eligibility':False,
                'sensitivity_values_attached':False}})
    return plan
