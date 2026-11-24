"""Experiment group assignment.

`assignments` is the legacy subject-level provisional view (last record per
subject wins) kept only for the seed fixture; it is not launch-grade. Launch
decisions use the org-level `AssignmentRegistry`: every enterprise org
inherits exactly one pre-registered arm derived from the frozen seed, and
reinstalls or account changes cannot move an org between arms.
"""
import hashlib


def assignments(events):
    result={}
    for event in events:
        result[event['subject_id']]=event['arm']
    return result


def derive_arm(seed, experiment, org_id):
    """Deterministic pre-registered arm for an org, reviewable from the frozen seed."""
    digest = hashlib.sha256(f'{seed}|{experiment}|{org_id}'.encode('utf-8')).hexdigest()
    return 'B' if int(digest, 16) % 2 else 'A'


class AssignmentRegistry:
    """Org-level single-inheritance assignment registry pinned to a frozen seed.

    The first valid pre-registered assignment of an org wins. Any later
    attempt for the same org — a reinstall, a new account, or a tampered arm —
    is rejected and recorded in `conflicts`, so one org can never land in
    both arms. Assignments after `frozen_at` or inconsistent with the seed
    are rejected the same way.
    """

    def __init__(self, plan):
        self.plan = plan
        self._arms = {}
        self._assigned_at = {}
        self._subjects = {}
        self._domains = {}
        self.conflicts = []

    def register(self, events):
        rows = sorted((e for e in events if e['kind'] == 'assignment'),
                      key=lambda e: (e['at'], e['id']))
        for event in rows:
            org = event['org_id']
            reasons = []
            if event['at'] > self.plan['frozen_at']:
                reasons.append('post_freeze_assignment')
            if event['arm'] != derive_arm(self.plan['seed'], self.plan['id'], org):
                reasons.append('seed_mismatch')
            if org in self._arms:
                if self._arms[org] != event['arm']:
                    reasons.append('inheritance_conflict')
                else:
                    reasons.append('duplicate_registration')
            if reasons:
                self.conflicts.append({'event': event['id'], 'org_id': org, 'reasons': reasons})
                continue
            self._arms[org] = event['arm']
            self._assigned_at[org] = event['at']
            self._domains[org] = event['domain']
            for subject in event['subjects']:
                self._subjects[subject] = org
        return self

    def arm_of(self, org_id):
        return self._arms.get(org_id)

    def assigned_at(self, org_id):
        return self._assigned_at.get(org_id)

    def org_of(self, subject_id):
        return self._subjects.get(subject_id)

    def subjects_of(self, org_id):
        return sorted(s for s, o in self._subjects.items() if o == org_id)

    def domain_of(self, org_id):
        return self._domains.get(org_id)

    def assigned_orgs(self, arm=None):
        return sorted(o for o, a in self._arms.items() if arm is None or a == arm)

    def domains(self):
        return dict(self._domains)
