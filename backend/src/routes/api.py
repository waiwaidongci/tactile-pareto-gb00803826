from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from src.config.settings import ARTIFACTS
from src.constants.enums import Role
from src.controllers.query import delivery_status, jobs, recent_activity, require_row, revisions
from src.models.tables import datasets, runs
from src.schemas.requests import Delivery, Import, Login, Review, Run
from src.services.data import PROFILE, csv_records, save_revision
from src.services.domain import sample_rows
from src.services.shift_domain import normalize_kind
from src.services.shifts import submit_form
from src.services.workflow import add_run, cancel_schedule, review_run, schedule_report
from src.utils.exports import write_csv
from src.utils.security import allowed, authenticate, signed_in

router = APIRouter()
analyst = allowed(Role.analyst.value)
reviewer = allowed(Role.reviewer.value)


@router.post('/login')
def login(body: Login):
    return authenticate(body.username, body.password)


@router.get('/me')
def current(account=Depends(signed_in)):
    return dict(account)


@router.get('/profile')
def profile():
    return PROFILE


def download_csv(data, name):
    return Response(data, media_type='text/csv; charset=utf-8',
                    headers={'content-disposition': "attachment; filename*=UTF-8''" + quote(name)})


@router.get('/example.csv')
def example(account=Depends(signed_in)):
    return download_csv(write_csv(sample_rows(), [c['key'] for c in PROFILE['columns']], False), PROFILE['filename'])


@router.get('/datasets')
def list_datasets(account=Depends(signed_in)):
    return revisions()


@router.get('/datasets/{key}')
def get_dataset(key: int, account=Depends(signed_in)):
    dataset = require_row(datasets, key, '数据版本')
    records = dataset.pop('records')
    return {**dataset, 'count': len(records), 'records': records[:100]}


@router.post('/datasets', status_code=201)
def import_data(body: Import, request: Request, account=Depends(analyst)):
    extra = body.model_dump()
    shift_ref = body.shift or body.shift_code or body.code or (str(body.shift_id) if body.shift_id else None)
    kind = normalize_kind(body.kind or body.form_type or body.doc_type or body.type or extra.get('document_type'))
    records = body.rows or body.records or extra.get('rows') or extra.get('records')
    csv_text = body.csv
    if not csv_text:
        for key in ('content', 'csv_text', 'text', 'data', 'file_content', 'body'):
            value = extra.get(key)
            if isinstance(value, str) and value.strip():
                csv_text = value
                break

    # 新流程：/datasets 同时兼容“按班次分开交两份原表”。kind 可省略，按记录类型自动识别/拆分。
    if shift_ref:
        view, status_code = submit_form(
            str(shift_ref), kind, csv_text=csv_text, records=records,
            filename=(body.name or extra.get('filename') or None), actor=account['username'])
        form = (view.get('forms') or {}).get(kind) if kind in ('reject', 'recheck') else None
        form = form or view.get('recheck_form') or view.get('reject_form') or {}
        payload = {
            # 保持旧契约 id/created/rows：这里的 id 是所交原表的数据版本。
            'id': form.get('dataset_id') or view.get('dataset_id'),
            'form_id': form.get('id'),
            'created': view.get('created', status_code == 201),
            'rows': form.get('row_count'),
            'shift_id': view.get('id'),
            'shift': view.get('code'),
            'shift_code': view.get('code'),
            'kind': kind or form.get('kind'),
            'status': view.get('status'),
            'state': view.get('state'),
            'status_label': view.get('status_label'),
            'passed': view.get('passed'),
            'reconciled': view.get('reconciled'),
            'complete': view.get('complete'),
            'closed': view.get('closed'),
            'mismatch_count': view.get('mismatch_count'),
            'mismatches': view.get('mismatches'),
            # 核对通过后整班合并数据版本，分析应绑定它（或直接用 shift）。
            'dataset_id': view.get('dataset_id'),
            'analysis_dataset_id': view.get('dataset_id'),
            'shift_detail': view,
            'notice': view.get('notice'),
        }
        return JSONResponse(payload, status_code=status_code)

    if records is not None:
        from src.services.data import PROFILE as _profile, convert
        rows = [{field['key']: convert(row.get(field['key']), field) for field in _profile['columns']}
                for row in records]
    else:
        rows = csv_records(csv_text or '')
    name = (body.name or '').strip()
    if not name:
        raise ValueError('数据名称不能为空。')
    key, created = save_revision(name, rows, account['username'])
    return {'id': key, 'created': created, 'rows': len(rows)}


@router.get('/runs')
def list_runs(account=Depends(signed_in)):
    return jobs()


@router.post('/runs', status_code=202)
def queue(body: Run, request: Request, account=Depends(analyst)):
    idem = (body.idempotency_key or body.request_key
            or request.headers.get('idempotency-key') or request.headers.get('x-idempotency-key'))
    result = add_run(body, account['username'], idem)
    if result.get('idempotent_replay'):
        return JSONResponse(result, status_code=200)
    return result


@router.get('/runs/{key}')
def result(key: int, account=Depends(signed_in)):
    return require_row(runs, key, '分析任务')


@router.post('/runs/{key}/review')
def review(key: int, body: Review, account=Depends(reviewer)):
    return review_run(key, body, account['username'])


@router.post('/runs/{key}/schedules', status_code=201)
def schedule(key: int, body: Delivery, account=Depends(reviewer)):
    return schedule_report(key, body, account['username'])


@router.get('/delivery')
def delivery(account=Depends(signed_in)):
    return delivery_status()


@router.delete('/schedules/{key}')
def stop_schedule(key: int, account=Depends(reviewer)):
    return cancel_schedule(key, account['username'])


@router.get('/audit')
def activity(account=Depends(signed_in)):
    return recent_activity()


@router.get('/runs/{key}/report.csv')
def report(key: int, account=Depends(signed_in)):
    run = require_row(runs, key, '分析任务')
    if run['decision'] != 'approved' and account['role'] == Role.viewer.value:
        raise HTTPException(403, '只读账号可导出已批准的报告。')
    if not run['result']:
        raise HTTPException(409, '分析结果尚不可导出。')
    path = ARTIFACTS / f"run-{key}-{run['started'].hex()}.csv"
    data = path.read_bytes() if path.exists() else write_csv(run['result']['rows'], run['result'].get('columns'))
    return download_csv(data, f"{PROFILE['title']}-{key}.csv")
