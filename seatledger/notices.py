"""Create offline license compliance notices."""

def prepare(findings):
    return {row['notice_id']: {'tenant': row['tenant'], 'amount': row['amount']}
            for row in findings}
