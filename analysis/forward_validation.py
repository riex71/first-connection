"""Frozen, offline evaluation of aggregate activity; never isolation ground truth.

Uses only the standard library. No new period is supplied with this project.
"""
import argparse
import csv
import hashlib
import io
import json
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from first_connection.signal_episode import replay_signal

VERSION = 'forward-activity-evaluation/1'
FIELDS = ['date', 'region', 'age_min', 'USE_CNT']
PINNED = ['analysis/forward_validation.py', 'first_connection/signal_episode.py',
          'analysis/episode_policy.json', 'analysis/episodes/manifest.json',
          'analysis/results/card1_daily.csv', 'analysis/drift/challenger_calibration.csv']


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def now():
    return datetime.now(timezone(timedelta(hours=9))).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def parse_rows(text, exact_fields=False):
    reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
    if (reader.fieldnames != FIELDS if exact_fields else not set(FIELDS).issubset(reader.fieldnames or [])):
        raise ValueError('CSV 열은 date,region,age_min,USE_CNT 순서여야 합니다.')
    rows, seen = [], set()
    for line, r in enumerate(reader, 2):
        try:
            if None in r or any(r.get(k) is None for k in FIELDS): raise ValueError()
            day = date.fromisoformat(r['date'])
            if day.isoformat() != r['date']: raise ValueError()
            age = int(r['age_min'])
            value = float(r['USE_CNT'])
            if (str(age) != r['age_min'] or not math.isfinite(value) or value < 0
                    or value > 2**53-1 or not value.is_integer()):
                raise ValueError()
            key = (r['region'], age, day)
            if key in seen: raise ValueError('중복 날짜·집단')
            seen.add(key)
            rows.append({'date': day, 'region': r['region'], 'age_min': age, 'USE_CNT': value})
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError(f'{line}행 날짜·연령·비음수 정수 건수·중복을 확인하세요.') from error
    if not rows: raise ValueError('후속 관측이 없습니다. 빈 입력으로 평가하지 않습니다.')
    return rows


def grid(rows, cohorts, start, end):
    expected_days = {start + timedelta(days=i) for i in range((end-start).days+1)}
    grouped = {(r, a): {} for r, a in cohorts}
    for row in rows:
        key = row['region'], row['age_min']
        if key not in grouped: raise ValueError('지역·연령집단 범위가 고정 평가와 다릅니다.')
        if row['date'] in grouped[key]: raise ValueError('중복 날짜·집단')
        grouped[key][row['date']] = row
    if any(set(days) != expected_days for days in grouped.values()):
        raise ValueError('모든 집단에 같은 연속 날짜가 필요합니다. 결측을 0으로 채우지 않습니다.')
    return grouped


def replay(rows, threshold, policy, end=None):
    days = sorted(rows)
    return replay_signal([rows[d]['USE_CNT'] for d in days], [d.weekday() for d in days],
        [d.isoformat() for d in days], [d.isoformat() for d in days].index('2025-10-01'),
        threshold, False, policy['open_days'], policy['recovery_days'], end=end,
        audit_closure=True, anchor_max_age_days=policy['closure_anchor_max_age_days'])


