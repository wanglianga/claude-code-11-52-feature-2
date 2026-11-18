from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .timeutil import utcnow

# 语义口径（reading_semantics）取值：
#   cumulative 累计表码（只能在可确认的连续读数之间求差）
#   interval   区间读数（已经是区间用量，不得再次累计）
#   instant    瞬时量（如功率 kW，只能按可支持积分的间隔换算，不能与电量直接相加）
SEMANTIC_CUMULATIVE = "cumulative"
SEMANTIC_INTERVAL = "interval"
SEMANTIC_INSTANT = "instant"
SEMANTIC_VALUES = (SEMANTIC_CUMULATIVE, SEMANTIC_INTERVAL, SEMANTIC_INSTANT)

# 标准化结论
COMPILE_STANDARD = "standard"  # 证据完整，已转为标准区间事实
COMPILE_QUARANTINED = "quarantined"  # 无法证明语义，隔离待核（不填零）

# 待核原因代码（quarantine reason）
Q_NO_CONTRACT = "missing_contract"  # 来源声明缺失，没有适用契约
Q_CONTRACT_CONFLICT = "contract_conflict"  # 同一有效期存在两个冲突契约
Q_DIMENSION = "dimension_mismatch"  # 量纲不兼容（如 kW 与 kWh 混入）
Q_CUMULATIVE_RESET = "cumulative_reset"  # 累计读数回退且无连续基线
Q_CUMULATIVE_BASELINE = "cumulative_baseline"  # 累计表缺少可衔接基线
Q_BRIDGE_GAP = "bridge_gap"  # 表码差跨过未覆盖槽位
Q_INTERVAL_REACCUM = "interval_reaccumulation"  # 区间读数被当作累计再次求差
Q_UNIT_FACTOR = "unit_factor_unknown"  # 单位换算缺少明确因子/方向
Q_SIGN_FEEDBACK = "sign_feedback_unverified"  # 负向符号没有可验证的回送含义
Q_INSTANT_INTERVAL = "instant_interval_unsupported"  # 读数间隔不支持功率积分
Q_MULTIPLIER = "multiplier_unresolved"  # 倍率引用无法解析
Q_NUMERIC = "numeric_unparseable"  # 数值本身不可解析


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(32), default="operator")
    password_hash: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SimClock(Base):
    __tablename__ = "sim_clock"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    now: Mapped[datetime] = mapped_column(DateTime)
    day: Mapped[str] = mapped_column(String(16))
    revision: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Building(Base):
    __tablename__ = "buildings"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(64))


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    unit: Mapped[str] = mapped_column(String(32))
    product_field: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text, default="")

    meters: Mapped[list[Meter]] = relationship(back_populates="source")


class Meter(Base):
    __tablename__ = "meters"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    building_id: Mapped[int] = mapped_column(ForeignKey("buildings.id"))
    multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    cumulative: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="active")
    linked_from_meter_id: Mapped[int | None] = mapped_column(
        "linked_from_meter_id", ForeignKey("meters.id"), nullable=True
    )
    link_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")

    source: Mapped[Source] = relationship(back_populates="meters")
    building: Mapped[Building] = relationship()
    linked_from: Mapped[Meter | None] = relationship(
        "Meter",
        primaryjoin="Meter.linked_from_meter_id == Meter.id",
        remote_side="Meter.id",
        foreign_keys=[linked_from_meter_id],
        post_update=True,
    )


