"""Hand-authored constraint regression cases, not social impact or model accuracy."""
import json
from datetime import date
from .catalog import ROOT, load_catalog
from .engine import profile_from, search_resources


def keyword_region_baseline(profile, services):
    candidates=[]
    for service in services:
        in_region=service['region']==profile['region'] or (service['region']=='서울' and profile['region']=='강남구')
        hits=sum(tag in profile['goal'].split() for tag in service['tags'])
        if in_region and hits and service['use_status']!='research_only':
            candidates.append((-(hits*10+(5 if service['kind']=='program' else 0)),service['id']))
    return [id for _,id in sorted(candidates)[:3]]


def evaluate_cases(root=ROOT):
    services=load_catalog()['services']
    cases=json.loads((root/'data/examples/evaluation_cases_SYNTHETIC.json').read_text())
    outcomes=[]
    for case in cases:
        profile=profile_from(case['profile'])
        baseline=keyword_region_baseline(profile,services)
        rows,_=search_resources(profile,services,as_of=date(2026,10,24))
        guarded=[row['resource']['id'] for row in rows[:3]]
        forbidden=set(case['forbidden_ids'])
        outcomes.append({'id':case['id'],'description':case['description'],'baseline_ids':baseline,'guarded_ids':guarded,
            'baseline_violations':sorted(forbidden.intersection(baseline)),
            'guarded_violations':sorted(forbidden.intersection(guarded)),
            'expected_empty_satisfied':not guarded if case.get('expected_empty') else None})
    report={'as_of':'2026-10-24','data_kind':'synthetic','case_count':len(cases),
        'scope':'수작업으로 정한 제약 사례의 회귀 비교. 실제 사용자·고립 개선·LLM 성능의 측정이 아님.',
        'baseline':'목표 태그 일치 + 지역 범위 + 프로그램 우선. 나이·모집기간·예산·방식·세부 자격은 검사하지 않는 명시적 기준선.',
        'baseline_cases_with_violation':sum(bool(row['baseline_violations']) for row in outcomes),
        'guarded_cases_with_violation':sum(bool(row['guarded_violations']) for row in outcomes),
        'outcomes':outcomes}
    (root/'data/processed/evaluation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    lines=['# 합성 제약 사례 비교 — v0.2','',report['scope'],'',
        f"- 기준일: {report['as_of']}",f"- 사례 수: {len(cases)}",f"- 기준선에서 금지 후보가 포함된 사례: {report['baseline_cases_with_violation']}/{len(cases)}",f"- 조건 검증 후 금지 후보가 포함된 사례: {report['guarded_cases_with_violation']}/{len(cases)}",'',
        '모든 오류 유형을 대표하는 무작위 표본이 아니다. 개발자가 설계한 유한한 제약 사례이며 일반화 성능이나 실제 이용 전환율을 의미하지 않는다. 미확인 조건을 가진 자원의 자격이 실제 충족됐다고 평가하지 않는다.','',
        '기준선: '+report['baseline'],'','| 사례 | 기준선 위반 후보 수 | 조건 검증 후 위반 후보 수 |','|---|---:|---:|']
    lines += [f"| {r['description']} | {len(r['baseline_violations'])} | {len(r['guarded_violations'])} |" for r in outcomes]
    lines += ['', '재현: `python3 -m first_connection.evaluate_demo`. 상세 후보 ID는 `data/processed/evaluation.json`에서 확인한다.']
    (root/'docs/합성사례_비교.md').write_text('\n'.join(lines)+'\n')
    return report


if __name__=='__main__':
    r=evaluate_cases();print(json.dumps({k:v for k,v in r.items() if k!='outcomes'},ensure_ascii=False,indent=2))
