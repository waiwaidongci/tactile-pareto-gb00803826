import threading
import time

from fastapi import HTTPException
from sqlalchemy import insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from src.models.database import engine, fetch_one
from src.models.tables import datasets, idempotency_keys, shift_forms, shifts
from src.services.data import csv_records, records_checksum
from src.services.shift_domain import (
    CLOSED,
    COLLECTING,
    KINDS,
    KIND_LABELS,
    MISMATCH,
    READY,
    RECHECK,
    REJECT,
    STATUS_LABELS,
    normalize_kind,
    progress_text,
    read_form,
    reconcile_forms,
)

# 单进程内按班次串行化“交单/补交”和“封班”，与数据库行锁共同保证：
# 核对与入库读到、写到的始终是同一组原单。
_locks_guard = threading.Lock()
_shift_locks = {}


def _shift_lock(shift_id):
    with _locks_guard:
        lock = _shift_locks.get(shift_id)
        if lock is None:
            lock = threading.RLock()
            _shift_locks[shift_id] = lock
        return lock


def _now():
    return time.time()


def find_shift(connection, key, lock=False):
    statement = select(shifts)
    if lock:
        statement = statement.with_for_update()
    if isinstance(key, int):
        condition = shifts.c.id == key
    elif isinstance(key, str) and key.strip().isdigit():
        value = key.strip()
        condition = or_(shifts.c.id == int(value), shifts.c.code == value)
    else:
        condition = shifts.c.code == str(key).strip()
    row = connection.execute(statement.where(condition).order_by(shifts.c.id).limit(1)).mappings().first()
    return dict(row) if row is not None else None


def _resolve_shift_id(key):
    """按 id 或班次编号找到已有班次；不存在返回 None。"""
    with engine.connect() as connection:
        row = find_shift(connection, key)
        return row['id'] if row else None


def get_or_create_by_code(code, actor, name=None):
    code = str(code or '').strip()
    if not code:
        raise ValueError('请填写班次编号（例如 20261125-早班）。')
    if len(code) > 80:
        raise ValueError('班次编号最长 80 个字符。')
    now = _now()
    with engine.begin() as connection:
        existing = connection.execute(select(shifts.c.id).where(shifts.c.code == code)).scalar_one_or_none()
        if existing is not None:
            return existing, False
        try:
            shift_id = connection.execute(insert(shifts).values(
                code=code, name=(name or code), status=COLLECTING, version=0, mismatch_count=0,
                owner=actor, created=now, updated=now)).inserted_primary_key[0]
        except IntegrityError:
            shift_id = connection.execute(select(shifts.c.id).where(shifts.c.code == code)).scalar_one()
            return shift_id, False
        from src.models.database import add_event
        add_event(connection, actor, 'shift_create', shift_id, now)
        return shift_id, True


def create_shift(payload, actor):
    payload = payload or {}
    code = (payload.get('code') or payload.get('shift') or payload.get('shift_code')
            or payload.get('banci') or payload.get('batch_shift') or payload.get('id'))
    if not code:
        day = payload.get('shift_date') or payload.get('date') or payload.get('inspection_date')
        slot = payload.get('shift_slot') or payload.get('slot') or payload.get('period') or payload.get('shift_type')
        if day and slot:
            code = f'{day}-{slot}'
        elif day:
            code = str(day)
        elif payload.get('name'):
            code = payload.get('name')
        else:
            code = f"SHIFT-{time.strftime('%Y%m%d%H%M%S')}"
    code = str(code).strip()
    name = str(payload.get('name') or payload.get('shift_name') or code).strip() or code
    shift_id, created = get_or_create_by_code(code, actor, name)
    return get_shift(shift_id), 201 if created else 200


def _get_or_create_dataset(connection, name, records, owner, now):
    checksum = records_checksum(records)
    existing = connection.execute(select(datasets.c.id).where(datasets.c.checksum == checksum)).scalar_one_or_none()
    if existing is not None:
        return existing, False, checksum
    try:
        with connection.begin_nested():
            dataset_id = connection.execute(insert(datasets).values(
                name=name[:180], owner=owner, records=records, checksum=checksum, created=now
            )).inserted_primary_key[0]
        return dataset_id, True, checksum
    except IntegrityError:
        dataset_id = connection.execute(select(datasets.c.id).where(datasets.c.checksum == checksum)).scalar_one()
        return dataset_id, False, checksum


