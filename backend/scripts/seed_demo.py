import json

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from src.constants.enums import Role
from src.models.database import engine
from src.models.tables import datasets, users
from src.services.data import PROFILE, convert, save_revision
from src.services.domain import sample_rows
from src.services.reconcile_workflow import create_reconciliation, save_version
from src.utils.security import protect


def seed():
    for role in Role:
        try:
            with engine.begin() as connection:
                if not connection.scalar(select(users.c.username).where(users.c.username == role.value)):
                    connection.execute(insert(users).values(username=role.value, role=role.value,
                                                            password=protect(f'{role.value}-demo-24')))
        except IntegrityError:
            with engine.connect() as connection:
                if not connection.scalar(select(users.c.username).where(users.c.username == role.value)):
                    raise
    records = [{field['key']: convert(row[field['key']], field) for field in PROFILE['columns']} for row in sample_rows()]
    revision, created = save_revision(PROFILE['filename'], records, 'seed')

    lift_rows = [
        {'record_id': 'DEMO-L-001', 'elevator_id': 'B-HL01',
         'started_at': '2026-11-22 18:00', 'stopped_at': '2026-11-22 18:30'},
        {'record_id': 'DEMO-L-002', 'elevator_id': 'B-HL02',
         'started_at': '2026-11-22 19:00', 'stopped_at': '2026-11-22 20:00'},
        {'record_id': 'DEMO-L-003', 'elevator_id': 'B-HL03',
         'started_at': '2026-11-22 21:00', 'stopped_at': '2026-11-22 21:10'},
    ]
    receipt_rows = [
        {'record_id': 'DEMO-R-001', 'elevator_id': 'B-HL01',
         'started_at': '2026-11-22 17:55', 'wait_minutes': 10, 'duration_minutes': 40},
        {'record_id': 'DEMO-R-002', 'elevator_id': 'B-HL02',
         'started_at': '2026-11-22 18:50', 'wait_minutes': 10, 'duration_minutes': 40},
        {'record_id': 'DEMO-R-003', 'elevator_id': 'B-HL02',
         'started_at': '2026-11-22 18:55', 'wait_minutes': 10, 'duration_minutes': 40},
    ]
    lift_version = save_version('lift', 'demo-elevator-start-stop.csv', lift_rows, 'seed')
    receipt_version = save_version('receipt', 'demo-handling-signoff.csv', receipt_rows, 'seed')
    reconciliation, _ = create_reconciliation({
        'lift_version_id': lift_version['id'],
        'receipt_version_id': receipt_version['id'],
    }, 'seed')

    with engine.connect() as connection:
        counts = {table.name: connection.scalar(select(func.count()).select_from(table)) for table in (users, datasets)}
    return {
        'revision': revision,
        'created': created,
        **counts,
        'rows': len(records),
        'reconciliation_version_ids': [lift_version['id'], receipt_version['id']],
        'reconciliation_id': reconciliation['id'],
    }


if __name__ == '__main__':
    print(json.dumps(seed(), ensure_ascii=False))
