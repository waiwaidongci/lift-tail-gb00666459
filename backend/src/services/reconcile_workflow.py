from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import time
from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from src.constants.enums import State
from src.models.database import add_event, engine, fetch_one
from src.models.tables import datasets, reconciliation_versions, reconciliations
from src.services.reconciliation import lookup_record, reconcile

KIND_ALIASES = {
    'lift': 'lift',
    'lifts': 'lift',
    'elevator': 'lift',
    'elevators': 'lift',
    'start_stop': 'lift',
    'startstop': 'lift',
    '货梯': 'lift',
    '货梯启停': 'lift',
    '货梯启停记录': 'lift',
    'receipt': 'receipt',
    'receipts': 'receipt',
    'signoff': 'receipt',
    'sign_off': 'receipt',
    'sign': 'receipt',
    'handling': 'receipt',
    '搬运': 'receipt',
    '签收': 'receipt',
    '搬运签收单': 'receipt',
}


def kind_of(value: str | None) -> str:
    key = str(value or '').strip().lower().replace('-', '_').replace(' ', '_')
    if key not in KIND_ALIASES:
        raise ValueError('对账输入类型无效，请使用 lift（货梯启停）或 receipt（搬运签收单）。')
    return KIND_ALIASES[key]


def csv_rows(text: str) -> list[dict[str, str]]:
    if not isinstance(text, str) or not text.strip():
        raise ValueError('CSV 内容为空。')
    reader = csv.DictReader(io.StringIO(text.lstrip('﻿'), newline=''), strict=True)
    try:
        fieldnames = reader.fieldnames
    except csv.Error as error:
        raise ValueError('CSV 表头的引号格式有误。') from error
    if not fieldnames:
        raise ValueError('CSV 缺少表头。')
    rows: list[dict[str, str]] = []
    try:
        for row in reader:
            if None in row:
                raise ValueError(f'CSV 第 {reader.line_num} 行列数多于表头。')
            rows.append({(k or '').strip().lstrip('﻿'): (v.strip() if isinstance(v, str) else v)
                         for k, v in row.items()})
            if len(rows) > 5000:
                raise ValueError('单份对账输入最多 5000 行。')
    except csv.Error as error:
        raise ValueError('CSV 引号或字段格式有误。') from error
    if not rows:
        raise ValueError('CSV 需要至少一条记录。')
    return rows


def _checksum(records: list[dict[str, Any]]) -> str:
    payload = json.dumps(records, sort_keys=True, ensure_ascii=False,
                         separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def save_version(kind: str, name: str, records: list[dict[str, Any]], owner: str) -> dict[str, Any]:
    kind = kind_of(kind)
    if not name or not name.strip():
        name = '货梯启停记录' if kind == 'lift' else '搬运签收单'
    if not isinstance(records, list) or not records:
        raise ValueError('输入版本至少需要一条记录。')
    if len(records) > 5000:
        raise ValueError('单份对账输入最多 5000 行。')
    checksum = _checksum(records)
    now = time.time()
    key = None
    try:
        with engine.begin() as connection:
            existing = connection.execute(
                select(reconciliation_versions).where(
                    reconciliation_versions.c.kind == kind,
                    reconciliation_versions.c.checksum == checksum,
                )
            ).mappings().one_or_none()
            if existing:
                key = existing['id']
                created = False
            else:
                key = connection.execute(insert(reconciliation_versions).values(
                    kind=kind, name=name.strip()[:180], checksum=checksum, records=records,
                    owner=owner, created=now,
                )).inserted_primary_key[0]
                add_event(connection, owner, 'recon_import', key, now)
                created = True
    except IntegrityError:
        with engine.connect() as connection:
            key = connection.execute(
                select(reconciliation_versions.c.id).where(
                    reconciliation_versions.c.kind == kind,
                    reconciliation_versions.c.checksum == checksum,
                )
            ).scalar_one()
        created = False
    return {'id': key, 'created': created, 'kind': kind, 'checksum': checksum, 'rows': len(records)}


def import_csv(kind: str, name: str, text: str, owner: str) -> dict[str, Any]:
    normalized_kind = kind_of(kind)
    records = csv_rows(text)
    return save_version(normalized_kind, name, records, owner)


def list_versions(kind: str | None = None) -> list[dict[str, Any]]:
    statement = select(
        reconciliation_versions.c.id, reconciliation_versions.c.kind,
        reconciliation_versions.c.name, reconciliation_versions.c.checksum,
        reconciliation_versions.c.owner, reconciliation_versions.c.created,
    ).order_by(reconciliation_versions.c.id.desc()).limit(100)
    if kind:
        statement = statement.where(reconciliation_versions.c.kind == kind_of(kind))
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(statement).mappings()]


