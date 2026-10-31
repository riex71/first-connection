"""Exercise the original rule engine with entirely fictional resources."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from first_connection.engine import profile_from, run_rules


def resource(key, cost, channel):
    return {
        'id': key, 'name': '가상 프로그램 · ' + key,
        'region': '강남구', 'kind': 'program', 'age_min': 18, 'age_max': None,
        'single_required': True, 'required_facts': {}, 'tags': ['관계', '취미'],
        'application_channels': [channel], 'participation_channels': [channel],
        'cost_krw': cost, 'application_start': None, 'application_end': None,
        'summary': '실제 기관·공고가 없는 공개 코드 시연용 합성 자료',
        'target_text': '성인 1인가구라는 가정', 'application_text': '합성 자료이므로 신청 불가',
        'action_url': None, 'hours': '가상 일정', 'conditions': ['실제 복지서비스가 아닙니다.'],
        'evidence': ['개발자가 작성한 합성 조건'], 'use_status': 'synthetic_demo',
        'source': {
            'id': 'synthetic-' + key, 'url': 'https://example.invalid/' + key,
            'publisher': 'SYNTHETIC DEMO', 'collected_at': datetime.now(timezone.utc).isoformat(),
            'max_age_days': 1, 'sha256': None, 'raw_path': None,
        },
    }


def main():
    services = [resource('online-free', 0, 'web'),
                resource('online-paid', 20000, 'web'),
                resource('phone-only', 0, 'phone')]
    profile = profile_from({
        'region': '강남구', 'age': 58, 'single': True, 'budget_krw': 0,
        'goal': '관계 취미 문화', 'avoid_channels': ['phone', 'visit'],
    })
    plan = run_rules(profile, services=services)
    ids = [r['id'] for r in plan['recommendations']]
    if ids != ['online-free']:
        raise RuntimeError('Budget/channel filtering changed: ' + repr(ids))
    print(json.dumps({
        'data_kind': 'synthetic', 'scope': '규칙 동작 시연이며 실제 기관·이용자 평가가 아닙니다.',
        'candidate_ids': ids, 'excluded': plan['excluded'],
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
