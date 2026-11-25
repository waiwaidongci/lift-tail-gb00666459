from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from src.constants.enums import Role
from src.services.reconcile_workflow import (
    attribution,
    create_reconciliation,
    get_reconciliation,
    global_attribution,
    import_csv,
    list_reconciliations,
    list_versions,
    retry_reconciliation,
)
from src.utils.security import allowed, signed_in

router = APIRouter()
analyst = allowed(Role.analyst.value)


def csv_response(content: str, filename: str) -> Response:
    return Response(content, media_type='text/csv; charset=utf-8',
                    headers={'content-disposition': "attachment; filename*=UTF-8''" + quote(filename)})


LIFT_EXAMPLE = (
    'record_id,elevator_id,started_at,stopped_at\n'
    'LQ-2401,B-HL01,2026-11-22 18:00,2026-11-22 18:24\n'
    'LQ-2402,B-HL02,2026-11-22 18:10,2026-11-22 18:35\n'
)
RECEIPT_EXAMPLE = (
    'task_id,elevator_id,started_at,duration_minutes,wait_minutes\n'
    'QS-2401,B-HL01,2026-11-22 17:56,38,8\n'
    'QS-2402,B-HL02,2026-11-22 18:05,42,12\n'
)


async def json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise ValueError('请求内容必须是 JSON 对象。')
    return body


@router.get('/reconcile/version-profile')
def version_profile(account=Depends(signed_in)):
    return {
        'lift': {
            'kind': 'lift',
            'name': '货梯启停记录',
            'aliases': ['record_id', 'elevator_id', 'started_at', 'stopped_at'],
            'example': LIFT_EXAMPLE,
        },
        'receipt': {
            'kind': 'receipt',
            'name': '搬运签收单',
            'aliases': ['task_id/receipt_id', 'elevator_id', 'started_at',
                        'duration_minutes', 'wait_minutes'],
            'interval_rule': '搬运区间=started_at+wait_minutes 至 started_at+duration_minutes',
            'example': RECEIPT_EXAMPLE,
        },
    }


@router.get('/reconcile/example/{kind}.csv')
def example_csv(kind: str, account=Depends(signed_in)):
    if kind in ('lift', 'elevator', '货梯启停'):
        return csv_response(LIFT_EXAMPLE, 'elevator-start-stop-example.csv')
    return csv_response(RECEIPT_EXAMPLE, 'handling-signoff-example.csv')


@router.get('/reconcile/versions')
def get_versions(kind: str | None = None, account=Depends(signed_in)):
    return list_versions(kind)


@router.post('/reconcile/versions', status_code=201)
async def create_version(request: Request, account=Depends(analyst)):
    body = await json_body(request)
    kind = body.get('kind') or request.query_params.get('kind')
    name = body.get('name') or body.get('filename') or ''
    if body.get('csv') is not None:
        return import_csv(str(kind), str(name), str(body['csv']), account['username'])
    records = body.get('records')
    if records is None and isinstance(body.get('rows'), list):
        records = body['rows']
    if records is None:
        raise ValueError('请提供 CSV 文本或 records 记录数组。')
    from src.services.reconcile_workflow import save_version
    return save_version(str(kind), str(name), records, account['username'])


@router.post('/reconcile/versions/{kind}', status_code=201)
async def create_named_version(kind: str, request: Request, account=Depends(analyst)):
    body = await json_body(request)
    body['kind'] = kind
    return _create_version_body(body, account)


def _create_version_body(body: dict, account) -> dict:
    kind = body.get('kind')
    name = body.get('name') or body.get('filename') or ''
    if body.get('csv') is not None:
        return import_csv(str(kind), str(name), str(body['csv']), account['username'])
    records = body.get('records') if body.get('records') is not None else body.get('rows')
    from src.services.reconcile_workflow import save_version
    return save_version(str(kind), str(name), records, account['username'])


@router.get('/reconciliations')
def reconciliation_list(account=Depends(signed_in)):
    return list_reconciliations()


@router.get('/reconcile/jobs')
def reconcile_job_list(account=Depends(signed_in)):
    return list_reconciliations()


@router.post('/reconciliations', status_code=201)
async def create_reconciliation_route(request: Request, response: Response,
                                     account=Depends(analyst)):
    body = await json_body(request)
    key = request.headers.get('idempotency-key') or request.headers.get('x-idempotency-key')
    job, created = create_reconciliation(body, account['username'], key)
    if not created:
        response.status_code = 200
    return {**job, 'created': created}


