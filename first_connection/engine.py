"""Deterministic eligibility checks shared by the rule and LLM orchestrators."""
import copy
import csv
import io
import json
from datetime import date, datetime, timedelta, timezone
from .catalog import load_catalog
from .support import normalize_support, build_support_context

KST = timezone(timedelta(hours=9))
CHANNELS = {"phone": "전화", "visit": "방문·대면", "web": "온라인", "chat": "채팅", "home": "가정 지원", "remote": "비대면", "accompanied": "동행"}
REGIONS = {"강남구", "춘천시"}


def today_kst():
    return datetime.now(KST).date()


def profile_from(data):
    if not isinstance(data, dict):
        raise ValueError("입력은 객체여야 합니다.")
    if data.get("region") not in REGIONS:
        raise ValueError("첫 버전의 지원 지역은 강남구·춘천시입니다.")
    age = data.get("age")
    if age is not None and (type(age) is not int or not 18 <= age <= 110):
        raise ValueError("나이는 18~110 사이 정수 또는 미응답이어야 합니다. 첫 버전은 성인 대상입니다.")
    for key in ["single", "android", "benefit_eligible", "center_member", "care_need", "no_caregiver"]:
        if data.get(key) is not None and type(data[key]) is not bool:
            raise ValueError(f"{key}: 예·아니오·미응답 중 선택하세요.")
    avoided = data.get("avoid_channels", [])
    if not isinstance(avoided, list) or any(x not in CHANNELS for x in avoided):
        raise ValueError("지원하지 않는 연락·참여 방식입니다.")
    budget = data.get("budget_krw")
    if budget is not None and (type(budget) is not int or not 0 <= budget <= 1000000):
        raise ValueError("예산은 0~1,000,000원 정수 또는 미응답이어야 합니다.")
    text = data.get("request", "")
    if not isinstance(text, str) or len(text) > 2000:
        raise ValueError("요청은 2,000자 이내 문자열이어야 합니다.")
    goal = data.get("goal", "관계 취미 일상")
    if goal not in {"관계 취미 문화", "안부 일상 생활", "동행 병원 생활", "관계 취미 일상"}:
        raise ValueError("지원 목표를 선택하세요.")
    return {"region": data["region"], "age": age, "single": data.get("single"),
            "android": data.get("android"), "benefit_eligible": data.get("benefit_eligible"), "center_member": data.get("center_member"),
            "care_need": data.get("care_need"), "no_caregiver": data.get("no_caregiver"),
            "budget_krw": budget, "avoid_channels": sorted(set(avoided)), "request": text, "goal": goal,
            "support_check": normalize_support(data.get('support_check'))}


def apply_feedback(profile, feedback):
    updated = copy.deepcopy(profile)
    if not feedback:
        return updated, []
    if isinstance(feedback, list):
        if len(feedback) > 20:
            raise ValueError("대안 탐색 기록은 최대 20건입니다.")
        excluded = []
        for entry in feedback:
            updated, ids = apply_feedback(updated, entry)
            excluded.extend(ids)
        return updated, list(dict.fromkeys(excluded))
    if not isinstance(feedback, dict) or feedback.get("reason") not in {"phone", "visit", "closed", "cost", "other"}:
        raise ValueError("대안 탐색 사유를 선택하세요.")
    failed = feedback.get("resource_id")
    if not isinstance(failed, str) or len(failed) > 100:
        raise ValueError("대상 서비스 ID가 필요합니다.")
    reason = feedback["reason"]
    if reason in {"phone", "visit"}:
        updated["avoid_channels"] = sorted(set(updated["avoid_channels"] + [reason]))
    if reason == "cost":
        updated["budget_krw"] = 0
    return updated, [failed]


