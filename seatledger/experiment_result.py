"""Org-level intent-to-treat analysis and versioned release decision.

`conversions` is the legacy provisional subject-level counter kept for the
seed fixture; it is not launch-grade. The launch decision below rests on
reviewable org-level random grouping, real exposure, valid conversion and a
pre-registered observation window:

- one org inherits exactly one pre-registered arm (see experiments.py);
- the intent-to-treat denominator includes orgs that never launched;
- a conversion counts only after a valid exposure and inside the window —
  pre-start or post-window events are never backfilled;
- seed, stop threshold and window are frozen before results are seen, so
  late回传 events only produce a new analysis version while earlier release
  recommendations stay readable;
- without authorized evidence that the exposure-time org identity matches
  the pre-registered grouping identity, the favorable reading is disallowed.
"""
import math
from .experiments import AssignmentRegistry, assignments
from .experiment_capture import CaptureLedger
from .store import digest

ARMS = ('A', 'B')
RECOMMENDATION_LABELS = {
    'expand_next_batch': '扩大到下一批',
    'pause_and_regroup': '暂停扩大发布并重做分组',
    'hold_identity_unconfirmed': '组织边界未确认，不得扩大',
    'hold_insufficient_evidence': '证据不足，继续观察',
}


def conversions(exposures, outcomes):
    arms=assignments(exposures)
    result={'A':0,'B':0}
    for outcome in outcomes:
        result[arms[outcome['subject_id']]]+=int(outcome['converted'])
    return result


def validate_plan(plan):
    """Check the pre-registered plan: seed, threshold and window frozen before start."""
    for field in ('id', 'seed', 'frozen_at', 'start', 'window_end', 'stop_threshold'):
        if field not in plan:
            raise ValueError(f'plan missing {field}')
    threshold = plan['stop_threshold']
    for field in ('min_orgs_per_arm', 'min_lift', 'alpha'):
        if field not in threshold:
            raise ValueError(f'stop_threshold missing {field}')
    if not (plan['frozen_at'] <= plan['start'] < plan['window_end']):
        raise ValueError('seed, threshold and window must be frozen before the experiment starts')
    return dict(plan)


def _resolve_identity(registry, identity_events):
    """Resolve org boundary questions with authorized identity evidence only."""
    by_domain = {}
    for org, domain in registry.domains().items():
        by_domain.setdefault(domain, set()).add(org)
    questions = {domain: sorted(orgs) for domain, orgs in by_domain.items() if len(orgs) > 1}
    merges = []
    confirmations = []
    for event in sorted(identity_events, key=lambda e: (e['at'], e['id'])):
        if not event.get('verified'):
            continue  # unverified self-claims cannot resolve a boundary question
        orgs = sorted({o for o in (registry.org_of(s) for s in event['subjects']) if o})
        if event['conclusion'] == 'same_org':
            into = event.get('org_id')
            if into not in registry.assigned_orgs():
                into = min(orgs, key=lambda o: (registry.assigned_at(o), o)) if orgs else None
            if into:
                merges.append({'into': into, 'absorbed': [o for o in orgs if o != into],
                               'evidence': event['id']})
        else:
            confirmations.append({'orgs': orgs, 'evidence': event['id']})
    unresolved = []
    for domain, orgs in questions.items():
        involved = set(orgs)
        merged = any(involved & ({m['into']} | set(m['absorbed'])) for m in merges)
        confirmed = any(involved <= set(c['orgs']) for c in confirmations)
        if merged == confirmed:
            unresolved.append(domain)  # no evidence, or contradictory evidence
    if unresolved:
        status = 'unconfirmed'
    elif merges:
        status = 'merged_same_org'
    else:
        status = 'confirmed_independent'
    return {'status': status, 'questions': questions, 'unresolved': sorted(unresolved),
            'merges': merges, 'confirmations': confirmations}


def _analysis_orgs(registry, identity):
    orgs = {}
    for org in registry.assigned_orgs():
        orgs[org] = {'org_id': org, 'arm': registry.arm_of(org), 'domain': registry.domain_of(org),
                     'subjects': registry.subjects_of(org), 'merged_from': []}
    for merge in identity['merges']:
        into = merge['into']
        if into not in orgs:
            continue
        for absorbed in merge['absorbed']:
            if absorbed not in orgs:
                continue
            orgs[into]['subjects'] = sorted(set(orgs[into]['subjects']) | set(orgs[absorbed]['subjects']))
            orgs[into]['merged_from'].append(absorbed)
            del orgs[absorbed]
    return orgs


def _evaluate_orgs(plan, orgs, exposures, conversions_):
    start, end = plan['start'], plan['window_end']
    exp_by_subject = {}
    for event in exposures:
        exp_by_subject.setdefault(event['subject_id'], []).append(event)
    conv_by_subject = {}
    for event in conversions_:
        conv_by_subject.setdefault(event['subject_id'], []).append(event)
    for org in orgs.values():
        valid_exposures = sorted(
            (e for s in org['subjects'] for e in exp_by_subject.get(s, []) if start <= e['at'] <= end),
            key=lambda e: (e['at'], e['id']))
        exposed_arms = sorted({e['arm'] for e in valid_exposures})
        first_exposure_at = valid_exposures[0]['at'] if valid_exposures else None
        converted = False
        invalid = []
        for subject in org['subjects']:
            for event in conv_by_subject.get(subject, []):
                if not (start <= event['at'] <= end):
                    invalid.append({'id': event['id'], 'reason': 'outside_window'})
                elif first_exposure_at is None:
                    invalid.append({'id': event['id'], 'reason': 'no_exposure'})
                elif event['at'] < first_exposure_at:
                    invalid.append({'id': event['id'], 'reason': 'before_exposure'})
                else:
                    converted = True
        contaminated = None
        if len(exposed_arms) > 1:
            contaminated = 'cross_arm_exposure'
        elif exposed_arms and exposed_arms[0] != org['arm']:
            contaminated = 'arm_mismatch'
        org.update({'exposed': bool(valid_exposures), 'exposed_arms': exposed_arms,
                    'first_exposure_at': first_exposure_at, 'converted': converted,
                    'invalid_conversions': invalid, 'contaminated': contaminated})
    return orgs


