from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (
    Building,
    Consumer,
    ConsumerDependency,
    MaintenanceWindow,
    Meter,
    MeterCommitment,
    Sample,
    SimClock,
    Source,
    User,
)

SIM_DAY = "2026-11-10"
SIM_START = datetime(2026, 11, 10, 0, 0)


def _password_hash(raw: str) -> str:
    return hashlib.sha256(("slo-demo::" + raw).encode()).hexdigest()


def _ensure_users(db: Session) -> None:
    if db.execute(select(func.count()).select_from(User)).scalar_one() > 0:
        return
    db.add_all(
        [
            User(
                username="admin",
                display_name="平台管理员",
                role="admin",
                password_hash=_password_hash("admin123"),
            ),
            User(
                username="ops",
                display_name="值班调度",
                role="operator",
                password_hash=_password_hash("ops123"),
            ),
        ]
    )


def _ensure_catalog(db: Session) -> None:
    if db.execute(select(func.count()).select_from(Source)).scalar_one() > 0:
        return

    sources = {
        "electricity": Source(
            code="electricity",
            name="电力",
            unit="kWh",
            product_field="electricity",
            description="园区总表与楼栋分表，15 分钟累计表码",
        ),
        "water": Source(
            code="water",
            name="自来水",
            unit="m³",
            product_field="water",
            description="楼栋水表，15 分钟区间用量",
        ),
        "steam": Source(
            code="steam",
            name="蒸汽",
            unit="t",
            product_field="steam",
            description="生产蒸汽流量计，15 分钟累计表码",
        ),
        "pv": Source(
            code="pv",
            name="光伏",
            unit="kWh",
            product_field="pv",
            description="光伏逆变器，15 分钟区间发电量",
        ),
    }
    db.add_all(sources.values())
    db.flush()

    buildings = {
        "B1": Building(code="B1", name="研发楼", kind="office"),
        "B2": Building(code="B2", name="生产车间", kind="factory"),
        "B3": Building(code="B3", name="仓储中心", kind="warehouse"),
        "B4": Building(code="B4", name="综合办公楼", kind="office"),
    }
    db.add_all(buildings.values())
    db.flush()

    meter_defs = [
        ("M-ELE-B1-01", "研发楼 1# 进线", "electricity", "B1", True, 1.0),
        ("M-ELE-B1-02", "研发楼 2# 进线", "electricity", "B1", True, 1.0),
        ("M-ELE-B2-01", "生产车间动力进线", "electricity", "B2", True, 1.0),
        ("M-ELE-B3-01", "仓储中心进线", "electricity", "B3", True, 1.0),
        ("M-ELE-B4-01", "综合办公楼进线", "electricity", "B4", True, 1.0),
        ("M-WAT-B1-01", "研发楼水表", "water", "B1", False, 1.0),
        ("M-WAT-B2-01", "生产车间水表", "water", "B2", False, 1.0),
        ("M-WAT-B4-01", "综合办公楼水表", "water", "B4", False, 1.0),
        ("M-STM-B2-01", "生产车间蒸汽流量计", "steam", "B2", True, 1.0),
        ("M-STM-B3-01", "仓储蒸汽流量计", "steam", "B3", True, 1.0),
        ("M-PV-01", "光伏 1# 逆变器", "pv", "B2", False, 1.0),
    ]
    for code, name, src, bld, cumulative, mult in meter_defs:
        db.add(
            Meter(
                code=code,
                name=name,
                source_id=sources[src].id,
                building_id=buildings[bld].id,
                cumulative=cumulative,
                multiplier=mult,
            )
        )
    db.flush()

    for meter in db.execute(select(Meter)).scalars():
        db.add(
            MeterCommitment(
                meter_id=meter.id,
                valid_from=datetime(2026, 1, 1),
                cadence_minutes=15,
                allowed_lateness_minutes=30,
                note="标准 15 分钟采样承诺",
            )
        )

    b3 = db.execute(select(Meter).where(Meter.code == "M-ELE-B3-01")).scalars().one()
    db.add(
        MaintenanceWindow(
            meter_id=b3.id,
            starts_at=datetime(2026, 11, 10, 2, 0),
            ends_at=datetime(2026, 11, 10, 3, 0),
            allowed_deduction=True,
            recorded_at=datetime(2026, 11, 9, 18, 0),
            reason="计划停电检修（提前登记）",
            created_by="ops",
        )
    )

    consumers = {
        "property_dashboard": Consumer(
            code="property_dashboard",
            name="物业调度大屏",
            kind="property_dashboard",
            description="近实时负荷曲线，用于安排楼宇负荷；容忍暂定数据与短缺口",
            tolerance_minutes=60,
            min_coverage=0.5,
            max_gap_slots=40,
            require_certified=False,
        ),
        "settlement_billing": Consumer(
            code="settlement_billing",
            name="月度结算账单",
            kind="settlement",
            description="按核验日汇总出账，要求完整、按期到达且已核验",
            tolerance_minutes=30,
            min_coverage=0.98,
            max_gap_slots=0,
            require_certified=True,
        ),
        "energy_assessment": Consumer(
            code="energy_assessment",
            name="节能考核报表",
            kind="assessment",
            description="考核研发楼与生产车间单位能耗，依赖核验日汇总与光伏",
            tolerance_minutes=30,
            min_coverage=0.95,
            max_gap_slots=3,
            require_certified=True,
        ),
    }
    db.add_all(consumers.values())
    db.flush()

    rt, daily = "energy.realtime", "energy.daily"

    def dep(consumer: Consumer, product: str, field: str, building: str | None = None):
        db.add(
            ConsumerDependency(
                consumer_id=consumer.id,
                product_code=product,
                product_field=field,
                building_code=building,
                valid_from=datetime(2026, 1, 1),
                note="",
            )
        )

    for field in ("electricity", "water"):
        dep(consumers["property_dashboard"], rt, field)
    for field in ("electricity", "water", "steam"):
        dep(consumers["settlement_billing"], daily, field)
    for building in ("B1", "B2"):
        dep(consumers["energy_assessment"], daily, "electricity", building)
    dep(consumers["energy_assessment"], daily, "pv")
    db.flush()


def seed_if_empty(db: Session) -> None:
    _ensure_users(db)
    _ensure_catalog(db)
    db.flush()
    clock = db.get(SimClock, 1)
    if clock is not None:
        db.commit()
        return

    from .services.ingest import generate_samples
    from .services.simulator import run_checks

    start = SIM_START - timedelta(hours=12)
    clock = SimClock(id=1, now=SIM_START + timedelta(hours=6), day=SIM_DAY)
    db.add(clock)
    db.flush()
    generate_samples(db, start, clock.now)
    db.commit()
    run_checks(db, clock.now)
    db.commit()
