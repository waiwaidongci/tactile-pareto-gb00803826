"""按班次分开收交拒收单与复检单，并在两份原表到齐后逐件核对。

本模块只放不依赖数据库的纯逻辑，便于单测与复用：
- 单据类型（拒收单 / 复检单）的宽松识别；
- 单份原表的表内校验（不依赖另一份是否已到）；
- 两份原表按（批次, 单件）逐件核对，输出注明批次与单件的不符项。
"""
from datetime import date

from src.services.domain import (
    DEFECTS,
    DEFECT_CHECK,
    REGISTER_CODE,
    REGISTERED,
    SEVERITY_WEIGHTS,
)

REJECT = 'reject'
RECHECK = 'recheck'
KINDS = (REJECT, RECHECK)

KIND_LABELS = {REJECT: '拒收单', RECHECK: '复检单'}

# 班次状态：收单中（未齐）→ 核对不符 / 核对通过（可分析）→ 已封班（不可换内容）。
COLLECTING = 'collecting'
MISMATCH = 'mismatch'
READY = 'ready'
CLOSED = 'closed'

STATUS_LABELS = {
    COLLECTING: '收单中',
    MISMATCH: '核对不符',
    READY: '核对通过',
    CLOSED: '已封班',
}

# 不符项代码与说明。每条不符都带 lot_id / unit_id，便于注明批次和单件。
MISSING_REJECT = 'CHECK_WITHOUT_REJECT'
MISSING_RECHECK = 'REJECT_WITHOUT_CHECK'
DATE_ORDER = 'CHECK_BEFORE_REJECT'

MISMATCH_LABELS = {
    MISSING_REJECT: '复检单有该件，但拒收单查无此件',
    MISSING_RECHECK: '拒收单有该件，但复检单缺少逐件复检记录',
    DATE_ORDER: '复检日期早于拒收登记日期',
}

_KIND_TOKENS = {
    REJECT: ('reject', 'refus', '拒收', '登记'),
    RECHECK: ('recheck', 'reinspect', 'inspection', 'inspect', 'check', 'defect', '复检', '缺陷'),
}


def normalize_kind(value):
    """把各种可能的单据类型写法归一为 reject / recheck，无法识别返回 None。"""
    if value is None:
        return None
    text = str(value).strip().lower().replace('-', '_').replace(' ', '_')
    if not text:
        return None
    direct = {
        'reject': REJECT, 'rejects': REJECT, 'rejection': REJECT, 'reject_form': REJECT,
        'rejection_form': REJECT, 'reject_sheet': REJECT, '拒收': REJECT, '拒收单': REJECT,
        '拒收登记': REJECT, '拒收表': REJECT,
        'recheck': RECHECK, 'rechecks': RECHECK, 'recheck_form': RECHECK,
        'reinspection': RECHECK, 'reinspect': RECHECK, 'inspection': RECHECK, 'inspect': RECHECK,
        'check': RECHECK, 'checks': RECHECK, 'check_form': RECHECK, 'defect': RECHECK,
        'defects': RECHECK, 'defect_check': RECHECK, '复检': RECHECK, '复检单': RECHECK,
        '缺陷复检': RECHECK, '复检表': RECHECK,
        'all': 'auto', 'both': 'auto', 'auto': 'auto', 'merged': 'auto', 'combined': 'auto',
        'merge': 'auto', '全部': 'auto', '两份': 'auto',
    }
    if text in direct:
        return direct[text]
    compact = text.replace('_', '')
    if any(token in compact for token in ('merged', 'combined', 'both', 'all')):
        return 'auto'
    if '拒收' in compact or 'reject' in compact or 'refus' in compact:
        return REJECT
    if any(token in compact for token in ('recheck', 'reinspect', 'inspect', 'check', 'defect', '复检', '缺陷')):
        return RECHECK
    return None


def _unit(row, line):
    lot = row.get('lot_id')
    unit_id = row.get('unit_id')
    if not all(isinstance(value, str) and value.strip() for value in (lot, unit_id)):
        raise ValueError(f'第 {line} 行缺少批次或单件编号，请按实物标签补齐。')
    return str(lot).strip(), str(unit_id).strip()


def _day(row, line):
    day_text = row.get('inspection_date')
    try:
        day = date.fromisoformat(day_text)
        if day.isoformat() != day_text:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError(f'第 {line} 行日期无效，请填写 YYYY-MM-DD 格式的实际登记或复检日期。') from None
    return day


