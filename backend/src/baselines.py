"""Hourly baseline assembly for trial-production equipment combinations.

The regressor only has evidence for equipment combinations that already ran in
the same line's sealed history.  Hours whose combination is unfamiliar stay
blank until a supervisor confirms a baseline by hand.  Every reader (plan page,
approval gate, review sheet) goes through :func:`finalize`, so they agree on
which hours are fitted, manually confirmed or still pending.
"""
from copy import deepcopy

PENDING = 'pending'
FITTED = 'fitted'
MANUAL = 'manual'


def combination(row):
    """Which of the three metered assets ran during this hour."""
    return ''.join('1' if value > 0 else '0' for value in row['features'][:3])


def seen_combinations(history):
    return {combination(row) for row in history}


def normalize(result, snapshot):
    """Backfill per-hour baseline fields on results written before this rule.

    Older results identify hours by start time only.  The plan's input snapshot
    still carries the target rows, so familiarity can be reclassified without
    re-running the regression.
    """
    if result is None:
        return None
    profile = result.get('profile') or []
    if all('end_read_id' in row and 'unseen' in row for row in profile):
        return result
    target = {row['start']: row for row in (snapshot or {}).get('target', [])}
    familiar = seen_combinations((snapshot or {}).get('history', []))
    upgraded = []
    for row in profile:
        source = target.get(row.get('start'))
        unseen = source is not None and combination(source) not in familiar
        upgraded.append({**row, 'shift': source['shift'] if source else row.get('shift'),
                         'start_read_id': source['start_read_id'] if source else row.get('start_read_id'),
                         'end_read_id': source['end_read_id'] if source else None,
                         'unseen': unseen,
                         'model_kg': row.get('baseline_kg') if unseen else None,
                         'baseline_kg': None if unseen else row.get('baseline_kg'),
                         'flagged': False if unseen else row.get('flagged', False)})
    return {**result, 'profile': upgraded}


def finalize(result, manual):
    """Merge supervisor-confirmed baselines and recompute the whole-shift outcome.

    ``manual`` maps ``end_read_id`` to ``{'baseline_kg': number, 'note': str}``.
    The stored result is never mutated; the returned copy is what the plan page
    shows and, at sign-off, what gets locked onto the review sheet.  Results
    still lacking per-hour fields are passed through as-is.
    """
    profile = result.get('profile') or []
    if not all('end_read_id' in row and 'unseen' in row for row in profile):
        return deepcopy(result)

    outcome = deepcopy(result)
    threshold = float((result.get('metrics') or {}).get('spike_threshold_kg') or 0)
    spikes = [spike for spike in result.get('spikes', []) if spike.get('scope') == 'history']
    rows = []
    for row in profile:
        entry = manual.get(row['end_read_id']) if row['unseen'] else None
        if entry is not None:
            baseline, source, note = round(float(entry['baseline_kg']), 2), MANUAL, entry.get('note')
            model_kg = row.get('model_kg')
        elif row['unseen']:
            baseline, source, note, model_kg = None, PENDING, None, row.get('model_kg')
        else:
            baseline, source, note, model_kg = row['baseline_kg'], FITTED, None, None
        observed = float(row['observed_kg'])
        flagged = baseline is not None and observed - baseline >= threshold
        rows.append({'shift': row['shift'], 'start': row['start'],
                     'start_read_id': row['start_read_id'], 'end_read_id': row['end_read_id'],
                     'observed_kg': observed, 'baseline_kg': baseline, 'source': source,
                     'unseen': bool(row['unseen']), 'note': note, 'model_kg': model_kg,
                     'flagged': flagged})
        if flagged:
            spikes.append({'scope': 'target', 'shift': row['shift'], 'start': row['start'],
                           'start_read_id': row['start_read_id'], 'end_read_id': row['end_read_id'],
                           'observed_kg': observed, 'baseline_kg': baseline})

    pending = [row for row in rows if row['source'] == PENDING]
    observed_total = round(sum(row['observed_kg'] for row in rows), 2)
    baseline_total = None if pending else round(sum(row['baseline_kg'] for row in rows), 2)
    outcome.update(
        profile=rows, spikes=spikes, observed_kg=observed_total, baseline_kg=baseline_total,
        excess_kg=None if baseline_total is None else round(observed_total - baseline_total, 2),
        pending_count=len(pending), pending_hours=[row['start'] for row in pending],
    )
    return outcome
