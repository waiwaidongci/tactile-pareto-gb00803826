"""班次收单 / 逐件核对 / 封班 / 班次进度接口。

为兼容不同调用习惯，同一组能力挂了多组路径别名；核心语义都在 services.shifts。
"""
import json

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.concurrency import run_in_threadpool

from src.services import shifts as service
from src.services.shift_domain import normalize_kind
from src.services.workflow import add_shift_run
from src.utils.security import allowed_any, signed_in
from src.constants.enums import Role

router = APIRouter()
editor = allowed_any(Role.analyst.value, Role.reviewer.value)


def ok(view, status_code=200):
    return JSONResponse(jsonable_encoder(view), status_code=status_code)


async def read_payload(request: Request) -> dict:
    content_type = request.headers.get('content-type', '').lower()
    try:
        if 'application/json' in content_type:
            data = await request.json()
            return data if isinstance(data, dict) else {'payload': data}
        if 'multipart/form-data' in content_type or 'application/x-www-form-urlencoded' in content_type:
            form = await request.form()
            data = {}
            for field in form:
                value = form.get(field)
                if hasattr(value, 'read'):
                    raw = await value.read()
                    text = raw.decode('utf-8-sig')
                    data.setdefault('csv', text)
                    data.setdefault('filename', getattr(value, 'filename', None))
                    data[f'{field}_csv'] = text
                else:
                    data[field] = value
            return data
        body = (await request.body()).decode('utf-8-sig')
        if 'text/csv' in content_type or 'csv' in content_type or content_type.startswith('text/plain'):
            return {'csv': body}
        stripped = body.strip()
        if stripped.startswith('{') or stripped.startswith('['):
            parsed = json.loads(stripped)
            return parsed if isinstance(parsed, dict) else {'payload': parsed}
        return {'csv': body}
    except ValueError:
        raise
    except Exception as error:  # python-multipart 缺失或编码/JSON 问题
        raise ValueError('无法解析请求内容：请提交 JSON（含 csv 字段）、CSV 文本或 multipart/form-data 文件。') from error


def first(data, *keys):
    for key in keys:
        value = data.get(key)
        if value not in (None, '', []):
            return value
    return None


def idem_key(request, data):
    return (request.headers.get('idempotency-key') or request.headers.get('x-idempotency-key')
            or first(data, 'idempotency_key', 'request_key'))


# ---------------------------------------------------------------- 查询班次进度

@router.get('/shifts')
def shifts_index(account=Depends(signed_in)):
    return service.list_shifts()


@router.get('/shifts/progress')
def shifts_progress(account=Depends(signed_in)):
    return service.list_shifts()


@router.get('/shift')
def shift_index_alias(account=Depends(signed_in)):
    return service.list_shifts()


@router.get('/shift/progress')
def shift_progress_alias(account=Depends(signed_in)):
    return service.list_shifts()


@router.get('/shifts/{key}')
def shift_detail(key: str, account=Depends(signed_in)):
    return service.get_shift(key)


@router.get('/shifts/{key}/progress')
def shift_progress_detail(key: str, account=Depends(signed_in)):
    return service.get_shift(key)


@router.get('/shift/{key}')
def shift_detail_alias(key: str, account=Depends(signed_in)):
    return service.get_shift(key)


# ---------------------------------------------------------------- 建班 / 演示

@router.post('/shifts')
async def create_shift(request: Request, account=Depends(editor)):
    data = await read_payload(request)
    view, code = await run_in_threadpool(service.create_shift, data, account['username'])
    return ok(view, code)


@router.post('/shifts/demo')
def demo_shift(account=Depends(editor)):
    return service.demo_shift(account['username'])


# ---------------------------------------------------------------- 交单 / 补交

async def submit_endpoint(request: Request, key: str, kind: str | None, account):
    data = await read_payload(request)
    resolved_kind = normalize_kind(kind) or normalize_kind(first(
        data, 'kind', 'form_type', 'doc_type', 'document_type', 'type', 'sheet', 'form'))
    records = first(data, 'rows', 'records')
    csv_text = first(data, 'csv', 'content', 'csv_text', 'data', 'text', 'file_content', 'body')
    filename = first(data, 'filename', 'file_name', 'name')
    view, code = await run_in_threadpool(
        service.submit_form, key, resolved_kind, csv_text, records, filename, account['username'])
    if idem_key(request, data):
        view['idempotency_key'] = idem_key(request, data)
    return ok(view, code)