def _version_payload(version_id: int, kind: str) -> dict[str, Any]:
    with engine.connect() as connection:
        row = fetch_one(connection, reconciliation_versions, version_id)
    if row is None or row['kind'] != kind:
        raise HTTPException(404, f"{'货梯启停' if kind == 'lift' else '搬运签收'}输入版本不存在。")
    return dict(row)


def _dataset_payload(dataset_id: int, kind: str) -> dict[str, Any]:
    with engine.connect() as connection:
        row = fetch_one(connection, datasets, dataset_id)
    if row is None:
        raise HTTPException(404, '签收数据版本不存在。')
    # Existing analysis datasets are handling/sign-off ledgers.  They cannot be
    # elevator start/stop versions unless their records expose those fields.
    return {
        'id': row['id'], 'checksum': row['checksum'], 'records': row['records'],
        'name': row['name'], 'source': 'analysis_dataset', 'kind': kind,
    }


def _first(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in payload and payload[key] not in (None, ''):
            return payload[key]
    return None


def _resolve_side(payload: dict[str, Any], side: str, owner: str) -> dict[str, Any]:
    if side == 'lift':
        inline_keys = ('lift_records', 'elevator_records', 'start_stop_records',
                       'startstop_records', 'left_records', 'source_a_records',
                       'lift', 'elevator', 'left', 'source_a')
        csv_keys = ('lift_csv', 'elevator_csv', 'start_stop_csv', 'left_csv', 'source_a_csv')
        id_keys = ('lift_version_id', 'elevator_version_id', 'start_stop_version_id',
                   'startstop_version_id', 'left_version_id', 'source_a_version_id',
                   'lift_revision_id', 'elevator_revision_id', 'lift_dataset_version_id')
        dataset_keys = ('lift_dataset_id', 'elevator_dataset_id', 'left_dataset_id',
                        'source_a_dataset_id')
        default_name = 'API货梯启停记录'
    else:
        inline_keys = ('receipt_records', 'signoff_records', 'sign_records', 'handling_records',
                       'delivery_records', 'right_records', 'source_b_records',
                       'receipts', 'receipt', 'right', 'source_b')
        csv_keys = ('receipt_csv', 'signoff_csv', 'sign_csv', 'handling_csv',
                    'delivery_csv', 'right_csv', 'source_b_csv')
        id_keys = ('receipt_version_id', 'signoff_version_id', 'sign_version_id',
                   'handling_version_id', 'delivery_version_id', 'right_version_id',
                   'source_b_version_id', 'receipt_revision_id', 'sign_dataset_version_id')
        dataset_keys = ('receipt_dataset_id', 'signoff_dataset_id', 'sign_dataset_id',
                        'handling_dataset_id', 'right_dataset_id', 'source_b_dataset_id',
                        'dataset_id')
        default_name = 'API搬运签收单'

    inline = _first(payload, inline_keys)
    if isinstance(inline, dict):
        nested_id = _first(inline, ('version_id', 'id', 'reconciliation_version_id'))
        if nested_id is not None and _first(inline, ('records', 'rows', 'csv')) is None:
            return _version_payload(int(nested_id), side)
        if inline.get('csv') is not None:
            saved = import_csv(side, str(inline.get('name') or default_name), str(inline['csv']), owner)
            return _version_payload(saved['id'], side)
        inline = inline.get('records', inline.get('rows'))
    if isinstance(inline, (int, float)) and not isinstance(inline, bool):
        return _version_payload(int(inline), side)
    if isinstance(inline, str) and inline.strip().isdigit():
        return _version_payload(int(inline.strip()), side)
    if inline is not None:
        if isinstance(inline, str):
            inline = csv_rows(inline)
        if not isinstance(inline, list):
            raise ValueError(f'{side} 记录必须是数组、CSV 文本或版本编号。')
        name = _first(payload, (f'{side}_name', f'{side}_version_name', 'name')) or default_name
        saved = save_version(side, str(name), inline, owner)
        return _version_payload(saved['id'], side)

    csv_text = _first(payload, csv_keys)
    if csv_text is not None:
        name = _first(payload, (f'{side}_name', f'{side}_version_name', 'name')) or default_name
        saved = import_csv(side, str(name), str(csv_text), owner)
        return _version_payload(saved['id'], side)

    version_id = _first(payload, id_keys)
    if version_id is not None:
        return _version_payload(int(version_id), side)

    dataset_id = _first(payload, dataset_keys)
    if dataset_id is not None:
        return _dataset_payload(int(dataset_id), side)

    raise ValueError(f'请提供{ "货梯启停记录版本" if side == "lift" else "搬运签收单版本"}。')


def _public_job(row: Any) -> dict[str, Any]:
    data = dict(row)
    data.pop('inputs', None)
    data['status'] = data.get('state')
    return data


def create_reconciliation(payload: dict[str, Any], owner: str,
                          idempotency_key: str | None = None) -> tuple[dict[str, Any], bool]:
    lift = _resolve_side(payload, 'lift', owner)
    receipt = _resolve_side(payload, 'receipt', owner)
    key = (idempotency_key or _first(payload, ('idempotency_key', 'request_key', 'client_token')))
    key = str(key)[:100] if key else None
    now = time.time()

    try:
        with engine.begin() as connection:
            if key:
                existing = connection.execute(select(reconciliations).where(
                    reconciliations.c.idempotency_key == key)).mappings().one_or_none()
                if existing:
                    return _public_job(existing), False
            same_inputs = connection.execute(select(reconciliations).where(
                reconciliations.c.lift_checksum == lift['checksum'],
                reconciliations.c.receipt_checksum == receipt['checksum'],
            )).mappings().one_or_none()
            if same_inputs:
                return _public_job(same_inputs), False

            lock = {
                'lift': {'source': lift.get('source', 'reconciliation_version'), 'id': lift['id'],
                         'checksum': lift['checksum'], 'name': lift.get('name')},
                'receipt': {'source': receipt.get('source', 'reconciliation_version'), 'id': receipt['id'],
                            'checksum': receipt['checksum'], 'name': receipt.get('name')},
                'locked_at': now,
            }
            inputs = {'lift_records': lift['records'], 'receipt_records': receipt['records']}
            job_id = connection.execute(insert(reconciliations).values(
                lift_version_id=lift['id'] if lock['lift']['source'] == 'reconciliation_version' else None,
                receipt_version_id=receipt['id'] if lock['receipt']['source'] == 'reconciliation_version' else None,
                lift_checksum=lift['checksum'], receipt_checksum=receipt['checksum'],
                input_lock=lock, inputs=inputs, state=State.queued.value, owner=owner,
                created=now, attempts=0, idempotency_key=key,
            )).inserted_primary_key[0]
            add_event(connection, owner, 'reconcile_queue', job_id, now)
    except IntegrityError:
        with engine.connect() as connection:
            existing = None
            if key:
                existing = connection.execute(select(reconciliations).where(
                    reconciliations.c.idempotency_key == key)).mappings().one_or_none()
            if existing is None:
                existing = connection.execute(select(reconciliations).where(
                    reconciliations.c.lift_checksum == lift['checksum'],
                    reconciliations.c.receipt_checksum == receipt['checksum'],
                )).mappings().one()
        return _public_job(existing), False

    execute_reconciliation(job_id)
    return get_reconciliation(job_id), True


def list_reconciliations(limit: int = 50) -> list[dict[str, Any]]:
    fields = [c for c in reconciliations.c if c.name not in ('result', 'inputs')]
    with engine.connect() as connection:
        rows = connection.execute(select(*fields).order_by(reconciliations.c.id.desc()).limit(limit)).mappings()
        return [dict(row) for row in rows]


def get_reconciliation(key: int) -> dict[str, Any]:
    with engine.connect() as connection:
        row = fetch_one(connection, reconciliations, key)
    if row is None:
        raise HTTPException(404, '对账任务不存在。')
    return _public_job(row)


def claim_reconciliation() -> tuple[int, float, list[dict], list[dict]] | None:
    now = time.time()
    claimable = or_(
        reconciliations.c.state == State.queued.value,
        and_(reconciliations.c.state == State.running.value,
             reconciliations.c.started < now - 120),
    )
    with engine.begin() as connection:
        job = connection.execute(
            select(reconciliations).where(claimable).order_by(reconciliations.c.id).limit(1)
        ).mappings().one_or_none()
        if job is None:
            return None
        key = job['id']
        claim = connection.execute(update(reconciliations).where(
            reconciliations.c.id == key, claimable,
        ).values(state=State.running.value, started=now,
                 attempts=reconciliations.c.attempts + 1))
        if claim.rowcount != 1:
            return None
        inputs = job['inputs'] or {}
        return key, now, inputs['lift_records'], inputs['receipt_records']


def execute_reconciliation(key: int) -> dict[str, Any] | None:
    """Claim one queued job for the API process and calculate it immediately."""
    now = time.time()
    with engine.begin() as connection:
        job = fetch_one(connection, reconciliations, key)
        if job is None or job['state'] == State.completed.value:
            return _public_job(job) if job else None
        claim = connection.execute(update(reconciliations).where(
            reconciliations.c.id == key,
            reconciliations.c.state.in_([State.queued.value, State.failed.value]),
        ).values(state=State.running.value, started=now,
                 attempts=reconciliations.c.attempts + 1))
        if not claim.rowcount:
            return get_reconciliation(key)
        inputs = job['inputs']
        lift_records = inputs['lift_records']
        receipt_records = inputs['receipt_records']

    try:
        result = reconcile(lift_records, receipt_records)
    except ValueError as error:
        finish_reconciliation(key, now, None, str(error))
    except Exception:
        logging.exception('Reconciliation %s failed during API execution', key)
        finish_reconciliation(key, now, None, '对账计算失败，请核对两份记录后重试。')
    else:
        finish_reconciliation(key, now, result, None)
    return get_reconciliation(key)


def finish_reconciliation(key: int, claim: float, result: dict | None, error: str | None) -> None:
    now = time.time()
    state = State.failed.value if error else State.completed.value
    with engine.begin() as connection:
        saved = connection.execute(update(reconciliations).where(
            reconciliations.c.id == key,
            reconciliations.c.started == claim,
            reconciliations.c.state == State.running.value,
        ).values(state=state, result=result, summary=(result or {}).get('summary'),
                 error=error, finished=now))
        if saved.rowcount:
            add_event(connection, 'worker', f'reconcile_{state}', key, now)


def retry_reconciliation(key: int, actor: str, run_now: bool = True) -> dict[str, Any]:
    with engine.begin() as connection:
        job = fetch_one(connection, reconciliations, key)
        if job is None:
            raise HTTPException(404, '对账任务不存在。')
        if job['state'] == State.completed.value:
            return _public_job(job)
        now = time.time()
        # Any non-completed state can be continued.  The original input_lock and
        # inputs JSON are intentionally untouched.
        connection.execute(update(reconciliations).where(
            reconciliations.c.id == key,
            reconciliations.c.state != State.completed.value,
        ).values(state=State.queued.value, started=None, finished=None, error=None))
        add_event(connection, actor, 'reconcile_retry', key, now)
    if run_now:
        execute_reconciliation(key)
    return get_reconciliation(key)


def attribution(key: int, record_no: str) -> dict[str, Any]:
    job = get_reconciliation(key)
    if not job.get('result'):
        raise HTTPException(409, job.get('error') or '对账结果尚未完成，暂时不能按记录号查询归属。')
    record = lookup_record(job['result'], record_no)
    if record is None:
        raise HTTPException(404, '本次锁定的两份版本中没有这个记录号。')
    counterpart = None
    if record.get('counterpart_record_id'):
        counterpart = lookup_record(job['result'], record['counterpart_record_id'])
    lift_record = record if record['side'] == 'lift' else counterpart
    receipt_record = record if record['side'] == 'receipt' else counterpart
    candidate_attributions = []
    for pair in record.get('candidates', []):
        candidate_lift = lookup_record(job['result'], pair['lift_record_id'])
        candidate_receipt = lookup_record(job['result'], pair['receipt_record_id'])
        candidate_attributions.append({
            'pair': pair,
            'lift': candidate_lift,
            'receipt': candidate_receipt,
        })
    return {
        'reconciliation_id': key,
        'record_no': record_no.strip(),
        'side': record['side'],
        'status': record['status'],
        'record': record,
        'counterpart': counterpart,
        'lift_attribution': lift_record,
        'receipt_attribution': receipt_record,
        'lift': lift_record,
        'receipt': receipt_record,
        'lift_record': lift_record,
        'receipt_record': receipt_record,
        'candidate_attributions': candidate_attributions,
        'candidates': record.get('candidates', []),
        'input_lock': job['input_lock'],
    }


def global_attribution(record_no: str, reconciliation_id: int | None = None) -> dict[str, Any]:
    if reconciliation_id:
        return attribution(reconciliation_id, record_no)
    with engine.connect() as connection:
        jobs = connection.execute(select(reconciliations.c.id).where(
            reconciliations.c.state == State.completed.value,
        ).order_by(reconciliations.c.id.desc()).limit(100)).all()
    for (key,) in jobs:
        found = attribution_or_none(key, record_no)
        if found is not None:
            return found
    raise HTTPException(404, '最近的对账结果中没有这个记录号。可指定对账任务编号查询更早结果。')


def attribution_or_none(key: int, record_no: str) -> dict[str, Any] | None:
    try:
        return attribution(key, record_no)
    except HTTPException as error:
        if error.status_code in (404, 409):
            return None
        raise
