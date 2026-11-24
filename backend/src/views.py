from fastapi.encoders import jsonable_encoder
from sqlalchemy import select

from .models import EnergyReview, ProductionShift, SteamMeter, User
from .planning import get_plan, get_spikes, plan_outcome
from .shift_work import boundaries, get_readings, get_runs


def fields(row, names):
    return jsonable_encoder({name: getattr(row, name) for name in names.split()})


def user_view(user):
    return fields(user, 'id username display_name role')


def meter_view(row):
    return fields(row, 'id shift_id reading_at cumulative_kg source_ref recorded_by recorded_at')


def run_view(row):
    return fields(row, 'id shift_id asset starts_at ends_at logged_by updated_at')


def shift_view(db, shift, detail=False):
    readings = get_readings(db, shift.id)
    runs = get_runs(db, shift.id)
    slots = boundaries(shift)
    recorded = {row.reading_at for row in readings}
    result = fields(shift, 'id code line product batch_code starts_at ends_at ambient_c state sealed_at')
    result.update(reading_count=len(readings), run_count=len(runs), expected_count=len(slots),
                  missing=[slot.isoformat(timespec='minutes') for slot in slots if slot not in recorded])
    if detail:
        result.update(readings=[meter_view(row) for row in readings], runs=[run_view(row) for row in runs])
    return result


def plan_view(db, plan):
    shift = db.get(ProductionShift, plan.shift_id)
    result = fields(plan, 'id shift_id title revision state requested_at approved_at input_sha256 error attempts')
    review_id = db.scalar(select(EnergyReview.id).where(EnergyReview.plan_id == plan.id))
    outcome = plan_outcome(db, plan)
    if review_id is not None:
        locked = db.get(EnergyReview, review_id).baselines
        if locked:
            # Once signed off, the review sheet's snapshot wins for the baseline set,
            # while the model sections (features, metrics, sample) stay visible.
            outcome = {**(outcome or {}), **locked}
        else:
            # Sheets opened before baseline snapshots were stored keep the values
            # they were signed with; their result is not reclassified.
            outcome = plan.result
    result.update(shift=fields(shift, 'code line product starts_at ends_at'), evidence=bool(plan.evidence_file),
                  review_id=review_id, result=outcome)
    return result


def spike_view(db, spike):
    left = db.get(SteamMeter, spike.start_read_id)
    right = db.get(SteamMeter, spike.end_read_id)
    shift = db.get(ProductionShift, right.shift_id)
    verifier = db.get(User, spike.verified_by) if spike.verified_by else None
    result = fields(spike, 'id observed_kg baseline_kg sample_scope finding findings_note verified_at')
    result.update(shift=shift.code, start_read=meter_view(left), end_read=meter_view(right),
                  verified_by=verifier.display_name if verifier else None)
    return result


def review_view(db, review):
    plan = get_plan(db, review.plan_id)
    result = fields(review, 'id plan_id state opened_at closed_at conclusion follow_up')
    # Sheets opened before baseline snapshots existed fall back to the estimated
    # result they were signed with rather than being reclassified today.
    baselines = review.baselines if review.baselines is not None else plan.result
    result.update(plan=plan_view(db, plan),
                  spikes=[spike_view(db, row) for row in get_spikes(db, review.id)],
                  baselines=baselines)
    return result
