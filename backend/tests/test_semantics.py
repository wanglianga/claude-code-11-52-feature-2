"""表计数据语义契约与兼容转换治理规则测试。

覆盖：kWh/kW 混入、累计表重置、升/立方米切换、回送功率、同日协议升级、冲突契约，
以及量纲/时间语义、单位换算方向、边界拆窗、来源声明缺失、积分间隔、版本固定与
契约更正重放、语义待核对确认版的阻断。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app.models import (
    Building,
    Meter,
    QuarantineRecord,
    SEMANTIC_CUMULATIVE,
    SEMANTIC_INSTANT,
    SEMANTIC_INTERVAL,
    Source,
    StandardIntervalFact,
)
from app.services import semantics
from app.services.semantic_demo import run_semantic_acceptance

DAY = datetime(2026, 11, 10, 0, 0)
WS = DAY
WE = DAY + timedelta(hours=6)
YESTERDAY = DAY - timedelta(days=1)


def _db():
    from app.db import SessionLocal

    return SessionLocal()


def _source(db, code):
    return db.execute(select(Source).where(Source.code == code)).scalars().one()


def _building(db, code="B1"):
    return db.execute(select(Building).where(Building.code == code)).scalars().one()


def _probe(db, source, code, building=None):
    m = Meter(
        code=f"T-SEM-{code}",
        name=f"单测探针 {code}",
        source_id=source.id,
        building_id=(building or _building(db)).id,
        cumulative=False,
        multiplier=1.0,
        status="inactive",
    )
    db.add(m)
    db.flush()
    return m


def _read(db, meter, slot, value, unit, version="v1", **kw):
    return semantics.ingest_raw_reading(
        db, meter, slot, value, raw_unit=unit, source_code="t",
        source_version=version, received_at=WE, slot_minutes=15, **kw
    )


def _contract(db, meter, **kw):
    kw.setdefault("source_code", "t")
    kw.setdefault("source_version", "v1")
    kw.setdefault("measurement_object", meter.name)
    kw.setdefault("valid_from", YESTERDAY)
    kw.setdefault("representative_minutes", 15)
    return semantics.register_contract(db, meter, **kw)


def _facts(db, meter, version=None):
    stmt = select(StandardIntervalFact).where(StandardIntervalFact.meter_id == meter.id)
    if version is not None:
        stmt = stmt.where(StandardIntervalFact.contract_release_version == version)
    return db.execute(stmt.order_by(StandardIntervalFact.slot_start)).scalars().all()


def _quars(db, meter, version=None):
    stmt = select(QuarantineRecord).where(QuarantineRecord.meter_id == meter.id)
    if version is not None:
        stmt = stmt.where(QuarantineRecord.contract_release_version == version)
    return db.execute(stmt.order_by(QuarantineRecord.slot_start)).scalars().all()


def _cleanup(db, *meters):
    from app.services.semantic_demo import purge_meters

    purge_meters(db, [m.id for m in meters])
    db.commit()


# ------------------------------------------------------------------ 端到端验收


def test_semantic_acceptance_all_scenarios_pass(client):
    resp = client.post("/api/semantics/acceptance/run", json={})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert len(data["scenarios"]) == 8
    assert all(s["passed"] for s in data["scenarios"])
    # 六类题目样例 + 缺失声明 + 积分间隔，均产生隔离或标准事实
    assert data["summary"]["standard_facts"] > 0
    assert data["summary"]["quarantined"] > 0


# ------------------------------------------------------------------ 量纲：kWh/kW


def test_power_must_not_be_added_to_energy_and_instant_integrates(client):
    with _db() as db:
        elec = _source(db, "electricity")
        m = _probe(db, elec, "DIM")
        _contract(db, m, physical_quantity="electricity", raw_unit="kWh",
                  reading_semantics=SEMANTIC_INTERVAL, valid_from=WS,
                  valid_to=WS + timedelta(minutes=30))
        _contract(db, m, physical_quantity="electricity", raw_unit="kW",
                  reading_semantics=SEMANTIC_INSTANT,
                  valid_from=WS + timedelta(minutes=30), source_version="v2")
        _read(db, m, WS, 25.0, "kWh", "v1")
        _read(db, m, WS + timedelta(minutes=15), 1500.0, "kW", "v1")  # 错配
        _read(db, m, WS + timedelta(minutes=30), 1500.0, "kW", "v2")
        r = semantics.compile_window(db, elec, WS, WE, WE)
        db.commit()
        facts = _facts(db, m, r["contract_release_version"])
        quars = _quars(db, m, r["contract_release_version"])
        by_slot = {f.slot_start: f for f in facts}
        # 区间 kWh 直接成立
        assert abs(by_slot[WS].standard_value - 25.0) < 1e-9
        # 1500 kW × 0.25h = 375 kWh
        integ = by_slot[WS + timedelta(minutes=30)]
        assert abs(integ.standard_value - 375.0) < 1e-6
        assert any(s["step"] == "integrate" for s in integ.conversion_steps)
        # 错配的 kW 被量纲隔离，未与电量相加
        assert any(q.reason_code == "dimension_mismatch" for q in quars)
        assert all(f.standard_unit == "kWh" for f in facts)
        _cleanup(db, m)


# ------------------------------------------------------------------ 累计表


def test_cumulative_difference_requires_continuous_readings_and_reset_quarantines(client):
    with _db() as db:
        water = _source(db, "water")
        m = _probe(db, water, "CUM")
        _contract(db, m, physical_quantity="water", raw_unit="m³",
                  reading_semantics=SEMANTIC_CUMULATIVE)
        _read(db, m, WS - timedelta(minutes=15), 100.0, "m³")
        _read(db, m, WS, 108.0, "m³")
        _read(db, m, WS + timedelta(minutes=15), 5.0, "m³")  # 回退
        r = semantics.compile_window(db, water, WS, WE, WE)
        db.commit()
        facts = _facts(db, m, r["contract_release_version"])
        quars = _quars(db, m, r["contract_release_version"])
        assert len(facts) == 1 and abs(facts[0].standard_value - 8.0) < 1e-9
        assert any(q.reason_code == "cumulative_reset" for q in quars)
        # 负差绝不计为节能
        assert all(f.standard_value >= 0 for f in facts)
        _cleanup(db, m)


def test_cumulative_bridge_gap_and_baseline_quarantine(client):
    with _db() as db:
        water = _source(db, "water")
        m = _probe(db, water, "BRG")
        _contract(db, m, physical_quantity="water", raw_unit="m³",
                  reading_semantics=SEMANTIC_CUMULATIVE)
        # 无基线：首槽隔离
        _read(db, m, WS, 10.0, "m³")
        r1 = semantics.compile_window(db, water, WS, WE, WE)
        db.commit()
        assert any(q.reason_code == "cumulative_baseline" for q in _quars(db, m, r1["contract_release_version"]))
        _cleanup(db, m)

    with _db() as db:
        water = _source(db, "water")
        m = _probe(db, water, "BRG2")
        _contract(db, m, physical_quantity="water", raw_unit="m³",
                  reading_semantics=SEMANTIC_CUMULATIVE)
        _read(db, m, WS - timedelta(minutes=15), 10.0, "m³")
        _read(db, m, WS, 12.0, "m³")
        # 跳过 00:15，直接 00:30 → 跨未覆盖槽位
        _read(db, m, WS + timedelta(minutes=30), 20.0, "m³")
        r = semantics.compile_window(db, water, WS, WE, WE)
        db.commit()
        quars = _quars(db, m, r["contract_release_version"])
        assert any(q.reason_code == "bridge_gap" for q in quars)
        _cleanup(db, m)


# ------------------------------------------------------------------ 单位换算


def test_litre_to_cubic_metre_factor_and_boundary_split(client):
    with _db() as db:
        water = _source(db, "water")
        m = _probe(db, water, "UNIT")
        _contract(db, m, physical_quantity="water", raw_unit="L",
                  reading_semantics=SEMANTIC_INTERVAL, valid_from=WS,
                  valid_to=WS + timedelta(minutes=30))
        _contract(db, m, physical_quantity="water", raw_unit="m³",
                  reading_semantics=SEMANTIC_INTERVAL,
                  valid_from=WS + timedelta(minutes=30), source_version="v2")
        _read(db, m, WS, 5000.0, "L", "v1")
        _read(db, m, WS + timedelta(minutes=30), 4.2, "m³", "v2")
        r = semantics.compile_window(db, water, WS, WE, WE)
        db.commit()
        facts = {f.slot_start: f for f in _facts(db, m, r["contract_release_version"])}
        first = facts[WS]
        assert abs(first.standard_value - 5.0) < 1e-9  # 5000 L = 5 m³
        assert any(x.get("factor") == 0.001 for x in first.conversion_steps)
        second = facts[WS + timedelta(minutes=30)]
        assert second.boundary_split is True  # 有效期交界拆窗
        assert abs(second.standard_value - 4.2) < 1e-9
        _cleanup(db, m)


def test_unknown_unit_factor_is_quarantined_not_guessed(client):
    with _db() as db:
        water = _source(db, "water")
        m = _probe(db, water, "GAL")
        _contract(db, m, physical_quantity="water", raw_unit="m³",
                  reading_semantics=SEMANTIC_INTERVAL)
        _read(db, m, WS, 2.0, "gal")  # 注册表没有 gal
        r = semantics.compile_window(db, water, WS, WE, WE)
        db.commit()
        quars = _quars(db, m, r["contract_release_version"])
        assert any(q.reason_code == "unit_factor_unknown" for q in quars)
        assert _facts(db, m, r["contract_release_version"]) == []
        _cleanup(db, m)


# ------------------------------------------------------------------ 符号方向


def test_feedback_negative_needs_verifiable_meaning(client):
    with _db() as db:
        elec = _source(db, "electricity")
        bi = _probe(db, elec, "BI")
        _contract(db, bi, physical_quantity="electricity", raw_unit="kW",
                  reading_semantics=SEMANTIC_INSTANT, sign_direction="bidirectional",
                  feedback_meaning="反向馈送园区电网")
        _read(db, bi, WS, -200.0, "kW")
        r = semantics.compile_window(db, elec, WS, WE, WE)
        db.commit()
        facts = _facts(db, bi, r["contract_release_version"])
        assert len(facts) == 1 and abs(facts[0].standard_value - (-50.0)) < 1e-6

        fwd = _probe(db, elec, "FWD")
        _contract(db, fwd, physical_quantity="electricity", raw_unit="kW",
                  reading_semantics=SEMANTIC_INSTANT, sign_direction="forward")
        _read(db, fwd, WS, -50.0, "kW")
        r2 = semantics.compile_window(db, elec, WS, WE, WE)
        db.commit()
        assert any(q.reason_code == "sign_feedback_unverified"
                   for q in _quars(db, fwd, r2["contract_release_version"]))
        _cleanup(db, bi, fwd)


# ------------------------------------------------------------------ 协议升级


def test_same_day_protocol_upgrade_splits_at_boundary(client):
    with _db() as db:
        elec = _source(db, "electricity")
        m = _probe(db, elec, "UP")
        _contract(db, m, physical_quantity="electricity", raw_unit="kWh",
                  reading_semantics=SEMANTIC_CUMULATIVE, valid_from=YESTERDAY,
                  valid_to=WS + timedelta(minutes=30))
        _contract(db, m, physical_quantity="electricity", raw_unit="kWh",
                  reading_semantics=SEMANTIC_INTERVAL,
                  valid_from=WS + timedelta(minutes=30), source_version="v2")
        _read(db, m, WS - timedelta(minutes=15), 5000.0, "kWh", "v1")
        _read(db, m, WS, 5020.0, "kWh", "v1")
        _read(db, m, WS + timedelta(minutes=30), 6.5, "kWh", "v2")
        r = semantics.compile_window(db, elec, WS, WE, WE)
        db.commit()
        facts = {f.slot_start: f for f in _facts(db, m, r["contract_release_version"])}
        assert abs(facts[WS].standard_value - 20.0) < 1e-9  # 累计求差
        upgraded = facts[WS + timedelta(minutes=30)]
        assert upgraded.source_semantics == SEMANTIC_INTERVAL  # 不二次累计
        assert upgraded.boundary_split is True
        assert abs(upgraded.standard_value - 6.5) < 1e-9
        _cleanup(db, m)


# ------------------------------------------------------------------ 冲突 / 缺失


def test_conflicting_contracts_same_period_quarantine(client):
    with _db() as db:
        elec = _source(db, "electricity")
        m = _probe(db, elec, "CF")
        _contract(db, m, physical_quantity="electricity", raw_unit="kWh",
                  reading_semantics=SEMANTIC_INTERVAL, allow_overlap=True)
        _contract(db, m, physical_quantity="electricity", raw_unit="kW",
                  reading_semantics=SEMANTIC_INSTANT, allow_overlap=True,
                  source_version="vx")
        _read(db, m, WS, 1500.0, "kW", "vx")
        r = semantics.compile_window(db, elec, WS, WE, WE)
        db.commit()
        quars = _quars(db, m, r["contract_release_version"])
        assert len(quars) == 1 and quars[0].reason_code == "contract_conflict"
        assert len(quars[0].contract_ids) == 2
        assert _facts(db, m, r["contract_release_version"]) == []
        _cleanup(db, m)


def test_numeric_parseable_is_not_usable_without_contract(client):
    with _db() as db:
        elec = _source(db, "electricity")
        m = _probe(db, elec, "NC")
        _read(db, m, WS, 42.0, "kWh", "mystery-v9")  # 数值合法，无契约
        r = semantics.compile_window(db, elec, WS, WE, WE)
        db.commit()
        quars = _quars(db, m, r["contract_release_version"])
        assert len(quars) == 1
        assert quars[0].reason_code == "missing_contract"
        assert quars[0].numeric_parseable is True  # 可解析 ≠ 可用
        assert _facts(db, m, r["contract_release_version"]) == []
        _cleanup(db, m)


def test_unparseable_numeric_is_quarantined(client):
    with _db() as db:
        elec = _source(db, "electricity")
        m = _probe(db, elec, "NAN")
        _contract(db, m, physical_quantity="electricity", raw_unit="kWh",
                  reading_semantics=SEMANTIC_INTERVAL)
        semantics.ingest_raw_reading(
            db, m, WS, "N/A", raw_unit="kWh", source_code="t",
            source_version="v1", received_at=WE, slot_minutes=15
        )
        r = semantics.compile_window(db, elec, WS, WE, WE)
        db.commit()
        quars = _quars(db, m, r["contract_release_version"])
        assert any(q.reason_code == "numeric_unparseable" and q.numeric_parseable is False
                   for q in quars)
        _cleanup(db, m)


def test_instant_without_representative_interval_cannot_integrate(client):
    with _db() as db:
        elec = _source(db, "electricity")
        m = _probe(db, elec, "II")
        _contract(db, m, physical_quantity="electricity", raw_unit="kW",
                  reading_semantics=SEMANTIC_INSTANT, representative_minutes=0)
        _read(db, m, WS, 900.0, "kW")
        r = semantics.compile_window(db, elec, WS, WE, WE)
        db.commit()
        quars = _quars(db, m, r["contract_release_version"])
        assert any(q.reason_code == "instant_interval_unsupported" for q in quars)
        _cleanup(db, m)


# ------------------------------------------------------------------ 版本固定与重放


def test_contract_correction_replay_keeps_historical_interpretation(client):
    with _db() as db:
        water = _source(db, "water")
        m = _probe(db, water, "REP")
        c = _contract(db, m, physical_quantity="water", raw_unit="L",
                      reading_semantics=SEMANTIC_INTERVAL)
        _read(db, m, WS, 5000.0, "L")
        r1 = semantics.compile_window(db, water, WS, WE, WE)
        v1 = r1["contract_release_version"]
        f1 = _facts(db, m, v1)
        assert len(f1) == 1 and abs(f1[0].standard_value - 5.0) < 1e-9

        # 契约更正：显式把 L→m³ 因子改成错误的 0.002（模拟登记更正），产生新版本
        c.unit_factor = 0.002
        c.unit_factor_evidence = "更正后因子 standard = raw × 0.002"
        db.flush()
        replay = semantics.replay(db, meter_id=m.id)
        db.commit()
        v2 = replay["new_release_version"]
        assert v2 != v1

        # 新版本按更正解释；旧版本事实仍保留，历史产品可按 v1 复演
        facts_v2 = _facts(db, m, v2)
        facts_v1 = _facts(db, m, v1)
        assert len(facts_v2) == 1 and abs(facts_v2[0].standard_value - 10.0) < 1e-9
        assert len(facts_v1) == 1 and abs(facts_v1[0].standard_value - 5.0) < 1e-9
        # 同一原记录在两个解释版本各一条，互不覆盖
        assert facts_v1[0].raw_reading_id == facts_v2[0].raw_reading_id
        assert facts_v1[0].contract_release_version != facts_v2[0].contract_release_version
        _cleanup(db, m)


# ------------------------------------------------------------------ 确认版阻断


def test_semantic_gate_blocks_certification_when_quarantined(client):
    from app.services.health import evaluate_window

    with _db() as db:
        elec = _source(db, "electricity")
        m = _probe(db, elec, "GATE")
        # 激活探针但不建采样承诺：不影响覆盖分母，只触发语义闸门
        m.status = "active"
        _read(db, m, WS, 42.0, "kWh", "no-contract-x")  # 无契约 → 必隔离
        ev = evaluate_window(db, elec, WS, WE, WE)
        db.commit()
        assert ev.contract_release_version is not None
        assert ev.details["semantic"]["semantic_blocked"] is True
        assert ev.details["counts"]["quarantined"] >= 1
        # 即便覆盖/核验本身没问题，语义待核也阻止确认版
        assert ev.certified is False
        assert ev.confirmable is False
        m.status = "inactive"
        _cleanup(db, m)


def test_standard_facts_carry_conversion_trace_and_release(client):
    with _db() as db:
        water = _source(db, "water")
        m = _probe(db, water, "TRACE")
        _contract(db, m, physical_quantity="water", raw_unit="L",
                  reading_semantics=SEMANTIC_INTERVAL)
        _read(db, m, WS, 3000.0, "L")
        r = semantics.compile_window(db, water, WS, WE, WE)
        db.commit()
        f = _facts(db, m, r["contract_release_version"])[0]
        steps = {(s["step"]) for s in f.conversion_steps}
        assert "raw" in steps and "unit" in steps
        assert f.standard_unit == "m³"
        assert f.contract_release_version == r["contract_release_version"]
        series = semantics.standard_series(db, water.id, WS, WE)
        assert any(abs(x["value"] - 3.0) < 1e-9 for x in series)
        _cleanup(db, m)
