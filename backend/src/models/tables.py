from sqlalchemy import JSON, Column, Double, Integer, MetaData, String, Table, Text, UniqueConstraint

metadata = MetaData()
users = Table('workspace_users', metadata, Column('username', String(40), primary_key=True),
              Column('role', String(20), nullable=False), Column('password', String(160), nullable=False))
datasets = Table('data_revisions', metadata, Column('id', Integer, primary_key=True),
                 Column('name', String(180), nullable=False), Column('checksum', String(64), nullable=False, unique=True),
                 Column('records', JSON, nullable=False), Column('owner', String(40), nullable=False),
                 Column('created', Double, nullable=False))
runs = Table('analysis_runs', metadata, Column('id', Integer, primary_key=True),
             Column('dataset_id', Integer, nullable=False, index=True), Column('options', JSON, nullable=False),
             Column('state', String(16), nullable=False, index=True), Column('owner', String(40), nullable=False),
             Column('created', Double, nullable=False), Column('started', Double), Column('finished', Double),
             Column('result', JSON), Column('error', Text), Column('decision', String(16)),
             Column('reviewer', String(40)), Column('review_note', Text), Column('reviewed', Double))
schedules = Table('report_schedules', metadata, Column('id', Integer, primary_key=True),
                  Column('run_id', Integer, nullable=False), Column('recipient', String(100), nullable=False),
                  Column('cadence', String(16), nullable=False), Column('next_at', Double, nullable=False, index=True),
                  Column('active', Integer, nullable=False), Column('owner', String(40), nullable=False))
deliveries = Table('report_deliveries', metadata, Column('id', Integer, primary_key=True),
                   Column('schedule_id', Integer, nullable=False), Column('run_id', Integer, nullable=False),
                   Column('recipient', String(100), nullable=False), Column('scheduled', Double, nullable=False),
                   Column('delivered', Double, nullable=False), UniqueConstraint('schedule_id', 'scheduled'))
audit = Table('operation_audit', metadata, Column('id', Integer, primary_key=True),
              Column('at', Double, nullable=False), Column('actor', String(40), nullable=False),
              Column('action', String(30), nullable=False), Column('subject', Integer, nullable=False))

# 按班次分开收交的两份原表（拒收单 / 复检单）及其核对、封班状态。
shifts = Table('shift_batches', metadata, Column('id', Integer, primary_key=True),
               Column('code', String(80), nullable=False, unique=True, index=True),
               Column('name', String(180)), Column('status', String(16), nullable=False, index=True),
               Column('dataset_id', Integer, index=True), Column('version', Integer, nullable=False, default=0),
               Column('reject_form_id', Integer), Column('recheck_form_id', Integer),
               Column('reject_checksum', String(64)), Column('recheck_checksum', String(64)),
               Column('pair_checksum', String(64)), Column('mismatch_count', Integer, nullable=False, default=0),
               Column('reconciliation', JSON), Column('owner', String(40)),
               Column('created', Double, nullable=False), Column('updated', Double, nullable=False),
               Column('closed_at', Double), Column('closed_by', String(40)))

# 每次交单/补交都作为不可变原表留存；shifts 上的 *_form_id 指向当前采用的那份。
shift_forms = Table('shift_forms', metadata, Column('id', Integer, primary_key=True),
                    Column('shift_id', Integer, nullable=False, index=True),
                    Column('kind', String(16), nullable=False, index=True),
                    Column('dataset_id', Integer, index=True), Column('filename', String(240)),
                    Column('checksum', String(64), nullable=False, index=True),
                    Column('records', JSON, nullable=False), Column('row_count', Integer, nullable=False),
                    Column('revision', Integer, nullable=False, default=1),
                    Column('owner', String(40), nullable=False), Column('created', Double, nullable=False))

# 可选的幂等键：入库/提交计算中断后用同一键重试，返回首次结果而不是新增版本。
idempotency_keys = Table('idempotency_keys', metadata, Column('key', String(120), primary_key=True),
                         Column('actor', String(40), nullable=False), Column('scope', String(40), nullable=False),
                         Column('request_hash', String(64), nullable=False), Column('subject_id', Integer),
                         Column('payload', JSON), Column('status_code', Integer),
                         Column('created', Double, nullable=False))
