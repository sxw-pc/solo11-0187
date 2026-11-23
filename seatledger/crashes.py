"""Offline application crash report normalization."""

def collect(reports):
    seen = set()
    result = []
    for report in reports:
        if report['report_id'] in seen:
            continue
        seen.add(report['report_id'])
        result.append(report)
    return result