def _form_dict(row, with_records=False):
    if row is None:
        return None
    data = {
        'id': row['id'],
        'shift_id': row['shift_id'],
        'kind': row['kind'],
        'kind_label': KIND_LABELS.get(row['kind'], row['kind']),
        'dataset_id': row['dataset_id'],
        'version_id': row['dataset_id'],
        'filename': row['filename'],
        'name': row['filename'],
        'checksum': row['checksum'],
        'sha256': row['checksum'],
        'revision': row['revision'],
        'row_count': row['row_count'],
        'count': row['row_count'],
        'owner': row['owner'],
        'created': row['created'],
    }
    if with_records:
        data['records'] = row['records']
    return data


def _load_form(connection, form_id, with_records=True):
    if not form_id:
        return None
    row = fetch_one(connection, shift_forms, form_id)
    return _form_dict(dict(row), with_records=with_records) if row else None


def _public_reconciliation(recon):
    return {key: value for key, value in recon.items() if key != 'merged_records'}


def shift_view(connection, shift, detailed=True):
    shift = dict(shift)
    reject_form = _load_form(connection, shift.get('reject_form_id'), with_records=detailed)
    recheck_form = _load_form(connection, shift.get('recheck_form_id'), with_records=detailed)
    forms_present = {
        'reject': bool(shift.get('reject_form_id') and shift.get('reject_checksum')),
        'recheck': bool(shift.get('recheck_form_id') and shift.get('recheck_checksum')),
    }
    status = shift['status']
    complete = all(forms_present.values())
    reconciled = status == READY or status == CLOSED
    closed = status == CLOSED
    mismatches = (shift.get('reconciliation') or {}).get('mismatches', [])
    visible_mismatches = mismatches if detailed else []
    received_count = sum(1 for value in forms_present.values() if value)
    progress_percent = 25 * received_count
    if reconciled and not shift.get('mismatch_count'):
        progress_percent += 25
    if closed:
        progress_percent = 100
    elif status == MISMATCH:
        progress_percent = max(progress_percent, 50)
    view = {
        **{key: value for key, value in shift.items() if key != 'reconciliation'},
        'shift': shift['code'],
        'shift_code': shift['code'],
        'shift_id': shift['id'],
        'status': status,
        'state': status,
        'stage': status,
        'status_label': STATUS_LABELS.get(status, status),
        'progress_label': STATUS_LABELS.get(status, status),
        'progress_text': progress_text(status, forms_present),
        'progress_percent': progress_percent,
        'progress_pct': progress_percent,
        'progress': {
            'received_forms': received_count,
            'required_forms': 2,
            'complete': complete,
            'reconciled': reconciled,
            'closed': closed,
            'percent': progress_percent,
        },
        'reconciliation_status': 'passed' if reconciled and not shift.get('mismatch_count')
        else ('mismatch' if status == MISMATCH else 'pending'),
        'seal_status': 'sealed' if closed else 'open',
        'complete': complete,
        'collected': complete,
        'both_received': complete,
        'reconciled': reconciled,
        'passed': reconciled and not shift.get('mismatch_count'),
        'matched': reconciled and not shift.get('mismatch_count'),
        'verified': reconciled and not shift.get('mismatch_count'),
        'ready': status in (READY, CLOSED),
        'analysis_ready': status in (READY, CLOSED) and bool(shift.get('dataset_id')),
        'closed': closed,
        'sealed': closed,
        'locked': closed,
        'can_close': complete and status != MISMATCH and (status == READY or (complete and reconciled)),
        'can_analyze': status in (READY, CLOSED) and bool(shift.get('dataset_id')),
        'received': [kind for kind in KINDS if forms_present[kind]],
        'missing': [kind for kind in KINDS if not forms_present[kind]],
        'forms_present': forms_present,
        'forms': {'reject': reject_form, 'recheck': recheck_form},
        'reject_form': reject_form,
        'recheck_form': recheck_form,
        'mismatches': visible_mismatches,
        'issues': visible_mismatches,
        'discrepancies': visible_mismatches,
        'mismatch_count': shift.get('mismatch_count', len(mismatches)),
        'issue_count': shift.get('mismatch_count', len(mismatches)),
        'dataset_id': shift.get('dataset_id'),
        'merged_dataset_id': shift.get('dataset_id'),
        'version_id': shift.get('dataset_id'),
        'analysis_version_id': shift.get('dataset_id'),
        'reconciliation': shift.get('reconciliation') if detailed else None,
    }
    if not detailed:
        # 列表只给不符数量，不带明细与原表记录。
        view['forms'] = {
            'reject': _form_dict(fetch_one(connection, shift_forms, shift['reject_form_id']), False)
            if shift.get('reject_form_id') else None,
            'recheck': _form_dict(fetch_one(connection, shift_forms, shift['recheck_form_id']), False)
            if shift.get('recheck_form_id') else None,
        }
        view['reject_form'] = view['forms']['reject']
        view['recheck_form'] = view['forms']['recheck']
    return view


