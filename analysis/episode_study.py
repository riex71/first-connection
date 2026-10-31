"""Factorial reference/width study and aggregate episode review drafts."""
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from first_connection.signal_episode import replay_signal
from run_analysis import np, pd, WORK

OUT = HERE/'episodes'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    OUT.mkdir(exist_ok=True)
    policy = json.loads((HERE/'episode_policy.json').read_text(encoding='utf-8'))
    original = json.loads((HERE/'run_manifest.json').read_text(encoding='utf-8'))
    activity = json.loads((HERE/'activity/manifest.json').read_text(encoding='utf-8'))
    drift = json.loads((HERE/'drift/manifest.json').read_text(encoding='utf-8'))
    if sha(HERE/'results/card1_daily.csv') != original['output_sha256']['results/card1_daily.csv']:
        raise ValueError('원본 집계 해시 불일치')
    for name in ['calibration.csv', 'stress_cases_SYNTHETIC.csv']:
        if sha(HERE/'activity'/name) != activity['output_sha256'][name]: raise ValueError(name)
    if sha(HERE/'drift/challenger_calibration.csv') != drift['output_sha256']['challenger_calibration.csv']:
        raise ValueError('비교 보정값 변경')
    daily = pd.read_csv(HERE/'results/card1_daily.csv', parse_dates=['date'])
    old_q = pd.read_csv(HERE/'activity/calibration.csv').set_index(['region', 'age_min']).relative_lower_error_threshold
    daily_q = pd.read_csv(HERE/'drift/challenger_calibration.csv').set_index(['region', 'age_min']).relative_lower_error_threshold
    scenarios = pd.read_csv(HERE/'activity/stress_cases_SYNTHETIC.csv', dtype={'month': str})
    scenarios = scenarios.loc[scenarios.method == 'calibrated']
    replay_rows, event_rows, actual_summaries, stress = [], [], [], []
    latest = []
    for (region, age), g in daily.groupby(['region', 'age_min']):
        g = g.sort_values('date').reset_index(drop=True)
        values = g.USE_CNT.to_numpy(dtype=float).tolist()
        weekdays = g.date.dt.dayofweek.tolist()
        dates = g.date.dt.strftime('%Y-%m-%d').tolist()
        start = dates.index('2025-10-01')
        thresholds = {'monthly': float(old_q.loc[(region, age)]), 'daily': float(daily_q.loc[(region, age)])}
        for variant in policy['variants']:
            freeze = variant.startswith('freeze_')
            q = thresholds['monthly' if 'monthly' in variant else 'daily']
            max_reference = policy['max_reference_days'] if variant.endswith('_bounded') else None
            audit = variant.endswith('_audited')
            audit_args = {'audit_closure': audit, 'anchor_max_age_days': policy['closure_anchor_max_age_days']}
            result = replay_signal(values, weekdays, dates, start, q, freeze, policy['open_days'], policy['recovery_days'],
                                   max_reference_days=max_reference, **audit_args)
            for row in result['daily']:
                replay_rows.append({'region': region, 'age_min': int(age), 'variant': variant, **row})
            for event in result['events']:
                event_rows.append({'region': region, 'age_min': int(age), 'variant': variant, **event})
            actual_summaries.append({'region': region, 'age_min': int(age), 'variant': variant,
                'confirmed_events': len(result['events']), 'open_days': sum(r['state_after'] == 'open' for r in result['daily']),
                'state_at_end': result['state'], 'unclosed_events': sum(r['closed_at'] is None for r in result['events']),
                'pending_closure_reviews': len(result['pending_closure_reviews'])})
            if variant == policy['primary_review_variant']:
                latest.append({'region': region, 'age_min': int(age), 'age_max': 120 if age == 60 else int(age)+9,
                    'state': result['state'], 'candidate_start': result['candidate_start'],
                    'current_event': result['current_event'], 'events': result['events'],
                    'confirmed_events': len(result['events']), 'calibration_q': q,
                    'pending_closure_reviews': result['pending_closure_reviews']})
            for scenario in scenarios.loc[(scenarios.region == region) & (scenarios.age_min == age)].itertuples():
                positions = np.flatnonzero(g.date.dt.strftime('%Y%m') == scenario.month)
                injection = positions[int(scenario.offset):int(scenario.offset)+int(scenario.duration)]
                first, last = int(injection[0]), int(injection[-1])
                stop = min(len(values), last+15)
                altered = list(values)
                for i in injection: altered[i] *= 1-float(scenario.decline)
                original_run = replay_signal(values, weekdays, dates, int(positions[0]), q, freeze,
                                             policy['open_days'], policy['recovery_days'], end=stop, max_reference_days=max_reference, **audit_args)
                injected_run = replay_signal(altered, weekdays, dates, int(positions[0]), q, freeze,
                                             policy['open_days'], policy['recovery_days'], end=stop, max_reference_days=max_reference, **audit_args)
                lookup = {r['date']: r for r in injected_run['daily']}
                original_lookup = {r['date']: r for r in original_run['daily']}
                tail_dates = [dates[i] for i in injection[-7:]]
                tail_open = all(lookup[d]['state_after'] == 'open' for d in tail_dates)
                original_tail = all(original_lookup[d]['state_after'] == 'open' for d in tail_dates)
                window_dates = {dates[i] for i in injection}
                new_confirmation = any(e['confirmed_at'] in window_dates for e in injected_run['events'])
                available_after = stop-last-1
                after7 = lookup[dates[last+7]]['state_after'] == 'open' if available_after >= 7 else None
                after14 = lookup[dates[last+14]]['state_after'] == 'open' if available_after >= 14 else None
                last_event_id = lookup[dates[last]]['open_event_id']
                same_event14 = (bool(last_event_id and lookup[dates[last+14]]['open_event_id'] == last_event_id)
                                if available_after >= 14 else None)
                review14 = lookup[dates[last+14]]['state_after'] == 'reference_review' if available_after >= 14 else None
                cutoff14 = dates[last+14] if available_after >= 14 else None
                pending14 = (sum(e['closure_review_required_at'] is not None and e['closure_review_required_at'] <= cutoff14
                                 for e in injected_run['events']) if cutoff14 else None)
                stress.append({'region': region, 'age_min': int(age), 'month': scenario.month, 'variant': variant,
                    'decline': float(scenario.decline), 'duration': int(scenario.duration), 'offset': int(scenario.offset),
                    'new_confirmation_in_window': new_confirmation, 'tail_seven_open': tail_open,
                    'original_tail_seven_open': original_tail, 'new_tail_retention': tail_open and not original_tail,
                    'open_on_last_injected_day': lookup[dates[last]]['state_after'] == 'open',
                    'available_after_days': available_after, 'open_7_days_after_restoration': after7,
                    'open_14_days_after_restoration': after14, 'same_event_open_after14': same_event14,
                    'reference_review_after14': review14, 'pending_closure_review_events_after14': pending14,
                    'data_kind': 'synthetic_attenuation'})
    pd.DataFrame(replay_rows).to_csv(OUT/'actual_daily_replay.csv', index=False, encoding='utf-8-sig')
    pd.DataFrame(event_rows).to_csv(OUT/'actual_events.csv', index=False, encoding='utf-8-sig')
    pd.DataFrame(actual_summaries).to_csv(OUT/'actual_summary.csv', index=False, encoding='utf-8-sig')
    frame = pd.DataFrame(stress)
    frame.to_csv(OUT/'paired_stress_SYNTHETIC.csv', index=False, encoding='utf-8-sig')
    summaries = []
    for (variant, decline, duration), rows in frame.groupby(['variant', 'decline', 'duration']):
        observed7 = rows.open_7_days_after_restoration.dropna()
        observed14 = rows.open_14_days_after_restoration.dropna()
        same14 = rows.same_event_open_after14.dropna()
        review14 = rows.reference_review_after14.dropna()
        pending14 = rows.pending_closure_review_events_after14.dropna()
        summaries.append({'variant': variant, 'decline': float(decline), 'duration': int(duration), 'scenarios': len(rows),
            'confirmed_in_window': int(rows.new_confirmation_in_window.sum()), 'tail_seven_open': int(rows.tail_seven_open.sum()),
            'original_tail_seven_open': int(rows.original_tail_seven_open.sum()),
            'new_tail_retention': int(rows.new_tail_retention.sum()),
            'observed_after7': len(observed7), 'open_after7': int(observed7.sum()),
            'observed_after14': len(observed14), 'open_after14': int(observed14.sum()),
            'same_event_open_after14': int(same14.sum()), 'reference_review_after14': int(review14.sum()),
            'scenarios_with_pending_closure_after14': int(pending14.gt(0).sum())})
    payload = {'version': policy['version'], 'mode': policy['mode'], 'period_start': '2025-10-01',
        'period_end': '2025-12-31', 'variant': policy['primary_review_variant'], 'cohorts': latest,
        'open_days': policy['open_days'], 'recovery_days': policy['recovery_days'],
        'max_reference_days': policy['max_reference_days'],
        'closure_anchor_max_age_days': policy['closure_anchor_max_age_days'],
        'assurance': '과거 집단 거래의 사건 재생·종료 근거 확인 기록입니다. 두 하한 기준 재충족도 실제 지원 완료·고립 회복이나 거래량의 완전한 원상회복을 뜻하지 않습니다.'}
    (OUT/'latest.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    summary = {'version': policy['version'], 'stress_scenarios': len(scenarios), 'paired_rows': len(frame),
        'factorial_design': '2x2 rolling/freeze crossed with monthly/daily q; bounded-freeze and audited-closure engineering conditions are separate',
        'stress_summary': summaries, 'actual_summary': actual_summaries,
        'limitations': ['7일 지속·7일 재충족·28일 기준 유효기간은 연구용 정책이며 고립 정답으로 검증된 기준 아님.',
            '사건 유지와 원시 하한 이탈은 다른 지표이며 주입 탐지 정확도로 혼용하지 않음.',
            '주입 종료 후 원자료로 복귀한 것은 정상·고립 회복이라는 정답이 아님.',
            '회복 관측이 부족한 경우 종료 성능을 계산하지 않고 우측 절단으로 표시.',
            '기존 원자료·주입 구간을 이미 본 뒤의 탐색적 비교. 독립 검증 아님.']}
    (OUT/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    os.environ.setdefault('MPLCONFIGDIR', str(WORK/'mplconfig'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    font = Path('C:/Windows/Fonts/malgun.ttf')
    if font.exists():font_manager.fontManager.addfont(str(font));plt.rcParams['font.family'] = 'Malgun Gothic'
    fig, ax = plt.subplots(figsize=(9, 4))
    for variant in policy['variants']:
        rows = [r for r in summaries if r['variant'] == variant and r['decline'] == .2 and r['duration'] >= 14]
        ax.plot([r['duration'] for r in rows], [r['tail_seven_open']/r['scenarios']*100 for r in rows], 'o-', label=variant)
    ax.set_xticks([14, 28]);ax.set_ylim(0, 105);ax.set_xlabel('20% 주입 감소 지속 기간 (일)')
    ax.set_ylabel('마지막 7일 사건 유지 비율 (%)');ax.set_title('기준선·하한 보정 폭과 재검토 상한의 비교\n사건 유지 지표이며 고립 탐지 성능 아님');ax.legend(fontsize=8)
    fig.tight_layout();fig.savefig(OUT/'factorial_comparison.png', dpi=180);plt.close(fig)
    inputs = ['episode_policy.json', 'run_manifest.json', 'results/card1_daily.csv', 'activity/manifest.json',
              'activity/calibration.csv', 'activity/stress_cases_SYNTHETIC.csv', 'drift/manifest.json', 'drift/challenger_calibration.csv']
    manifest = {'version': policy['version'], 'generated_at': datetime.now(timezone(timedelta(hours=9))).isoformat(),
        'script_sha256': sha(Path(__file__)), 'engine_sha256': sha(HERE.parent/'first_connection/signal_episode.py'),
        'input_sha256': {p: sha(HERE/p) for p in inputs},
        'output_sha256': {p.name: sha(p) for p in sorted(OUT.iterdir()) if p.is_file() and p.name != 'manifest.json'}}
    (OUT/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'stress20': [r for r in summaries if r['decline'] == .2 and r['duration'] >= 14],
        'latest': [{k:r[k] for k in ['region', 'age_min', 'state', 'confirmed_events', 'current_event']} for r in latest]}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    main()