def evaluate(service, profile, as_of):
    blocked, unknown, matched = [], [], []
    prerequisite_checks = []
    source = service["source"]
    if service["use_status"] == "research_only":
        blocked.append("이용조건 검토 중인 참고자료: 추천에서 제외")
    if service["region"] != profile["region"] and not (service["region"] == "서울" and profile["region"] == "강남구"):
        blocked.append("지원 지역 불일치")
    else:
        if service.get("region_is_eligibility", True):
            matched.append("공고의 지원 지역과 선택 지역 일치")
        else:
            matched.append("선택 지역에서 안내하는 프로그램")
            unknown.append("프로그램별 거주지 자격은 별도 확인 필요")
    collected = datetime.fromisoformat(source["collected_at"]).astimezone(KST).date()
    if collected > as_of:
        blocked.append("수집 시점 이전의 과거 재생에는 사용할 수 없는 자료")
    elif (as_of - collected).days > source["max_age_days"]:
        blocked.append("출처 갱신 주기 초과: 다시 수집해야 함")
    if service["application_end"] and date.fromisoformat(service["application_end"]) < as_of:
        blocked.append("모집 기간 종료")
    elif service["application_start"] and date.fromisoformat(service["application_start"]) > as_of:
        blocked.append("모집 시작 전")
    else:
        unknown.append("현재 접수·정원은 기관 확인 필요")
    age = profile["age"]
    if service["age_min"] is not None or service["age_max"] is not None:
        if age is None:
            unknown.append("연령 조건 확인 필요")
        elif ((service["age_min"] is not None and age < service["age_min"])
              or (service["age_max"] is not None and age > service["age_max"])):
            blocked.append("연령 조건 불일치")
        else:
            matched.append("공고의 연령 조건 충족")
    if service["single_required"]:
        if profile["single"] is False:
            blocked.append("1인가구 조건 불일치")
        elif profile["single"] is None:
            unknown.append("1인가구 여부 확인 필요")
        else:
            matched.append("1인가구 조건 확인")
    for fact, required in service["required_facts"].items():
        label = {"android": "안드로이드 기기", "benefit_eligible": "수급·차상위·기초연금 요건",
                 "center_member": "센터 멤버십", "care_need": "돌봄 필요성", "no_caregiver": "돌봄자 부재"}.get(fact, fact)
        response = profile.get(fact)
        state = "unanswered" if response is None else ("reported_match" if response == required else "reported_mismatch")
        institutional = fact in {"care_need", "no_caregiver"}
        prerequisite_checks.append({"field": fact, "label": label, "required": required, "response": response,
                                    "state": state, "basis": "self_report", "institution_check_required": institutional})
        if response is None:
            unknown.append(label + " 확인 필요")
        elif response != required:
            blocked.append(label + " 불일치")
        elif institutional:
            unknown.append(label + ": 본인 응답은 예이며, 증빙·선정은 기관 확인 필요")
    avoided = set(profile["avoid_channels"])
    if "visit" in avoided:
        avoided.update({"home", "accompanied"})
    allowed_application = [x for x in service["application_channels"] if x not in avoided]
    allowed_participation = [x for x in service["participation_channels"] if x not in avoided]
    if not allowed_application:
        blocked.append("원하지 않는 신청 방식만 확인됨")
    if not allowed_participation:
        blocked.append("원하지 않는 참여 방식만 확인됨")
    cost = service["cost_krw"]
    if cost is None:
        unknown.append("비용 미공개: 무료로 단정할 수 없음")
    elif profile["budget_krw"] is not None and cost > profile["budget_krw"]:
        blocked.append("예산 초과")
    if not service["hours"]:
        unknown.append("이용 가능 시간 확인 필요")
    keywords = profile["goal"].split()
    hits = [tag for tag in service["tags"] if tag in keywords]
    if not hits:
        blocked.append("선택한 지원 목표와 직접 일치하는 항목 없음")
    else:
        matched.append("지원 목표: " + ", ".join(hits))
    return {"resource": service, "blocked": blocked, "unknown": list(dict.fromkeys(unknown + service["conditions"])),
            "matched": matched, "score": len(hits) * 10 + (5 if service["kind"] == "program" else 0),
            "prerequisite_checks": prerequisite_checks,
            "application_channels": allowed_application, "participation_channels": allowed_participation}


def search_resources(profile, services, as_of=None, exclude=None, query=""):
    as_of = as_of or today_kst()
    candidates, excluded = [], []
    exclude = set(exclude or [])
    for service in services:
        row = evaluate(service, profile, as_of)
        if service["id"] in exclude:
            row["blocked"].append("이번 이용자의 대안 탐색에서 제외한 서비스")
        if row["blocked"]:
            excluded.append({"id": service["id"], "name": service["name"], "reasons": row["blocked"]})
        else:
            row["score"] += sum(1 for term in query.split()[:12] if term in service["name"] + " ".join(service["tags"]))
            candidates.append(row)
    candidates.sort(key=lambda row: (-row["score"], len(row["unknown"]), row["resource"]["id"]))
    return candidates, excluded