def freeze(path, root=ROOT):
    """Create once; exact content digest also prevents accidental parameter edits."""
    a = root/'analysis'
    policy = read_json(a/'episode_policy.json')
    if policy['primary_review_variant'] != 'rolling_daily_q_audited':
        raise ValueError('현재 검토 정책과 일치하지 않습니다.')
    episode = read_json(a/'episodes/manifest.json')
    if (sha(root/'first_connection/signal_episode.py') != episode['engine_sha256'] or
            sha(a/'episode_policy.json') != episode['input_sha256']['episode_policy.json'] or
            sha(a/'results/card1_daily.csv') != episode['input_sha256']['results/card1_daily.csv'] or
            sha(a/'drift/challenger_calibration.csv') != episode['input_sha256']['drift/challenger_calibration.csv']):
        raise ValueError('v0.8 재생 입력과 정책을 먼저 검증해야 합니다.')
    rows = parse_rows((a/'results/card1_daily.csv').read_text(encoding='utf-8-sig'))
    cohorts = sorted({(r['region'], r['age_min']) for r in rows})
    cutoff = max(r['date'] for r in rows)
    grid(rows, cohorts, min(r['date'] for r in rows), cutoff)
    thresholds = []
    with (a/'drift/challenger_calibration.csv').open(encoding='utf-8-sig', newline='') as f:
        for r in csv.DictReader(f):
            thresholds.append({'region': r['region'], 'age_min': int(r['age_min']),
                'q': float(r['relative_lower_error_threshold']), 'calibration_end': r['calibration_end']})
    if ({(r['region'],r['age_min']) for r in thresholds} != set(cohorts) or
            len(thresholds) != len(cohorts) or any(not 0 <= r['q'] < 1 or
                r['calibration_end'] != '2025-09-30' for r in thresholds)):
        raise ValueError('보정값의 범위·사전 시점이 일치하지 않습니다.')
    payload = {'version': VERSION, 'frozen_at': now(), 'observed_through': cutoff.isoformat(),
        'first_allowed_date': (cutoff+timedelta(days=1)).isoformat(),
        'replay_start': '2025-10-01', 'minimum_reporting_days': 28,
        'scope': 'aggregate_card_activity_only', 'variant': policy['primary_review_variant'],
        'parameters': {k: policy[k] for k in ['open_days','recovery_days','closure_anchor_max_age_days']},
        'cohorts': thresholds, 'pinned_sha256': {name: sha(root/name) for name in PINNED},
        'policy_selection': 'post_result_exploratory_v0.8; this lock precedes any new supplied observations',
        'isolated_person_labels_available': False}
    envelope = {'payload': payload, 'payload_sha256': hashlib.sha256(canonical(payload)).hexdigest()}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as f:
        json.dump(envelope, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write('\n')
    return envelope


def load_lock(path, root=ROOT):
    envelope = read_json(path)
    p = envelope['payload']
    if hashlib.sha256(canonical(p)).hexdigest() != envelope['payload_sha256']:
        raise ValueError('고정 계획 내용이 변경됐습니다.')
    if p['version'] != VERSION or set(p['pinned_sha256']) != set(PINNED):
        raise ValueError('고정 계획 버전·코드 범위가 다릅니다.')
    for name, expected in p['pinned_sha256'].items():
        if sha(root/name) != expected: raise ValueError('고정 이후 코드·정책·기존 입력 변경: '+name)
    return p


def evaluate(lock_path, input_path, declaration_path, out, root=ROOT):
    p = load_lock(lock_path, root)
    declaration = read_json(declaration_path)
    if (set(declaration) != {'data_kind','source','unseen_during_policy_design'} or
            declaration['data_kind'] not in {'observational','synthetic'} or
            not isinstance(declaration['source'],str) or not declaration['source'].strip() or
            type(declaration['unseen_during_policy_design']) is not bool):
        raise ValueError('자료 종류·출처·설계 시 미관측 여부를 명시하세요.')
    if declaration['data_kind'] == 'observational' and not declaration['unseen_during_policy_design']:
        raise ValueError('이미 검토한 자료는 새 기간 검증으로 실행하지 않습니다.')
    incoming = parse_rows(input_path.read_text(encoding='utf-8-sig'), exact_fields=True)
    first = date.fromisoformat(p['first_allowed_date'])
    last = max(r['date'] for r in incoming)
    if min(r['date'] for r in incoming) != first or last < first:
        raise ValueError('이미 본 기간 재사용·과거 날짜·후속 시작일 누락을 거부합니다.')
    cohorts = [(r['region'],r['age_min']) for r in p['cohorts']]
    new_grid = grid(incoming, cohorts, first, last)
    historical = parse_rows((root/'analysis/results/card1_daily.csv').read_text(encoding='utf-8-sig'))
    old_grid = grid(historical, cohorts, min(r['date'] for r in historical), first-timedelta(days=1))
    summaries, daily, event_rows = [], [], []
    for c in p['cohorts']:
        key = c['region'],c['age_min']
        old = replay(old_grid[key], c['q'], p['parameters'])
        result = replay({**old_grid[key], **new_grid[key]}, c['q'], p['parameters'])
        after = [r for r in result['daily'] if r['date'] >= first.isoformat()]
        pending = result['pending_closure_reviews']
        original_pending = {e['id'] for e in old['pending_closure_reviews']}
        absolute_error = sum(abs(r['actual']-r['predicted']) for r in after)
        total_actual = sum(r['actual'] for r in after)
        summary = {'region':c['region'], 'age_min':c['age_min'], 'days':len(after),
            'wape_pct':100*absolute_error/total_actual if total_actual else None,
            'lower_bound_coverage':sum(not r['below_lower'] for r in after)/len(after),
            'below_lower_days':sum(r['below_lower'] for r in after),
            'new_confirmed_events':sum(e['confirmed_at'] >= first.isoformat() for e in result['events']),
            'inherited_pending_reviews':len(original_pending),
            'new_pending_reviews':sum(e['closure_review_required_at'] >= first.isoformat()
                for e in pending),
            'pending_reviews_at_end':len(pending),
            'pending_case_days':sum(e['closure_review_required_at'] <= r['date'] for e in pending for r in after),
            'state_at_end':result['state'], 'candidate_start':result['candidate_start'],
            'absolute_error_sum':absolute_error, 'actual_count_sum':total_actual}
        summaries.append(summary)
        daily.extend({'region':key[0],'age_min':key[1],**r} for r in after)
        event_rows.extend({'region':key[0],'age_min':key[1],**e} for e in result['events'])
    period_days = (last-first).days+1
    report = {'version':VERSION, 'evaluated_at':now(), 'freeze_sha256':sha(lock_path),
        'input_sha256':sha(input_path), 'declaration_sha256':sha(declaration_path),
        'data_kind':declaration['data_kind'], 'source_declaration':declaration,
        'unseen_status':'synthetic_fixture' if declaration['data_kind']=='synthetic' else 'declared_not_independently_verified',
        'result_kind':'synthetic_pipeline_check' if declaration['data_kind']=='synthetic' else 'new_period_activity_evaluation',
        'period_start':first.isoformat(), 'period_end':last.isoformat(), 'period_days':period_days,
        'followup_status':'minimum_28_days_available' if period_days >= p['minimum_reporting_days'] else 'short_followup',
        'review_mode':'offline_only', 'production_detector_changed':False,
        'isolation_accuracy':None, 'isolation_validity':'not_evaluated',
        'personal_risk':None, 'welfare_priority':None, 'actual_support_outcomes':'not_tracked',
        'cohorts':summaries,
        'meaning':['WAPE and lower-bound coverage concern aggregate transaction counts only.',
                   'Pending case-days count retained review records, not measured review hours or people.',
                   'No manual review completion or isolation recovery labels were supplied.',
                   'Source and unseen declarations are assertions, not verified provenance.',
                   'No policy tuning or live-app deployment is performed.']}
    outputs = {'report.json':report, 'daily.json':daily, 'events.json':event_rows}
    # All input validation and replay finishes before creating a new result folder.
    out.mkdir(parents=True, exist_ok=False)
    for name, value in outputs.items():
        (out/name).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    (out/'manifest.json').write_text(json.dumps({'version':VERSION,'output_sha256':{
        name:sha(out/name) for name in outputs}},indent=2)+'\n',encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command',required=True)
    f = sub.add_parser('freeze'); f.add_argument('--out', type=Path,required=True)
    s = sub.add_parser('status'); s.add_argument('--lock', type=Path,required=True)
    e = sub.add_parser('evaluate')
    for arg in ['lock','input','declaration','out']: e.add_argument('--'+arg,type=Path,required=True)
    args = parser.parse_args()
    try:
        if args.command == 'freeze':
            result = freeze(args.out)['payload']
        elif args.command == 'status':
            p = load_lock(args.lock)
            result = {'status':'waiting_for_new_observations', 'observed_through':p['observed_through'],
                'first_allowed_date':p['first_allowed_date'],'minimum_reporting_days':p['minimum_reporting_days'],
                'isolation_validity':'not_evaluated','runtime_dependencies':'stdlib_only'}
        else: result = evaluate(args.lock,args.input,args.declaration,args.out)
        print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))
    except (ValueError,KeyError,TypeError,OSError) as error:
        parser.exit(2, '평가 중단: '+str(error)+'\n')


if __name__ == '__main__': main()
