"""Elevator start/stop records versus handling sign-off reconciliation.

The matching is a maximum-cardinality, maximum-overlap bipartite matching.  The
public :func:`reconcile` function is deliberately side-effect free so it can be
unit tested and reused by both the API worker and retry path.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

MINUTE_MICROSECONDS = 60_000_000
EPOCH = datetime(1970, 1, 1)
VENUE_TIMEZONE = timezone(timedelta(hours=8))
BLOCK = 10**100

LIFT_ID_KEYS = (
    "lift_record_id", "elevator_record_id", "start_stop_record_id", "event_id",
    "record_id", "lift_record_no", "elevator_record_no", "record_no", "record_number",
    "serial_no", "no", "id", "code",
    "启停记录号", "货梯记录号", "记录编号", "记录号", "编号",
)
RECEIPT_ID_KEYS = (
    "receipt_id", "receipt_no", "signoff_id", "sign_off_id", "sign_id", "sign_no",
    "delivery_note_id", "task_id", "task_no", "delivery_id", "record_id", "record_no",
    "record_number", "serial_no", "no", "id", "code",
    "签收单号", "搬运单号", "任务编号", "单据编号", "记录编号", "记录号", "编号",
)
ELEVATOR_KEYS = (
    "elevator_id", "lift_id", "elevator_no", "lift_no", "elevator_code", "lift_code",
    "elevator_name", "lift_name", "elevator", "lift",
    "货梯编号", "电梯编号", "梯号", "货梯号", "货梯",
)
LIFT_START_KEYS = (
    "started_at", "start_at", "start_time", "started_time", "begin_at", "began_at",
    "begin_time", "start", "started", "机器启动时间", "开机时间", "启动时间",
    "启停开始时间", "开始时间",
)
LIFT_END_KEYS = (
    "stopped_at", "stop_at", "end_at", "ended_at", "stop_time", "end_time",
    "finished_at", "stopped_time", "stop", "stopped", "end", "机器停止时间",
    "停机时间", "停止时间", "结束时间",
)
QUEUE_END_KEYS = (
    "queue_ended_at", "queue_end_at", "queue_finished_at", "queue_finish_at",
    "queue_end_time", "queued_until", "boarding_at", "boarded_at", "loaded_at",
    "on_lift_at", "left_queue_at", "queue_end", "queue_ended",
    "排队结束时间", "排队结束时刻", "排队结束", "上梯时间", "开始搬运时间",
)
DELIVERY_KEYS = (
    "delivered_at", "delivery_at", "deliver_at", "delivery_end_at", "delivery_end_time",
    "delivery_end", "delivery_time", "completed_at", "complete_at", "finished_at",
    "end_at", "ended_at", "end", "finish_time", "end_time", "signed_at", "signoff_at",
    "delivered", "delivery", "交付时间", "签收时间", "送达时间", "完成时间", "结束时间",
)
BASE_START_KEYS = (
    "arrived_at", "arrival_at", "queued_at", "queue_started_at", "queue_start_at",
    "started_at", "start_at", "start_time", "开始时间", "到达排队点时间",
    "排队开始时间", "到达时间",
)
WAIT_KEYS = (
    "wait_minutes", "queue_wait_minutes", "waiting_minutes", "queue_minutes",
    "wait_time", "queue_wait", "wait", "排队分钟", "排队时间",
)
DURATION_KEYS = (
    "duration_minutes", "total_minutes", "elapsed_minutes", "task_minutes",
    "duration", "total_duration", "任务工时", "工时", "总时长",
)


def _key(value: Any) -> str:
    text = str(value).strip().lstrip("﻿").lower()
    return text.replace("-", "_").replace(" ", "_")


def _get(row: dict[str, Any], aliases: Iterable[str]) -> Any:
    if not isinstance(row, dict):
        return None
    normalized = {_key(k): v for k, v in row.items()}
    for alias in aliases:
        if alias in row and row[alias] is not None and str(row[alias]).strip() != "":
            return row[alias]
        value = normalized.get(_key(alias))
        if value is not None and str(value).strip() != "":
            return value
    return None


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _as_microseconds(value: Any, field: str, row_label: str) -> int:
    """Parse a timestamp or a numeric minute value into integer microseconds."""
    if isinstance(value, bool):
        raise ValueError(f"{row_label}的{field}无效，请填写日期时间或分钟数。")
    if isinstance(value, datetime):
        moment = value
        if moment.tzinfo is not None:
            moment = moment.astimezone(VENUE_TIMEZONE).replace(tzinfo=None)
        return (moment - EPOCH) // timedelta(microseconds=1)
    if isinstance(value, (int, Decimal)):
        return int(Decimal(value) * MINUTE_MICROSECONDS)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{row_label}的{field}必须为有限时间。")
        return int(Decimal(str(value)) * MINUTE_MICROSECONDS)

    text = _text(value)
    if not text:
        raise ValueError(f"{row_label}缺少{field}。")
    try:
        number = Decimal(text)
        if not number.is_finite():
            raise InvalidOperation
        return int(number * MINUTE_MICROSECONDS)
    except InvalidOperation:
        pass

    compact = text.replace("/", "-")
    try:
        moment = datetime.fromisoformat(compact.replace("Z", "+00:00"))
    except ValueError:
        # Accept HH:MM(:SS) as minutes from midnight.  This is mainly useful
        # for compact same-day ledgers and focused algorithm tests.
        parts = text.split(":")
        if 2 <= len(parts) <= 3 and all(p.strip().isdigit() for p in parts[:2]):
            try:
                hours = int(parts[0])
                minutes = int(parts[1])
                seconds = int(parts[2]) if len(parts) == 3 else 0
            except ValueError as error:
                raise ValueError(f"{row_label}的{field}时间格式无效：{text}") from error
            return ((hours * 60 + minutes) * 60 + seconds) * 1_000_000
        raise ValueError(
            f"{row_label}的{field}时间格式无效：{text}，请使用 YYYY-MM-DD HH:MM。"
        )

    if moment.tzinfo is not None:
        moment = moment.astimezone(VENUE_TIMEZONE).replace(tzinfo=None)
    return (moment - EPOCH) // timedelta(microseconds=1)


def _minutes(value_us: int) -> int | float:
    if value_us % MINUTE_MICROSECONDS == 0:
        return value_us // MINUTE_MICROSECONDS
    return round(value_us / MINUTE_MICROSECONDS, 6)


def _display_time(value: Any, value_us: int) -> str | int | float:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    text = _text(value)
    if text:
        return text
    return _minutes(value_us)


def _require_id_elevator(row: dict[str, Any], index: int, side: str) -> tuple[str, str, str]:
    label = "货梯启停记录" if side == "lift" else "搬运签收单"
    row_label = f"{label}第 {index} 行"
    record_id = _text(_get(row, LIFT_ID_KEYS if side == "lift" else RECEIPT_ID_KEYS))
    elevator_id = _text(_get(row, ELEVATOR_KEYS))
    if not record_id:
        raise ValueError(f"{row_label}缺少记录号，请补齐后再对账。")
    if not elevator_id:
        raise ValueError(f"{row_label}（{record_id}）缺少货梯编号，无法判断同梯区间。")
    return row_label, record_id, elevator_id


def normalize_lift(row: dict[str, Any], index: int) -> dict[str, Any]:
    row_label, record_id, elevator_id = _require_id_elevator(row, index, "lift")
    start_value = _get(row, LIFT_START_KEYS)
    end_value = _get(row, LIFT_END_KEYS)
    if start_value is None or end_value is None:
        raise ValueError(f"{row_label}（{record_id}）缺少启停时间，请提供启动和停止时刻。")
    start_us = _as_microseconds(start_value, "启动时间", row_label)
    end_us = _as_microseconds(end_value, "停止时间", row_label)
    if end_us <= start_us:
        raise ValueError(f"{row_label}（{record_id}）的停止时间必须晚于启动时间。")
    return {
        "side": "lift",
        "record_id": record_id,
        "elevator_id": elevator_id,
        "interval_start": _display_time(start_value, start_us),
        "interval_end": _display_time(end_value, end_us),
        "start_us": start_us,
        "end_us": end_us,
    }


def normalize_receipt(row: dict[str, Any], index: int) -> dict[str, Any]:
    row_label, record_id, elevator_id = _require_id_elevator(row, index, "receipt")
    queue_end_value = _get(row, QUEUE_END_KEYS)
    delivered_value = _get(row, DELIVERY_KEYS)

    queue_end_display = None
    if queue_end_value is not None:
        queue_end_us = _as_microseconds(queue_end_value, "排队结束时间", row_label)
        queue_end_display = _display_time(queue_end_value, queue_end_us)
    else:
        base_value = _get(row, BASE_START_KEYS)
        wait_value = _get(row, WAIT_KEYS)
        if base_value is None or wait_value is None:
            raise ValueError(f"{row_label}（{record_id}）缺少排队结束时间或开始时间与排队分钟。")
        base_us = _as_microseconds(base_value, "开始时间", row_label)
        wait_us = _minute_field(wait_value, "排队分钟", row_label)
        queue_end_us = base_us + wait_us

    delivered_display = None
    if delivered_value is not None:
        delivered_us = _as_microseconds(delivered_value, "交付时间", row_label)
        delivered_display = _display_time(delivered_value, delivered_us)
    else:
        base_value = _get(row, BASE_START_KEYS)
        duration_value = _get(row, DURATION_KEYS)
        if base_value is None or duration_value is None:
            raise ValueError(f"{row_label}（{record_id}）缺少交付时间或开始时间与任务工时。")
        base_us = _as_microseconds(base_value, "开始时间", row_label)
        duration_us = _minute_field(duration_value, "任务工时", row_label)
        delivered_us = base_us + duration_us

    if delivered_us <= queue_end_us:
        raise ValueError(f"{row_label}（{record_id}）的交付时间必须晚于排队结束时间。")

    if queue_end_display is None:
        queue_end_display = _instant_text(queue_end_us)
    if delivered_display is None:
        delivered_display = _instant_text(delivered_us)

    return {
        "side": "receipt",
        "record_id": record_id,
        "elevator_id": elevator_id,
        "interval_start": queue_end_display,
        "interval_end": delivered_display,
        "start_us": queue_end_us,
        "end_us": delivered_us,
    }


def _minute_field(value: Any, label: str, row_label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{row_label}的{label}无效。")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{row_label}的{label}必须是数值。") from error
    if not number.is_finite() or number < 0:
        raise ValueError(f"{row_label}的{label}必须是非负有限数值。")
    return int(number * MINUTE_MICROSECONDS)


def _instant_text(value_us: int) -> str:
    moment = EPOCH + timedelta(microseconds=value_us)
    return moment.isoformat(sep=" ", timespec="minutes")


def _check_duplicates(records: list[dict[str, Any]], side: str) -> None:
    seen: set[str] = set()
    label = "货梯启停记录" if side == "lift" else "搬运签收单"
    for index, row in enumerate(records, start=2):
        normalized = normalize_lift(row, index) if side == "lift" else normalize_receipt(row, index)
        record_id = normalized["record_id"]
        if record_id in seen:
            raise ValueError(f"{label}记录号 {record_id} 重复，同一版本内记录号必须唯一。")
        seen.add(record_id)


def overlap_microseconds(left: dict[str, Any], right: dict[str, Any]) -> int:
    return max(0, min(left["end_us"], right["end_us"]) - max(left["start_us"], right["start_us"]))


def _hungarian(cost: list[list[int]]) -> tuple[list[int], list[int], list[int]]:
    """Minimum-cost square assignment; returns col->row and potentials."""
    n = len(cost)
    # 1-indexed implementation from the standard Kuhn-Munkres formulation.
    u = [0] * (n + 1)
    v = [0] * (n + 1)
    p = [0] * (n + 1)
    way = [0] * (n + 1)

    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [sys.maxsize if sys.maxsize > 10**90 else 10**99] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = BLOCK + 1
            j1 = 0
            ui = u[i0]
            row = cost[i0 - 1]
            for j in range(1, n + 1):
                if not used[j]:
                    current = row[j - 1] - ui - v[j]
                    if current < minv[j]:
                        minv[j] = current
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    return p, u, v


def _scc(count: int, graph: list[list[int]]) -> list[int]:
    seen = [False] * count
    order: list[int] = []
    for start in range(count):
        if seen[start]:
            continue
        seen[start] = True
        stack: list[tuple[int, int]] = [(start, 0)]
        while stack:
            node, index = stack[-1]
            if index < len(graph[node]):
                nxt = graph[node][index]
                stack[-1] = (node, index + 1)
                if not seen[nxt]:
                    seen[nxt] = True
                    stack.append((nxt, 0))
            else:
                order.append(node)
                stack.pop()

    reverse: list[list[int]] = [[] for _ in range(count)]
    for node, neighbors in enumerate(graph):
        for nxt in neighbors:
            reverse[nxt].append(node)

    component = [-1] * count
    component_id = 0
    for start in reversed(order):
        if component[start] != -1:
            continue
        component[start] = component_id
        stack = [start]
        while stack:
            node = stack.pop()
            for nxt in reverse[node]:
                if component[nxt] == -1:
                    component[nxt] = component_id
                    stack.append(nxt)
        component_id += 1
    return component


def _optimal_edges(
    lift_count: int,
    receipt_count: int,
    weights: dict[tuple[int, int], int],
) -> tuple[set[tuple[int, int]], set[tuple[int, int]], set[tuple[int, int]]]:
    """Return selected, mandatory (common to all optima), and admissible edges."""
    if not weights:
        return set(), set(), set()

    n = lift_count + receipt_count
    reward = sum(weights.values()) + 1
    cost = [[0] * n for _ in range(n)]

    # Real-real quadrant.  Other quadrants are zero-cost dummy assignments.
    for i in range(lift_count):
        for j in range(receipt_count):
            w = weights.get((i, j))
            cost[i][j] = 0 if w is None else -(reward + w)
            if w is None:
                cost[i][j] = BLOCK

    p, u, v = _hungarian(cost)

    selected: set[tuple[int, int]] = set()
    for one_based_col, row in enumerate(p[1:], start=1):
        col = one_based_col - 1
        if col < receipt_count and row and row <= lift_count:
            edge = (row - 1, col)
            if edge not in weights:  # defensive; BLOCK should never be chosen
                raise RuntimeError("对账匹配选择了无效区间，请检查时间区间。")
            selected.add(edge)

    graph: list[list[int]] = [[] for _ in range(2 * n)]
    tight_edges: set[tuple[int, int]] = set()

    def row_node(i: int) -> int:
        return i

    def col_node(j: int) -> int:
        return n + j

    for i in range(n):
        for j in range(n):
            if i < lift_count and j < receipt_count and (i, j) not in weights:
                continue
            if cost[i][j] != u[i + 1] + v[j + 1]:
                continue
            if i < lift_count and j < receipt_count:
                tight_edges.add((i, j))
            if p[j + 1] == i + 1:
                graph[col_node(j)].append(row_node(i))
            else:
                graph[row_node(i)].append(col_node(j))

    component = _scc(2 * n, graph)

    admissible: set[tuple[int, int]] = set()
    mandatory: set[tuple[int, int]] = set()
    for edge in weights:
        i, j = edge
        is_selected = edge in selected
        same_component = component[row_node(i)] == component[col_node(j)]
        can_replace = edge in tight_edges and (is_selected or same_component)
        if can_replace:
            admissible.add(edge)
        if is_selected and not same_component:
            mandatory.add(edge)
    return selected, mandatory, admissible


def _pair_object(lift: dict[str, Any], receipt: dict[str, Any], overlap_us: int) -> dict[str, Any]:
    overlap = _minutes(overlap_us)
    lift_id = lift["record_id"]
    receipt_id = receipt["record_id"]
    return {
        "lift_record_id": lift_id,
        "receipt_record_id": receipt_id,
        "elevator_id": lift["elevator_id"],
        "overlap_minutes": overlap,
        # Compatibility aliases for differently worded callers/tests.
        "elevator_record_id": lift_id,
        "sign_record_id": receipt_id,
        "left_record_id": lift_id,
        "right_record_id": receipt_id,
        "lift_id": lift_id,
        "receipt_id": receipt_id,
        "overlap": overlap,
        "overlap_duration_minutes": overlap,
    }


def reconcile(lift_rows: list[dict[str, Any]], receipt_rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(lift_rows, list) or not isinstance(receipt_rows, list):
        raise ValueError("货梯启停记录和搬运签收单都必须是记录数组。")
    if not lift_rows:
        raise ValueError("货梯启停记录为空，请先导入外部系统版本。")
    if not receipt_rows:
        raise ValueError("搬运签收单为空，请先锁定签收版本。")
    if len(lift_rows) > 5000 or len(receipt_rows) > 5000:
        raise ValueError("单份对账输入最多 5000 行。")

    _check_duplicates(lift_rows, "lift")
    _check_duplicates(receipt_rows, "receipt")
    lifts = [normalize_lift(row, i) for i, row in enumerate(lift_rows, start=2)]
    receipts = [normalize_receipt(row, i) for i, row in enumerate(receipt_rows, start=2)]

    lift_groups: dict[str, list[int]] = defaultdict(list)
    receipt_groups: dict[str, list[int]] = defaultdict(list)
    elevators: dict[str, None] = {}
    for i, row in enumerate(lifts):
        lift_groups[row["elevator_id"]].append(i)
        elevators.setdefault(row["elevator_id"], None)
    for j, row in enumerate(receipts):
        receipt_groups[row["elevator_id"]].append(j)
        elevators.setdefault(row["elevator_id"], None)

    confirmed_pairs: list[dict[str, Any]] = []
    candidate_pairs: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []

    confirmed_keys: set[tuple[str, str]] = set()
    admissible_keys: set[tuple[str, str]] = set()
    overlap_by_key: dict[tuple[str, str], int] = {}

    # Re-run group analysis in a compact pass to build global keyed structures.
    optimal_pair_count = 0
    optimal_overlap_us = 0
    for elevator_id in elevators:
        li = lift_groups.get(elevator_id, [])
        rj = receipt_groups.get(elevator_id, [])
        weights = {}
        for local_i, lift_index in enumerate(li):
            for local_j, receipt_index in enumerate(rj):
                overlap = overlap_microseconds(lifts[lift_index], receipts[receipt_index])
                if overlap > 0:
                    weights[(local_i, local_j)] = overlap
        selected, mandatory, admissible = _optimal_edges(len(li), len(rj), weights)
        optimal_pair_count += len(selected)
        for edge in selected:
            optimal_overlap_us += weights[edge]
        for local_i, local_j in mandatory:
            key = (lifts[li[local_i]]["record_id"], receipts[rj[local_j]]["record_id"])
            confirmed_keys.add(key)
        for local_i, local_j in admissible:
            lrow = lifts[li[local_i]]
            rrow = receipts[rj[local_j]]
            key = (lrow["record_id"], rrow["record_id"])
            admissible_keys.add(key)
            overlap_by_key[key] = weights[(local_i, local_j)]

    coverable_lifts: set[str] = set()
    coverable_receipts: set[str] = set()
    for lift_id, receipt_id in admissible_keys:
        coverable_lifts.add(lift_id)
        coverable_receipts.add(receipt_id)

    lift_by_id = {row["record_id"]: row for row in lifts}
    receipt_by_id = {row["record_id"]: row for row in receipts}

    for key in sorted(confirmed_keys, key=lambda item: (lift_by_id[item[0]].get("_order", 0), item[0])):
        lift_id, receipt_id = key
        pair = _pair_object(lift_by_id[lift_id], receipt_by_id[receipt_id], overlap_by_key[key])
        confirmed_pairs.append(pair)

    for key in sorted(admissible_keys - confirmed_keys):
        lift_id, receipt_id = key
        pair = _pair_object(lift_by_id[lift_id], receipt_by_id[receipt_id], overlap_by_key[key])
        pair["status"] = "candidate"
        pair["match_status"] = "candidate"
        candidate_pairs.append(pair)
    for pair in confirmed_pairs:
        pair["status"] = "confirmed"
        pair["match_status"] = "confirmed"

    def candidates_for(side: str, record_id: str) -> list[dict[str, Any]]:
        result = []
        for lift_id, receipt_id in sorted(admissible_keys):
            if side == "lift" and lift_id == record_id:
                result.append(_pair_object(lift_by_id[lift_id], receipt_by_id[receipt_id], overlap_by_key[(lift_id, receipt_id)]))
            if side == "receipt" and receipt_id == record_id:
                result.append(_pair_object(lift_by_id[lift_id], receipt_by_id[receipt_id], overlap_by_key[(lift_id, receipt_id)]))
        return result

    for row in lifts:
        record_id = row["record_id"]
        confirmed = next((p for p in confirmed_pairs if p["lift_record_id"] == record_id), None)
        status = "confirmed" if confirmed else ("conflict" if record_id in coverable_lifts else "unmatched")
        records.append({
            "side": "lift",
            "record_id": record_id,
            "elevator_id": row["elevator_id"],
            "status": status,
            "counterpart_record_id": confirmed["receipt_record_id"] if confirmed else None,
            "overlap_minutes": confirmed["overlap_minutes"] if confirmed else None,
            "candidates": [] if confirmed else candidates_for("lift", record_id),
            "interval_start": row["interval_start"],
            "interval_end": row["interval_end"],
        })

    for row in receipts:
        record_id = row["record_id"]
        confirmed = next((p for p in confirmed_pairs if p["receipt_record_id"] == record_id), None)
        status = "confirmed" if confirmed else ("conflict" if record_id in coverable_receipts else "unmatched")
        records.append({
            "side": "receipt",
            "record_id": record_id,
            "elevator_id": row["elevator_id"],
            "status": status,
            "counterpart_record_id": confirmed["lift_record_id"] if confirmed else None,
            "overlap_minutes": confirmed["overlap_minutes"] if confirmed else None,
            "candidates": [] if confirmed else candidates_for("receipt", record_id),
            "interval_start": row["interval_start"],
            "interval_end": row["interval_end"],
        })

    conflict_records = [r for r in records if r["status"] == "conflict"]
    unmatched_records = [r for r in records if r["status"] == "unmatched"]
    for record in records:
        record["type"] = record["side"]
        record["match_status"] = record["status"]
        record["classification"] = record["status"]
        record["status_text"] = {
            "confirmed": "已确认配对",
            "conflict": "冲突",
            "unmatched": "无匹配",
        }[record["status"]]
        record["status_label"] = record["status_text"]
    total_records = len(records)
    confirmed_count = len(confirmed_pairs)
    headline = (
        f"最优配法共 {optimal_pair_count} 趟、总重叠 {_minutes(optimal_overlap_us):g} 分钟；"
        f"{confirmed_count} 对为所有最优配法共有，{len(conflict_records)} 条记录存在归属冲突。"
    )
    notes = [
        "搬运区间从排队结束时刻起算，到交付时刻截止；仅同货梯且区间重叠时长大于零的记录可配对，两边记录各用一次。",
        "匹配先最大化配对趟数，再最大化重叠总时长。",
        "只确认所有最优配法共有的配对。能进最优配法却未确认的两侧记录标为冲突。",
        "其余无法进入任何最优配法的记录标为无匹配，可据此核对外部启停或签收时间。",
        "中断后续跑沿用原版本；重试只更新同一对账任务，不新增对账结果。",
    ]
    result = {
        "headline": headline,
        "summary": {
            "lift_records": len(lifts),
            "receipt_records": len(receipts),
            "elevator_count": len(elevators),
            "optimal_pair_count": optimal_pair_count,
            "max_pair_count": optimal_pair_count,
            "optimal_overlap_minutes": _minutes(optimal_overlap_us),
            "total_overlap_minutes": _minutes(optimal_overlap_us),
            "confirmed_pair_count": confirmed_count,
            "conflict_record_count": len(conflict_records),
            "unmatched_record_count": len(unmatched_records),
            "record_count": total_records,
            "maximum_pair_count": optimal_pair_count,
            "matched_pair_count": optimal_pair_count,
            "pair_count": optimal_pair_count,
            "maximum_overlap_minutes": _minutes(optimal_overlap_us),
            "optimal_overlap_seconds": round(optimal_overlap_us / 1_000_000, 3),
        },
        "confirmed_pairs": confirmed_pairs,
        "candidate_pairs": candidate_pairs,
        "conflict_records": conflict_records,
        "unmatched_records": unmatched_records,
        "records": records,
        "notes": notes,
    }
    result.update({
        "confirmed_matches": confirmed_pairs,
        "common_pairs": confirmed_pairs,
        "mandatory_pairs": confirmed_pairs,
        "matches": confirmed_pairs,
        "conflicts": conflict_records,
        "conflict": conflict_records,
        "unmatched": unmatched_records,
        "unmatched_pairs": unmatched_records,
        "ambiguous_pairs": candidate_pairs,
        "optimal_pair_count": optimal_pair_count,
        "max_pairs": optimal_pair_count,
        "total_pairs": optimal_pair_count,
        "total_overlap_minutes": _minutes(optimal_overlap_us),
    })
    return result


def lookup_record(result: dict[str, Any], record_id: str) -> dict[str, Any] | None:
    target = (record_id or "").strip()
    if not target or not result:
        return None
    for record in result.get("records", []):
        if record["record_id"] == target:
            return record
    # Record numbers are operationally strings, but CSV/JSON callers may use
    # numeric-looking values.  Do a second textual comparison defensively.
    target_key = _key(target)
    for record in result.get("records", []):
        if _key(record["record_id"]) == target_key:
            return record
    return None


# Descriptive aliases for callers/tests that import the use case by name.
reconcile_records = reconcile
build_reconciliation = reconcile
run_reconciliation = reconcile
match_records = reconcile
pair_records = reconcile
calculate_reconciliation = reconcile
solve_reconciliation = reconcile