def read_form(rows):
    """校验并读取一份原表。

    只做表内校验（非法代码/严重度/日期/记录类型），不因为另一份原表未到而报错，
    从而保证“先到的单照收”。跨表核对交给 reconcile_forms。
    """
    if rows is None:
        return None
    registered = {}
    registrations = []
    checks = []
    type_counts = {REGISTERED: 0, DEFECT_CHECK: 0}
    for line, row in enumerate(rows, start=2):
        unit = _unit(row, line)
        day = _day(row, line)
        kind = row.get('record_type')
        code = row.get('defect_code')
        severity = row.get('severity')
        if kind == REGISTERED:
            if code != REGISTER_CODE or type(severity) is not int or severity != 0:
                raise ValueError(f'第 {line} 行拒收登记请填写缺陷代码 NONE、严重度 0。')
            registered[unit] = min(registered.get(unit, day), day)
            registrations.append((line, unit, day))
            type_counts[REGISTERED] += 1
        elif kind == DEFECT_CHECK:
            if not isinstance(code, str) or code not in DEFECTS:
                raise ValueError(f'第 {line} 行缺陷代码无效，请核对代码表（{"、".join(DEFECTS)}）。')
            if type(severity) is not int or severity not in SEVERITY_WEIGHTS:
                raise ValueError(f'第 {line} 行复检严重度无效，请填写 1 至 5 的整数。')
            checks.append({'line': line, 'unit': unit, 'day': day, 'code': code, 'severity': severity})
            type_counts[DEFECT_CHECK] += 1
        else:
            raise ValueError(f'第 {line} 行记录类型无效，请填写「{REGISTERED}」或「{DEFECT_CHECK}」。')
    detected = None
    if type_counts[REGISTERED] and not type_counts[DEFECT_CHECK]:
        detected = REJECT
    elif type_counts[DEFECT_CHECK] and not type_counts[REGISTERED]:
        detected = RECHECK
    return {
        'registered': registered,
        'registrations': registrations,
        'checks': checks,
        'type_counts': type_counts,
        'detected_kind': detected,
        'row_count': len(rows),
    }


def _mismatch(code, unit, message=None, **extra):
    lot_id, unit_id = unit
    item = {
        'code': code,
        'mismatch_type': code,
        'lot_id': lot_id,
        'unit_id': unit_id,
        'batch': lot_id,
        'unit': unit_id,
        '批次': lot_id,
        '单件': unit_id,
        'side': RECHECK if code == MISSING_REJECT else REJECT,
        'message': message or MISMATCH_LABELS[code],
        'label': MISMATCH_LABELS[code],
    }
    item.update(extra)
    return item


def reconcile_forms(reject_rows=None, recheck_rows=None):
    """逐件核对两份原表，返回是否通过、不符项和合并后的整班记录。"""
    reject = read_form(reject_rows) if reject_rows is not None else None
    recheck = read_form(recheck_rows) if recheck_rows is not None else None
    both_present = reject is not None and recheck is not None

    # 兼容混装原表：登记集合与复检集合都从两份表的全部行中归集。
    registered = {}
    checks = []
    source_rows = []
    for form_rows, ledger in ((reject_rows, reject), (recheck_rows, recheck)):
        source_rows.append(form_rows or [])
        if ledger is None:
            continue
        for unit, day in ledger['registered'].items():
            registered[unit] = min(registered.get(unit, day), day)
        checks.extend(ledger['checks'])

    checked_units = {item['unit'] for item in checks}
    mismatches = []

    if both_present:
        for item in checks:
            unit = item['unit']
            if unit not in registered:
                mismatches.append(_mismatch(MISSING_REJECT, unit, defect_code=item['code'], line=item['line']))
            elif item['day'] < registered[unit]:
                mismatches.append(_mismatch(
                    DATE_ORDER, unit, defect_code=item['code'], line=item['line'],
                    inspection_date=item['day'].isoformat(), registered_date=registered[unit].isoformat()))
        for unit in sorted(set(registered) - checked_units):
            mismatches.append(_mismatch(MISSING_RECHECK, unit, registered_date=registered[unit].isoformat()))

    # 去重（同一批次/单件/类型/缺陷可能由多行触发），保持稳定顺序。
    unique = {}
    for item in mismatches:
        key = (item['code'], item['lot_id'], item['unit_id'], item.get('defect_code'), item.get('line'))
        unique[key] = item
    mismatches = sorted(unique.values(), key=lambda m: (m['lot_id'], m['unit_id'], m['code'],
                                                        str(m.get('defect_code') or ''), m.get('line') or 0))

    merged = list(reject_rows or []) + list(recheck_rows or [])
    passed = both_present and not mismatches
    return {
        'complete': both_present,
        'passed': passed,
        'reconciled': passed,
        'mismatch_count': len(mismatches),
        'mismatches': mismatches,
        'registered_units': len(registered),
        'checked_units': len(checked_units & set(registered)),
        'check_rows': len(checks),
        'merged_rows': len(merged),
        'merged_records': merged if passed else [],
    }


def progress_text(status, forms_present):
    have = [KIND_LABELS[k] for k in KINDS if forms_present.get(k)]
    missing = [KIND_LABELS[k] for k in KINDS if not forms_present.get(k)]
    if status == CLOSED:
        return f"整班已封班，数据版本固定；{'、'.join(have)}均已收齐。"
    if status == READY:
        return '两份原表已收齐并逐件核对通过，整班数据可用于分析，可封班固定版本。'
    if status == MISMATCH:
        return '两份原表已到齐，但逐件核对存在对不上的批次/单件，请按不符项补交对应原表。'
    if len(have) == 1 and missing:
        return f"已收到{have[0]}（先到照收），尚待{missing[0]}；到齐后自动逐件核对。"
    return '尚未收到原表；拒收单与复检单可分开提交，先到的一份照收。'


def split_sample_rows(sample):
    """把演示样例按记录类型拆成拒收单、复检单两份原表（保持各自顺序）。"""
    reject_rows = [dict(row) for row in sample if row['record_type'] == REGISTERED]
    recheck_rows = [dict(row) for row in sample if row['record_type'] == DEFECT_CHECK]
    return {REJECT: reject_rows, RECHECK: recheck_rows}