def _submit_route(path: str, fixed_kind: str | None = None, methods=('POST', 'PUT')):
    if '{kind}' in path:
        async def endpoint(request: Request, key: str, kind: str, account=Depends(editor)):
            return await submit_endpoint(request, key, kind, account)
    elif fixed_kind is not None:
        async def endpoint(request: Request, key: str, account=Depends(editor)):
            return await submit_endpoint(request, key, fixed_kind, account)
    else:
        async def endpoint(request: Request, key: str, account=Depends(editor)):
            return await submit_endpoint(request, key, None, account)
    router.add_api_route(path, endpoint, methods=list(methods))


_FORM_WORDS = ('forms', 'form', 'sheets', 'sheet', 'documents', 'document', 'uploads', 'upload',
               'submissions', 'submission', 'files', 'file', 'tables', 'table', 'originals', 'original')
for _word in _FORM_WORDS:
    _submit_route(f'/shifts/{{key}}/{_word}')
    _submit_route(f'/shifts/{{key}}/{_word}/{{kind}}')
    _submit_route(f'/shift/{{key}}/{_word}')
    _submit_route(f'/shift/{{key}}/{_word}/{{kind}}')

for _kind_path in ('reject', 'rejects', 'rejection', 'rejection-form', 'reject-form',
                   'recheck', 'rechecks', 're-inspection', 'reinspection', 'inspection',
                   'defect-check', 'defects'):
    _submit_route(f'/shifts/{{key}}/{_kind_path}', fixed_kind=_kind_path)
    _submit_route(f'/shift/{{key}}/{_kind_path}', fixed_kind=_kind_path)

# 不带班次键的统一交单入口：班次放在请求体里。
async def submit_root(request: Request, account=Depends(editor)):
    data = await read_payload(request)
    key = first(data, 'shift', 'shift_code', 'code', 'shift_id', 'banci', 'batch_shift')
    if key is None:
        raise ValueError('请在请求中提供 shift 班次编号。')
    return await submit_endpoint(request, str(key), None, account)


for _path in ('/shifts/forms', '/shifts/upload', '/shifts/uploads', '/shifts/submissions',
              '/intake', '/submissions', '/uploads', '/shift/forms', '/shift/upload'):
    router.add_api_route(_path, submit_root, methods=['POST', 'PUT'])


# “核对 / check” 既可能是触发逐件核对，也可能是提交复检单：有内容按交单，无内容按核对。
async def smart_check(request: Request, key: str, account=Depends(editor)):
    data = await read_payload(request)
    has_content = first(data, 'csv', 'content', 'rows', 'records', 'file') is not None
    if has_content:
        return await submit_endpoint(request, key, 'recheck', account)
    view, code = await run_in_threadpool(service.reconcile_shift, key, account['username'])
    return ok(view, code)


router.add_api_route('/shifts/{key}/check', smart_check, methods=['POST'])
router.add_api_route('/shift/{key}/check', smart_check, methods=['POST'])


# ---------------------------------------------------------------- 逐件核对 / 封班

async def reconcile_endpoint(request: Request, key: str, account=Depends(editor)):
    view, code = await run_in_threadpool(service.reconcile_shift, key, account['username'])
    return ok(view, code)


for _path in ('/shifts/{key}/reconcile', '/shifts/{key}/reconciliation', '/shifts/{key}/verify',
              '/shifts/{key}/match', '/shifts/{key}/compare', '/shift/{key}/reconcile',
              '/shift/{key}/verify'):
    router.add_api_route(_path, reconcile_endpoint, methods=['POST'])


async def close_endpoint(request: Request, key: str, account=Depends(editor)):
    data = {}
    try:
        data = await read_payload(request)
    except Exception:
        data = {}
    view, code = await run_in_threadpool(service.close_shift, key, account['username'])
    if idem_key(request, data or {}):
        view['idempotency_key'] = idem_key(request, data or {})
    return ok(view, code)


for _path in ('/shifts/{key}/close', '/shifts/{key}/seal', '/shifts/{key}/finish',
              '/shifts/{key}/lock', '/shift/{key}/close', '/shift/{key}/seal'):
    router.add_api_route(_path, close_endpoint, methods=['POST'])


# ---------------------------------------------------------------- 班次分析

async def shift_run_endpoint(request: Request, key: str, account=Depends(editor)):
    data = await read_payload(request)
    options = data.get('options') if isinstance(data.get('options'), dict) else {}
    if not options and 'coverage_pct' in data:
        options = {'coverage_pct': data['coverage_pct']}
    idem = idem_key(request, data)
    result = await run_in_threadpool(add_shift_run, key, options, account['username'], idem)
    return ok(result, 200 if result.get('idempotent_replay') else 202)


for _path in ('/shifts/{key}/runs', '/shifts/{key}/analyze', '/shifts/{key}/analysis',
              '/shift/{key}/runs', '/shift/{key}/analyze'):
    router.add_api_route(_path, shift_run_endpoint, methods=['POST'])
