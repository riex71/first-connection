"""Read completed aggregate-analysis artifacts without importing analysis libraries."""
import hashlib
import json
from pathlib import Path
from .engine import read_regional_signals

ANALYSIS_ROOT = Path(__file__).resolve().parents[1] / 'analysis'


def load_latest_analysis(root=ANALYSIS_ROOT):
    if not (root / 'run_manifest.json').exists():
        return {'available': False, 'reason': '완료된 제공 데이터 분석 결과가 없습니다.'}
    try:
        run = json.loads((root / 'run_manifest.json').read_text(encoding='utf-8'))
        if run['inputs_verified'] is not True:
            raise ValueError('원본 검증 미완료')
        if hashlib.sha256((root / 'input_manifest.json').read_bytes()).hexdigest() != run['input_manifest_sha256']:
            raise ValueError('입력 기록 해시 불일치')
        if hashlib.sha256((root / 'run_analysis.py').read_bytes()).hexdigest() != run['code_sha256']:
            raise ValueError('분석 코드 변경 후 재실행 필요')
        for name in ['results/regional_signals.csv', 'results/summary.json']:
            if hashlib.sha256((root / name).read_bytes()).hexdigest() != run['output_sha256'][name]:
                raise ValueError('분석 출력 해시 불일치')
        csv_text = (root / 'results/regional_signals.csv').read_text(encoding='utf-8-sig')
        rows = read_regional_signals(csv_text)
        if not rows or any(row['data_kind'] != 'analysis' for row in rows):
            raise ValueError('실제 분석 구분값 확인 실패')
        summary = json.loads((root / 'results/summary.json').read_text(encoding='utf-8'))
        return {'available': True, 'csv': csv_text, 'signals': rows,
                'version': run['version'], 'generated_at': run['generated_at'],
                'input_files': summary['input_files'], 'population_rows': summary['population_rows'],
                'card_rows': summary['card_rows'],
                'interpretation': summary['interpretation'],
                'assurance': '원본 해시를 확인한 로컬 분석 산출물. 개인의 고립 여부·위험도·원인을 검증한 자료가 아님.'}
    except (OSError, ValueError, KeyError, TypeError):
        return {'available': False, 'reason': '분석 결과의 완료 기록·해시를 확인하지 못했습니다. 분석 재실행이 필요합니다.'}
