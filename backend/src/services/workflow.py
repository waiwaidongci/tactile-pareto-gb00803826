import hashlib
import json
import time
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from src.constants.enums import State
from src.models.database import add_event, engine, fetch_one
from src.models.tables import datasets, deliveries, idempotency_keys, runs, schedules
from src.services.data import parse_options
from src.services.shifts import resolve_runnable_dataset


def _request_fingerprint(payload):
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def add_run(body, actor, idempotency_key=None):
    options = parse_options(body.options)
    shift_ref = (getattr(body, 'shift_id', None) or getattr(body, 'shift', None)
                 or getattr(body, 'shift_code', None) or getattr(body, 'code', None))
    if idempotency_key is None:
        idempotency_key = getattr(body, 'idempotency_key', None) or getattr(body, 'request_key', None)
    idempotency_key = idempotency_key.strip() if isinstance(idempotency_key, str) and idempotency_key.strip() else None
    if idempotency_key and len(idempotency_key) > 120:
        idempotency_key = 'k:' + hashlib.sha256(idempotency_key.encode()).hexdigest()
    now = time.time()
    with engine.begin() as connection:
        dataset_id = resolve_runnable_dataset(connection, getattr(body, 'dataset_id', None), shift_ref)
        if fetch_one(connection, datasets, dataset_id) is None:
            raise HTTPException(404, '数据版本不存在。')
        fingerprint = _request_fingerprint({'dataset_id': dataset_id, 'options': options})

        if idempotency_key:
            reserved = connection.execute(select(idempotency_keys).where(
                idempotency_keys.c.key == idempotency_key)).mappings().one_or_none()
            if reserved is not None:
                if reserved['request_hash'] != fingerprint:
                    raise HTTPException(409, '同一幂等键已用于不同的数据版本或参数，请更换请求键。')
                return {'id': reserved['subject_id'], 'state': State.queued.value,
                        'idempotent_replay': True, 'created': False}
            try:
                with connection.begin_nested():
                    connection.execute(insert(idempotency_keys).values(
                        key=idempotency_key, actor=actor, scope='run', request_hash=fingerprint,
                        status_code=202, created=now))
            except IntegrityError:
                winner = connection.execute(select(idempotency_keys).where(
                    idempotency_keys.c.key == idempotency_key)).mappings().one()
                if winner['request_hash'] != fingerprint:
                    raise HTTPException(409, '同一幂等键已用于不同的数据版本或参数，请更换请求键。') from None
                return {'id': winner['subject_id'], 'state': State.queued.value, 'idempotent_replay': True}

        key = connection.execute(insert(runs).values(dataset_id=dataset_id, options=options,
                    state=State.queued.value, owner=actor, created=now)).inserted_primary_key[0]
        if idempotency_key:
            connection.execute(update(idempotency_keys).where(idempotency_keys.c.key == idempotency_key)
                               .values(subject_id=key))
        add_event(connection, actor, 'queue', key, now)
    return {'id': key, 'state': State.queued.value, 'created': True}


def add_shift_run(shift_key, raw_options, actor, idempotency_key=None):
    """为已核对通过的班次提交分析；同一班次数据版本+参数的重试不新增分析版本。"""
    options = parse_options(raw_options or {})
    with engine.connect() as connection:
        shift = resolve_runnable_dataset(connection, None, shift_key)  # 返回 dataset_id 或 409
        from src.services.shifts import find_shift
        row = find_shift(connection, shift_key)
        shift_id, dataset_id = row['id'], shift
    fingerprint = _request_fingerprint({'shift': shift_id, 'dataset_id': dataset_id, 'options': options})
    deterministic_key = idempotency_key or f'shift-run:{shift_id}:{dataset_id}:{fingerprint[:24]}'
    body = SimpleNamespace(dataset_id=dataset_id, options=options, shift_id=None)
    return add_run(body, actor, deterministic_key)


def claim_run():
    now = time.time()
    claimable = or_(runs.c.state == State.queued.value, and_(runs.c.state == State.running.value, runs.c.started < now - 120))
    with engine.begin() as connection:
        run = connection.execute(select(runs).where(claimable).order_by(runs.c.id).limit(1)).mappings().one_or_none()
        if run is None:
            return None
        key = run['id']
        claim = connection.execute(update(runs).where(runs.c.id == key, claimable)
                                   .values(state=State.running.value, started=now))
        if claim.rowcount != 1:
            return None
        data = fetch_one(connection, datasets, run['dataset_id'])
        return key, now, data['records'], run['options']


