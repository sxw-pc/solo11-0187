"""Provisional conversion count by experiment arm."""
from .experiments import assignments


def conversions(exposures, outcomes):
    arms=assignments(exposures)
    result={'A':0,'B':0}
    for outcome in outcomes:
        result[arms[outcome['subject_id']]]+=int(outcome['converted'])
    return result