def list_shifts():
    with engine.connect() as connection:
        rows = connection.execute(select(shifts).order_by(shifts.c.updated.desc(), shifts.c.id.desc())
                                 .limit(200)).mappings().all()
        return [shift_view(connection, row, detailed=False) for row in rows]


def get_shift(key):
    with engine.connect() as connection:
        shift = find_shift(connection, key)
        if shift is None:
            raise HTTPException(404, '班次不存在。')
        return shift_view(connection, shift, detailed=True)


def _recompute_locked(connection, shift, actor, now):
    """在已持有班次行锁/事务内，按当前指向的同一组原单核对并决定是否入库。"""
    reject_rows = recheck_rows = None
    if shift.get('reject_form_id'):
        form = fetch_one(connection, shift_forms, shift['reject_form_id'])
        reject_rows = form['records'] if form else None
    if shift.get('recheck_form_id'):
        form = fetch_one(connection, shift_forms, shift['recheck_form_id'])
        recheck_rows = form['records'] if form else None

    recon = reconcile_forms(reject_rows, recheck_rows)
    values = {'reconciliation': _public_reconciliation(recon),
              'mismatch_count': recon['mismatch_count'], 'updated': now}
    event_action = None
    if not recon['complete']:
        values['status'] = COLLECTING
    elif recon['passed']:
        merged = recon['merged_records']
        dataset_id, _created, pair_checksum = _get_or_create_dataset(
            connection, f"{shift['code']}-整班核对通过.csv", merged, actor or shift.get('owner') or 'system', now)
        values.update(status=READY, dataset_id=dataset_id, pair_checksum=pair_checksum)
        event_action = 'shift_pass'
    else:
        values['status'] = MISMATCH
        event_action = 'shift_mismatch'
    connection.execute(update(shifts).where(shifts.c.id == shift['id']).values(**values))
    if event_action:
        from src.models.database import add_event
        add_event(connection, actor or 'system', event_action, shift['id'], now)
    return recon


