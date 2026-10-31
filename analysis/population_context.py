"""Separate marginal profiles and quarantine duplicate source files.

Original raw files and v0.3-v0.9 model artifacts are never modified.
"""
import argparse
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from run_analysis import pd, np, WORK

HERE = Path(__file__).resolve().parent
OUT = HERE/'population_context'
KEYS = ['BLOCK_CD','X_COORD','Y_COORD']
REGIONS = {'11230':'강남구','32010':'춘천시'}
VIEWS = {'time':[f'TMST_{h:02d}' for h in range(24)],
         'wkdy':[f'FLOW_POP_CNT_{d}' for d in ['MON','TUS','WED','THU','FRI','SAT','SUN']]}


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect_frame(df, month, columns, name):
    expected = ['STD_YM',*KEYS,*columns]
    if list(df.columns) != expected: raise ValueError('열 정의 불일치: '+name)
    if df[expected].isna().any().any() or set(df.STD_YM.astype(str)) != {month}:
        raise ValueError('기준월·결측 확인 필요: '+name)
    values = df[columns].to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values < 0).any(): raise ValueError('유동량 수치 확인 필요: '+name)
    duplicates = int(df.duplicated(['STD_YM',*KEYS]).sum())
    clones = int(df.duplicated().sum())
    multiplicity = df.groupby(['STD_YM',*KEYS],sort=False,dropna=False).size()
    cleaned = df.drop_duplicates().copy()
    if cleaned.duplicated(['STD_YM',*KEYS]).any():
        raise ValueError('값이 다른 중복 키는 선택·평균하지 않습니다: '+name)
    cleaned['region'] = cleaned.BLOCK_CD.str[:5].map(REGIONS)
    status = 'deduplicated_sensitivity_only' if duplicates else 'primary_descriptive'
    audit = {'file':name,'month':month,'raw_rows':len(df),'unique_rows':len(cleaned),
        'duplicate_key_rows':duplicates,'identical_extra_rows':clones,
        'key_multiplicity_min':int(multiplicity.min()),'key_multiplicity_max':int(multiplicity.max()),
        'primary_eligible':not bool(duplicates),'use_status':status,
        'outside_region_rows_raw':int(df.BLOCK_CD.str[:5].map(REGIONS).isna().sum()),
        'target_unique_rows':int(cleaned.region.notna().sum()),
        'raw_to_unique_value_sum_ratio':float(values.sum()/cleaned[columns].to_numpy().sum())
            if cleaned[columns].to_numpy().sum() else None}
    return cleaned.loc[cleaned.region.notna()].set_index(KEYS), audit


def make_baseline(frames, columns, baseline_months):
    common = frames[baseline_months[0]].index
    for month in baseline_months[1:]: common = common.intersection(frames[month].index)
    if not len(common): raise ValueError('기준 격자 교집합이 없습니다.')
    baseline = sum(frames[m].loc[common,columns] for m in baseline_months)/len(baseline_months)
    baseline['region'] = frames[baseline_months[0]].loc[common,'region']
    return baseline


