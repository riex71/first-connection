"""Verified aggregate observation history and a draft review memo, not a live case."""
import hashlib
import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABELS = {'monitoring': '열린 관측 사건 없음', 'candidate': '연속 관측 추가 확인',
          'open': '관측 사건 유지', 'reference_review': '기준선 재검토 대기'}


def load_episode_review(root=ROOT):
    folder = root/'analysis/episodes'
    if not (folder/'manifest.json').exists():
        return {'available': False, 'reason': '완료된 관측 사건 재생 기록이 없습니다.'}
    try:
        manifest = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
        sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
        if sha(root/'analysis/episode_study.py') != manifest['script_sha256']:
            raise ValueError('사건 분석 코드 변경')
        if sha(root/'first_connection/signal_episode.py') != manifest['engine_sha256']:
            raise ValueError('사건 규칙 변경')
        for name, expected in manifest['input_sha256'].items():
            if sha(root/'analysis'/name) != expected: raise ValueError('사건 입력 변경')
        name = 'latest.json'
        if sha(folder/name) != manifest['output_sha256'][name]: raise ValueError('사건 출력 변경')
        payload = json.loads((folder/name).read_text(encoding='utf-8'))
        if (payload['mode'] != 'retrospective_review_only' or payload['variant'] != 'rolling_daily_q_audited'
                or len(payload['cohorts']) != 10 or any(r['state'] not in LABELS for r in payload['cohorts'])):
            raise ValueError('검토 기록 범위 불일치')
        for row in payload['cohorts']:
            for event in row['events']:
                audit = event.get('closure_audit')
                if event['closed_at'] and (event['closure'] != 'dual_reference_rule' or not audit or audit['reason'] is not None):
                    raise ValueError('종료 근거와 상태 불일치')
                if event['status'] == 'closure_review_required' and event['closed_at'] is not None:
                    raise ValueError('검토 대기 사건을 종료로 표시할 수 없음')
            pending_ids = {e['id'] for e in row['events'] if e['status'] == 'closure_review_required'}
            if pending_ids != {e['id'] for e in row['pending_closure_reviews']}:
                raise ValueError('종료 검토 대기 기록 누락')
        return {'available': True, 'generated_at': manifest['generated_at'], **payload}
    except (OSError, ValueError, KeyError, TypeError):
        return {'available': False, 'reason': '관측 사건 기록의 코드·입출력 일관성을 확인하지 못했습니다.'}


def attach_episode_review(assessment, payload, profile):
    if not payload.get('available') or assessment['status'] != 'assessed' or payload['period_end'] != assessment['period_end']:
        return False
    target = assessment['cohort']
    rows = [r for r in payload['cohorts'] if r['region'] == target['region'] and r['age_min'] == target['age_min']]
    if len(rows) != 1: return False
    row = rows[0]
    notes = [f"{payload['period_start']} ~ {payload['period_end']}의 집단 거래를 순서대로 재생한 기록입니다.",
             f"{payload['open_days']}일 연속 하한 미만이면 사건을 기록합니다. 갱신 기준과 시작 전 하한을 모두 {payload['recovery_days']}일 재충족하고 시작 전 기준이 {payload['closure_anchor_max_age_days']}일 이내일 때만 관측 규칙상 종료합니다."]
    if row['state'] == 'candidate':
        days = (date.fromisoformat(payload['period_end'])-date.fromisoformat(row['candidate_start'])).days+1
        notes.append(f"후보 시작 {row['candidate_start']} · 자료 종료일 기준 {days}일 연속으로, 사건 확인에 필요한 {payload['open_days']}일에 미달합니다. 이후 관측은 제공되지 않았습니다.")
    notes.append('두 하한 기준 재충족이나 열린 사건 없음은 원래 거래량의 완전한 회복·고립 해소·지원 완료·현재 안전을 뜻하지 않습니다.')
    history, pending = [], []
    reason_labels = {'below_anchor_lower': '시작 전 하한 미충족', 'anchor_stale': '시작 전 기준 유효기간 초과',
                     'anchor_unavailable': '시작 전 기준 확인 불가'}
    for e in row['events']:
        audit = e.get('closure_audit')
        entry = {'id': e['id'], 'start': e['start'], 'confirmed_at': e['confirmed_at'], 'closed_at': e['closed_at'],
                 'status': e['status'], 'closure_label': '두 하한 기준 재충족' if e['closed_at'] else
                    ('종료 근거 확인 대기' if e['status'] == 'closure_review_required' else '관측 사건 유지'),
                 'rolling_rule_satisfied_at': e.get('rolling_rule_recovered_at'),
                 'review_required_at': e.get('closure_review_required_at'),
                 'closure_audit': audit, 'review_reason_label': reason_labels.get((audit or {}).get('reason')),
                 'reference_review_required_at': e.get('reference_review_required_at')}
        history.append(entry)
        if e['status'] == 'closure_review_required':
            pending.append({**entry, 'review_questions': [
                f"시작 전 기준 종료일 {audit['anchor_end']}와 종료 후보일의 관측을 원본 집계에서 확인",
                f"마지막 {audit['observed_days']}일 중 시작 전 하한 미만 {audit['below_anchor_days']}일과 보정·휴일·지역 공통 변화 확인",
                '현재 이용자의 지원 희망은 과거 집단 지표와 별도로 확인'],
                'anchor_age_at_data_end': (date.fromisoformat(payload['period_end'])-date.fromisoformat(audit['anchor_end'])).days})
    if pending:
        notes.append(f"관측 상태와 별도로 종료 근거 확인 {len(pending)}건이 대기 중입니다. 검토를 완료하거나 회복으로 확정한 기록은 아닙니다.")
    identity = f"{payload['version']}:{row['region']}:{row['age_min']}:{payload['period_end']}"
    draft = ''
    if profile['support_check']['preferred_next_step'] != 'information_only':
        draft = '\n'.join([f"관측 검토 메모 초안 · {row['region']} {row['age_min']}세 이상 연령집단" if row['age_min'] == 60
                           else f"관측 검토 메모 초안 · {row['region']} {row['age_min']}대",
                           f"자료 종료일 {payload['period_end']} · 재생 상태: {LABELS[row['state']]}",
                           *notes, *[question for e in pending for question in e['review_questions']],
                           '자료·모형의 보정 경고와 현재 이용자의 지원 희망을 별도로 확인할 것.',
                           '기관 연락·지원 착수·완료 확인을 수행한 기록이 아님.'])
    assessment['episode_review'] = {'version': payload['version'], 'mode': payload['mode'], 'variant': payload['variant'],
        'record_id': hashlib.sha256(identity.encode()).hexdigest()[:16], 'state': row['state'], 'label': LABELS[row['state']],
        'candidate_start': row['candidate_start'], 'events': history, 'notes': notes,
        'pending_closure_reviews': pending, 'closure_review_completed': False,
        'draft_memo': draft, 'active_detector_replaced': False, 'actual_support_status': 'not_tracked',
        'assurance': payload['assurance']}
    return True