def submit_form(key, kind, csv_text=None, records=None, filename=None, actor='api'):
    requested_kind = normalize_kind(kind)

    if records is not None:
        from src.services.data import convert
        from src.services.data import PROFILE
        try:
            normalized = [{field['key']: convert(row.get(field['key']), field) for field in PROFILE['columns']}
                          for row in records]
        except (ValueError, TypeError) as error:
            raise ValueError(f'原表记录字段无效（{error}）。') from error
    else:
        if csv_text is None or not str(csv_text).strip():
            raise ValueError('请上传 CSV 原表内容。')
        normalized = csv_records(str(csv_text))

    # 先做表内校验：非法代码/严重度/记录类型一律提示修复，不入库。
    ledger = read_form(normalized)

    # 未指定类型或显式 auto/merged：若一份 CSV 内同时含两类记录，自动拆成两份原表分别收交。
    from src.services.domain import DEFECT_CHECK as _DC, REGISTERED as _REG
    has_reg = ledger['type_counts'][_REG] > 0
    has_chk = ledger['type_counts'][_DC] > 0
    if requested_kind == 'auto' or (requested_kind is None and has_reg and has_chk):
        last_view, last_code = None, 200
        any_created = False
        base = filename or 'merged.csv'
        if has_reg:
            reg_rows = [dict(row) for row in normalized if row.get('record_type') == _REG]
            last_view, last_code = submit_form(
                key, REJECT, records=reg_rows, filename=f'reject-{base}', actor=actor)
            any_created = any_created or last_code == 201
        if has_chk:
            chk_rows = [dict(row) for row in normalized if row.get('record_type') == _DC]
            last_view, last_code = submit_form(
                key, RECHECK, records=chk_rows, filename=f'recheck-{base}', actor=actor)
            any_created = any_created or last_code == 201
        return last_view, 201 if any_created else 200

    kind = requested_kind or ledger.get('detected_kind')
    if kind not in KINDS:
        if has_reg and not has_chk:
            kind = REJECT
        elif has_chk and not has_reg:
            kind = RECHECK
        else:
            raise ValueError('请说明原表类型：拒收单（reject）或复检单（recheck）。')

    checksum = records_checksum(normalized)

    shift_id = _resolve_shift_id(key)
    if shift_id is None:
        shift_id, _ = get_or_create_by_code(str(key), actor)

    with _shift_lock(shift_id):
        now = _now()
        with engine.begin() as connection:
            shift = find_shift(connection, shift_id, lock=True)
            if shift is None:
                raise HTTPException(404, '班次不存在。')

            current_checksum = shift.get(f'{kind}_checksum')
            if shift['status'] == CLOSED:
                view = shift_view(connection, shift, detailed=True)
                if current_checksum == checksum:
                    view.update(replayed=True, created=False, same_version=True,
                                notice='该班次已封班；重传的是同一份原单，仍回到同一版本。')
                    return view, 200
                raise HTTPException(409, '该班次已封班，原单内容与封存版本不同，按规定拒收；相同原单重传仍返回原版本。')

            if current_checksum == checksum:
                view = shift_view(connection, shift, detailed=True)
                view.update(replayed=True, created=False, same_version=True,
                            notice='与已交原表内容一致，未新增版本（重试幂等）。')
                return view, 200

            previous = fetch_one(connection, shift_forms, shift[f'{kind}_form_id']) if shift.get(f'{kind}_form_id') else None
            revision = (previous['revision'] + 1) if previous else 1
            dataset_id, ds_created, _ = _get_or_create_dataset(
                connection, (filename or f"{shift['code']}-{kind}.csv"), normalized, actor, now)
            form_id = connection.execute(insert(shift_forms).values(
                shift_id=shift_id, kind=kind, dataset_id=dataset_id, filename=(filename or f'{kind}.csv')[:240],
                checksum=checksum, records=normalized, row_count=len(normalized), revision=revision,
                owner=actor, created=now)).inserted_primary_key[0]
            connection.execute(update(shifts).where(shifts.c.id == shift_id).values(
                **{f'{kind}_form_id': form_id, f'{kind}_checksum': checksum},
                version=shift['version'] + 1, updated=now))
            from src.models.database import add_event
            add_event(connection, actor, 'shift_upload', shift_id, now)

            fresh = find_shift(connection, shift_id, lock=True)
            _recompute_locked(connection, fresh, actor, now)
            final = find_shift(connection, shift_id, lock=True)
            view = shift_view(connection, final, detailed=True)
            other = 'recheck' if kind == 'reject' else 'reject'
            if final.get(f'{other}_form_id'):
                other_form = view.get(f'{other}_form') or {}
                view['kept_form'] = other
                view['reused_form'] = other
                view['unchanged_form'] = other
                view['kept_forms'] = [other]
                view['reused_form_id'] = final.get(f'{other}_form_id')
                view['kept_form_id'] = final.get(f'{other}_form_id')
                view['kept_form_dataset_id'] = other_form.get('dataset_id')
                view['notice'] = f'本次更新{KIND_LABELS[kind]}；无误的{KIND_LABELS[other]}沿用原版本，未要求重交。'
            view.update(created=True, form_id=form_id, form_dataset_id=dataset_id)
            return view, 201


def reconcile_shift(key, actor='api'):
    shift_id = _resolve_shift_id(key)
    if shift_id is None:
        raise HTTPException(404, '班次不存在。')
    with _shift_lock(shift_id):
        with engine.begin() as connection:
            shift = find_shift(connection, shift_id, lock=True)
            if shift['status'] == CLOSED:
                return shift_view(connection, shift, detailed=True), 200
            if not (shift.get('reject_form_id') and shift.get('recheck_form_id')):
                raise HTTPException(409, '两份原表尚未收齐，到齐后才能逐件核对。')
            _recompute_locked(connection, shift, actor, _now())
            final = find_shift(connection, shift_id, lock=True)
            return shift_view(connection, final, detailed=True), 200