class MeterCommitment(Base):
    """采样承诺：有效安装期 + 计划频率 + 允许迟到 + 核验要求。"""

    __tablename__ = "meter_commitments"

    id: Mapped[int] = mapped_column(primary_key=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"), index=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cadence_minutes: Mapped[int] = mapped_column(Integer, default=15)
    allowed_lateness_minutes: Mapped[int] = mapped_column(Integer, default=30)
    certification_required: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    meter: Mapped[Meter] = relationship()


class MaintenanceWindow(Base):
    __tablename__ = "maintenance_windows"

    id: Mapped[int] = mapped_column(primary_key=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"), index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime)
    ends_at: Mapped[datetime] = mapped_column(DateTime)
    allowed_deduction: Mapped[bool] = mapped_column(Boolean, default=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="ops")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    meter: Mapped[Meter] = relationship()


class Sample(Base):
    __tablename__ = "samples"
    __table_args__ = (
        Index("ix_samples_meter_slot", "meter_id", "slot_start"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"))
    slot_start: Mapped[datetime] = mapped_column(DateTime)
    value: Mapped[float] = mapped_column(Float)
    received_at: Mapped[datetime] = mapped_column(DateTime)
    is_declared_zero: Mapped[bool] = mapped_column(Boolean, default=False)
    is_correction: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(32), default="meter")
    note: Mapped[str] = mapped_column(Text, default="")

    meter: Mapped[Meter] = relationship()


class HealthEvaluation(Base):
    __tablename__ = "health_evaluations"
    __table_args__ = (
        UniqueConstraint(
            "source_id", "window_start", "window_end", "data_cutoff", "revision",
            name="uq_health_eval_key",
        ),
        Index("ix_health_eval_window", "window_start", "window_end"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    window_start: Mapped[datetime] = mapped_column(DateTime)
    window_end: Mapped[datetime] = mapped_column(DateTime)
    data_cutoff: Mapped[datetime] = mapped_column(DateTime)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32))
    freshness: Mapped[str] = mapped_column(String(32))
    coverage_num: Mapped[int] = mapped_column(Integer, default=0)
    coverage_den: Mapped[int] = mapped_column(Integer, default=0)
    coverage_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    verification_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    certified: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmable: Mapped[bool] = mapped_column(Boolean, default=False)
    max_lateness_minutes: Mapped[int] = mapped_column(Integer, default=0)
    recompute_count: Mapped[int] = mapped_column(Integer, default=1)
    # 本次评估锁定的语义契约解释版本（历史评估可按该版本复演解释）
    contract_release_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    source: Mapped[Source] = relationship()
    meters: Mapped[list[MeterEvaluation]] = relationship(
        back_populates="evaluation", cascade="all, delete-orphan"
    )


class MeterEvaluation(Base):
    __tablename__ = "meter_evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    evaluation_id: Mapped[int] = mapped_column(
        ForeignKey("health_evaluations.id", ondelete="CASCADE"), index=True
    )
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"))
    building_code: Mapped[str] = mapped_column(String(32))
    expected: Mapped[int] = mapped_column(Integer, default=0)
    observed: Mapped[int] = mapped_column(Integer, default=0)
    missing: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    pending: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    late: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    maintenance: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    segments: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    verification: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    duplicate_slots: Mapped[int] = mapped_column(Integer, default=0)
    declared_zero_slots: Mapped[int] = mapped_column(Integer, default=0)
    consumption: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    evaluation: Mapped[HealthEvaluation] = relationship(back_populates="meters")
    meter: Mapped[Meter] = relationship()


class BreachFact(Base):
    """不可抹去的违约事实：即使事后补维护或补数据，事实保留并标记处置结果。"""

    __tablename__ = "breach_facts"
    __table_args__ = (
        UniqueConstraint("meter_id", "slot_start", "kind", name="uq_breach_meter_slot_kind"),
        Index("ix_breach_source", "source_id", "slot_start"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"))
    slot_start: Mapped[datetime] = mapped_column(DateTime)
    slot_end: Mapped[datetime] = mapped_column(DateTime)
    kind: Mapped[str] = mapped_column(String(32))  # missing | verification | late
    reason: Mapped[str] = mapped_column(String(128), default="")
    detected_at: Mapped[datetime] = mapped_column(DateTime)
    evaluation_id: Mapped[int | None] = mapped_column(ForeignKey("health_evaluations.id"), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by_sample_id: Mapped[int | None] = mapped_column(ForeignKey("samples.id"), nullable=True)
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("incidents.id"), nullable=True)
    retroactive_maintenance_id: Mapped[int | None] = mapped_column(
        ForeignKey("maintenance_windows.id"), nullable=True
    )
    note: Mapped[str] = mapped_column(Text, default="")

    meter: Mapped[Meter] = relationship()


class ProductRun(Base):
    __tablename__ = "product_runs"
    __table_args__ = (
        UniqueConstraint(
            "product_code", "product_field", "window_start", "window_end",
            "data_cutoff", "revision", name="uq_product_run_key",
        ),
        Index("ix_product_window", "product_code", "product_field", "window_start"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_code: Mapped[str] = mapped_column(String(64))
    product_field: Mapped[str] = mapped_column(String(32))
    window_start: Mapped[datetime] = mapped_column(DateTime)
    window_end: Mapped[datetime] = mapped_column(DateTime)
    data_cutoff: Mapped[datetime] = mapped_column(DateTime)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32))
    # 产品发布固定引用的语义契约解释版本；所有消费者必须引用同一解释版本
    contract_release_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    qualified_ranges: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    evaluation_ids: Mapped[list[int]] = mapped_column(JSONB, default=list)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Consumer(Base):
    __tablename__ = "consumers"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32))  # property_dashboard | settlement | assessment
    description: Mapped[str] = mapped_column(Text, default="")
    tolerance_minutes: Mapped[int] = mapped_column(Integer, default=60)
    min_coverage: Mapped[float] = mapped_column(Float, default=0.8)
    max_gap_slots: Mapped[int] = mapped_column(Integer, default=0)
    require_certified: Mapped[bool] = mapped_column(Boolean, default=False)
    thresholds_version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    dependencies: Mapped[list[ConsumerDependency]] = relationship(
        back_populates="consumer", cascade="all, delete-orphan"
    )


class ConsumerDependency(Base):
    __tablename__ = "consumer_dependencies"

    id: Mapped[int] = mapped_column(primary_key=True)
    consumer_id: Mapped[int] = mapped_column(ForeignKey("consumers.id"), index=True)
    product_code: Mapped[str] = mapped_column(String(64))  # energy.realtime | energy.daily
    product_field: Mapped[str] = mapped_column(String(32))
    building_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")

    consumer: Mapped[Consumer] = relationship(back_populates="dependencies")


class ConsumerImpact(Base):
    __tablename__ = "consumer_impacts"
    __table_args__ = (
        UniqueConstraint(
            "consumer_id", "window_start", "window_end", "revision",
            name="uq_consumer_impact_window",
        ),
        Index("ix_impact_window", "window_start", "window_end"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    consumer_id: Mapped[int] = mapped_column(ForeignKey("consumers.id"))
    window_start: Mapped[datetime] = mapped_column(DateTime)
    window_end: Mapped[datetime] = mapped_column(DateTime)
    data_cutoff: Mapped[datetime] = mapped_column(DateTime)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32))  # ok | restricted | unavailable | waiting
    severity: Mapped[str] = mapped_column(String(32))
    affected_slots: Mapped[int] = mapped_column(Integer, default=0)
    ranges: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    facts: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    threshold_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    consumer: Mapped[Consumer] = relationship()


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    cause_category: Mapped[str] = mapped_column(String(32))  # data_gap | verification
    title: Mapped[str] = mapped_column(String(256))
    summary: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="open")
    opened_at_sim: Mapped[datetime] = mapped_column(DateTime)
    closed_at_sim: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    first_breach_at: Mapped[datetime] = mapped_column(DateTime)
    last_breach_at: Mapped[datetime] = mapped_column(DateTime)
    ranges: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    prior_incident_id: Mapped[int | None] = mapped_column(ForeignKey("incidents.id"), nullable=True)
    recovery_check_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    source: Mapped[Source] = relationship()
    events: Mapped[list[IncidentEvent]] = relationship(
        back_populates="incident", cascade="all, delete-orphan", order_by="IncidentEvent.id"
    )


class IncidentEvent(Base):
    __tablename__ = "incident_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), index=True)
    kind: Mapped[str] = mapped_column(String(48))
    at_sim: Mapped[datetime] = mapped_column(DateTime)
    message: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    incident: Mapped[Incident] = relationship(back_populates="events")


class RecoveryCheck(Base):
    __tablename__ = "recovery_checks"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), index=True)
    evaluation_id: Mapped[int] = mapped_column(ForeignKey("health_evaluations.id"))
    window_start: Mapped[datetime] = mapped_column(DateTime)
    window_end: Mapped[datetime] = mapped_column(DateTime)
    data_cutoff: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(32), default="completed")
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    incident: Mapped[Incident] = relationship()
    evaluation: Mapped[HealthEvaluation] = relationship()


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at_sim: Mapped[datetime] = mapped_column(DateTime)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(128), default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