def finish_run(key, claim, result, error):
    now = time.time()
    state = State.failed.value if error else State.completed.value
    with engine.begin() as connection:
        saved = connection.execute(update(runs).where(runs.c.id == key, runs.c.started == claim,
                                   runs.c.state == State.running.value).values(state=state, result=result,
                                                  error=error, finished=now))
        if saved.rowcount:
            add_event(connection, 'worker', state, key, now)


def review_run(key, body, actor):
    now = time.time()
    note = body.note.strip()
    if len(note) < 2:
        raise ValueError('请输入至少两个字符的复核意见。')
    with engine.begin() as connection:
        run = fetch_one(connection, runs, key)
        if run is None:
            raise HTTPException(404, '分析任务不存在。')
        if run['state'] != State.completed.value:
            raise HTTPException(409, '分析完成后才能复核。')
        if body.decision == 'approved' and run['result'].get('can_publish') is False:
            raise HTTPException(409, '结果未达到本项目的发布门槛。')
        updated = connection.execute(update(runs).where(runs.c.id == key, runs.c.decision.is_(None))
                                     .values(decision=body.decision, reviewer=actor, review_note=note, reviewed=now))
        if updated.rowcount != 1:
            raise HTTPException(409, '已完成复核。请重新运行分析以提交新的复核。')
        add_event(connection, actor, body.decision, key, now)
    return {'decision': body.decision}


def schedule_report(key, body, actor):
    now = time.time()
    recipient = body.recipient.strip()
    if len(recipient) < 2:
        raise ValueError('请填写站内收件组。')
    if body.scheduled_at and body.scheduled_at.utcoffset() is None:
        raise ValueError('投递时间须含时区。')
    timestamp = body.scheduled_at.timestamp() if body.scheduled_at else now
    if body.scheduled_at and timestamp < now - 60:
        raise ValueError('投递时间已过，请调整时间。')
    with engine.begin() as connection:
        run = fetch_one(connection, runs, key)
        if run is None or run['decision'] != 'approved':
            raise HTTPException(409, '仅已批准的结果可分发。')
        required_recipient = run['result'].get('dispatch_recipient')
        if required_recipient and recipient != required_recipient:
            raise HTTPException(422, f'该报告应投递给{required_recipient}。')
        schedule_id = connection.execute(insert(schedules).values(run_id=key, recipient=recipient, cadence=body.cadence,
                                      next_at=timestamp, active=1, owner=actor)).inserted_primary_key[0]
        add_event(connection, actor, 'schedule', schedule_id, now)
    return {'id': schedule_id, 'next_at': timestamp}


def cancel_schedule(key, actor):
    with engine.begin() as connection:
        updated = connection.execute(update(schedules).where(schedules.c.id == key, schedules.c.active == 1)
                                     .values(active=0))
        if not updated.rowcount:
            raise HTTPException(409, '该计划已停止或已完成。')
        add_event(connection, actor, 'cancel', key, time.time())
    return {'id': key, 'active': False}


def publish_due():
    now = time.time()
    with engine.begin() as connection:
        pending = connection.execute(select(schedules).where(schedules.c.active == 1, schedules.c.next_at <= now)
                                     .order_by(schedules.c.next_at, schedules.c.id).limit(20)).mappings().all()
        for item in pending:
            previous = item['next_at']
            daily = item['cadence'] == 'daily'
            future = previous + (int((now - previous) / 86400) + 1) * 86400 if daily else previous
            changed = connection.execute(update(schedules).where(schedules.c.id == item['id'], schedules.c.active == 1,
                              schedules.c.next_at == previous).values(active=int(daily), next_at=future))
            if changed.rowcount:
                delivery_id = connection.execute(insert(deliveries).values(schedule_id=item['id'], run_id=item['run_id'],
                                               recipient=item['recipient'], scheduled=previous, delivered=now)).inserted_primary_key[0]
                add_event(connection, 'worker', 'delivered', delivery_id, now)
