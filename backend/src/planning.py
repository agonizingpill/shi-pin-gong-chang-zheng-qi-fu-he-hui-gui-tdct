from redis.exceptions import RedisError
from sqlalchemy import select

from .auth import DomainError
from .baselines import finalize, normalize
from .config import QUEUE, redis, utc_now
from .constants import PlanState, ReviewState, ShiftState
from .models import EnergyReview, LoadPlan, PlanBaseline, ProductionShift, ReviewSpike
from .shift_work import audit, capture_rows, get_shift
from .steam_model import fingerprint


def queue_hint(plan_id):
    try:
        redis.rpush(QUEUE, str(plan_id))
    except RedisError:
        # Worker also polls committed SQL rows when delivery is unavailable.
        return


def request_plan(db, shift_id, title, user):
    shift = get_shift(db, shift_id, lock=True)
    target = capture_rows(db, shift)
    if shift.state != ShiftState.SEALED:
        raise DomainError('handover_required', '请先完成班次封账，再建立负荷方案')
    latest = db.scalar(select(LoadPlan).where(LoadPlan.shift_id == shift.id).order_by(LoadPlan.revision.desc()).limit(1))
    if latest and latest.state != PlanState.FAILED:
        raise DomainError('plan_exists', '这个班次已有进行中或已获批的负荷方案', plan_id=latest.id)
    windows = list(db.scalars(select(ProductionShift).where(
        ProductionShift.line == shift.line, ProductionShift.state == ShiftState.SEALED,
        ProductionShift.ends_at <= shift.starts_at,
    ).order_by(ProductionShift.starts_at.desc()).limit(12)))
    historical_windows = [capture_rows(db, item) for item in reversed(windows)]
    if len(historical_windows) < 2 or sum(map(len, historical_windows[:-1])) < 12:
        raise DomainError('insufficient_history', '同线历史不足：至少需要两个封账班次，并留出最近一班评估')
    if len(historical_windows[-1]) < 2:
        raise DomainError('holdout_too_short', '最近封账班次不足两小时，无法用作留出评估')
    snapshot = {'line': shift.line, 'target': target, 'history': [row for window in historical_windows for row in window]}
    plan = LoadPlan(shift_id=shift.id, title=title, revision=latest.revision + 1 if latest else 1,
                    requested_by=user.id, input_snapshot=snapshot, input_sha256=fingerprint(snapshot))
    db.add(plan)
    db.flush()
    audit(db, user.username, 'plan.queued', plan.id, shift=shift.code, fingerprint=plan.input_sha256)
    return plan


def get_plan(db, plan_id, lock=False):
    query = select(LoadPlan).where(LoadPlan.id == plan_id)
    if lock:
        query = query.with_for_update()
    plan = db.scalar(query)
    if plan is None:
        raise DomainError('plan_not_found', '找不到负荷方案', 404)
    return plan


def manual_baselines(db, plan_id):
    rows = db.scalars(select(PlanBaseline).where(PlanBaseline.plan_id == plan_id).order_by(PlanBaseline.id))
    return {row.end_read_id: row for row in rows}


def plan_outcome(db, plan):
    """The plan's hourly baselines with any supervisor confirmations merged in."""
    if plan.result is None:
        return None
    manual = {key: {'baseline_kg': float(row.baseline_kg), 'note': row.note}
              for key, row in manual_baselines(db, plan.id).items()}
    return finalize(normalize(plan.result, plan.input_snapshot), manual)


def set_manual_baseline(db, plan, end_read_id, data, user):
    if plan.state == PlanState.APPROVED:
        raise DomainError('baselines_locked', '方案已签批，核定值已随复核单锁定，不能再改')
    if plan.state != PlanState.ESTIMATED or plan.result is None:
        raise DomainError('plan_not_estimated', '只有完成估计的方案可以核定基线')
    outcome = plan_outcome(db, plan)
    if 'pending_count' not in outcome:
        raise DomainError('plan_revision_legacy', '这个方案修订早于逐小时核定，请重新提交估计')
    hour = next((row for row in outcome['profile'] if row['end_read_id'] == end_read_id), None)
    if hour is None:
        raise DomainError('baseline_hour_missing', '该时点不属于这个方案的估计曲线', 404)
    if not hour['unseen']:
        raise DomainError('combination_known', '这个设备组合在同线历史里出现过，沿用拟合基线即可')
    existing = manual_baselines(db, plan.id).get(end_read_id)
    previous = None if existing is None else float(existing.baseline_kg)
    if existing is None:
        existing = PlanBaseline(plan_id=plan.id, end_read_id=end_read_id)
        db.add(existing)
    existing.baseline_kg, existing.note = data.baseline_kg, data.note
    existing.set_by, existing.set_at = user.id, utc_now()
    db.flush()
    audit(db, user.username, 'plan.baseline_set', plan.id, end_read_id=end_read_id,
          baseline_kg=float(data.baseline_kg), reason=data.note, previous_kg=previous)
    return existing