# ============================================================
# 语义契约治理：契约登记 → 原始受理 → 标准化事实 / 待核隔离 → 产品发布
# 五者通过“契约版本号 + 解释版本号”串联，历史产品永远可按其锁定版本复演。
# ============================================================


class SemanticContract(Base):
    """表计数据语义契约（按来源版本登记，带有效期）。

    一条契约完整描述一个来源版本在有效期内如何解释其读数：计量对象、物理量、
    单位、累计/区间/瞬时含义、采样代表时段、符号方向、倍率引用与有效期。
    契约只追加、不就地改写；更正通过关闭旧契约有效期 + 登记新版本实现。
    """

    __tablename__ = "semantic_contracts"
    __table_args__ = (
        Index("ix_contract_meter_valid", "meter_id", "valid_from", "valid_to"),
        Index("ix_contract_source_version", "source_code", "source_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"), index=True)
    # 来源版本（厂商/协议批次标识），同一来源版本只允许一种已批准解释
    source_code: Mapped[str] = mapped_column(String(64))
    source_version: Mapped[str] = mapped_column(String(64))
    # 计量对象（如“研发楼 1# 进线有功电量”）与物理量（electricity/water/steam/pv）
    measurement_object: Mapped[str] = mapped_column(String(128))
    physical_quantity: Mapped[str] = mapped_column(String(32))
    # 原始读数单位与目标标准单位
    raw_unit: Mapped[str] = mapped_column(String(32))
    standard_unit: Mapped[str] = mapped_column(String(32))
    # 累计 / 区间 / 瞬时
    reading_semantics: Mapped[str] = mapped_column(String(16))
    # 采样代表时段（分钟）：区间读数覆盖的时长；瞬时量的积分代表时段
    representative_minutes: Mapped[int] = mapped_column(Integer, default=15)
    # 符号方向：bidirectional 允许负值（需可验证回送含义）；forward 仅正向
    sign_direction: Mapped[str] = mapped_column(String(32), default="forward")
    # 负向符号的可验证回送含义（sign_direction=bidirectional 时必填，如“反向馈送发电”）
    feedback_meaning: Mapped[str] = mapped_column(Text, default="")
    # 倍率引用（指向 meter.multiplier 或显式因子）
    multiplier_ref: Mapped[str] = mapped_column(String(64), default="meter.multiplier")
    multiplier_factor: Mapped[float] = mapped_column(Float, default=1.0)
    # 单位换算因子与方向：standard_value = raw_value * unit_factor（含方向）
    unit_factor: Mapped[float] = mapped_column(Float, default=1.0)
    unit_factor_evidence: Mapped[str] = mapped_column(Text, default="")
    # 有效期：窗口在交界处按边界拆分，不能用当前配置解释整天
    valid_from: Mapped[datetime] = mapped_column(DateTime)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | superseded
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="ops")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    meter: Mapped[Meter] = relationship()