@router.post('/reconcile')
@router.post('/reconcile/run', status_code=201)
@router.post('/reconciliation', status_code=201)
@router.post('/reconciliation/run', status_code=201)
async def reconcile_alias(request: Request, response: Response, account=Depends(analyst)):
    body = await json_body(request)
    key = request.headers.get('idempotency-key') or request.headers.get('x-idempotency-key')
    job, created = create_reconciliation(body, account['username'], key)
    response.status_code = 201 if created else 200
    return {**job, 'created': created}


@router.post('/reconcile/jobs', status_code=201)
async def create_reconcile_job_alias(request: Request, response: Response,
                                     account=Depends(analyst)):
    return await reconcile_alias(request, response, account)


def _record_query(request: Request, record_no: str | None) -> str:
    query = request.query_params
    value = record_no or query.get('recordNo') or query.get('record_id') or query.get('recordId') \
        or query.get('q') or query.get('keyword') or query.get('number')
    value = (value or '').strip()
    if not value:
        raise ValueError('请提供要查询的记录号 record_no。')
    return value


def _job_query(request: Request, reconciliation_id: int | None) -> int | None:
    query = request.query_params
    value = reconciliation_id or query.get('reconciliationId') or query.get('reconcileId') \
        or query.get('jobId')
    if value in (None, ''):
        return None
    return int(value)


@router.get('/reconcile/lookup')
def lookup_global(request: Request, record_no: str | None = None,
                  reconciliation_id: int | None = None, account=Depends(signed_in)):
    record_value = _record_query(request, record_no)
    job_id = _job_query(request, reconciliation_id)
    return global_attribution(record_value, job_id)


@router.post('/reconcile/lookup')
async def lookup_global_post(request: Request, account=Depends(signed_in)):
    body = await json_body(request)
    record_value = body.get('record_no') or body.get('recordNo') or body.get('record_id')
    if not record_value:
        raise ValueError('请提供要查询的记录号 record_no。')
    job_id = body.get('reconciliation_id') or body.get('reconciliationId') or body.get('id')
    return global_attribution(str(record_value), int(job_id) if job_id else None)


@router.get('/reconcile/records/{record_no}')
def lookup_global_path(record_no: str, reconciliation_id: int | None = None,
                       account=Depends(signed_in)):
    return global_attribution(record_no, reconciliation_id)


@router.get('/reconciliations/{key}')
def reconciliation_detail(key: int, account=Depends(signed_in)):
    return get_reconciliation(key)


@router.get('/reconciliations/{key}/result')
def reconciliation_result(key: int, account=Depends(signed_in)):
    job = get_reconciliation(key)
    if not job.get('result'):
        raise HTTPException(409, job.get('error') or '对账结果尚未完成。')
    return job['result']


@router.get('/reconcile/jobs/{key}')
def reconcile_job_detail(key: int, account=Depends(signed_in)):
    return get_reconciliation(key)


@router.get('/reconcile/{key}')
def reconcile_short_detail(key: int, account=Depends(signed_in)):
    return get_reconciliation(key)


@router.get('/reconcile/{key}/result')
def reconcile_short_result(key: int, account=Depends(signed_in)):
    return reconciliation_result(key, account)


@router.post('/reconcile/retry')
@router.post('/reconciliations/retry')
async def retry_reconciliation_body(request: Request, account=Depends(analyst)):
    body = await json_body(request)
    key = body.get('id') or body.get('reconciliation_id') or body.get('reconcile_id') or body.get('job_id')
    if key is None:
        raise ValueError('请提供要重试的对账任务编号 id。')
    return retry_reconciliation(int(key), account['username'])


@router.post('/reconciliations/{key}/retry')
@router.post('/reconciliations/{key}/run')
@router.post('/reconciliations/{key}/resume')
def reconciliation_retry(key: int, account=Depends(analyst)):
    return retry_reconciliation(key, account['username'])


@router.post('/reconcile/jobs/{key}/retry')
def reconcile_job_retry(key: int, account=Depends(analyst)):
    return retry_reconciliation(key, account['username'])


@router.post('/reconcile/{key}/retry')
def reconcile_short_retry(key: int, account=Depends(analyst)):
    return retry_reconciliation(key, account['username'])


@router.get('/reconciliations/{key}/lookup')
def reconciliation_lookup(key: int, request: Request, record_no: str | None = None,
                          account=Depends(signed_in)):
    return attribution(key, _record_query(request, record_no))


@router.get('/reconciliations/{key}/records/{record_no}')
def reconciliation_lookup_path(key: int, record_no: str, account=Depends(signed_in)):
    return attribution(key, record_no)
