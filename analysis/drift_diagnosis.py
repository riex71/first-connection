"""Retrospective diagnostics: calendar windows, peers, and an adaptive challenger.

The challenger has a different information horizon and is not deployed here.
"""
import hashlib
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from run_analysis import np, pd, WORK
from activity_model import longest_streak

OUT = HERE/'drift'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rolling_predictions(values, weekdays, positions, window=28):
    result = []
    for i in positions:
        if i < window:
            raise ValueError('직전 28일 관측 필요')
        prior = np.arange(i-window, i)
        selected = prior[weekdays[prior] == weekdays[i]]
        result.append(float(np.mean(values[selected])))
    return np.asarray(result)


def lower_threshold(actual, predicted, quantile):
    return max(0., float(np.quantile((predicted-actual)/predicted, quantile, method='higher')))


def main(source_archive):
    OUT.mkdir(exist_ok=True)
    policy = json.loads((HERE/'drift_policy.json').read_text(encoding='utf-8'))
    base = json.loads((HERE/'run_manifest.json').read_text(encoding='utf-8'))
    model = json.loads((HERE/'activity/manifest.json').read_text(encoding='utf-8'))
    if sha(HERE/'run_analysis.py') != base['code_sha256'] or sha(HERE/'activity_model.py') != model['script_sha256']:
        raise ValueError('참조 분석·모형 코드 변경 후 원래 분석 재실행 필요')
    if sha(HERE/'results/card1_daily.csv') != base['output_sha256']['results/card1_daily.csv']:
        raise ValueError('원본 집계 입력 변경')
    for name, expected in model['output_sha256'].items():
        if sha(HERE/'activity'/name) != expected: raise ValueError('v0.5 모형 출력 변경: '+name)
    source = json.loads((HERE/'drift_sources/source_manifest.json').read_text(encoding='utf-8'))
    for name, expected in source['archived_raw_files'].items():
        if sha(source_archive/name) != expected: raise ValueError('달력 참조 원본 변경')
    daily = pd.read_csv(HERE/'results/card1_daily.csv', parse_dates=['date'])
    baseline = pd.read_csv(HERE/'activity/daily_predictions.csv', parse_dates=['date'], dtype={'month': str})
    pairs = pd.read_csv(HERE/'activity/stress_cases_SYNTHETIC.csv', dtype={'month': str})
    pairs = pairs.loc[pairs.method == 'calibrated'].copy()
    predictions, calibrations, stress = [], [], []
    for (region, age), group in daily.groupby(['region', 'age_min']):
        group = group.sort_values('date').reset_index(drop=True)
        values = group.USE_CNT.to_numpy(dtype=float)
        weekdays = group.date.dt.dayofweek.to_numpy()
        cal_positions = np.flatnonzero((group.date >= '2025-09-01') & (group.date < '2025-10-01'))
        cal_pred = rolling_predictions(values, weekdays, cal_positions)
        q = lower_threshold(values[cal_positions], cal_pred, policy['calibration_quantile'])
        calibrations.append({'region': region, 'age_min': int(age), 'relative_lower_error_threshold': q,
                             'calibration_end': '2025-09-30', 'calibration_days': len(cal_positions)})
        for month in policy['evaluation_months']:
            positions = np.flatnonzero(group.date.dt.strftime('%Y%m') == month)
            pred = rolling_predictions(values, weekdays, positions)
            old = baseline.loc[(baseline.month == month) & (baseline.region == region) & (baseline.age_min == age)].sort_values('date')
            if len(old) != len(positions) or not np.allclose(old.actual, values[positions]):
                raise ValueError('비교 대상 날짜·관측 불일치')
            for index, estimate in zip(positions, pred):
                predictions.append({'date': group.date.iloc[index].strftime('%Y-%m-%d'), 'month': month,
                    'region': region, 'age_min': int(age), 'actual': values[index], 'predicted': float(estimate),
                    'lower_bound': float(estimate*(1-q)), 'below_lower': bool(values[index] < estimate*(1-q)),
                    'train_start': group.date.iloc[index-28].strftime('%Y-%m-%d'),
                    'train_end': group.date.iloc[index-1].strftime('%Y-%m-%d'), 'calibration_end': '2025-09-30'})
            scenarios = pairs.loc[(pairs.month == month) & (pairs.region == region) & (pairs.age_min == age)]
            for scenario in scenarios.itertuples():
                offset, duration = int(scenario.offset), int(scenario.duration)
                altered = values.copy()
                injection_positions = positions[offset:offset+duration]
                altered[injection_positions] *= 1-float(scenario.decline)
                adapted_prediction = rolling_predictions(altered, weekdays, positions)
                for method, original_lower, lower in [
                    ('monthly_frozen', old.lower_bound.to_numpy(), old.lower_bound.to_numpy()),
                    ('rolling_next_day', pred*(1-q), adapted_prediction*(1-q))]:
                    original_flags = values[injection_positions] < original_lower[offset:offset+duration]
                    flags = altered[injection_positions] < lower[offset:offset+duration]
                    event = longest_streak(flags) >= 7
                    if method == 'monthly_frozen' and event != bool(scenario.injected_window_alert):
                        raise ValueError('기존 주입 실험과 재구성 결과 불일치')
                    stress.append({'region': region, 'age_min': int(age), 'month': month,
                        'decline': float(scenario.decline), 'duration': duration, 'offset': offset, 'method': method,
                        'original_window_event': longest_streak(original_flags) >= 7,
                        'injected_event': event, 'flagged_injected_days': int(flags.sum()),
                        'tail_seven_event': longest_streak(flags[-7:]) >= 7,
                        'data_kind': 'synthetic_attenuation'})
    challenger = pd.DataFrame(predictions)
    challenger.to_csv(OUT/'challenger_predictions.csv', index=False, encoding='utf-8-sig')
    pd.DataFrame(calibrations).to_csv(OUT/'challenger_calibration.csv', index=False, encoding='utf-8-sig')
    metrics = []
    for method, frame in [('monthly_frozen', baseline), ('rolling_next_day', challenger)]:
        for month, g in frame.groupby('month'):
            error = np.abs(g.actual-g.predicted)
            macro = [float(np.abs(c.actual-c.predicted).sum()/c.actual.sum()) for _, c in g.groupby(['region', 'age_min'])]
            metrics.append({'method': method, 'month': month, 'cohort_days': len(g),
                'wape': float(error.sum()/g.actual.sum()), 'macro_cohort_wape': float(np.mean(macro)),
                'lower_coverage': float((g.actual >= g.lower_bound).mean()),
                'mean_relative_lower_width': float(((g.predicted-g.lower_bound)/g.predicted).mean())})
    pd.DataFrame(metrics).to_csv(OUT/'forecast_comparison.csv', index=False, encoding='utf-8-sig')
    calendar = []
    for window in policy['diagnostic_windows']:
        month = pd.Timestamp(window['start']).strftime('%Y%m')
        for region, g in baseline.loc[baseline.month == month].groupby('region'):
            inside = (g.date >= window['start']) & (g.date <= window['end'])
            for label, rows in [('inside_window', g.loc[inside]), ('outside_window', g.loc[~inside])]:
                calendar.append({'window': window['name'], 'region': region, 'stratum': label,
                    'start': window['start'], 'end': window['end'], 'cohort_days': len(rows),
                    'below_lower_days': int(rows.below_lower.sum()),
                    'lower_coverage': float((~rows.below_lower).mean()),
                    'wape': float(np.abs(rows.actual-rows.predicted).sum()/rows.actual.sum())})
    pd.DataFrame(calendar).to_csv(OUT/'calendar_windows.csv', index=False, encoding='utf-8-sig')
    peers = []
    for (month, region, age), target in baseline.groupby(['month', 'region', 'age_min']):
        others = baseline.loc[(baseline.month == month) & (baseline.region == region) & (baseline.age_min != age)]
        target_ratio = float(target.actual.sum()/target.predicted.sum())
        other_ratio = float(others.actual.sum()/others.predicted.sum())
        shared = others.groupby('date').below_lower.sum().ge(3)
        peers.append({'month': month, 'region': region, 'age_min': int(age),
            'age_max': 120 if age == 60 else int(age)+9, 'observed_days': len(target),
            'cohort_vs_expected_pct': (target_ratio-1)*100, 'other_ages_vs_expected_pct': (other_ratio-1)*100,
            'relative_to_other_ages_pct': (target_ratio/other_ratio-1)*100,
            'shared_peer_decline_days': int(shared.sum()), 'cohort_below_days': int(target.below_lower.sum()),
            'cooccurring_decline_days': sum(bool(shared.loc[r.date] and r.below_lower) for r in target.itertuples())})
    pd.DataFrame(peers).to_csv(OUT/'peer_context.csv', index=False, encoding='utf-8-sig')
    stress = pd.DataFrame(stress)
    stress.to_csv(OUT/'paired_stress_SYNTHETIC.csv', index=False, encoding='utf-8-sig')
    stress_summary = []
    for (method, decline, duration), g in stress.groupby(['method', 'decline', 'duration']):
        stress_summary.append({'method': method, 'decline': float(decline), 'duration': int(duration), 'scenarios': len(g),
            'events_detected': int(g.injected_event.sum()), 'original_window_events': int(g.original_window_event.sum()),
            'tail_seven_detected': int(g.tail_seven_event.sum()),
            'flagged_day_fraction': float(g.flagged_injected_days.sum()/(len(g)*duration))})
    latest = [r for r in peers if r['month'] == '202512']
    payload = {'version': policy['version'], 'period_end': '2025-12-31', 'challenger_deployed': False,
        'scope': 'retrospective_context_not_causal_or_personal_risk', 'cohorts': latest,
        'calendar': [r for r in calendar if r['window'] == 'dec25_single_day'],
        'assurance': '다른 연령과 비교한 후향 맥락입니다. 당일 다른 연령의 관측도 사용하므로 예측 성능·인과 원인으로 해석하지 않습니다.'}
    (OUT/'latest_context.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    summary = {'version': policy['version'], 'forecast_metrics': metrics, 'calendar': calendar,
        'stress_summary': stress_summary, 'challenger_deployed': False,
        'decision': policy['adoption'],
        'limitations': ['월초 한 달 고정 예측과 매일 직전일까지 갱신하는 다음 날 예측은 정보 시점이 다르다.',
            '달력 제외 비교는 인과 추정·예측 개선이 아니다. 12/25는 단일 비교일이며 공휴일 전수 목록이 아니다.',
            '다른 4개 연령집단 비교는 당일 관측을 사용한 후향 설명이다. 전체 지역 주민을 의미하지 않는다.',
            '원자료와 이전 성능을 본 뒤의 연구이며 독립 검증·고립 정답 평가가 아니다.']}
    (OUT/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    os.environ.setdefault('MPLCONFIGDIR', str(WORK/'mplconfig'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    font = Path('C:/Windows/Fonts/malgun.ttf')
    if font.exists():font_manager.fontManager.addfont(str(font));plt.rcParams['font.family'] = 'Malgun Gothic'
    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    for method, label in [('monthly_frozen', '월초 한 달 고정'), ('rolling_next_day', '매일 갱신 다음 날')]:
        rows = [r for r in metrics if r['method'] == method]
        axes[0].plot([r['month'][-2:] for r in rows], [r['wape']*100 for r in rows], 'o-', label=label)
        rows = [r for r in stress_summary if r['method'] == method and r['decline'] == .2]
        axes[1].plot([r['duration'] for r in rows], [r['tail_seven_detected']/r['scenarios']*100 for r in rows], 'o-', label=label)
    axes[0].set_title('정보 시점이 다른 예측 비교');axes[0].set_xlabel('2025년 월');axes[0].set_ylabel('거래량 WAPE (%)');axes[0].legend(fontsize=8)
    axes[1].set_title('20% 주입 감소: 마지막 7일의 지속 포착');axes[1].set_xlabel('주입 지속 기간 (일)');axes[1].set_ylabel('마지막 7일 모두 포착한 구간 (%)');axes[1].legend(fontsize=8)
    fig.tight_layout();fig.savefig(OUT/'comparison.png', dpi=180);plt.close(fig)
    inputs = ['drift_policy.json', 'run_analysis.py', 'activity_model.py', 'run_manifest.json', 'results/card1_daily.csv', 'activity/manifest.json',
              'activity/daily_predictions.csv', 'activity/stress_cases_SYNTHETIC.csv', 'drift_sources/source_manifest.json']
    manifest = {'version': policy['version'], 'generated_at': datetime.now(timezone(timedelta(hours=9))).isoformat(),
        'script_sha256': sha(Path(__file__)), 'input_sha256': {p: sha(HERE/p) for p in inputs},
        'source_archive_sha256': source['archived_raw_files'],
        'output_sha256': {p.name: sha(p) for p in sorted(OUT.iterdir()) if p.is_file() and p.name != 'manifest.json'}}
    (OUT/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'metrics': metrics, 'calendar': calendar,
        'stress20': [r for r in stress_summary if r['decline'] == .2],
        'chuncheon20': next(r for r in latest if r['region'] == '춘천시' and r['age_min'] == 20)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-archive', type=Path, default=HERE.parents[2]/'work/drift')
    args = parser.parse_args()
    main(args.source_archive)