class ContractRelease(Base):
    """契约发布/锁定版本。

    每次发布把当前所有有效契约固化为一个不可变的契约版本号；产品运行固定引用
    某个发布版本，新契约只影响新运行，历史产品仍按其引用版本复演解释。
    """

    __tablename__ = "contract_releases"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer, unique=True)
    status: Mapped[str] = mapped_column(String(16), default="locked")  # locked | drafted
    contract_ids: Mapped[list[int]] = mapped_column(JSONB, default=list)
    # 契约内容指纹，用于判断重放时解释是否发生变化
    fingerprint: Mapped[str] = mapped_column(String(64), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="ops")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RawReading(Base):
    """原始受理（不可变原载荷）。

    保留厂商上报的原始报文与来源版本；标准化在其之上做投影，不修改原始记录。
    同一原记录在同一解释版本下只有一种已批准解释（unique 约束兜底）。
    """

    __tablename__ = "raw_readings"
    __table_args__ = (
        UniqueConstraint(
            "meter_id", "slot_start", "source_version",
            name="uq_raw_reading_identity",
        ),
        Index("ix_raw_meter_slot", "meter_id", "slot_start"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"))
    slot_start: Mapped[datetime] = mapped_column(DateTime)
    slot_end: Mapped[datetime] = mapped_column(DateTime)
    raw_value: Mapped[float] = mapped_column(Float)
    raw_unit: Mapped[str] = mapped_column(String(32), default="")
    source_code: Mapped[str] = mapped_column(String(64))
    source_version: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    received_at: Mapped[datetime] = mapped_column(DateTime)
    # 受理时解析出的样本 id（与既有 samples 链路对应，可空）
    sample_id: Mapped[int | None] = mapped_column(ForeignKey("samples.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    meter: Mapped[Meter] = relationship()


class StandardIntervalFact(Base):
    """标准区间事实（不可变）。

    只有证据完整的读数才会被编译器转换为标准区间事实：统一到标准单位、统一为
    “区间用量”口径，记录完整换算步骤、来源契约与解释版本。同一原记录在同一
    解释版本下唯一。
    """

    __tablename__ = "standard_interval_facts"
    __table_args__ = (
        UniqueConstraint(
            "raw_reading_id", "contract_release_version",
            name="uq_standard_raw_release",
        ),
        Index("ix_standard_meter_slot", "meter_id", "slot_start"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"))
    building_code: Mapped[str] = mapped_column(String(32))
    raw_reading_id: Mapped[int] = mapped_column(ForeignKey("raw_readings.id"))
    contract_id: Mapped[int] = mapped_column(ForeignKey("semantic_contracts.id"))
    # 解释版本：固定引用某次契约发布，历史产品按此复演
    contract_release_version: Mapped[int] = mapped_column(Integer, index=True)
    slot_start: Mapped[datetime] = mapped_column(DateTime)
    slot_end: Mapped[datetime] = mapped_column(DateTime)
    physical_quantity: Mapped[str] = mapped_column(String(32))
    standard_unit: Mapped[str] = mapped_column(String(32))
    # 标准化后的区间用量（标准单位）
    standard_value: Mapped[float] = mapped_column(Float)
    # 来源语义与逐步换算证据
    source_semantics: Mapped[str] = mapped_column(String(16))
    conversion_steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    # 适用契约在该槽位是否处于有效期交界处拆分而来
    boundary_split: Mapped[bool] = mapped_column(Boolean, default=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    meter: Mapped[Meter] = relationship()
    contract: Mapped[SemanticContract] = relationship()
    raw_reading: Mapped[RawReading] = relationship()


class QuarantineRecord(Base):
    """待核隔离槽位（质量槽位）。

    无法证明语义的读数不填零、不进标准事实，隔离为“待解释”，并阻断确认版。
    记录隔离原因、命中的契约（或缺失/冲突情况）与解释版本，供后续契约更正后重放。
    """

    __tablename__ = "quarantine_records"
    __table_args__ = (
        UniqueConstraint(
            "raw_reading_id", "contract_release_version",
            name="uq_quarantine_raw_release",
        ),
        Index("ix_quarantine_meter_slot", "meter_id", "slot_start"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"))
    building_code: Mapped[str] = mapped_column(String(32))
    raw_reading_id: Mapped[int | None] = mapped_column(
        ForeignKey("raw_readings.id"), nullable=True
    )
    # 可能命中 0 个（缺失）或多个（冲突）契约
    contract_ids: Mapped[list[int]] = mapped_column(JSONB, default=list)
    contract_release_version: Mapped[int] = mapped_column(Integer, index=True)
    slot_start: Mapped[datetime] = mapped_column(DateTime)
    slot_end: Mapped[datetime] = mapped_column(DateTime)
    physical_quantity: Mapped[str] = mapped_column(String(32), default="")
    reason_code: Mapped[str] = mapped_column(String(48))
    reason_detail: Mapped[str] = mapped_column(Text, default="")
    # 数值可解析但语义不可用：明确区分“数值可解析”与“可用于能耗产品”
    numeric_parseable: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | resolved
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by_fact_id: Mapped[int | None] = mapped_column(
        ForeignKey("standard_interval_facts.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    meter: Mapped[Meter] = relationship()
