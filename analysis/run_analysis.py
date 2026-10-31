"""Aggregate activity analysis for the isolation-agent track, independent of climate work.

No isolation labels exist in these inputs. Forecast targets are transaction counts;
monitoring rules concern aggregate activity changes, not individual isolation risk.
"""
import argparse
import calendar
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = HERE.parents[2] / 'work' / 'isolation-analysis'
if (WORK / 'deps').exists():
    sys.path.append(str(WORK / 'deps'))
import numpy as np
import pandas as pd

REGION = {'11230': '강남구', '32010': '춘천시'}
CARD_REGION = {'서울 강남구': '강남구', '강원 춘천시': '춘천시'}
AGE_GROUPS = [20, 30, 40, 50, 60]
BASE_MONTHS = ['202507', '202508', '202509']
VERSION = 'isolation-activity-v1'


def safe_ratio(a, b):
    return np.divide(a, b, out=np.full_like(np.asarray(a, dtype=float), np.nan), where=np.asarray(b) > 0)


def age_band(series):
    numbers = pd.to_numeric(series.astype(str).str.extract(r'(\d+)', expand=False), errors='coerce')
    return numbers.clip(upper=60)


def verify_inputs(raw):
    records = json.loads((HERE / 'input_manifest.json').read_text(encoding='utf-8'))
    for record in records:
        path = raw / record['name']
        with path.open('rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
        if path.stat().st_size != record['size_bytes'] or digest != record['sha256']:
            raise ValueError('원본 크기·해시 불일치: ' + record['name'])
    return records


def population(raw, out):
    frames, audit = {}, []
    value_cols = [f'{sex}_FLOW_POP_CNT_{age}' for sex in ['MAN', 'WMAN'] for age in ['10G','20G','30G','40G','50G','60GU']]
    keys = ['BLOCK_CD', 'X_COORD', 'Y_COORD']
    for path in sorted(raw.glob('flow_age_pop_2025*.csv')):
        df = pd.read_csv(path, sep='|', dtype={'BLOCK_CD': str})
        month = path.stem[-6:]
        if list(df.STD_YM.astype(str).unique()) != [month]:
            raise ValueError('파일명·월 불일치: ' + path.name)
        df['region'] = df.BLOCK_CD.str[:5].map(REGION)
        target = df.loc[df.region.notna()].copy()
        duplicates = int(target.duplicated(keys).sum())
        missing = int(target[value_cols].isna().sum().sum())
        negative = int(target[value_cols].lt(0).sum().sum())
        if duplicates or missing or negative:
            raise ValueError('유동인구 품질 검증 실패: ' + path.name)
        indexed = target.set_index(keys)
        frames[month] = indexed
        audit.append({'file': path.name, 'rows': len(df), 'target_rows': len(target),
                      'out_of_scope_rows': int(df.region.isna().sum()), 'duplicate_key': duplicates,
                      'missing_values': missing, 'negative_values': negative})
    if sorted(frames) != [f'2025{m:02d}' for m in range(7,13)]:
        raise ValueError('7~12월 유동인구 6개 파일이 필요합니다.')
    # Fixed historical panel: no future month is used to choose baseline cells.
    base_keys = frames[BASE_MONTHS[0]].index
    for month in BASE_MONTHS[1:]:
        base_keys = base_keys.intersection(frames[month].index)
    base = sum(frames[m].loc[base_keys, value_cols] for m in BASE_MONTHS) / len(BASE_MONTHS)
    base['region'] = frames[BASE_MONTHS[0]].loc[base_keys, 'region']
    records = []
    for month, frame in frames.items():
        available = base_keys.intersection(frame.index)
        current, reference = frame.loc[available], base.loc[available]
        for region in REGION.values():
            mask = current.region == region
            baseline_cell_count = int((base.region == region).sum())
            if baseline_cell_count == 0:
                continue
            for age in AGE_GROUPS:
                suffix = '60GU' if age == 60 else f'{age}G'
                cols = [f'{sex}_FLOW_POP_CNT_{suffix}' for sex in ['MAN','WMAN']]
                observed = float(current.loc[mask, cols].to_numpy().sum())
                expected = float(reference.loc[mask, cols].to_numpy().sum())
                full_baseline = float(base.loc[base.region.eq(region), cols].to_numpy().sum())
                records.append({'month': month, 'region': region, 'age_min': age,
                    'population_daily_cell_sum': observed, 'population_baseline_same_cells': expected,
                    'population_index': 100 * observed / expected if expected > 0 else np.nan,
                    'baseline_cells': baseline_cell_count, 'available_cells': int(mask.sum()),
                    'cell_coverage': float(mask.sum()) / baseline_cell_count,
                    'baseline_population_mass_coverage': expected / full_baseline if full_baseline else np.nan})
    result = pd.DataFrame(records)
    pd.DataFrame(audit).to_csv(out / 'population_quality.csv', index=False, encoding='utf-8-sig')
    return result, audit


def cards(raw, out):
    results, audit = {}, []
    specs = [('card1', '신한카드_빅콘테스트2026_데이터1.txt'),
             ('card2', '신한카드_빅콘테스트2026_데이터2_수정.txt')]
    for name, filename in specs:
        path = raw / filename
        dtypes = {c:'category' for c in ['MCT_SGG_CD','MCT_RY_CD','SEX_CCD','AGE_CCD']}
        dtypes['TIME_GB' if name == 'card1' else 'CLN_SGG_CD'] = 'category'
        df = pd.read_csv(path, sep='\t', encoding='cp949', dtype=dtypes)
        numeric = ['TS_AT','USE_CNT']
        keys = [c for c in df if c not in numeric]
        corporate = df.SEX_CCD.astype(str).eq('법인') | df.AGE_CCD.astype(str).eq('법인')
        audit.append({'file': filename, 'rows': len(df), 'duplicate_key': int(df.duplicated(keys).sum()),
            'missing_values': int(df.isna().sum().sum()), 'negative_values': int(df[numeric].lt(0).sum().sum()),
            'corporate_rows_excluded': int(corporate.sum()),
            'corporate_use_count_excluded': int(df.loc[corporate,'USE_CNT'].sum()),
            'first_date': str(df.TA_YMD.min()), 'last_date': str(df.TA_YMD.max())})
        if any(audit[-1][field] for field in ['duplicate_key','missing_values','negative_values']):
            raise ValueError('카드 품질 검증 실패: ' + filename)
        df['region'] = df.MCT_SGG_CD.astype(str).map(CARD_REGION)
        df['age_min'] = age_band(df.AGE_CCD)
        df = df.loc[~corporate & df.region.notna() & df.age_min.isin(AGE_GROUPS)].copy()
        df['date'] = pd.to_datetime(df.TA_YMD.astype(str), format='%Y%m%d')
        df['month'] = df.date.dt.strftime('%Y%m')
        df['age_min'] = df.age_min.astype(int)
        daily = df.groupby(['date','region','age_min'], observed=True)[numeric].sum().reset_index()
        expected_days = len(pd.date_range('2025-07-01','2025-12-31'))
        if len(daily) != expected_days * 10 or not daily.groupby(['region','age_min']).size().eq(expected_days).all():
            raise ValueError('카드 일별 집단 관측 누락: 0으로 자동 대체하지 않음')
        daily['month'] = daily.date.dt.strftime('%Y%m')
        results[name] = daily
        monthly = daily.groupby(['month','region','age_min'], observed=True).agg(
            card_uses=('USE_CNT','sum'), card_amount=('TS_AT','sum'), observed_days=('date','size')).reset_index()
        monthly['card_daily_uses'] = monthly.card_uses / monthly.observed_days
        monthly.to_csv(out / f'{name}_monthly.csv', index=False, encoding='utf-8-sig')
        if name == 'card2':
            # Province of residence is not city/district of residence.
            province = df.region.map({'강남구':'서울','춘천시':'강원'})
            df['same_province_uses'] = df.USE_CNT.where(df.CLN_SGG_CD.astype(str).eq(province),0)
            residence = df.groupby(['month','region','age_min'], observed=True).agg(
                total_uses=('USE_CNT','sum'), same_province_uses=('same_province_uses','sum')).reset_index()
            residence['same_province_share'] = residence.same_province_uses / residence.total_uses
            residence.to_csv(out / 'card2_residence_province.csv', index=False, encoding='utf-8-sig')
        else:
            by_time = df.groupby(['month','region','age_min','TIME_GB'],observed=True).USE_CNT.sum().reset_index()
            by_time.to_csv(out / 'card1_time_profile.csv',index=False,encoding='utf-8-sig')
        del df
    pd.DataFrame(audit).to_csv(out / 'card_quality.csv', index=False, encoding='utf-8-sig')
    results['card1'].to_csv(out / 'card1_daily.csv',index=False,encoding='utf-8-sig')
    return results, audit


def design(dates):
    dates = pd.DatetimeIndex(dates)
    trend = np.asarray((dates - pd.Timestamp('2025-07-01')).days, dtype=float) / 30
    weekdays = np.eye(7)[dates.dayofweek][:,1:]
    return np.column_stack([np.ones(len(dates)), weekdays, trend])


def forecast(train, future_dates, method):
    if method == 'weekday_28d':
        recent = train.tail(28)
        means = recent.groupby(recent.date.dt.dayofweek).USE_CNT.mean()
        return np.asarray([means.loc[d.dayofweek] for d in future_dates],dtype=float)
    x = design(train.date)
    penalty = np.eye(x.shape[1]); penalty[0,0] = 0
    beta = np.linalg.solve(x.T @ x + penalty, x.T @ np.log1p(train.USE_CNT.to_numpy(dtype=float)))
    return np.maximum(0, np.expm1(design(future_dates) @ beta))


def model_evaluation(daily, out):
    methods = ['weekday_28d','ridge_log_weekday_trend']
    validation, predictions = [], []
    for (region, age), group in daily.groupby(['region','age_min']):
        group = group.sort_values('date')
        train = group.loc[group.date < '2025-09-01']
        valid = group.loc[(group.date >= '2025-09-01') & (group.date < '2025-10-01')]
        for method in methods:
            pred = forecast(train,list(valid.date),method)
            mae = float(np.abs(valid.USE_CNT.to_numpy()-pred).mean())
            validation.append({'region':region,'age_min':int(age),'method':method,'mae':mae,
                               'scaled_mae':mae/float(train.USE_CNT.mean())})
    validation = pd.DataFrame(validation)
    selected = str(validation.groupby('method').scaled_mae.mean().sort_values().index[0])
    for month in ['202510','202511','202512']:
        cutoff = pd.Timestamp(month[:4]+'-'+month[4:]+'-01')
        next_month = cutoff + pd.offsets.MonthBegin(1)
        for (region,age), group in daily.groupby(['region','age_min']):
            group = group.sort_values('date')
            train = group.loc[group.date < cutoff]
            test = group.loc[(group.date >= cutoff) & (group.date < next_month)]
            for method in methods:
                pred = forecast(train,list(test.date),method)
                for day,actual,estimate in zip(test.date,test.USE_CNT,pred):
                    predictions.append({'date':day.strftime('%Y-%m-%d'),'month':month,'region':region,'age_min':int(age),
                        'train_end':train.date.max().strftime('%Y-%m-%d'),'method':method,
                        'selected_on_september':method==selected,'actual':int(actual),'predicted':float(estimate)})
    predictions = pd.DataFrame(predictions)
    metrics=[]
    for (month,method), g in predictions.groupby(['month','method']):
        error=np.abs(g.actual-g.predicted)
        per_group=[float(np.abs(c.actual-c.predicted).sum()/c.actual.sum()) for _,c in g.groupby(['region','age_min'])]
        metrics.append({'month':month,'method':method,'selected_on_september':method==selected,
                        'n':len(g),'mae_transactions':float(error.mean()),'wape':float(error.sum()/g.actual.sum()),
                        'macro_cohort_wape':float(np.mean(per_group))})
    validation.to_csv(out/'forecast_validation_september.csv',index=False,encoding='utf-8-sig')
    predictions.to_csv(out/'forecast_predictions.csv',index=False,encoding='utf-8-sig')
    pd.DataFrame(metrics).to_csv(out/'forecast_test_metrics.csv',index=False,encoding='utf-8-sig')
    regional=[]
    for (month,method,region),g in predictions.groupby(['month','method','region']):
        regional.append({'month':month,'method':method,'region':region,'wape':float(np.abs(g.actual-g.predicted).sum()/g.actual.sum())})
    pd.DataFrame(regional).to_csv(out/'forecast_test_metrics_by_region.csv',index=False,encoding='utf-8-sig')
    return {'target':'지역·연령집단의 가맹점 카드 결제 건수. 고립 위험 분류가 아님.',
        'validation_period':'2025-09, training 2025-07~08', 'test_period':'2025-10~12, monthly rolling origin',
        'selected_method':selected,'validation_scaled_mae':validation.groupby('method').scaled_mae.mean().to_dict(),
        'test_metrics':metrics,'leakage_control':'각 월의 예측 입력은 이전 월 말까지. 같은 검증 월 내 실제값을 사용하지 않음.'}


def activity_signals(pop, out):
    card = pd.read_csv(out/'card1_monthly.csv',dtype={'month':str})
    baselines = card.loc[card.month.isin(BASE_MONTHS)].groupby(['region','age_min']).card_daily_uses.mean()
    card['card_baseline_daily_uses']=[baselines.loc[(r,a)] for r,a in zip(card.region,card.age_min)]
    card['card_index']=100 * card.card_daily_uses/card.card_baseline_daily_uses
    combined=card.merge(pop,on=['month','region','age_min'],validate='one_to_one')
    for (month,region), g in combined.groupby(['month','region']):
        ci=100*g.card_daily_uses.sum()/g.card_baseline_daily_uses.sum()
        pi=100*g.population_daily_cell_sum.sum()/g.population_baseline_same_cells.sum()
        combined.loc[g.index,'card_relative_index']=100*g.card_index/ci
        combined.loc[g.index,'population_relative_index']=100*g.population_index/pi
    combined['card_change_pct']=combined.card_index-100
    combined['population_change_pct']=combined.population_index-100
    combined['review_signal']=np.select([
        combined.cell_coverage.lt(.9),
        combined.card_index.le(90)&combined.population_index.le(90),
        combined.card_index.le(90)|combined.population_index.le(90)],
        ['격자 관측범위 확인','활동 동반감소 추가확인','단일 지표 감소 확인'],default='동반감소 기준 미충족')
    combined.to_csv(out/'activity_monthly.csv',index=False,encoding='utf-8-sig')
    sensitivity=[]
    for threshold in [5,10,20]:
        target=combined.loc[combined.month.ge('202510')]
        for coverage in [.9,.95]:
            flagged=target.card_index.le(100-threshold)&target.population_index.le(100-threshold)&target.cell_coverage.ge(coverage)
            sensitivity.append({'decline_threshold_pct':threshold,'min_cell_coverage':coverage,
                                'review_month_cohorts':int(flagged.sum()),'total_month_cohorts':len(target)})
    pd.DataFrame(sensitivity).to_csv(out/'threshold_sensitivity.csv',index=False,encoding='utf-8-sig')
    latest=combined.loc[combined.month.eq('202512')].copy()
    records=[]
    for row in latest.itertuples():
        evidence=(f'실제 제공 원본 분석. 2025-12 대 2025-07~09 기준: 개인카드 일평균 결제건수 {row.card_change_pct:+.2f}%, '
            f'동일 관측격자 연령별 유동량 {row.population_change_pct:+.2f}%. '
            f'기준 격자 관측률 {row.cell_coverage:.1%}. 가맹점 소재지의 활동이며 지역 주민·개인의 고립 판정이 아님.')
        records.append({'region':row.region,'age_min':int(row.age_min),'age_max':59 if row.age_min==50 else (120 if row.age_min==60 else int(row.age_min)+9),
            'period_start':'2025-12-01','period_end':'2025-12-31','signal_label':row.review_signal,
            'evidence':evidence,'model_version':VERSION,'data_kind':'analysis',
            'risk_level':'','cause_summary':'','priority_rank':'','priority_basis':'',
            'dataset_refs':'SKT flow_age_pop_202507~202512 + 신한카드 데이터1; analysis/input_manifest.json'})
    pd.DataFrame(records).to_csv(out/'regional_signals.csv',index=False,encoding='utf-8-sig')
    return combined, sensitivity


def figures(combined, metrics):
    os.environ.setdefault('MPLCONFIGDIR',str(WORK/'mplconfig'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    font=Path('C:/Windows/Fonts/malgun.ttf')
    if font.exists():font_manager.fontManager.addfont(str(font));plt.rcParams['font.family']='Malgun Gothic'
    plt.rcParams['axes.unicode_minus']=False
    destination=HERE/'figures';destination.mkdir(exist_ok=True)
    colors=plt.get_cmap('tab10').colors
    fig,axes=plt.subplots(1,2,figsize=(12,4.5),sharey=True)
    for ax,region in zip(axes,REGION.values()):
        for i,age in enumerate(AGE_GROUPS):
            g=combined.loc[(combined.region==region)&(combined.age_min==age)].sort_values('month')
            label=f'{age}세 이상' if age==60 else f'{age}대'
            ax.plot(g.month.str[-2:],g.card_index,color=colors[i],label=label+' 카드',linewidth=1.8)
            ax.plot(g.month.str[-2:],g.population_index,color=colors[i],linestyle='--',alpha=.7)
        ax.axhline(100,color='#777',linewidth=.8);ax.set_title(region);ax.set_xlabel('2025년 월')
        ax.grid(alpha=.15)
    axes[0].set_ylabel('7~9월 기준 = 100');axes[1].legend(fontsize=8,ncol=2)
    fig.suptitle('집계 활동 변화: 실선 카드 건수 · 점선 유동량 (고립 위험도 아님)')
    fig.tight_layout();fig.savefig(destination/'activity_trends.png',dpi=180);plt.close(fig)
    last=combined.loc[combined.month.eq('202512')].sort_values(['region','age_min'])
    values=last[['card_change_pct','population_change_pct']].to_numpy()
    fig,ax=plt.subplots(figsize=(7,6));limit=max(10,float(np.abs(values).max()))
    image=ax.imshow(values,cmap='RdBu',vmin=-limit,vmax=limit,aspect='auto')
    ax.set_xticks([0,1],['카드 일평균 건수','동일 격자 유동량'])
    ax.set_yticks(range(len(last)),[f'{r.region} {r.age_min}'+('세 이상' if r.age_min==60 else '대') for r in last.itertuples()])
    for i in range(len(values)):
        for j in range(2):ax.text(j,i,f'{values[i,j]:+.1f}%',ha='center',va='center',color='black',fontsize=10)
    ax.set_title('12월 변화율: 7~9월 기준 대비\n지역·연령집단 활동 지표, 개인 고립 판정 아님')
    fig.colorbar(image,ax=ax,label='변화율 (%)');fig.tight_layout();fig.savefig(destination/'december_changes.png',dpi=180);plt.close(fig)
    table=pd.DataFrame(metrics['test_metrics'])
    fig,ax=plt.subplots(figsize=(8,4))
    for i,method in enumerate(['weekday_28d','ridge_log_weekday_trend']):
        g=table.loc[table.method==method].sort_values('month')
        ax.bar(np.arange(3)+(i-.5)*.35,g.wape*100,width=.35,label=method)
    ax.set_xticks(range(3),['2025-10','2025-11','2025-12']);ax.set_ylabel('WAPE (%)')
    ax.set_title('카드 결제건수의 월 단위 시점 분리 예측 평가\n고립 위험 분류 성능이 아님');ax.legend(fontsize=8)
    fig.tight_layout();fig.savefig(destination/'forecast_evaluation.png',dpi=180);plt.close(fig)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--raw-dir',type=Path,default=WORK/'raw');args=parser.parse_args()
    out=HERE/'results';out.mkdir(exist_ok=True)
    manifest=verify_inputs(args.raw_dir)
    pop,pop_audit=population(args.raw_dir,out)
    daily,card_audit=cards(args.raw_dir,out)
    one=pd.read_csv(out/'card1_monthly.csv',dtype={'month':str})
    two=pd.read_csv(out/'card2_monthly.csv',dtype={'month':str})
    reconciliation=one.merge(two,on=['month','region','age_min'],suffixes=('_card1','_card2'),validate='one_to_one')
    reconciliation['use_count_gap']=reconciliation.card_uses_card2-reconciliation.card_uses_card1
    reconciliation['use_count_gap_pct']=100*reconciliation.use_count_gap/reconciliation.card_uses_card1
    reconciliation.to_csv(out/'card_reconciliation.csv',index=False,encoding='utf-8-sig')
    metrics=model_evaluation(daily['card1'],out)
    combined,sensitivity=activity_signals(pop,out)
    summary={'version':VERSION,'input_files':len(manifest),'population_rows':sum(r['rows'] for r in pop_audit),
        'card_rows':sum(r['rows'] for r in card_audit),'quality':{'population':pop_audit,'cards':card_audit},
        'forecast':metrics,'threshold_sensitivity':sensitivity,
        'latest':combined.loc[combined.month.eq('202512')].to_dict('records'),
        'interpretation':'지역 가맹점 결제 및 월별 연령 유동량의 변화. 고립 여부·원인·개인 위험도 정답은 관측되지 않음.'}
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    figures(combined,metrics)
    run_manifest={'version':VERSION,'generated_at':datetime.now(timezone(timedelta(hours=9))).isoformat(),
        'inputs_verified':True,'input_manifest_sha256':hashlib.sha256((HERE/'input_manifest.json').read_bytes()).hexdigest(),
        'code_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'runtime':{'python':sys.version.split()[0],'pandas':pd.__version__,'numpy':np.__version__},
        'output_sha256':{str(p.relative_to(HERE)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.iterdir()) if p.is_file()}}
    (HERE/'run_manifest.json').write_text(json.dumps(run_manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'input_files':len(manifest),'population_rows':summary['population_rows'],'card_rows':summary['card_rows'],
        'selected_method':metrics['selected_method'],'validation':metrics['validation_scaled_mae'],
        'sensitivity':sensitivity,'latest':combined.loc[combined.month.eq('202512'),['region','age_min','card_change_pct','population_change_pct','cell_coverage','review_signal']].to_dict('records')},ensure_ascii=False,indent=2))


if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    main()