def approve_plan(db, plan, user):
    # Sign-off retries are idempotent: the sheet is keyed to the plan and the
    # baseline set is whatever was locked the first time.
    review = db.scalar(select(EnergyReview).where(EnergyReview.plan_id == plan.id).with_for_update())
    if review is not None:
        return review
    if plan.state != PlanState.ESTIMATED:
        raise DomainError('plan_not_estimated', '只有完成估计的方案可以审批')
    outcome = plan_outcome(db, plan)
    if 'pending_count' not in outcome:
        raise DomainError('plan_revision_legacy', '这个方案修订早于逐小时核定，请重新提交估计')
    if outcome['pending_count']:
        raise DomainError('baselines_pending', '还有小时缺少核定值，补齐后才能出整班偏差并签批',
                          pending_hours=outcome['pending_hours'])
    plan.state, plan.approved_by, plan.approved_at = PlanState.APPROVED, user.id, utc_now()
    # Lock the complete, finalized baseline set onto the review sheet; later
    # edits to plan.result or the manual table cannot move it.
    review = EnergyReview(plan_id=plan.id, baselines={
        'locked_at': utc_now().isoformat(), 'observed_kg': outcome['observed_kg'],
        'baseline_kg': outcome['baseline_kg'], 'excess_kg': outcome['excess_kg'],
        'pending_count': outcome['pending_count'], 'pending_hours': outcome['pending_hours'],
        'profile': outcome['profile'],
    })
    db.add(review)
    db.flush()
    for flag in outcome['spikes']:
        db.add(ReviewSpike(review_id=review.id, sample_scope=flag['scope'],
                           **{key: flag[key] for key in ('start_read_id', 'end_read_id', 'observed_kg', 'baseline_kg')}))
    audit(db, user.username, 'plan.approved', plan.id, review_id=review.id,
          spike_count=len(outcome['spikes']), pending_count=outcome['pending_count'])
    return review


def get_review(db, review_id, lock=False):
    query = select(EnergyReview).where(EnergyReview.id == review_id)
    if lock:
        query = query.with_for_update()
    review = db.scalar(query)
    if review is None:
        raise DomainError('review_not_found', '找不到节能复核单', 404)
    return review


def get_spikes(db, review_id):
    return list(db.scalars(select(ReviewSpike).where(ReviewSpike.review_id == review_id).order_by(ReviewSpike.id)))


def require_pending(review):
    if review.state != ReviewState.PENDING:
        raise DomainError('review_closed', '复核单已结案')


def verify_spike(db, review, spike_id, data, user):
    require_pending(review)
    spike = db.get(ReviewSpike, spike_id)
    if spike is None or spike.review_id != review.id:
        raise DomainError('spike_not_found', '找不到该复核单上的尖峰', 404)
    previous = {'finding': spike.finding, 'note': spike.findings_note}
    spike.finding = data.finding
    spike.findings_note = data.note
    spike.verified_by, spike.verified_at = user.id, utc_now()
    audit(db, user.username, 'review.spike_verified', review.id, spike_id=spike_id,
          finding=data.finding, reason=data.note, previous=previous)
    return spike


def close_review(db, review, data, user):
    require_pending(review)
    pending = [spike.id for spike in get_spikes(db, review.id) if spike.finding is None]
    if pending:
        raise DomainError('spikes_unverified', '先核验所有尖峰记录，才能关闭节能复核单', spike_ids=pending)
    review.state, review.closed_at, review.closed_by = ReviewState.CLOSED, utc_now(), user.id
    review.conclusion, review.follow_up = data.conclusion, data.follow_up
    audit(db, user.username, 'review.closed', review.id, conclusion=data.conclusion, follow_up=data.follow_up)
    return review
