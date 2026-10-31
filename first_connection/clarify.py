"""Propose bounded phrase constraints and explicit support preferences for confirmation."""
import copy
import hashlib
import re
from .engine import profile_from
from .support import PREFERENCES


def budget_request(text):
    # Explicit upper bounds only. Keep negation local to its clause and abstain
    # on conflicting amounts; this remains a narrow rule, not general NLU.
    clauses = re.split(r'(?<!\d)\.|\.(?!\d)|[!?\n;]|하지만|지만|반면|그리고', text)
    amount_pattern = re.compile(
        r'(?<![\d.,])(?:(?P<man>\d[\d,]*)\s*만\s*(?P<cheon>[0-9])\s*천\s*원'
        r'|(?P<amount>\d[\d,]*)\s*(?P<unit>만\s*원|원))\s*(?:이하|까지|이내)')
    amounts = []
    for clause in clauses:
        for match in amount_pattern.finditer(clause):
            remainder = clause[match.end():match.end()+25]
            if re.search(r'(?:필요|필수).{0,8}없|(?:원하지|바라지).{0,6}않|^\s*(?:가|는|은)?\s*아니', remainder):
                continue
            value = (int(match['man'].replace(',', '')) * 10000 + int(match['cheon']) * 1000
                     if match['man'] is not None else
                     int(match['amount'].replace(',', '')) * (10000 if '만' in match['unit'] else 1))
            if 0 <= value <= 1000000:
                amounts.append((value, match.group()))
    if amounts:
        return amounts[0] if len({a[0] for a in amounts}) == 1 else None
    for clause in clauses:
        match = re.search(r'무료|돈.{0,6}(?:없|어려)|비용.{0,6}부담', clause)
        negated = re.search(
            r'무료.{0,12}(?:아니|아닌|않아도)|무료.{0,15}(?:필요|필수).{0,8}없'
            r'|유료.{0,8}(?:괜찮|가능)|비용.{0,12}(?:않|없|아니)'
            r'|돈.{0,15}(?:건|게|것은|것이).{0,4}아니', clause)
        if match and not negated:
            return 0, match.group()
    return None


def propose_constraints(profile):
    text = profile["request"]
    suggestions = []

    def add(field, value, question, evidence):
        key = hashlib.sha256(f"{field}:{value}:{evidence}".encode()).hexdigest()[:12]
        suggestions.append({"id": key, "field": field, "value": value, "question": question, "evidence": evidence})

    groups = {"phone": r"전화|통화", "visit": r"방문|대면|외출"}
    channel_tokens = re.compile(r"전화|통화|방문|대면|외출")
    difficulty = re.compile(r"부담|어렵|어려|싫|불편|못\s*(?:하|해|받)|피하고|안\s*(?:하고|받고)|하고\s*싶지|말고(?!도)")
    negation = re.compile(r"(?:부담|불편|어렵|어려|싫).{0,16}(?:없|않|아니)|괜찮|문제\s*없")
    for channel, pattern in groups.items():
        if channel in profile["avoid_channels"]:
            continue
        for occurrence in re.finditer(pattern, text):
            tail = text[occurrence.start():occurrence.end()+35]
            tail = re.split(r"[.!?\n,;]|지만|반면|그리고", tail)[0]
            other = channel_tokens.search(tail, occurrence.end()-occurrence.start())
            if other:
                tail = tail[:other.start()]
            if difficulty.search(tail) and not negation.search(tail):
                label = "전화" if channel == "phone" else "방문·대면"
                add("avoid_channels", channel, f"{label}를 피하는 조건으로 찾아볼까요?", tail.strip())
                break

    budget = budget_request(text)
    if budget and budget[0] != profile['budget_krw']:
        value, evidence = budget
        question = (f'이번 프로그램의 예산을 {value:,}원 이하로 둘까요?' if value else
                    '지출 예산을 0원으로 둘까요? 비용이 미공개인 자원은 확인 필요로 구분해요.')
        add('budget_krw', value, question, evidence)
    preference = profile.get('support_check', {}).get('preferred_next_step')
    preferred = PREFERENCES.get(preference)
    if preferred and preferred['goal'] and preferred['goal'] != profile['goal']:
        evidence = f"지원 필요 확인: {preferred['label']} · 현재 선택 목표: {profile['goal']}"
        label = '관계 만들기·취미 활동' if preference == 'social_activity' else '안부 확인·일상 지원'
        add('goal', preferred['goal'], f'선택한 희망에 맞춰 지원 목표를 ‘{label}’으로 바꿔 찾아볼까요?', evidence)
    return suggestions[:4]


def apply_choices(profile, choices):
    if choices is None:
        return profile, []
    if not isinstance(choices, list) or len(choices) > 4:
        raise ValueError("조건 확인 응답은 최대 4개입니다.")
    suggestions = {row["id"]: row for row in propose_constraints(profile)}
    updated, record, seen = copy.deepcopy(profile), [], set()
    for choice in choices:
        if not isinstance(choice, dict) or choice.get("id") not in suggestions or type(choice.get("accepted")) is not bool:
            raise ValueError("현재 입력에서 제안한 조건에 응답해 주세요.")
        if choice["id"] in seen:
            raise ValueError("같은 조건에 중복 응답할 수 없습니다.")
        seen.add(choice["id"])
        suggestion = suggestions[choice["id"]]
        if choice["accepted"]:
            if suggestion["field"] == "avoid_channels":
                updated["avoid_channels"] = sorted(set(updated["avoid_channels"] + [suggestion["value"]]))
            else:
                updated[suggestion["field"]] = suggestion["value"]
        record.append({**suggestion, "accepted": choice["accepted"]})
    return profile_from(updated), record
