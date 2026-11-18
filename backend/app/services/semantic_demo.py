"""语义契约治理验收场景。

用独立的“探针表计”（status=inactive、无采样承诺）承载受控报文，因此不参与既有
SLO 覆盖/核验链路，只用于验证语义编译器。每个场景都从不可变原始受理出发，编译后
回读标准区间事实与待核隔离，逐条核对来源语义、换算步骤、可用槽位与被隔离记录，
证明“数值可解析”不等于“可用于能耗产品”。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import (
    BreachFact,
    Building,
    ContractRelease,
    Meter,
    MeterEvaluation,
    QuarantineRecord,
    RawReading,
    SEMANTIC_CUMULATIVE,
    SEMANTIC_INSTANT,
    SEMANTIC_INTERVAL,
    Sample,
    SemanticContract,
    Source,
    StandardIntervalFact,
)
from ..timeutil import iso
from . import semantics

DEMO_DAY = datetime(2026, 11, 10)
WS = DEMO_DAY  # 2026-11-10 00:00
WE = DEMO_DAY + timedelta(hours=6)
DAY_BEFORE = DEMO_DAY - timedelta(days=1)

PROBE_PREFIX = "P-SEM-"


# ------------------------------------------------------------------ 重置


def purge_meters(db: Session, meter_ids: list[int]) -> None:
    """按外键依赖顺序清理一组表计的全部关联产物，再删除表计本身。"""
    if not meter_ids:
        return
    db.execute(delete(QuarantineRecord).where(QuarantineRecord.meter_id.in_(meter_ids)))
    db.execute(delete(StandardIntervalFact).where(StandardIntervalFact.meter_id.in_(meter_ids)))
    db.execute(delete(BreachFact).where(BreachFact.meter_id.in_(meter_ids)))
    db.execute(delete(MeterEvaluation).where(MeterEvaluation.meter_id.in_(meter_ids)))
    db.execute(delete(RawReading).where(RawReading.meter_id.in_(meter_ids)))
    db.execute(delete(Sample).where(Sample.meter_id.in_(meter_ids)))
    db.flush()
    db.execute(delete(SemanticContract).where(SemanticContract.meter_id.in_(meter_ids)))
    db.flush()
    for mid in meter_ids:
        meter = db.get(Meter, mid)
        if meter is not None:
            db.delete(meter)
    db.flush()


def reset_semantic_demo(db: Session) -> None:
    """清空语义治理全部产物与探针表计，回到可重建状态（主演示数据不受影响）。"""
    probe_meters = (
        db.execute(select(Meter).where(Meter.code.like(PROBE_PREFIX + "%")))
        .scalars()
        .all()
    )
    probe_ids = [m.id for m in probe_meters]
    if probe_ids:
        purge_meters(db, probe_ids)
    # 清理可能残留的本模块契约（按编码前缀）
    db.execute(delete(SemanticContract).where(SemanticContract.code.like("CTR-P-SEM-%")))
    db.flush()
    # 发布版本随契约清空后重新从 v1 开始
    db.execute(delete(ContractRelease))
    db.flush()


def _probe_meter(db: Session, code: str, name: str, source: Source, building: Building) -> Meter:
    meter = Meter(
        code=PROBE_PREFIX + code,
        name=name,
        source_id=source.id,
        building_id=building.id,
        cumulative=False,
        multiplier=1.0,
        status="inactive",  # 探针：不参与 SLO 覆盖评估
        note="语义契约验收探针表计",
    )
    db.add(meter)
    db.flush()
    return meter


def _reading(db: Session, meter: Meter, slot: datetime, value: Any, unit: str, version: str) -> RawReading:
    return semantics.ingest_raw_reading(
        db,
        meter,
        slot,
        value,
        raw_unit=unit,
        source_code="probe",
        source_version=version,
        received_at=WE,
        slot_minutes=15,
        payload={"scenario": True},
    )


def _contract(db: Session, meter: Meter, **kw: Any) -> SemanticContract:
    kw.setdefault("source_code", "probe")
    kw.setdefault("measurement_object", meter.name)
    return semantics.register_contract(db, meter, **kw)


# ------------------------------------------------------------------ 场景构建


def build_scenarios(db: Session) -> list[dict[str, Any]]:
    src_elec = db.execute(select(Source).where(Source.code == "electricity")).scalars().one()
    src_water = db.execute(select(Source).where(Source.code == "water")).scalars().one()
    b1 = db.execute(select(Building).where(Building.code == "B1")).scalars().one()
    b2 = db.execute(select(Building).where(Building.code == "B2")).scalars().one()
    b3 = db.execute(select(Building).where(Building.code == "B3")).scalars().one()
    b4 = db.execute(select(Building).where(Building.code == "B4")).scalars().one()

    # 1) kWh / kW 混入 ----------------------------------------------------
    mix = _probe_meter(db, "MIX", "电量/功率混入探针", src_elec, b1)
    _contract(
        db, mix, source_version="elec-v1", physical_quantity="electricity",
        raw_unit="kWh", reading_semantics=SEMANTIC_INTERVAL,
        valid_from=WS, valid_to=WS + timedelta(minutes=30), representative_minutes=15,
    )
    _contract(
        db, mix, source_version="elec-v2", physical_quantity="electricity",
        raw_unit="kW", reading_semantics=SEMANTIC_INSTANT,
        valid_from=WS + timedelta(minutes=30), representative_minutes=15,
    )
    _reading(db, mix, WS, 25.0, "kWh", "elec-v1")
    _reading(db, mix, WS + timedelta(minutes=15), 1500.0, "kW", "elec-v1")  # 语义错配
    _reading(db, mix, WS + timedelta(minutes=30), 1500.0, "kW", "elec-v2")  # 正确积分

    # 2) 累计表重置 --------------------------------------------------------
    rst = _probe_meter(db, "RST", "累计水量重置探针", src_water, b2)
    _contract(
        db, rst, source_version="water-cum", physical_quantity="water",
        raw_unit="m³", reading_semantics=SEMANTIC_CUMULATIVE,
        valid_from=DAY_BEFORE, representative_minutes=15,
    )
    _reading(db, rst, WS - timedelta(minutes=15), 100.0, "m³", "water-cum")  # 基线
    _reading(db, rst, WS, 108.0, "m³", "water-cum")
    _reading(db, rst, WS + timedelta(minutes=15), 5.0, "m³", "water-cum")  # 回退

    # 3) 升 / 立方米切换 ---------------------------------------------------
    unit = _probe_meter(db, "UNIT", "升/立方米切换探针", src_water, b2)
    _contract(
        db, unit, source_version="water-L", physical_quantity="water",
        raw_unit="L", reading_semantics=SEMANTIC_INTERVAL,
        valid_from=WS, valid_to=WS + timedelta(minutes=30), representative_minutes=15,
    )
    _contract(
        db, unit, source_version="water-m3", physical_quantity="water",
        raw_unit="m³", reading_semantics=SEMANTIC_INTERVAL,
        valid_from=WS + timedelta(minutes=30), representative_minutes=15,
    )
    _reading(db, unit, WS, 5000.0, "L", "water-L")
    _reading(db, unit, WS + timedelta(minutes=15), 3000.0, "L", "water-L")
    _reading(db, unit, WS + timedelta(minutes=30), 4.2, "m³", "water-m3")
    _reading(db, unit, WS + timedelta(minutes=45), 2.0, "gal", "water-m3")  # 无换算因子

    # 4) 回送功率 ----------------------------------------------------------
    fb = _probe_meter(db, "FB", "双向回送功率探针", src_elec, b3)
    _contract(
        db, fb, source_version="elec-bi", physical_quantity="electricity",
        raw_unit="kW", reading_semantics=SEMANTIC_INSTANT,
        valid_from=WS, representative_minutes=15,
        sign_direction="bidirectional", feedback_meaning="负值表示反向馈送园区电网",
    )
    _reading(db, fb, WS, 800.0, "kW", "elec-bi")
    _reading(db, fb, WS + timedelta(minutes=15), -200.0, "kW", "elec-bi")
    fbf = _probe_meter(db, "FBF", "单向表计出现负向探针", src_elec, b3)
    _contract(
        db, fbf, source_version="elec-fwd", physical_quantity="electricity",
        raw_unit="kW", reading_semantics=SEMANTIC_INSTANT,
        valid_from=WS, representative_minutes=15, sign_direction="forward",
    )
    _reading(db, fbf, WS, -50.0, "kW", "elec-fwd")  # 无回送含义

    # 5) 同日协议升级（累计→区间，边界拆分） -------------------------------
    up = _probe_meter(db, "UP", "同日协议升级探针", src_elec, b4)
    _contract(
        db, up, source_version="proto-v1", physical_quantity="electricity",
        raw_unit="kWh", reading_semantics=SEMANTIC_CUMULATIVE,
        valid_from=DAY_BEFORE, valid_to=WS + timedelta(minutes=30), representative_minutes=15,
    )
    _contract(
        db, up, source_version="proto-v2", physical_quantity="electricity",
        raw_unit="kWh", reading_semantics=SEMANTIC_INTERVAL,
        valid_from=WS + timedelta(minutes=30), representative_minutes=15,
    )
    _reading(db, up, WS - timedelta(minutes=15), 5000.0, "kWh", "proto-v1")  # 基线
    _reading(db, up, WS, 5020.0, "kWh", "proto-v1")
    _reading(db, up, WS + timedelta(minutes=15), 5040.0, "kWh", "proto-v1")
    _reading(db, up, WS + timedelta(minutes=30), 6.5, "kWh", "proto-v2")  # 升级后区间

    # 6) 冲突契约 ----------------------------------------------------------
    cf = _probe_meter(db, "CF", "冲突契约探针", src_elec, b1)
    _contract(
        db, cf, source_version="conflict-a", physical_quantity="electricity",
        raw_unit="kWh", reading_semantics=SEMANTIC_INTERVAL,
        valid_from=WS, representative_minutes=15, allow_overlap=True,
    )
    _contract(
        db, cf, source_version="conflict-b", physical_quantity="electricity",
        raw_unit="kW", reading_semantics=SEMANTIC_INSTANT,
        valid_from=WS, representative_minutes=15, allow_overlap=True,
    )
    _reading(db, cf, WS, 1500.0, "kW", "conflict-b")

    # 7) 来源声明缺失（无契约） --------------------------------------------
    nc = _probe_meter(db, "NC", "缺少契约声明探针", src_elec, b4)
    _reading(db, nc, WS, 42.0, "kWh", "unknown-v9")

    # 8) 间隔不支持功率积分 ------------------------------------------------
    ii = _probe_meter(db, "II", "积分间隔缺失探针", src_elec, b2)
    _contract(
        db, ii, source_version="inst-norep", physical_quantity="electricity",
        raw_unit="kW", reading_semantics=SEMANTIC_INSTANT,
        valid_from=WS, representative_minutes=0,
    )
    _reading(db, ii, WS, 900.0, "kW", "inst-norep")

    db.flush()
    return [
        {"key": "kwh_kw_mixing", "meter": mix.code, "title": "kWh 与 kW 混入：错配隔离、可证功率正确积分"},
        {"key": "cumulative_reset", "meter": rst.code, "title": "累计表重置：回退负差不计节能，隔离待核"},
        {"key": "unit_switch", "meter": unit.code, "title": "升/立方米切换：显式因子换算并按边界拆窗"},
        {"key": "feedback_power", "meter": fb.code, "title": "回送功率：负向符号需可验证回送含义"},
        {"key": "protocol_upgrade", "meter": up.code, "title": "同日协议升级：有效期交界拆窗，不整天套用当前配置"},
        {"key": "contract_conflict", "meter": cf.code, "title": "冲突契约：同期间两种解释一律待核"},
        {"key": "missing_contract", "meter": nc.code, "title": "来源声明缺失：数值可解析但无契约，待核不填零"},
        {"key": "instant_interval", "meter": ii.code, "title": "间隔不支持功率积分：无代表时段则待核"},
    ]


# ------------------------------------------------------------------ 验收断言


def _facts_by_meter(db: Session, meter_id: int, version: int) -> list[StandardIntervalFact]:
    return (
        db.execute(
            select(StandardIntervalFact).where(
                StandardIntervalFact.meter_id == meter_id,
                StandardIntervalFact.contract_release_version == version,
            ).order_by(StandardIntervalFact.slot_start)
        )
        .scalars()
        .all()
    )


def _quarantine_by_meter(db: Session, meter_id: int, version: int) -> list[QuarantineRecord]:
    return (
        db.execute(
            select(QuarantineRecord).where(
                QuarantineRecord.meter_id == meter_id,
                QuarantineRecord.contract_release_version == version,
            ).order_by(QuarantineRecord.slot_start)
        )
        .scalars()
        .all()
    )


def _expect(label: str, passed: bool, evidence: Any) -> dict[str, Any]:
    return {"label": label, "passed": bool(passed), "evidence": evidence}


def _verify(db: Session, meta: dict[str, Any], version: int) -> dict[str, Any]:
    meter = db.execute(select(Meter).where(Meter.code == meta["meter"])).scalars().one()
    facts = _facts_by_meter(db, meter.id, version)
    quars = _quarantine_by_meter(db, meter.id, version)
    expectations: list[dict[str, Any]] = []

    if meta["key"] == "kwh_kw_mixing":
        std_slots = {iso(f.slot_start): f for f in facts}
        q_dim = [q for q in quars if q.reason_code == "dimension_mismatch"]
        expectations.append(_expect(
            "00:00 的 25 kWh 区间读数成为标准事实",
            iso(WS) in std_slots and abs(std_slots[iso(WS)].standard_value - 25.0) < 1e-9,
            {s: round(std_slots[s].standard_value, 4) for s in std_slots},
        ))
        integrated = std_slots.get(iso(WS + timedelta(minutes=30)))
        expectations.append(_expect(
            "00:30 的 1500 kW 按 15 分钟积分 = 375 kWh",
            integrated is not None and abs(integrated.standard_value - 375.0) < 1e-6
            and any(x["step"] == "integrate" for x in integrated.conversion_steps),
            integrated.conversion_steps if integrated else None,
        ))
        expectations.append(_expect(
            "00:15 把 kW 标成电量区间 → 量纲隔离（功率不得与电量相加）",
            len(q_dim) == 1,
            [q.reason_detail for q in q_dim],
        ))

    elif meta["key"] == "cumulative_reset":
        std_slots = {iso(f.slot_start): f for f in facts}
        q_reset = [q for q in quars if q.reason_code == "cumulative_reset"]
        expectations.append(_expect(
            "00:00 连续读数求差 108−100 = 8 m³ 成为标准事实",
            iso(WS) in std_slots and abs(std_slots[iso(WS)].standard_value - 8.0) < 1e-9,
            std_slots[iso(WS)].conversion_steps if iso(WS) in std_slots else None,
        ))
        expectations.append(_expect(
            "00:15 读数回退到 5 → 重置隔离，负差不计为节能",
            len(q_reset) == 1 and all(f.standard_value >= 0 for f in facts),
            [q.reason_detail for q in q_reset],
        ))

    elif meta["key"] == "unit_switch":
        std_slots = {iso(f.slot_start): f for f in facts}
        q_factor = [q for q in quars if q.reason_code == "unit_factor_unknown"]
        expectations.append(_expect(
            "5000 L × 0.001 = 5 m³，换算步骤含明确因子与方向",
            iso(WS) in std_slots and abs(std_slots[iso(WS)].standard_value - 5.0) < 1e-9
            and any(x.get("factor") == 0.001 for x in std_slots[iso(WS)].conversion_steps),
            std_slots[iso(WS)].conversion_steps if iso(WS) in std_slots else None,
        ))
        boundary = std_slots.get(iso(WS + timedelta(minutes=30)))
        expectations.append(_expect(
            "00:30 切换为 m³，窗口按边界拆分且 boundary_split=True",
            boundary is not None and boundary.boundary_split is True
            and abs(boundary.standard_value - 4.2) < 1e-9,
            {"boundary_split": boundary.boundary_split if boundary else None},
        ))
        expectations.append(_expect(
            "加仑 gal 无登记换算因子 → 隔离待核",
            len(q_factor) == 1,
            [q.reason_detail for q in q_factor],
        ))

    elif meta["key"] == "feedback_power":
        neg = next((f for f in facts if f.standard_value < 0), None)
        expectations.append(_expect(
            "双向契约下 -200 kW 带可验证回送含义，积分为 -50 kWh 标准事实",
            neg is not None and abs(neg.standard_value - (-50.0)) < 1e-6
            and any(x["step"] == "sign" for x in neg.conversion_steps),
            neg.conversion_steps if neg else None,
        ))
        fbf_meter = db.execute(select(Meter).where(Meter.code == PROBE_PREFIX + "FBF")).scalars().one()
        fbf_q = _quarantine_by_meter(db, fbf_meter.id, version)
        expectations.append(_expect(
            "单向表计出现负值且无回送含义 → 符号隔离",
            any(q.reason_code == "sign_feedback_unverified" for q in fbf_q),
            [q.reason_detail for q in fbf_q],
        ))

    elif meta["key"] == "protocol_upgrade":
        std_slots = {iso(f.slot_start): f for f in facts}
        upgraded = std_slots.get(iso(WS + timedelta(minutes=30)))
        expectations.append(_expect(
            "升级前累计口径求差：5020−5000=20、5040−5020=20 kWh",
            iso(WS) in std_slots and iso(WS + timedelta(minutes=15)) in std_slots
            and abs(std_slots[iso(WS)].standard_value - 20.0) < 1e-9,
            [round(std_slots[s].standard_value, 4) for s in (iso(WS), iso(WS + timedelta(minutes=15))) if s in std_slots],
        ))
        expectations.append(_expect(
            "00:30 升级为区间口径，6.5 kWh 直接采用不二次求差，边界拆分",
            upgraded is not None and upgraded.source_semantics == SEMANTIC_INTERVAL
            and abs(upgraded.standard_value - 6.5) < 1e-9 and upgraded.boundary_split is True,
            {"semantics": upgraded.source_semantics if upgraded else None},
        ))

    elif meta["key"] == "contract_conflict":
        expectations.append(_expect(
            "同期间两个契约冲突，读数隔离为 contract_conflict",
            len(quars) == 1 and quars[0].reason_code == "contract_conflict"
            and len(quars[0].contract_ids) == 2 and not facts,
            [q.reason_detail for q in quars],
        ))

    elif meta["key"] == "missing_contract":
        expectations.append(_expect(
            "无契约读数 numeric_parseable=True 但隔离 missing_contract，不填零",
            len(quars) == 1 and quars[0].reason_code == "missing_contract"
            and quars[0].numeric_parseable is True and not facts,
            [q.reason_detail for q in quars],
        ))

    elif meta["key"] == "instant_interval":
        expectations.append(_expect(
            "代表时段为 0 的瞬时功率无法积分 → instant_interval_unsupported",
            len(quars) == 1 and quars[0].reason_code == "instant_interval_unsupported" and not facts,
            [q.reason_detail for q in quars],
        ))

    return {
        "key": meta["key"],
        "title": meta["title"],
        "meter": meta["meter"],
        "passed": all(e["passed"] for e in expectations),
        "expectations": expectations,
        "standard_facts": [_fact_brief(f) for f in facts],
        "quarantined": [_quarantine_brief(q) for q in quars],
    }


def _fact_brief(f: StandardIntervalFact) -> dict[str, Any]:
    return {
        "slot": iso(f.slot_start),
        "slot_end": iso(f.slot_end),
        "standard_unit": f.standard_unit,
        "standard_value": f.standard_value,
        "source_semantics": f.source_semantics,
        "boundary_split": f.boundary_split,
        "release_version": f.contract_release_version,
        "conversion_steps": f.conversion_steps,
    }


def _quarantine_brief(q: QuarantineRecord) -> dict[str, Any]:
    return {
        "slot": iso(q.slot_start),
        "reason_code": q.reason_code,
        "reason_detail": q.reason_detail,
        "numeric_parseable": q.numeric_parseable,
        "release_version": q.contract_release_version,
    }


# ------------------------------------------------------------------ 入口


def run_semantic_acceptance(db: Session) -> dict[str, Any]:
    reset_semantic_demo(db)
    metas = build_scenarios(db)
    db.flush()
    # 编译受影响的两个来源窗口
    elec = db.execute(select(Source).where(Source.code == "electricity")).scalars().one()
    water = db.execute(select(Source).where(Source.code == "water")).scalars().one()
    r1 = semantics.compile_window(db, elec, WS, WE, WE)
    r2 = semantics.compile_window(db, water, WS, WE, WE)
    version = r1["contract_release_version"]
    db.flush()

    results = [_verify(db, m, version) for m in metas]
    all_standards = (
        db.execute(select(StandardIntervalFact).where(
            StandardIntervalFact.contract_release_version == version
        ).order_by(StandardIntervalFact.meter_id, StandardIntervalFact.slot_start))
        .scalars().all()
    )
    all_quars = (
        db.execute(select(QuarantineRecord).where(
            QuarantineRecord.contract_release_version == version
        ).order_by(QuarantineRecord.meter_id, QuarantineRecord.slot_start))
        .scalars().all()
    )
    reasons: dict[str, int] = {}
    for q in all_quars:
        reasons[q.reason_code] = reasons.get(q.reason_code, 0) + 1
    return {
        "ok": all(r["passed"] for r in results),
        "window": {"start": iso(WS), "end": iso(WE)},
        "contract_release_version": version,
        "scenarios": results,
        "summary": {
            "standard_facts": len(all_standards),
            "quarantined": len(all_quars),
            "quarantine_reasons": reasons,
            "all_scenarios_passed": all(r["passed"] for r in results),
        },
        "standard_facts": [_fact_brief(f) for f in all_standards],
        "quarantined": [_quarantine_brief(q) for q in all_quars],
    }