def matched_rows(frame, baseline, columns, month, view, use_status):
    shared = baseline.index.intersection(frame.index)
    result = []
    for region in REGIONS.values():
        b = baseline.loc[baseline.region.eq(region)]
        valid = shared.intersection(b.index)
        current = frame.loc[valid,columns].sum()
        reference = b.loc[valid,columns].sum()
        total_base = b[columns].sum()
        for col in columns:
            expected,observed = float(reference[col]),float(current[col])
            result.append({'view':view,'month':month,'region':region,'axis':col,
                'use_status':use_status,'current_sum':observed,'baseline_same_cells':expected,
                'index':100*observed/expected if expected else None,
                'change_pct':100*(observed/expected-1) if expected else None,
                'baseline_cells':len(b),'observed_baseline_cells':len(valid),
                'cell_coverage':len(valid)/len(b) if len(b) else None,
                'baseline_mass_coverage':expected/float(total_base[col]) if total_base[col] else None})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raw-dir',type=Path,default=WORK.parent/'population-context/raw')
    parser.add_argument('--age-dir',type=Path,default=WORK/'raw')
    args = parser.parse_args()
    policy = json.loads((HERE/'population_context_policy.json').read_text(encoding='utf-8'))
    inputs = json.loads((HERE/'population_input_manifest.json').read_text(encoding='utf-8'))
    original = json.loads((HERE/'input_manifest.json').read_text(encoding='utf-8'))
    # Verify every raw input before producing outputs.
    for r in inputs:
        path = args.raw_dir/r['name']
        if path.stat().st_size != r['size_bytes'] or sha(path) != r['sha256']:
            raise ValueError('새 통신 원본 크기·해시 불일치: '+r['name'])
    for r in original:
        if r['name'].startswith('flow_age_') and sha(args.age_dir/r['name']) != r['sha256']:
            raise ValueError('기존 연령 격자 원본 해시 불일치: '+r['name'])
    dtype = {k:str for k in KEYS}
    frames, quality, profile_rows = {}, [], []
    for view,columns in VIEWS.items():
        frames[view] = {}
        audits = {}
        for month in policy['months']:
            name = f'flow_{view}_pop_{month}.csv'
            df = pd.read_csv(args.raw_dir/name,sep='|',dtype=dtype)
            frame,audit = inspect_frame(df,month,columns,name)
            frames[view][month] = frame;audits[month] = audit
            quality.append({'view':view,**audit})
        if any(not audits[m]['primary_eligible'] for m in policy['baseline_months']):
            raise ValueError('중복이 있는 기준월은 기준 격자로 사용하지 않습니다.')
        baseline = make_baseline(frames[view],columns,policy['baseline_months'])
        for month in policy['months']:
            profile_rows.extend(matched_rows(frames[view][month],baseline,columns,month,view,audits[month]['use_status']))
    overlap = []
    for month in policy['months']:
        age = pd.read_csv(args.age_dir/f'flow_age_pop_{month}.csv',sep='|',dtype=dtype,usecols=KEYS)
        for code,region in REGIONS.items():
            sets = {'age':set(age.loc[age.BLOCK_CD.str.startswith(code)].itertuples(index=False,name=None))}
            sets.update({v:set(f[month].loc[f[month].region.eq(region)].index) for v,f in frames.items()})
            overlap.append({'month':month,'region':region,**{f'{k}_cells':len(s) for k,s in sets.items()},
                'age_time_common':len(sets['age']&sets['time']),
                'age_weekday_common':len(sets['age']&sets['wkdy']),
                'time_weekday_common':len(sets['time']&sets['wkdy']),
                'all_three_common':len(set.intersection(*sets.values()))})
    latest = {'version':policy['version'],'month':'202512','scope':'region_age_unstratified',
        'mode':'source_quality_context_only','changes_response':False,'cohorts':[],
        'assurance':'월·지역 단위의 출처 품질 확인입니다. 연령별 활동·개인 고립·위험 원인·지원 순위를 판정하지 않습니다.'}
    for region in REGIONS.values():
        december = [q for q in quality if q['month']=='202512']
        latest['cohorts'].append({'region':region,'age_scope':'not_stratified',
            'status':'source_review_required' if any(not q['primary_eligible'] for q in december) else 'primary_descriptive',
            'sources':december,'sensitivity_values_attached':False,
            'notes':['12월 시간대·요일별 원본에는 동일한 행이 중복되어 기본 해석에서 제외했습니다.',
                     '원본의 중복을 제거한 비교는 별도 민감도 분석이며 확인된 수정본이 아닙니다.',
                     '이 자료에는 연령 구분이 없습니다. 선택한 연령대의 시간대·요일별 활동으로 해석하지 않습니다.',
                     '세 통신 집계의 격자 범위가 다릅니다. 시간대·요일·연령을 곱해 결합하거나 독립 신호로 더하지 않습니다.']})
    summary = {'version':policy['version'],'source_files':12,'raw_rows':sum(q['raw_rows'] for q in quality),
        'extra_identical_rows':sum(q['identical_extra_rows'] for q in quality),
        'primary_excluded_files':[q['file'] for q in quality if not q['primary_eligible']],
        'profile_rows':len(profile_rows),'primary_profile_rows':sum(r['use_status']=='primary_descriptive' for r in profile_rows),
        'sensitivity_only_profile_rows':sum(r['use_status']=='deduplicated_sensitivity_only' for r in profile_rows),
        'current_scope':'retrospective marginal descriptions; not independent validation or isolation labels'}
    OUT.mkdir(exist_ok=True)
    pd.DataFrame(quality).to_csv(OUT/'source_quality.csv',index=False,encoding='utf-8-sig')
    pd.DataFrame(profile_rows).to_csv(OUT/'monthly_profiles.csv',index=False,encoding='utf-8-sig')
    pd.DataFrame(overlap).to_csv(OUT/'grid_overlap.csv',index=False,encoding='utf-8-sig')
    for name,value in [('latest.json',latest),('summary.json',summary)]:
        (OUT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    os.environ.setdefault('MPLCONFIGDIR',str(WORK/'mplconfig'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    font = Path('C:/Windows/Fonts/malgun.ttf')
    if font.exists():font_manager.fontManager.addfont(str(font));plt.rcParams['font.family']='Malgun Gothic'
    plt.rcParams['axes.unicode_minus']=False
    fig,axes = plt.subplots(2,2,figsize=(12,7))
    profiles = pd.DataFrame(profile_rows)
    for col,(view,columns) in enumerate(VIEWS.items()):
        for row,region in enumerate(REGIONS.values()):
            ax=axes[row,col]
            for month in ['202510','202511','202512']:
                r=profiles.loc[profiles.view.eq(view)&profiles.month.eq(month)&profiles.region.eq(region)].set_index('axis').loc[columns]
                ax.plot(range(len(columns)),r.change_pct,linestyle='--' if month=='202512' else '-',
                    label=month[-2:]+'월'+(' 중복 제거 민감도' if month=='202512' else ''))
            ax.axhline(0,color='grey',linewidth=.6);ax.set_title(region+' · '+('시간대' if view=='time' else '요일'))
            ax.set_ylabel('7~9월 같은 격자 대비 변화 (%)')
            if view=='time':ax.set_xticks([0,6,12,18,23])
            else:ax.set_xticks(range(7),['월','화','수','목','금','토','일'])
            ax.legend(fontsize=8)
    fig.suptitle('연령 구분 없는 월별 일평균 유동량 · 12월 중복 파일은 기본 해석 제외')
    fig.tight_layout();fig.savefig(OUT/'marginal_profiles.png',dpi=170);plt.close(fig)
    pinned = ['population_context_policy.json','population_input_manifest.json','population_layout_facts.json','input_manifest.json']
    manifest = {'version':policy['version'],'generated_at':datetime.now(timezone(timedelta(hours=9))).isoformat(),
        'script_sha256':sha(Path(__file__)),'input_sha256':{p:sha(HERE/p) for p in pinned},
        'raw_input_sha256':{r['name']:r['sha256'] for r in inputs},
        'age_raw_sha256':{r['name']:r['sha256'] for r in original if r['name'].startswith('flow_age_')},
        'output_sha256':{p.name:sha(p) for p in sorted(OUT.iterdir()) if p.is_file() and p.name!='manifest.json'}}
    (OUT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
