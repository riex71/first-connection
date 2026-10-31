"""Causal replay of aggregate observation episodes, not welfare case status."""
import math
from datetime import date


def replay_signal(values, weekdays, dates, start, q, freeze_on_candidate,
                  open_days=7, recovery_days=7, end=None, max_reference_days=None,
                  audit_closure=False, anchor_max_age_days=28):
    if (len(values) != len(weekdays) or len(values) != len(dates) or type(start) is not int or start < 28
            or not 0 <= q < 1 or type(open_days) is not int or type(recovery_days) is not int
            or open_days < 1 or recovery_days < 1):
        raise ValueError('사건 재생 입력·기간·보정 기준 확인 필요')
    if any(not math.isfinite(float(v)) or v < 0 for v in values):
        raise ValueError('유효한 거래 관측 필요')
    end = len(values) if end is None else end
    if type(end) is not int or not start <= end <= len(values):
        raise ValueError('사건 재생 범위 확인 필요')
    if max_reference_days is not None and (type(max_reference_days) is not int or max_reference_days < open_days):
        raise ValueError('기준선 재검토 상한은 사건 확인 일수 이상이어야 합니다.')
    if type(audit_closure) is not bool or type(anchor_max_age_days) is not int or anchor_max_age_days < 1:
        raise ValueError('종료 근거 확인 설정을 확인하세요.')
    records, events = [], []
    state, candidate, event = 'monitoring', None, None
    frozen, reference_end, candidate_index = None, None, None
    anchor = None
    lows = recoveries = 0
    for i in range(start, end):
        if any(day not in weekdays[i-28:i] for day in range(7)):
            raise ValueError('직전 관측의 요일 범위 확인 필요')
        if (state == 'open' and freeze_on_candidate and max_reference_days is not None
                and i-candidate_index >= max_reference_days):
            state, frozen, reference_end, lows, recoveries = 'reference_review', None, None, 0, 0
            event.update({'status': 'reference_review_required', 'reference_review_required_at': dates[i]})
        means = {day: sum(values[j] for j in range(i-28, i) if weekdays[j] == day) /
                 sum(weekdays[j] == day for j in range(i-28, i)) for day in range(7)}
        use_frozen = freeze_on_candidate and frozen is not None
        estimate = (frozen if use_frozen else means)[weekdays[i]]
        reference = reference_end if use_frozen else dates[i-1]
        lower = estimate * (1-q)
        below = values[i] < lower
        if state == 'monitoring' and below:
            state, candidate, lows = 'candidate', dates[i], 1
            candidate_index = i
            anchor = means.copy()
            if freeze_on_candidate:
                frozen, reference_end = means.copy(), dates[i-1]
        elif state == 'candidate':
            if below:
                lows += 1
            else:
                state, candidate, frozen, reference_end, lows = 'monitoring', None, None, None, 0
                anchor = None
        elif state == 'open':
            lows = lows + 1 if below else 0
            recoveries = 0 if below else recoveries + 1
            if recoveries >= recovery_days:
                if audit_closure:
                    positions = range(i-recovery_days+1, i+1)
                    ratios = [values[j]/anchor[weekdays[j]] if anchor[weekdays[j]] > 0 else None for j in positions]
                    below_anchor = sum(values[j] < anchor[weekdays[j]]*(1-q) for j in positions)
                    age_days = (date.fromisoformat(dates[i])-date.fromisoformat(event['anchor_end'])).days
                    reason = ('anchor_unavailable' if any(r is None for r in ratios) else
                              'anchor_stale' if age_days > anchor_max_age_days else
                              'below_anchor_lower' if below_anchor else None)
                    event.update({'rolling_rule_recovered_at': dates[i],
                        'closure_audit': {'anchor_end': event['anchor_end'], 'anchor_age_days': age_days,
                            'observed_days': recovery_days, 'below_anchor_days': int(below_anchor),
                            'min_ratio_to_anchor': min(ratios) if all(r is not None for r in ratios) else None,
                            'threshold_ratio': 1-q, 'reason': reason}})
                    if reason:
                        event.update({'status': 'closure_review_required', 'closure_review_required_at': dates[i]})
                    else:
                        event.update({'closed_at': dates[i], 'closure': 'dual_reference_rule', 'status': 'closed_by_observation'})
                else:
                    event.update({'closed_at': dates[i], 'closure': 'observed_recovery_rule', 'status': 'closed_by_observation'})
                state, candidate, frozen, reference_end = 'monitoring', None, None, None
                event, anchor, lows, recoveries = None, None, 0, 0
        if state == 'candidate' and lows >= open_days:
            state, recoveries = 'open', 0
            event = {'id': f'event-{len(events)+1}', 'start': candidate, 'confirmed_at': dates[i],
                     'closed_at': None, 'closure': None,
                     'status': 'open', 'reference_review_required_at': None,
                     'anchor_end': dates[candidate_index-1], 'anchor_weekday_means': anchor.copy(),
                     'rolling_rule_recovered_at': None, 'closure_audit': None, 'closure_review_required_at': None,
                     'reference_end': reference_end if freeze_on_candidate else dates[i-1],
                     'reference_mode': 'frozen_on_candidate' if freeze_on_candidate else 'rolling',
                     'q': float(q)}
            events.append(event)
        records.append({'date': dates[i], 'actual': float(values[i]), 'predicted': float(estimate),
                        'lower_bound': float(lower), 'below_lower': bool(below),
                        'state_after': state, 'reference_frozen': bool(use_frozen), 'reference_end': reference,
                        'consecutive_low_days': lows if state in {'candidate', 'open'} else 0,
                        'recovery_days': recoveries, 'open_event_id': event['id'] if event and state == 'open' else None,
                        'review_event_id': event['id'] if event and state == 'reference_review' else None})
    return {'daily': records, 'events': events, 'state': state,
            'candidate_start': candidate if state == 'candidate' else None,
            'current_event': dict(event) if event else None,
            'pending_closure_reviews': [e for e in events if e['status'] == 'closure_review_required']}