def _summary(orgs):
    summary = {arm: {'assigned': 0, 'exposed': 0, 'converted': 0} for arm in ARMS}
    for org in orgs.values():
        bucket = summary[org['arm']]
        bucket['assigned'] += 1
        bucket['exposed'] += int(org['exposed'])
        bucket['converted'] += int(org['converted'])
    return summary


def _significance(summary):
    n_a, n_b = (summary[arm]['assigned'] for arm in ARMS)
    c_a, c_b = (summary[arm]['converted'] for arm in ARMS)
    if not n_a or not n_b:
        return {'rate_A': None, 'rate_B': None, 'lift': None, 'z': None, 'p_value': None}
    rate_a, rate_b = c_a / n_a, c_b / n_b
    lift = rate_b - rate_a
    pooled = (c_a + c_b) / (n_a + n_b)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n_a + 1 / n_b))
    if se == 0:
        return {'rate_A': rate_a, 'rate_B': rate_b, 'lift': lift, 'z': 0.0, 'p_value': 1.0}
    z = lift / se
    return {'rate_A': rate_a, 'rate_B': rate_b, 'lift': lift, 'z': z,
            'p_value': math.erfc(abs(z) / math.sqrt(2))}


def _thresholds_met(plan, summary, significance):
    threshold = plan['stop_threshold']
    if significance['p_value'] is None:
        return False
    enough = all(summary[arm]['assigned'] >= threshold['min_orgs_per_arm'] for arm in ARMS)
    return (enough and significance['lift'] >= threshold['min_lift']
            and significance['p_value'] <= threshold['alpha'])


def _recommend(identity, orgs, thresholds_met):
    if identity['status'] == 'unconfirmed':
        return ('hold_identity_unconfirmed',
                ['org boundary not resolved by authorized evidence; the favorable reading is not allowed'])
    contaminated = sorted(org['org_id'] for org in orgs.values() if org['contaminated'])
    if contaminated:
        return ('pause_and_regroup',
                [f'cross-arm contamination in {contaminated}; this round cannot be compared'])
    if thresholds_met:
        return ('expand_next_batch', ['pre-registered thresholds met on the intent-to-treat population'])
    return ('hold_insufficient_evidence', ['pre-registered thresholds not met'])


def analyze(plan, events):
    """One immutable analysis over the given captured events."""
    plan = validate_plan(plan)
    registry = AssignmentRegistry(plan).register(events)
    identity = _resolve_identity(registry, [e for e in events if e['kind'] == 'identity'])
    orgs = _analysis_orgs(registry, identity)
    _evaluate_orgs(plan, orgs,
                   [e for e in events if e['kind'] == 'exposure'],
                   [e for e in events if e['kind'] == 'conversion'])
    summary = _summary(orgs)
    significance = _significance(summary)
    thresholds_met = _thresholds_met(plan, summary, significance)
    recommendation, reasons = _recommend(identity, orgs, thresholds_met)
    return {'experiment': plan['id'], 'plan_digest': digest(plan),
            'window': {'start': plan['start'], 'end': plan['window_end'], 'frozen_at': plan['frozen_at']},
            'identity': identity, 'registry_conflicts': registry.conflicts,
            'orgs': {org: orgs[org] for org in sorted(orgs)},
            'summary': summary, 'significance': significance,
            'thresholds': {'required': plan['stop_threshold'], 'met': thresholds_met},
            'recommendation': recommendation,
            'recommendation_label': RECOMMENDATION_LABELS[recommendation],
            'reasons': reasons}


class AnalysisLedger:
    """Versioned analyses over a plan frozen before results are seen.

    Every `analyze` call appends a new immutable version computed from the
    events captured so far. A late回传 event therefore only produces a new
    analysis version; earlier release recommendations stay on record and
    remain readable through `recommendation(version=...)` and `history()`.
    """

    def __init__(self, plan):
        self.plan = validate_plan(plan)
        self.capture = CaptureLedger(self.plan['id'])
        self._versions = []

    def ingest(self, events, captured_at):
        return self.capture.ingest(events, captured_at)

    def analyze(self):
        result = analyze(self.plan, self.capture.events())
        result['version'] = len(self._versions) + 1
        result['captured_events'] = self.capture.count()
        self._versions.append(result)
        return result

    def recommendation(self, version=None):
        if not self._versions:
            raise ValueError('no analysis version yet')
        return self._versions[-1 if version is None else version - 1]['recommendation']

    def version(self, number):
        return self._versions[number - 1]

    def history(self):
        return [{'version': v['version'], 'recommendation': v['recommendation'],
                 'captured_events': v['captured_events']} for v in self._versions]
