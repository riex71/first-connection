import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from .ingest import ReadableText
from .facts import validate_field_checks

ROOT = Path(__file__).resolve().parents[1]


def normalize(text):
    return re.sub(r"\s+", "", text)


def build_catalog(root=ROOT):
    """Only promote manually mapped facts when each evidence string exists in the snapshot."""
    manifest = {s["id"]: s for s in json.loads((root / "data/manifest.json").read_text())}
    seeds = json.loads((root / "data/services.seed.json").read_text())
    services, rejected = [], []
    for seed in seeds:
        source = manifest.get(seed["source_id"])
        if not source or source["status"] != "collected":
            rejected.append({"id": seed["id"], "reason": "원본 수집 실패"})
            continue
        raw_bytes = (root / source["raw_path"]).read_bytes()
        if hashlib.sha256(raw_bytes).hexdigest() != source["sha256"]:
            rejected.append({"id": seed["id"], "reason": "원본 해시 불일치"})
            continue
        parser = ReadableText()
        parser.feed(raw_bytes.decode(source.get("encoding", "utf-8"), "replace"))
        text = parser.text()
        missing = [e for e in seed["evidence"] if normalize(e) not in normalize(text)]
        if missing:
            rejected.append({"id": seed["id"], "reason": "원문 근거 변경 또는 누락", "missing": missing})
            continue
        try:
            validate_field_checks(seed, normalize(text))
        except ValueError as exc:
            rejected.append({"id": seed["id"], "reason": str(exc)})
            continue
        row = dict(seed)
        row["source"] = {k: source[k] for k in ["id", "url", "publisher", "collected_at", "sha256", "max_age_days", "raw_path"]}
        if "제4유형" in text:
            row["use_status"] = "research_only"
            row["source"]["license_note"] = "공공누리 제4유형: 출처표시·상업적 이용금지·변경금지. 추천 및 LLM 입력에서 제외."
        else:
            row["source"]["license_note"] = "공식 무료 공개 자료의 사실 항목을 매핑. 대회 제출용 이용조건 검토는 별도 필요."
        services.append(row)
    result = {"built_at": datetime.now(timezone.utc).isoformat(), "services": services, "rejected": rejected,
              "mapping_method": "수작업 항목 매핑 + 원문 근거 문자열 및 원본 SHA-256 검증. 자동 추출 정확도 주장 없음."}
    (root / "data/processed/catalog.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def load_catalog():
    return json.loads((ROOT / "data/processed/catalog.json").read_text())


if __name__ == "__main__":
    result = build_catalog()
    print(json.dumps({"services": len(result["services"]), "rejected": result["rejected"]}, ensure_ascii=False))
