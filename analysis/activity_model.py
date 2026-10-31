"""Calibrated downward-activity detector and isolated synthetic stress checks.

No isolation labels are created. Original-data alerts have unknown truth status.
"""
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from first_connection.activity_policy import classify_activity, assign_review_order, STATES
from run_analysis import np, pd, WORK

OUTPUT = HERE / 'activity'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def longest_streak(flags):
    best = run = 0
    for flag in flags:
        run = run + 1 if flag else 0
        best = max(best, run)
    return best


def predict_weekdays(train, dates):
    recent = train.sort_values('date').tail(28)
    means = recent.groupby(recent.date.dt.dayofweek).USE_CNT.mean()
    if set(means.index) != set(range(7)):
        raise ValueError('요일별 과거 관측이 충분하지 않습니다.')
    return np.asarray([means.loc[d.dayofweek] for d in dates], dtype=float)


def fit_threshold(train, calibration, quantile):
    prediction = predict_weekdays(train, list(calibration.date))
    residual = (prediction - calibration.USE_CNT.to_numpy()) / prediction
    return max(0., float(np.quantile(residual, quantile, method='higher'))), prediction


def main():
    OUTPUT.mkdir(exist_ok=True)
    base = json.loads((HERE/'run_manifest.json').read_text(encoding='utf-8'))
    if not base['inputs_verified'] or sha(HERE/'run_analysis.py') != base['code_sha256']:
        raise ValueError('원본 분석 실행 확인 실패')
    if sha(HERE/'input_manifest.json') != base['input_manifest_sha256']:
        raise ValueError('원본 입력 기록 불일치')
    source_paths = ['results/card1_daily.csv', 'results/activity_monthly.csv']
    for name in source_paths:
        if sha(HERE/name) != base['output_sha256'][name]:
            raise ValueError('집계 입력 변조: '+name)
    policy = json.loads((HERE/'activity_policy.json').read_text(encoding='utf-8'))
    daily = pd.read_csv(HERE/'results/card1_daily.csv', parse_dates=['date'])
    monthly = pd.read_csv(HERE/'results/activity_monthly.csv', dtype={'month': str})
    thresholds, predictions, cohort_rows, stress = [], [], [], []
    rng = np.random.default_rng(policy['stress_seed'])
    for (region, age), group in daily.groupby(['region', 'age_min']):
        group = group.sort_values('date')
        train = group.loc[group.date < '2025-09-01']
        calibration = group.loc[(group.date >= '2025-09-01') & (group.date < '2025-10-01')]
        threshold, cal_pred = fit_threshold(train, calibration, policy['calibration_quantile'])
        thresholds.append({'region': region, 'age_min': int(age), 'train_end': '2025-08-31',
                           'calibration_end': '2025-09-30', 'calibration_days': len(calibration),
                           'relative_lower_error_threshold': threshold,
                           'calibration_lower_coverage': float((calibration.USE_CNT.to_numpy() >= cal_pred*(1-threshold)).mean())})
        for month in ['202510', '202511', '202512']:
            start = pd.Timestamp(month[:4]+'-'+month[4:]+'-01')
            end = start + pd.offsets.MonthBegin(1)
            history = group.loc[group.date < start]
            current = group.loc[(group.date >= start) & (group.date < end)]
            dates = list(current.date)
            prediction = predict_weekdays(history, dates)
            actual = current.USE_CNT.to_numpy(dtype=float)
            lower = prediction * (1-threshold)
            below = actual < lower
            for day, y, estimate, bound, flag in zip(dates, actual, prediction, lower, below):
                predictions.append({'date': day.strftime('%Y-%m-%d'), 'month': month, 'region': region,
                    'age_min': int(age), 'train_end': history.date.max().strftime('%Y-%m-%d'),
                    'calibration_end': '2025-09-30', 'actual': float(y), 'predicted': float(estimate),
                    'lower_bound': float(bound), 'below_lower': bool(flag)})
            source = monthly.loc[(monthly.month == month) & (monthly.region == region) & (monthly.age_min == age)].iloc[0]
            row = {k: float(source[k]) for k in ['population_change_pct', 'card_change_pct', 'population_relative_index',
                    'cell_coverage', 'baseline_population_mass_coverage']}
            row.update({'region': region, 'age_min': int(age), 'age_max': 120 if age == 60 else int(age)+9,
                'month': month, 'period_start': dates[0].strftime('%Y-%m-%d'), 'period_end': dates[-1].strftime('%Y-%m-%d'),
                'observed_days': len(current), 'below_lower_days': int(below.sum()),
                'longest_below_streak': longest_streak(below), 'relative_lower_error_threshold': threshold,
                'lower_coverage': float((~below).mean()),
                'actual_vs_expected_pct': float((actual.sum()/prediction.sum()-1)*100)})
            row['state'] = classify_activity(row, policy)
            row['state_label'] = STATES[row['state']][0]
            row['observations'] = [f"거래량 예측 하한 미만 {row['below_lower_days']}/{len(current)}일, 최장 연속 {row['longest_below_streak']}일",
                f"동일 격자 유동량 7~9월 대비 {row['population_change_pct']:+.2f}%, 지역 전체 대비 상대 지수 {row['population_relative_index']:.2f}",
                f"기준 격자 관측률 {row['cell_coverage']:.1%}, 기준 유동량 가중 관측률 {row['baseline_population_mass_coverage']:.1%}"]
            cohort_rows.append(row)
            # Each scenario independently perturbs only this cohort-month's test values.
            # Neither training, threshold, nor another scenario receives injected values.
            for decline in policy['stress_declines']:
                for duration in policy['stress_durations_days']:
                    offset = int(rng.integers(0, len(actual)-duration+1))
                    altered = actual.copy();altered[offset:offset+duration] *= 1-decline
                    for method, bound in [('calibrated', lower), ('fixed_10pct', prediction*.9)]:
                        original_flags = actual[offset:offset+duration] < bound[offset:offset+duration]
                        altered_flags = altered[offset:offset+duration] < bound[offset:offset+duration]
                        was_alert = longest_streak(original_flags) >= policy['persistent_days']
                        now_alert = longest_streak(altered_flags) >= policy['persistent_days']
                        stress.append({'data_kind': 'synthetic_attenuation', 'region': region, 'age_min': int(age),
                            'month': month, 'decline': decline, 'duration': duration, 'offset': offset, 'method': method,
                            'original_window_alert': was_alert, 'injected_window_alert': now_alert,
                            'new_alert': now_alert and not was_alert, 'injected_days': duration,
                            'flagged_injected_days': int(altered_flags.sum())})
    for month in ['202510', '202511', '202512']:
        current_predictions = [r for r in predictions if r['month'] == month]
        empirical_coverage = sum(not r['below_lower'] for r in current_predictions) / len(current_predictions)
        for row in [r for r in cohort_rows if r['month'] == month]:
            row['empirical_month_lower_coverage'] = empirical_coverage
            row['calibration_watch'] = empirical_coverage < policy['coverage_review_floor']
        assign_review_order([r for r in cohort_rows if r['month'] == month])
    pd.DataFrame(thresholds).to_csv(OUTPUT/'calibration.csv', index=False, encoding='utf-8-sig')
    pred = pd.DataFrame(predictions); pred.to_csv(OUTPUT/'daily_predictions.csv', index=False, encoding='utf-8-sig')
    pd.DataFrame(cohort_rows).drop(columns='observations').to_csv(OUTPUT/'monthly_assessments.csv', index=False, encoding='utf-8-sig')
    stress = pd.DataFrame(stress);stress.to_csv(OUTPUT/'stress_cases_SYNTHETIC.csv', index=False, encoding='utf-8-sig')
    stress_summary = []
    for (method, decline, duration), g in stress.groupby(['method', 'decline', 'duration']):
        stress_summary.append({'method': method, 'decline': float(decline), 'duration': int(duration), 'scenarios': len(g),
            'injected_events_detected': int(g.injected_window_alert.sum()), 'original_window_events': int(g.original_window_alert.sum()),
            'newly_triggered_events': int(g.new_alert.sum()), 'detection_fraction': float(g.injected_window_alert.mean())})
    empirical = []
    for month, g in pred.groupby('month'):
        empirical.append({'month': month, 'cohort_days': len(g), 'lower_coverage': float((~g.below_lower).mean()),
                          'below_lower_days': int(g.below_lower.sum())})
    latest = [r for r in cohort_rows if r['month'] == '202512']
    queue = sorted([r for r in latest if r['review_order'] is not None], key=lambda r: r['review_order'])
    payload = {'version': policy['version'], 'scope': policy['scope'], 'period_start': '2025-12-01', 'period_end': '2025-12-31',
               'policy': policy, 'cohorts': latest, 'review_queue': [{'region': r['region'], 'age_min': r['age_min'],
                   'age_max': r['age_max'], 'review_order': r['review_order'], 'state_label': r['state_label'],
                   'basis': r['observations'], 'reference_only': True,
                   'calibration_watch': r['calibration_watch']} for r in queue],
               'assurance': '과거 집계 활동의 추가 확인 상태·순서이며 개인의 고립 위험 등급·지원 대상 순위가 아님.'}
    (OUTPUT/'latest.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    summary = {'target': '거래량의 지속 하방 이탈과 집계 유동량 변화. 고립 여부 분류 아님.',
        'calibration': '7~8월 이력에서 9월 전체 예측 → 9월 상대 하방 오차의 경험적 90분위 고정',
        'forecast': '각 평가 월 직전 28일의 요일별 평균으로 다음 월 전체 고정 예측',
        'evaluation': '10~12월 후향 시점 분리. 원자료는 이미 탐색한 적 있음. 블라인드 평가 아님.',
        'empirical_months': empirical, 'state_counts': pd.DataFrame(cohort_rows).state.value_counts().to_dict(),
        'stress_scenario_pairs': len(stress)//2, 'stress_summary': stress_summary,
        'limitations': ['경험적 하한은 시계열 상관·분포 변화에서 90% 포함률을 보장하지 않음.',
            '원자료 경보의 고립 정답은 알 수 없어 오경보율·고립 정확도를 계산하지 않음.',
            '주입 감소 포착률은 인위적 거래 감소에 대한 반응으로, 고립 탐지 민감도가 아님.',
            '유동량 감소·연속일·관측률 정책은 고립 정답으로 검증된 절단값이 아님.']}
    (OUTPUT/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    os.environ.setdefault('MPLCONFIGDIR', str(WORK/'mplconfig'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    font = Path('C:/Windows/Fonts/malgun.ttf')
    if font.exists():
        font_manager.fontManager.addfont(str(font));plt.rcParams['font.family'] = 'Malgun Gothic'
    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    axes[0].bar([r['month'][-2:] for r in empirical], [r['lower_coverage']*100 for r in empirical], color='#27645b')
    axes[0].axhline(90, color='#777', linestyle='--', label='보정 분위 기준 90% (보장 아님)')
    axes[0].axhline(policy['coverage_review_floor']*100, color='#b45d3d', linestyle=':', label='모형 확인 분기 80%')
    axes[0].set_ylim(0, 100);axes[0].set_xlabel('2025년 월');axes[0].set_ylabel('원자료의 하한 포함률 (%)');axes[0].legend(fontsize=8)
    axes[0].set_title('10·12월의 보정 이탈: 경보 해석 보류')
    for method, color in [('calibrated', '#27645b'), ('fixed_10pct', '#b5a27d')]:
        entries = [r for r in stress_summary if r['method'] == method and r['duration'] == 14]
        axes[1].plot([r['decline']*100 for r in entries], [r['detection_fraction']*100 for r in entries], 'o-', label=method, color=color)
    axes[1].set_ylim(0, 105);axes[1].set_xlabel('주입 거래 감소율 (%)');axes[1].set_ylabel('주입 감소 포착률 (%)');axes[1].legend(fontsize=8)
    axes[1].set_title('14일 인위 감소 반응 (고립 탐지 성능 아님)')
    fig.tight_layout();fig.savefig(OUTPUT/'evaluation.png', dpi=180);plt.close(fig)
    inputs = ['run_manifest.json', 'input_manifest.json', 'run_analysis.py', 'activity_policy.json'] + source_paths
    manifest = {'version': policy['version'], 'generated_at': datetime.now(timezone(timedelta(hours=9))).isoformat(),
        'script_sha256': sha(Path(__file__)), 'policy_code_sha256': sha(HERE.parent/'first_connection/activity_policy.py'),
        'input_sha256': {p: sha(HERE/p) for p in inputs},
        'output_sha256': {p.name: sha(p) for p in sorted(OUTPUT.iterdir()) if p.is_file() and p.name != 'manifest.json'},
        'runtime': {'python': sys.version.split()[0], 'pandas': pd.__version__, 'numpy': np.__version__}}
    (OUTPUT/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'empirical': empirical, 'state_counts': summary['state_counts'], 'queue': payload['review_queue'],
                      'stress_summary': stress_summary}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    main()