def make_plan(profile, candidates, excluded, trace=None, mode="rules"):
    support_context = build_support_context(profile)
    information_only = support_context['answers']['preferred_next_step'] == 'information_only'
    trace = list(trace or [])
    if support_context['status'] == 'self_report_only':
        trace.insert(0, {'tool': 'read_support_check', 'result': {
            'answers': support_context['answers'], 'basis': 'self_report',
            'risk_assessment': 'not_assessed', 'information_only': information_only}})
    cards = []
    for row in candidates[:3]:
        service = row["resource"]
        cards.append({"id": service["id"], "name": service["name"], "summary": service["summary"],
                      "status": "조건 확인 후 이용 검토", "target": service["target_text"], "matched": row["matched"],
                      "needs_confirmation": row["unknown"], "source": service["source"], "evidence": service["evidence"],
                      "prerequisite_checks": row["prerequisite_checks"],
                      "application_channels": row["application_channels"], "participation_channels": row["participation_channels"],
                      "application": service["application_text"], "action_url": service["action_url"], "hours": service["hours"],
                      "application_period": {"start": service["application_start"], "end": service["application_end"]},
                      "participation_period": {"start": service.get("participation_start"), "end": service.get("participation_end")},
                      "cost_krw": service["cost_krw"], "cost_note": service.get("cost_note", "비용 미공개"),
                      "steps": ["공식 안내에서 대상 조건과 현재 모집 상태 확인", service["application_text"],
                                "비용·시간·참여 부담을 확인한 뒤 가능한 첫 행동 선택"],
                      "inquiry_draft": f"안녕하세요. {service['name']} 이용을 알아보고 있습니다. 현재 접수 가능한지, 대상 조건·비용·일정과 신청에 필요한 준비를 알려주세요."})
        if information_only:
            cards[-1].update({'preparation_scope': 'information_only', 'action_url': None, 'inquiry_draft': '',
                             'steps': ['공식 안내에서 대상 조건·비용·현재 모집 상태 살펴보기',
                                       '궁금한 조건을 기록하고, 문의·신청 준비는 원할 때 선택하기']})
    questions = []
    if profile["age"] is None:
        questions.append("연령 조건이 있는 서비스를 확인할 수 있도록 나이를 입력해 주세요.")
    if profile["single"] is None:
        questions.append("1인가구인지 알려주면 해당 조건을 확인할 수 있어요.")
    if not cards:
        questions.append("현재 조건에 맞는 확인 가능한 자원이 없습니다. 원하는 방식을 유지하며 수집 범위를 넓히거나, 바꿀 수 있는 조건을 선택해 주세요.")
    return {"mode": mode, "generated_at": datetime.now(KST).isoformat(), "as_of": today_kst().isoformat(),
            "profile": profile, "support_context": support_context,
            "recommendations": cards, "excluded": excluded, "questions": questions,
            "trace": trace or [], "limits": ["서비스 신청·기관 연락·후속 알림은 실행하지 않습니다.",
                "모집·정원·자격이 확정된 추천이 아닙니다. 공개 자료에 없는 조건은 기관 확인이 필요합니다.",
                "지역 집계 지표를 개인의 고립 진단이나 복지 수급 판정에 사용하지 않습니다."]}


def run_rules(profile, feedback=None, services=None):
    profile, exclude = apply_feedback(profile, feedback)
    services = services if services is not None else load_catalog()["services"]
    candidates, excluded = search_resources(profile, services, exclude=exclude)
    trace = [{"tool": "identify_constraints", "result": {"region": profile["region"], "avoid_channels": profile["avoid_channels"]}},
             {"tool": "search_resources", "result": {"candidate_count": len(candidates)}},
             {"tool": "verify_conditions", "result": {"excluded_count": len(excluded), "unknowns_preserved": True}}]
    if feedback:
        trace.insert(0, {"tool": "replan", "result": {"feedback": feedback, "excluded_ids": exclude}})
    trace.append({"tool": "create_action_plan", "result": {"resource_ids": [r["resource"]["id"] for r in candidates[:3]]}})
    return make_plan(profile, candidates, excluded, trace)


def read_regional_signals(csv_text):
    required = {"region", "age_min", "age_max", "period_start", "period_end", "signal_label", "evidence", "model_version", "data_kind"}
    reader = csv.DictReader(io.StringIO(csv_text.lstrip('\ufeff')))
    if not required.issubset(reader.fieldnames or []):
        raise ValueError("분석 결과 CSV의 필수 열이 누락됐습니다.")
    rows = []
    for row in reader:
        if len(rows) >= 500:
            raise ValueError("최대 500행까지 읽습니다.")
        if row["region"] not in REGIONS or row["data_kind"] not in {"synthetic", "analysis"}:
            raise ValueError("지역 또는 data_kind를 확인하세요.")
        age_min, age_max = int(row["age_min"]), int(row["age_max"])
        if not 0 <= age_min <= age_max <= 120:
            raise ValueError("분석 연령 범위를 확인하세요.")
        if date.fromisoformat(row["period_start"]) > date.fromisoformat(row["period_end"]):
            raise ValueError("분석 기간 시작이 종료보다 늦습니다.")
        for field in ["signal_label", "evidence", "model_version"]:
            if not isinstance(row[field], str) or not row[field].strip() or len(row[field]) > 2000:
                raise ValueError("분석 근거·모델 버전은 비어 있지 않은 2,000자 이내여야 합니다.")
        for field in ["risk_level", "cause_summary", "priority_basis", "dataset_refs"]:
            value = row.get(field) or ""
            if len(value) > 2000:
                raise ValueError("선택 분석 필드는 2,000자 이내여야 합니다.")
            row[field] = value.strip()
        priority = row.get("priority_rank") or ""
        if priority:
            try:
                priority = int(priority)
            except ValueError:
                raise ValueError("지원 우선순위는 양의 정수 또는 빈 값이어야 합니다.") from None
            if not 1 <= priority <= 1000000 or not row["priority_basis"]:
                raise ValueError("지원 우선순위는 양의 정수와 분석팀의 근거가 함께 있어야 합니다.")
        row["priority_rank"] = priority or None
        rows.append({**row, "age_min": age_min, "age_max": age_max})
    return rows