def close_shift(key, actor='api'):
    shift_id = _resolve_shift_id(key)
    if shift_id is None:
        raise HTTPException(404, '班次不存在。')
    with _shift_lock(shift_id):
        error = None
        with engine.begin() as connection:
            shift = find_shift(connection, shift_id, lock=True)
            if shift['status'] == CLOSED:
                view = shift_view(connection, shift, detailed=True)
                view.update(replayed=True, created=False, same_version=True,
                            notice='班次已封班，重复封班仍返回同一数据版本。')
                return view, 200

            if not (shift.get('reject_form_id') and shift.get('recheck_form_id')):
                error = '两份原表尚未收齐，不能封班；先到的一份已照收，请等另一份到齐并核对通过。'
            else:
                _recompute_locked(connection, shift, actor, _now())
                fresh = find_shift(connection, shift_id, lock=True)
                if fresh['status'] != READY or not fresh.get('dataset_id'):
                    error = '逐件核对未通过，请按注明的批次和单件补交对应原表，核对通过后才能封班入库。'
                else:
                    now = _now()
                    connection.execute(update(shifts).where(shifts.c.id == shift_id).values(
                        status=CLOSED, closed_at=now, closed_by=actor, updated=now))
                    from src.models.database import add_event
                    add_event(connection, actor, 'shift_close', shift_id, now)
            final = find_shift(connection, shift_id, lock=True)
            view = shift_view(connection, final, detailed=True)
        if error:
            raise HTTPException(409, error)
        view.update(closed=True, sealed=True, notice='封班完成，整班数据版本固定；相同原单重传回到同一版本，换内容将被拒收。')
        return view, 200


def resolve_runnable_dataset(connection, dataset_id=None, shift_ref=None):
    """返回可用于分析的 dataset_id；不满足整班核对门槛时抛 409。

    - 指定班次：必须核对通过（ready/closed）且已生成整班数据版本。
    - 指定数据版本：若它是某班次当前整班版本，按班次状态把关；若它只是某份原表，
      则在该班核对通过后解析到整班合并版本，否则拒绝；其余视为旧版自由导入，保持原行为。
    """
    if shift_ref is None and dataset_id is None:
        raise ValueError('请选择数据版本或已核对通过的班次。')
    if shift_ref is not None:
        shift = find_shift(connection, shift_ref)
        if shift is None:
            raise HTTPException(404, '班次不存在。')
        if shift['status'] not in (READY, CLOSED) or not shift.get('dataset_id'):
            if shift['status'] == MISMATCH:
                raise HTTPException(409, '该班次逐件核对未通过，请按批次/单件不符项补交，核对通过后才能分析整班数据。')
            raise HTTPException(409, '该班次两份原表尚未收齐或尚未核对通过，整班数据通过后才能用于分析。')
        return shift['dataset_id']

    bound = connection.execute(select(shifts).where(shifts.c.dataset_id == dataset_id).limit(1)
                               ).mappings().first()
    if bound is not None:
        if bound['status'] not in (READY, CLOSED):
            raise HTTPException(409, '该数据版本所属班次尚未核对通过，暂不能用于分析。')
        return dataset_id

    form = connection.execute(select(shift_forms).where(shift_forms.c.dataset_id == dataset_id)
                              .order_by(shift_forms.c.id.desc()).limit(1)).mappings().first()
    if form is not None:
        shift = fetch_one(connection, shifts, form['shift_id'])
        if shift is not None and shift['status'] in (READY, CLOSED) and shift.get('dataset_id'):
            return shift['dataset_id']
        raise HTTPException(409, '这只是分开提交的一份原表；请收齐两份原表并逐件核对通过后，再分析整班数据。')

    return dataset_id


def demo_shift(actor='demo'):
    from src.services.domain import sample_rows
    from src.services.shift_domain import split_sample_rows
    code = 'TG-1124-DEMO'
    shift_id, created = get_or_create_by_code(code, actor, '2026-11-24 演示班次')
    current = get_shift(shift_id)
    if created or not (current.get('reject_form_id') and current.get('recheck_form_id')):
        parts = split_sample_rows(sample_rows())
        # 复检单常先到：先交复检单，再交拒收单。
        submit_form(code, 'recheck', records=parts['recheck'], filename='tactile-recheck-20261124.csv', actor=actor)
        submit_form(code, 'reject', records=parts['reject'], filename='tactile-rejects-form-20261124.csv', actor=actor)
    return get_shift(shift_id)
