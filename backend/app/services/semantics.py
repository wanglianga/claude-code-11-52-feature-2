"""表计数据语义契约编译器与兼容转换治理。

治理链路：
  契约登记（SemanticContract，带有效期）
    → 契约发布锁定（ContractRelease，不可变解释版本）
      → 原始受理（RawReading，不可变原载荷）
        → 编译器：证据完整 → StandardIntervalFact（标准区间事实，含逐步换算）
                  无法证明语义 → QuarantineRecord（待核隔离，不填零，阻断确认版）

核心不变量：
- 量纲：功率（kW）不得直接与电量（kWh）相加；跨量纲只能在“瞬时功率 + 可积分
  代表时段”下换算为电量，否则隔离。
- 时间语义：累计值只能在可确认的连续读数间求差；区间读数不再二次累计；瞬时功率
  仅在读数间隔支持积分时换算。
- 单位换算必须有明确因子与方向；有效期交界处窗口按边界拆分，不用当前配置解释整天。
- 来源声明缺失、契约同期冲突、负向符号无可验证回送含义、间隔不支持积分 → 待核。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import (
    COMPILE_QUARANTINED,
    COMPILE_STANDARD,
    ContractRelease,
    Meter,
    Q_BRIDGE_GAP,
    Q_CONTRACT_CONFLICT,
    Q_CUMULATIVE_BASELINE,
    Q_CUMULATIVE_RESET,
    Q_DIMENSION,
    Q_INSTANT_INTERVAL,
    Q_INTERVAL_REACCUM,
    Q_MULTIPLIER,
    Q_NO_CONTRACT,
    Q_NUMERIC,
    Q_SIGN_FEEDBACK,
    Q_UNIT_FACTOR,
    QuarantineRecord,
    RawReading,
    SEMANTIC_CUMULATIVE,
    SEMANTIC_INSTANT,
    SEMANTIC_INTERVAL,
    SEMANTIC_VALUES,
    SemanticContract,
    Source,
    StandardIntervalFact,
)
from ..timeutil import iso, utcnow

# ----------------------------------------------------------------- 单位与量纲

# 每个单位登记：所属量纲 + 折算到该量纲标准单位的因子（含方向：standard = raw * factor）
UNIT_REGISTRY: dict[str, dict[str, Any]] = {
    "kWh": {"dimension": "energy", "factor": 1.0, "name": "千瓦时"},
    "Wh": {"dimension": "energy", "factor": 0.001, "name": "瓦时"},
    "kW": {"dimension": "power", "factor": 1.0, "name": "千瓦"},
    "W": {"dimension": "power", "factor": 0.001, "name": "瓦"},
    "m³": {"dimension": "volume", "factor": 1.0, "name": "立方米"},
    "m3": {"dimension": "volume", "factor": 1.0, "name": "立方米"},
    "L": {"dimension": "volume", "factor": 0.001, "name": "升"},
    "l": {"dimension": "volume", "factor": 0.001, "name": "升"},
    "t": {"dimension": "mass", "factor": 1.0, "name": "吨"},
    "kg": {"dimension": "mass", "factor": 0.001, "name": "千克"},
}

# 各物理量（来源产品字段）要求的标准量纲与标准单位
STANDARD_DIMENSION: dict[str, dict[str, str]] = {
    "electricity": {"dimension": "energy", "unit": "kWh"},
    "pv": {"dimension": "energy", "unit": "kWh"},
    "water": {"dimension": "volume", "unit": "m³"},
    "steam": {"dimension": "mass", "unit": "t"},
}


def _norm_unit(unit: str | None) -> str:
    return (unit or "").strip()


def unit_info(unit: str) -> dict[str, Any] | None:
    return UNIT_REGISTRY.get(_norm_unit(unit))


# ----------------------------------------------------------------- 契约登记


def register_contract(
    db: Session,
    meter: Meter,
    *,
    source_code: str,
    source_version: str,
    measurement_object: str,
    physical_quantity: str,
    raw_unit: str,
    reading_semantics: str,
    valid_from: datetime,
    valid_to: datetime | None = None,
    standard_unit: str | None = None,
    representative_minutes: int = 15,
    sign_direction: str = "forward",
    feedback_meaning: str = "",
    multiplier_ref: str = "meter.multiplier",
    multiplier_factor: float = 1.0,
    unit_factor: float | None = None,
    unit_factor_evidence: str = "",
    note: str = "",
    created_by: str = "ops",
    allow_overlap: bool = False,
) -> SemanticContract:
    """登记一条来源版本语义契约。

    契约只追加；与同一表计有效期重叠的旧契约在交界处被收口（valid_to 截断），
    相邻不重叠则各自有效（同日协议升级）。是否“冲突”留给编译器按槽位判定。
    allow_overlap=True 时跳过收口，用于登记两条互相冲突的来源声明，验证编译器
    能把同期间冲突隔离为待核。
    """
    if reading_semantics not in SEMANTIC_VALUES:
        raise ValueError(f"未知读数口径 {reading_semantics}")
    raw = _norm_unit(raw_unit)
    if raw not in UNIT_REGISTRY:
        raise ValueError(f"未登记单位 {raw_unit}，缺少明确换算因子")
    target = STANDARD_DIMENSION.get(physical_quantity)
    if target is None:
        raise ValueError(f"未知物理量 {physical_quantity}")
    std_unit = _norm_unit(standard_unit) or target["unit"]
    if std_unit not in UNIT_REGISTRY:
        raise ValueError(f"标准单位 {std_unit} 未登记")

    # 单位换算因子：显式优先，否则从注册表按量纲推导，并记录证据与方向
    if unit_factor is None:
        raw_info = UNIT_REGISTRY[raw]
        std_info = UNIT_REGISTRY[std_unit]
        if raw_info["dimension"] != std_info["dimension"]:
            # 跨量纲（如 kW→kWh）不能靠单位因子，必须走瞬时积分；因子留空由编译器裁决
            factor = 1.0
            evidence = unit_factor_evidence or "跨量纲，需按瞬时功率积分处理"
        else:
            factor = raw_info["factor"] / std_info["factor"]
            evidence = (
                unit_factor_evidence
                or f"{raw}→{std_unit} 同量纲换算，standard = raw × {factor:g}"
            )
    else:
        factor = unit_factor
        evidence = unit_factor_evidence or f"显式因子 standard = raw × {factor:g}"

    if sign_direction == "bidirectional" and not feedback_meaning.strip():
        raise ValueError("双向符号契约必须给出可验证的回送含义 feedback_meaning")

    # 收口与新契约有效期重叠的同表计旧契约（边界处截断，不删除历史）
    if not allow_overlap:
        overlapping = (
            db.execute(
                select(SemanticContract).where(
                    SemanticContract.meter_id == meter.id,
                    SemanticContract.status == "active",
                    SemanticContract.valid_from < (valid_to or datetime(9999, 1, 1)),
                    (SemanticContract.valid_to.is_(None))
                    | (SemanticContract.valid_to > valid_from),
                )
            )
            .scalars()
            .all()
        )
        for old in overlapping:
            old_end = old.valid_to
            # 真正重叠（非相邻）才截断；相邻边界 valid_from == old.valid_to 不冲突
            if old.valid_from < valid_from and (old_end is None or old_end > valid_from):
                old.valid_to = valid_from
                old.status = "superseded"
                old.note = (old.note + " | " if old.note else "") + "有效期被新契约收口"

    seq = (
        db.execute(
            select(SemanticContract).where(SemanticContract.meter_id == meter.id)
        ).scalars().all()
    )
    code = f"CTR-{meter.code}-{len(seq) + 1:03d}"
    contract = SemanticContract(
        code=code,
        meter_id=meter.id,
        source_code=source_code,
        source_version=source_version,
        measurement_object=measurement_object,
        physical_quantity=physical_quantity,
        raw_unit=raw,
        standard_unit=std_unit,
        reading_semantics=reading_semantics,
        representative_minutes=representative_minutes,
        sign_direction=sign_direction,
        feedback_meaning=feedback_meaning,
        multiplier_ref=multiplier_ref,
        multiplier_factor=multiplier_factor,
        unit_factor=factor,
        unit_factor_evidence=evidence,
        valid_from=valid_from,
        valid_to=valid_to,
        note=note,
        created_by=created_by,
    )
    db.add(contract)
    db.flush()
    return contract


def active_contracts_at(db: Session, meter_id: int, slot: datetime) -> list[SemanticContract]:
    """解析某表计在某槽位适用的全部有效契约（0=缺失，>1=冲突）。"""
    return (
        db.execute(
            select(SemanticContract)
            .where(
                SemanticContract.meter_id == meter_id,
                SemanticContract.status == "active",
                SemanticContract.valid_from <= slot,
                (SemanticContract.valid_to.is_(None))
                | (SemanticContract.valid_to > slot),
            )
            .order_by(SemanticContract.valid_from, SemanticContract.id)
        )
        .scalars()
        .all()
    )


# ----------------------------------------------------------------- 发布版本


def _contract_fingerprint(contracts: list[SemanticContract]) -> str:
    parts = [
        "|".join(
            str(x)
            for x in (
                c.id,
                c.meter_id,
                c.source_version,
                c.physical_quantity,
                c.raw_unit,
                c.standard_unit,
                c.reading_semantics,
                c.representative_minutes,
                c.sign_direction,
                c.feedback_meaning,
                c.multiplier_ref,
                c.multiplier_factor,
                c.unit_factor,
                iso(c.valid_from),
                iso(c.valid_to),
                c.status,
            )
        )
        for c in sorted(contracts, key=lambda c: c.id)
    ]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def ensure_release(db: Session) -> ContractRelease:
    """把当前全部有效契约固化为一个不可变发布版本；契约未变则复用同一版本。

    新契约/契约更正会改变指纹，从而产生新版本——新契约只影响新运行，历史产品
    仍按其引用的旧版本复演。
    """
    active = db.execute(
        select(SemanticContract).where(SemanticContract.status == "active")
    ).scalars().all()
    fingerprint = _contract_fingerprint(active)
    existing = (
        db.execute(
            select(ContractRelease)
            .where(ContractRelease.fingerprint == fingerprint)
            .order_by(ContractRelease.version.desc())
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return existing
    last = db.execute(
        select(ContractRelease).order_by(ContractRelease.version.desc())
    ).scalars().first()
    version = (last.version + 1) if last else 1
    release = ContractRelease(
        version=version,
        status="locked",
        contract_ids=[c.id for c in sorted(active, key=lambda c: c.id)],
        fingerprint=fingerprint,
        note="契约发布锁定（自动）",
    )
    db.add(release)
    db.flush()
    return release


def latest_release_version(db: Session) -> int:
    return ensure_release(db).version


# ----------------------------------------------------------------- 原始受理


def ingest_raw_reading(
    db: Session,
    meter: Meter,
    slot_start: datetime,
    raw_value: Any,
    *,
    raw_unit: str,
    source_code: str,
    source_version: str,
    received_at: datetime | None = None,
    slot_minutes: int = 15,
    payload: dict[str, Any] | None = None,
    sample_id: int | None = None,
) -> RawReading:
    """受理一条不可变原始读数；数值不可解析时仍保留载荷并标记，交由编译器隔离。"""
    clock_now = received_at or utcnow()
    numeric_parseable = True
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        value = 0.0
        numeric_parseable = False
    existing = (
        db.execute(
            select(RawReading).where(
                RawReading.meter_id == meter.id,
                RawReading.slot_start == slot_start,
                RawReading.source_version == source_version,
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return existing
    reading = RawReading(
        meter_id=meter.id,
        slot_start=slot_start,
        slot_end=slot_start + timedelta(minutes=slot_minutes),
        raw_value=value,
        raw_unit=_norm_unit(raw_unit),
        source_code=source_code,
        source_version=source_version,
        payload={
            **(payload or {}),
            "raw_value_original": raw_value,
            "numeric_parseable": numeric_parseable,
        },
        received_at=clock_now,
        sample_id=sample_id,
    )
    db.add(reading)
    db.flush()
    return reading


# ----------------------------------------------------------------- 编译校验


def _resolve_multiplier(meter: Meter, contract: SemanticContract) -> tuple[float | None, str]:
    if contract.multiplier_ref == "meter.multiplier":
        return (meter.multiplier or 1.0), f"引用表计倍率 meter.multiplier = {meter.multiplier:g}"
    if contract.multiplier_factor and contract.multiplier_factor > 0:
        return contract.multiplier_factor, f"显式倍率 × {contract.multiplier_factor:g}"
    return None, f"倍率引用 {contract.multiplier_ref} 无法解析"


def _quarantine(
    reading: RawReading | None,
    meter: Meter,
    contracts: list[SemanticContract],
    release_version: int,
    reason_code: str,
    reason_detail: str,
    *,
    physical_quantity: str = "",
    numeric_parseable: bool = True,
    slot_end: datetime | None = None,
) -> dict[str, Any]:
    return {
        "kind": COMPILE_QUARANTINED,
        "meter_id": meter.id,
        "building_code": meter.building.code,
        "raw_reading_id": reading.id if reading else None,
        "contract_ids": [c.id for c in contracts],
        "contract_release_version": release_version,
        "slot_start": reading.slot_start if reading else None,
        "slot_end": (reading.slot_end if reading else slot_end),
        "physical_quantity": physical_quantity or (contracts[0].physical_quantity if contracts else ""),
        "reason_code": reason_code,
        "reason_detail": reason_detail,
        "numeric_parseable": numeric_parseable,
    }


def _compile_one(
    db: Session,
    meter: Meter,
    reading: RawReading,
    release_version: int,
    missing_slots: set[datetime],
) -> dict[str, Any]:
    """把一条原始读数编译为标准事实或待核记录，返回可持久化的字典。"""
    contracts = active_contracts_at(db, meter.id, reading.slot_start)
    numeric_ok = bool((reading.payload or {}).get("numeric_parseable", True))
    if not numeric_ok:
        return _quarantine(
            reading, meter, contracts, release_version, Q_NUMERIC,
            f"原始值 {reading.payload.get('raw_value_original')!r} 无法解析为数值",
            numeric_parseable=False,
        )
    if not contracts:
        return _quarantine(
            reading, meter, contracts, release_version, Q_NO_CONTRACT,
            f"来源 {reading.source_code}/{reading.source_version} 在该槽位缺少语义契约声明",
        )
    if len(contracts) > 1:
        return _quarantine(
            reading, meter, contracts, release_version, Q_CONTRACT_CONFLICT,
            "同一有效期存在两个冲突契约：" + " / ".join(c.code for c in contracts),
            physical_quantity=contracts[0].physical_quantity,
        )

    contract = contracts[0]
    target = STANDARD_DIMENSION[contract.physical_quantity]
    steps: list[dict[str, Any]] = [
        {"step": "raw", "value": reading.raw_value, "unit": reading.raw_unit,
         "source_version": reading.source_version, "semantics": contract.reading_semantics}
    ]

    # 1) 单位/量纲核验：读数单位必须已登记，不允许静默套用契约单位
    raw_info = unit_info(reading.raw_unit)
    if raw_info is None:
        return _quarantine(reading, meter, contracts, release_version, Q_UNIT_FACTOR,
                           f"读数单位 {reading.raw_unit} 未登记，缺少明确换算因子与方向",
                           physical_quantity=contract.physical_quantity)
    raw_dim = raw_info["dimension"]
    target_dim = target["dimension"]
    # 同量纲但单位与契约声明不一致：换算因子与方向未被契约证明（瞬时积分不依赖单位因子，放行到积分分支）
    if (
        contract.reading_semantics != SEMANTIC_INSTANT
        and raw_dim == target_dim
        and _norm_unit(reading.raw_unit) != _norm_unit(contract.raw_unit)
    ):
        return _quarantine(reading, meter, contracts, release_version, Q_UNIT_FACTOR,
                           f"读数单位 {reading.raw_unit} 与契约声明单位 {contract.raw_unit} 同量纲但不一致，"
                           "换算因子与方向无法证明",
                           physical_quantity=contract.physical_quantity)

    # 2) 符号方向 / 回送含义
    if reading.raw_value < 0:
        if contract.sign_direction != "bidirectional":
            return _quarantine(reading, meter, contracts, release_version, Q_SIGN_FEEDBACK,
                               f"负向读数 {reading.raw_value} 但契约符号方向为 {contract.sign_direction}，"
                               "没有可验证的回送含义",
                               physical_quantity=contract.physical_quantity)
        if not contract.feedback_meaning.strip():
            return _quarantine(reading, meter, contracts, release_version, Q_SIGN_FEEDBACK,
                               "双向符号契约缺少可验证回送含义",
                               physical_quantity=contract.physical_quantity)
        steps.append({"step": "sign", "direction": "bidirectional",
                      "feedback_meaning": contract.feedback_meaning})

    # 3) 倍率解析
    multiplier, mult_note = _resolve_multiplier(meter, contract)
    if multiplier is None:
        return _quarantine(reading, meter, contracts, release_version, Q_MULTIPLIER, mult_note,
                           physical_quantity=contract.physical_quantity)

    # 4) 按口径换算
    reading_kind = contract.reading_semantics

    if reading_kind == SEMANTIC_INSTANT:
        # 瞬时功率：只有间隔支持积分时才能换算为电量，且功率不得直接与电量相加
        if raw_dim != "power":
            return _quarantine(reading, meter, contracts, release_version, Q_DIMENSION,
                               f"瞬时口径要求功率量纲，实际 {reading.raw_unit}（{raw_dim}）",
                               physical_quantity=contract.physical_quantity)
        if target_dim != "energy":
            return _quarantine(reading, meter, contracts, release_version, Q_DIMENSION,
                               f"瞬时功率积分只能得到电量，目标量纲却是 {target_dim}",
                               physical_quantity=contract.physical_quantity)
        rep = contract.representative_minutes or 0
        if rep <= 0:
            return _quarantine(reading, meter, contracts, release_version, Q_INSTANT_INTERVAL,
                               "读数间隔不支持功率积分：缺少采样代表时段 representative_minutes",
                               physical_quantity=contract.physical_quantity)
        hours = rep / 60.0
        steps.append({"step": "integrate", "formula": "kWh = kW × 代表时段(h)",
                      "representative_minutes": rep, "hours": hours})
        scaled = reading.raw_value * hours
        steps.append({"step": "multiply", "factor": multiplier, "note": mult_note})
        standard_value = scaled * multiplier
        steps.append({"step": "unit", "from": reading.raw_unit, "to": contract.standard_unit,
                      "factor": 1.0, "note": "积分结果即 kWh"})

    elif reading_kind == SEMANTIC_INTERVAL:
        # 区间读数：已经是区间用量，直接换算，绝不二次累计
        if (reading.payload or {}).get("requires_differencing"):
            return _quarantine(reading, meter, contracts, release_version, Q_INTERVAL_REACCUM,
                               "区间读数被要求按累计表再次求差，区间口径不得二次累计",
                               physical_quantity=contract.physical_quantity)
        if raw_dim != target_dim:
            return _quarantine(reading, meter, contracts, release_version, Q_DIMENSION,
                               f"区间口径量纲不兼容：{reading.raw_unit}（{raw_dim}）不能折算为"
                               f"{contract.standard_unit}（{target_dim}），功率不得与电量直接相加",
                               physical_quantity=contract.physical_quantity)
        factor = contract.unit_factor
        if not factor or factor <= 0:
            return _quarantine(reading, meter, contracts, release_version, Q_UNIT_FACTOR,
                               f"{reading.raw_unit}→{contract.standard_unit} 缺少明确换算因子",
                               physical_quantity=contract.physical_quantity)
        steps.append({"step": "interval", "note": "区间读数直接采用，不做二次累计"})
        steps.append({"step": "multiply", "factor": multiplier, "note": mult_note})
        scaled = reading.raw_value * multiplier
        steps.append({"step": "unit", "from": reading.raw_unit, "to": contract.standard_unit,
                      "factor": factor, "note": contract.unit_factor_evidence})
        standard_value = scaled * factor

    else:  # SEMANTIC_CUMULATIVE
        if raw_dim != target_dim:
            return _quarantine(reading, meter, contracts, release_version, Q_DIMENSION,
                               f"累计口径量纲不兼容：{reading.raw_unit}（{raw_dim}）vs 目标 {target_dim}",
                               physical_quantity=contract.physical_quantity)
        prior = (
            db.execute(
                select(RawReading)
                .where(
                    RawReading.meter_id == meter.id,
                    RawReading.slot_start < reading.slot_start,
                )
                .order_by(RawReading.slot_start.desc(), RawReading.id.desc())
            )
            .scalars()
            .first()
        )
        if prior is None:
            return _quarantine(reading, meter, contracts, release_version, Q_CUMULATIVE_BASELINE,
                               "累计表缺少可衔接的基线读数，首槽区间用量无法证明",
                               physical_quantity=contract.physical_quantity)
        prior_contracts = active_contracts_at(db, meter.id, prior.slot_start)
        prior_contract = prior_contracts[0] if prior_contracts else None
        # 契约在边界切换：跨契约的累计差不能直接相减
        if prior_contract is None or prior_contract.id != contract.id:
            return _quarantine(reading, meter, contracts, release_version, Q_CUMULATIVE_BASELINE,
                               "有效期交界处更换计量模式/单位，跨契约累计读数不可直接求差，需新基线",
                               physical_quantity=contract.physical_quantity)
        if any(prior.slot_start < ms < reading.slot_start for ms in missing_slots):
            return _quarantine(reading, meter, contracts, release_version, Q_BRIDGE_GAP,
                               "表码差跨过未覆盖槽位，无法归因到单个区间",
                               physical_quantity=contract.physical_quantity)
        delta = reading.raw_value - prior.raw_value
        steps.append({"step": "difference", "prior_slot": iso(prior.slot_start),
                      "prior_value": prior.raw_value, "delta": delta})
        if delta < 0:
            return _quarantine(reading, meter, contracts, release_version, Q_CUMULATIVE_RESET,
                               f"累计读数回退 {prior.raw_value:g} → {reading.raw_value:g}，"
                               "负差不可计为节能，疑似换表/重置",
                               physical_quantity=contract.physical_quantity)
        factor = contract.unit_factor
        if not factor or factor <= 0:
            return _quarantine(reading, meter, contracts, release_version, Q_UNIT_FACTOR,
                               f"{reading.raw_unit}→{contract.standard_unit} 缺少明确换算因子",
                               physical_quantity=contract.physical_quantity)
        steps.append({"step": "multiply", "factor": multiplier, "note": mult_note})
        scaled = delta * multiplier
        steps.append({"step": "unit", "from": reading.raw_unit, "to": contract.standard_unit,
                      "factor": factor, "note": contract.unit_factor_evidence})
        standard_value = scaled * factor

    # 5) 边界拆分标记：该槽位是否紧邻某契约的生效边界
    boundary = any(
        c.valid_from == reading.slot_start and c.id == contract.id for c in contracts
    )

    return {
        "kind": COMPILE_STANDARD,
        "meter_id": meter.id,
        "building_code": meter.building.code,
        "raw_reading_id": reading.id,
        "contract_id": contract.id,
        "contract_release_version": release_version,
        "slot_start": reading.slot_start,
        "slot_end": reading.slot_end,
        "physical_quantity": contract.physical_quantity,
        "standard_unit": contract.standard_unit,
        "standard_value": round(standard_value, 6),
        "source_semantics": reading_kind,
        "conversion_steps": steps,
        "boundary_split": boundary,
    }


# ----------------------------------------------------------------- 窗口编译


def compile_window(
    db: Session,
    source: Source,
    ws: datetime,
    we: datetime,
    cutoff: datetime,
) -> dict[str, Any]:
    """把窗口内某来源全部表计的原始读数编译为标准事实 / 待核隔离。

    幂等：同一发布版本下先清除该窗口的事实与隔离记录再重算；RawReading 不可变保留。
    """
    release = ensure_release(db)
    meters = (
        db.execute(select(Meter).where(Meter.source_id == source.id).order_by(Meter.code))
        .scalars()
        .all()
    )
    standard_rows: list[StandardIntervalFact] = []
    quarantine_rows: list[QuarantineRecord] = []
    per_meter: dict[int, dict[str, Any]] = {}

    for meter in meters:
        readings = (
            db.execute(
                select(RawReading)
                .where(
                    RawReading.meter_id == meter.id,
                    RawReading.slot_start >= ws,
                    RawReading.slot_start < we,
                    RawReading.received_at <= cutoff,
                )
                .order_by(RawReading.slot_start, RawReading.id)
            )
            .scalars()
            .all()
        )
        # 该表计窗口内未覆盖（无原始读数）的槽位，用于累计表桥接判定
        present_slots = {r.slot_start for r in readings}
        missing_slots: set[datetime] = set()
        # 以 15 分钟网格推导应有槽位；契约代表时段可能不同，缺失判定仅用于桥接
        cur = ws
        while cur < we:
            if cur not in present_slots:
                missing_slots.add(cur)
            cur += timedelta(minutes=15)

        m_std, m_q = 0, 0
        for reading in readings:
            outcome = _compile_one(db, meter, reading, release.version, missing_slots)
            if outcome["kind"] == COMPILE_STANDARD:
                standard_rows.append(
                    StandardIntervalFact(
                        meter_id=outcome["meter_id"],
                        building_code=outcome["building_code"],
                        raw_reading_id=outcome["raw_reading_id"],
                        contract_id=outcome["contract_id"],
                        contract_release_version=release.version,
                        slot_start=outcome["slot_start"],
                        slot_end=outcome["slot_end"],
                        physical_quantity=outcome["physical_quantity"],
                        standard_unit=outcome["standard_unit"],
                        standard_value=outcome["standard_value"],
                        source_semantics=outcome["source_semantics"],
                        conversion_steps=outcome["conversion_steps"],
                        boundary_split=outcome["boundary_split"],
                    )
                )
                m_std += 1
            else:
                quarantine_rows.append(
                    QuarantineRecord(
                        meter_id=outcome["meter_id"],
                        building_code=outcome["building_code"],
                        raw_reading_id=outcome["raw_reading_id"],
                        contract_ids=outcome["contract_ids"],
                        contract_release_version=release.version,
                        slot_start=outcome["slot_start"],
                        slot_end=outcome["slot_end"],
                        physical_quantity=outcome["physical_quantity"],
                        reason_code=outcome["reason_code"],
                        reason_detail=outcome["reason_detail"],
                        numeric_parseable=outcome["numeric_parseable"],
                    )
                )
                m_q += 1
        if readings:
            per_meter[meter.id] = {"standard": m_std, "quarantined": m_q, "total": len(readings)}

    # 幂等清理：仅清理当前发布版本在该窗口的产物，历史版本产物保留（版本谱系）
    for model in (StandardIntervalFact, QuarantineRecord):
        db.execute(
            delete(model).where(
                model.contract_release_version == release.version,
                model.slot_start >= ws,
                model.slot_start < we,
                model.meter_id.in_([m.id for m in meters]) if meters else False,
            )
        )
    db.flush()
    db.add_all(standard_rows)
    db.add_all(quarantine_rows)
    db.flush()

    return {
        "contract_release_version": release.version,
        "standard_count": len(standard_rows),
        "quarantined_count": len(quarantine_rows),
        "per_meter": per_meter,
        "quarantine_reasons": _count_reasons(quarantine_rows),
    }


def _count_reasons(rows: list[QuarantineRecord]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[r.reason_code] = out.get(r.reason_code, 0) + 1
    return out


def replay(
    db: Session,
    *,
    meter_id: int | None = None,
    since: datetime | None = None,
) -> dict[str, Any]:
    """契约更正后重放受影响历史：在新发布版本下重新编译，旧版本产物保留可复演。"""
    release = ensure_release(db)
    stmt = select(RawReading)
    if meter_id is not None:
        stmt = stmt.where(RawReading.meter_id == meter_id)
    if since is not None:
        stmt = stmt.where(RawReading.slot_start >= since)
    readings = db.execute(stmt.order_by(RawReading.slot_start)).scalars().all()
    meter_ids = {r.meter_id for r in readings}
    sources = (
        db.execute(select(Source).join(Meter, Meter.source_id == Source.id))
        .scalars()
        .all()
    )
    affected_windows: set[tuple[int, datetime]] = set()
    for r in readings:
        window_start = r.slot_start.replace(
            hour=(r.slot_start.hour // 6) * 6, minute=0, second=0, microsecond=0
        )
        meter = db.get(Meter, r.meter_id)
        if meter is not None:
            affected_windows.add((meter.source_id, window_start))
    recompiled = 0
    for source_id, ws in sorted(affected_windows, key=lambda x: (x[0], x[1])):
        source = db.get(Source, source_id)
        if source is None:
            continue
        compile_window(db, source, ws, ws + timedelta(hours=6), utcnow())
        recompiled += 1
    db.flush()
    return {
        "new_release_version": release.version,
        "readings_replayed": len(readings),
        "meters": sorted(meter_ids),
        "windows_recompiled": recompiled,
    }


# ----------------------------------------------------------------- 查询


def standard_facts_for_window(
    db: Session, source_id: int, ws: datetime, we: datetime, release_version: int | None = None
) -> list[StandardIntervalFact]:
    stmt = select(StandardIntervalFact).where(
        StandardIntervalFact.slot_start >= ws,
        StandardIntervalFact.slot_start < we,
    )
    if release_version is not None:
        stmt = stmt.where(StandardIntervalFact.contract_release_version == release_version)
    else:
        # 默认取窗口内最新发布版本，保证所有消费者引用同一解释版本
        latest = (
            db.execute(
                select(StandardIntervalFact.contract_release_version)
                .where(
                    StandardIntervalFact.slot_start >= ws,
                    StandardIntervalFact.slot_start < we,
                )
                .order_by(StandardIntervalFact.contract_release_version.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if latest is None:
            return []
        stmt = stmt.where(StandardIntervalFact.contract_release_version == latest)
    rows = db.execute(
        stmt.join(Meter, Meter.id == StandardIntervalFact.meter_id)
        .where(Meter.source_id == source_id)
        .order_by(StandardIntervalFact.slot_start, StandardIntervalFact.meter_id)
    ).scalars().all()
    return rows


def quarantine_for_window(
    db: Session, source_id: int, ws: datetime, we: datetime, release_version: int | None = None
) -> list[QuarantineRecord]:
    q = select(QuarantineRecord).where(
        QuarantineRecord.slot_start >= ws,
        QuarantineRecord.slot_start < we,
    )
    if release_version is not None:
        q = q.where(QuarantineRecord.contract_release_version == release_version)
    rows = db.execute(
        q.join(Meter, Meter.id == QuarantineRecord.meter_id)
        .where(Meter.source_id == source_id)
        .order_by(QuarantineRecord.slot_start, QuarantineRecord.meter_id)
    ).scalars().all()
    return rows


def has_raw_readings(db: Session, source_id: int, ws: datetime, we: datetime) -> bool:
    """该来源窗口内是否存在受语义治理的原始受理；没有则语义闸门不介入旧链路。"""
    meter_ids = list(
        db.execute(select(Meter.id).where(Meter.source_id == source_id)).scalars().all()
    )
    if not meter_ids:
        return False
    row = (
        db.execute(
            select(RawReading.id)
            .where(
                RawReading.meter_id.in_(meter_ids),
                RawReading.slot_start >= ws,
                RawReading.slot_start < we,
            )
            .limit(1)
        )
        .scalars()
        .first()
    )
    return row is not None


def semantic_gate(
    db: Session, source: Source, ws: datetime, we: datetime, cutoff: datetime
) -> dict[str, Any] | None:
    """健康评估语义闸门。

    仅当该来源窗口内存在原始受理时才编译；否则返回 None，完全不影响既有
    Sample 链路的覆盖/核验结论。存在待核隔离时 semantic_blocked=True，
    据此阻止窗口进入确认版（certified/confirmable）。
    """
    if not has_raw_readings(db, source.id, ws, we):
        return None
    result = compile_window(db, source, ws, we, cutoff)
    quarantined = quarantine_for_window(
        db, source.id, ws, we, result["contract_release_version"]
    )
    standards = standard_facts_for_window(
        db, source.id, ws, we, result["contract_release_version"]
    )
    by_meter: dict[int, dict[str, Any]] = {}
    for f in standards:
        b = by_meter.setdefault(f.meter_id, {"standard": 0, "quarantined": 0})
        b["standard"] += 1
    for q in quarantined:
        b = by_meter.setdefault(q.meter_id, {"standard": 0, "quarantined": 0})
        b["quarantined"] += 1
    return {
        "contract_release_version": result["contract_release_version"],
        "standard_count": result["standard_count"],
        "quarantined_count": result["quarantined_count"],
        "semantic_blocked": result["quarantined_count"] > 0,
        "quarantine_reasons": result["quarantine_reasons"],
        "quarantine_slots": [
            {
                "meter_id": q.meter_id,
                "slot": iso(q.slot_start),
                "reason_code": q.reason_code,
                "reason_detail": q.reason_detail,
                "numeric_parseable": q.numeric_parseable,
            }
            for q in quarantined
        ],
        "per_meter": by_meter,
    }


def standard_series(
    db: Session, source_id: int, ws: datetime, we: datetime
) -> list[dict[str, Any]]:
    """产品侧使用的标准区间序列（只含证据完整事实，待核槽位不填零）。"""
    facts = standard_facts_for_window(db, source_id, ws, we)
    out: dict[str, dict[str, Any]] = {}
    for f in facts:
        slot = iso(f.slot_start)
        entry = out.setdefault(
            slot,
            {
                "slot": slot,
                "slot_end": iso(f.slot_end),
                "value": 0.0,
                "unit": f.standard_unit,
                "physical_quantity": f.physical_quantity,
                "by_meter": {},
                "release_version": f.contract_release_version,
            },
        )
        entry["value"] = round(entry["value"] + f.standard_value, 6)
        entry["by_meter"][str(f.meter_id)] = round(
            entry["by_meter"].get(str(f.meter_id), 0.0) + f.standard_value, 6
        )
    return [out[k] for k in sorted(out)]
