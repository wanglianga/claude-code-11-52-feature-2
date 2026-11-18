# 园区能耗数据产品 SLO 与事故影响管理平台

工业园区能源管理中心的**数据质量与消费影响**管理平台。平台不操作楼宇设备、不调整真实负荷、不办理能源结算，只回答一个问题：

> 一条迟到、缺失或未核验的能耗数据，是否让某个数据产品对某类消费者不可用？谁该等、谁可以先用带缺口的曲线、事故何时能关闭？

四个模块联动决定一份数据能否被标记为可用：

| 模块 | 职责 |
| --- | --- |
| 来源登记 | 每类数据的采样承诺：表计有效安装期、计划频率、允许迟到、维护生效区间、产品依赖 |
| 语义契约 | 按来源版本登记计量对象、物理量、单位、累计/区间/瞬时口径、代表时段、符号方向、倍率引用与有效期；编译器只放行证据完整的读数 |
| 产品运行 | 生成两类产品：**近实时能耗曲线**（带截止点的暂定版本）与**核验日汇总**（完整重算后合格） |
| 消费目录 | 物业大屏、月度结算、节能考核各自登记的产品字段 / 楼栋 / 时段依赖与阈值 |
| 事故指挥 | 根据违约事实与消费影响驱动处置，恢复评估通过后才允许关闭 |

> 跨厂商数据接入后，“1500”可能是十五分钟电量也可能是瞬时功率，累计水量会因换表或由立方米改升而跳变。**数值范围正常不等于语义可用**：语义契约层把无法证明语义的记录隔离为“待核”而不是填零，日汇总与 SLO 覆盖不会被悄悄污染。

## 1. 技术栈

- **前端**：React 18 + TypeScript + Vite + React Router（自绘 SVG 曲线，无图表库依赖）
- **后端**：FastAPI 0.115 + SQLAlchemy 2.0 + Pydantic v2
- **数据库**：PostgreSQL 18（JSONB 保存评估明细与逐步换算证据）
- **测试**：pytest（后端 37 项，含 15 项语义治理）+ jsdom 渲染断言（前端 35 项）

## 2. 一键启动（Docker Compose）

```bash
docker compose up --build
```

> 本交付环境内没有安装 Docker 守护进程，因此 Compose 配置未能在本机实际执行；已通过等价路径验证同一镜像行为：先 `npm run build`，再以 `SPA_DIST=frontend/dist` 启动后端，确认 `/`、前端路由与 `/api/*` 在同一 8000 端口下均可访问。

- 控制台：<http://localhost:8000>
- OpenAPI 文档：<http://localhost:8000/docs>（Swagger UI） · <http://localhost:8000/openapi.json>
- 健康检查：<http://localhost:8000/healthz>

启动后自动建表并生成种子数据：模拟日 `2026-11-10`、模拟时钟 `06:00`，以及 18:00（前一日）至 06:00 的表计读数。首次打开建议先点「验收报告 → 重放验收场景」。

## 3. 本地开发启动（不使用 Docker）

容器内已预装运行时，三个进程分别启动：

```bash
# 1) 依赖
cd /workspace/52-feature-2
python3 -m venv .venv
.venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r backend/requirements.txt
# 若默认 npm 缓存目录不可写，追加 --cache /tmp/npm-cache
npm --prefix frontend install --registry https://registry.npmmirror.com

# 2) 后端（建表 + 种子都在启动时完成），端口 8000
cd backend
DATABASE_URL=postgresql+psycopg://ccdev:ccdev@127.0.0.1:5432/ccdev \
  ../.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 3) 前端开发服务器，端口 5173，/api 代理到 8000
cd frontend
npm run dev
```

打开 <http://localhost:5173>。数据库连接可用 `DATABASE_URL` 覆盖；默认是容器自带的 `127.0.0.1:5432/ccdev`。

> 单容器模式：`cd frontend && npm run build`，然后以 `SPA_DIST=$PWD/../frontend/dist` 启动后端，即可在 8000 端口同时提供 API 与前端静态资源（与 Docker 镜像行为一致）。

## 4. 测试账号

| 用户名 | 密码 | 角色 |
| --- | --- | --- |
| `ops` | `ops123` | 值班调度（operator） |
| `admin` | `admin123` | 平台管理员 |

登录为演示态鉴权（前端携带 token，后端校验用户名口令哈希）；本平台不承担园区统一身份认证职责。

## 5. 领域模型与计算规则

### 5.1 观察窗口与槽位

- 观察窗口固定 **6 小时**（`00:00`、`06:00`、`12:00`、`18:00` 起），槽位 **15 分钟**。
- 预期槽位**按窗口实际推导**，不是常量：
  - 按采样承诺（`meter_commitments`）的有效区间与**计划频率**分段生成；
  - 频率或安装关系在窗口内变化时**分别拆段**，各自计算分母（例如 15min×8 + 30min×8 = 16）；
  - 维护窗口仅在**规则允许扣除**（`allowed_deduction`）且**完整覆盖该槽位**时从分母扣除；
  - 截止点之后尚未结束的槽位标记 `beyond_cutoff`，**不进入分母、不算缺失、不填零**；
  - 重复到达只占一个槽位；声明零用量（`is_declared_zero`）是有效值。

### 5.2 健康状态

```
breached  ← 存在缺失槽位 或 核验缺口
pending   ← 尚有未到期槽位 / 窗口未结束
partial   ← 覆盖率不足但有数据
degraded  ← 覆盖与核验都通过，但存在超出允许迟到的槽位
healthy   ← 覆盖 100%、核验通过、无超时到达
```

合格（`certified`）需要同时满足：覆盖完整、核验通过、无超时到达、截止点晚于窗口结束、所有计划槽位均已到期。健康状态**不由采集任务存活或人工关闭事故决定**。

### 5.3 核验缺口（单列，不计为节能）

| 类型 | 触发 |
| --- | --- |
| `reset` | 累计表读数回退，负差不可计为节能 |
| `ratio` | 换表后新表首读数与旧表末读数 × 声明倍率偏差 > 5% |
| `bridge` | 表码差跨过未覆盖槽位，无法归因 |
| `baseline` | 累计表缺少可衔接的基线读数 |

### 5.4 违约事实（追加式）

`breach_facts` 只追加，不删除。迟到补传或事后补录维护会把事实标记为 `resolved_at` 并保留 `retroactive_maintenance_id` 备注，但**当时的违约事实仍然存在**。

### 5.5 消费影响

- 依赖按 `(产品, 字段, 楼栋, 有效区间)` 登记，消费者各自持有 `tolerance_minutes` / `min_coverage` / `max_gap_slots` / `require_certified`。
- **同一版本、不同阈值 → 不同结论**：槽位结束超过某消费者容忍时间仍未取得数据才计入该消费者的影响；物业可继续看受限曲线，结算与考核按自己的规则等待。
- 一个消费者依赖多个受影响字段时，影响区间**合并去重**，同一缺口只计一次。
- 下游日汇总失效会追溯到**源字段/楼栋的缺失区间**；缺失表计不会牵连无关楼栋（按楼栋分别计算）。
- 阈值预览不落库、不修改原始质量事实，只重算判定。

### 5.6 事故生命周期

```
open → (extended) → recovering → [恢复评估] → closed
                     ↑               |
                     └── 关闭前复查发现新缺口（reopened，旧结论作废）
closed ──同因再发──▶ 新事故区间（prior_incident_id 关联，两段间正常时段不计为连续故障）
```

- 关闭必须存在该事故的**已完成恢复评估**（`recovery_checks`），且窗口内无未解决事实；
- 关闭前复查若发现新缺口，拒绝关闭并把事故改回 `open`；
- 事故关联影响区间、违约事实与消费者影响证据；相同原因关闭后再缺失时，建立**新区间**并关联既往事故。

### 5.7 重算与缓存

- 每个观察窗口**完整重算**：事件（到达、补传、维护、换频、时钟推进）只决定**重算哪些窗口**，不会就地补一个槽位。
- 结果未变化的重复重算复用同一 `revision` 并累加 `recompute_count`；结果变化才追加新 revision，历史版本可追溯（`/api/health/evaluations/{id}` 返回 `revisions` 链）。

### 5.8 表计数据语义契约与兼容转换治理

语义层在“原始受理”与“产品计算”之间加了一道**可证明性闸门**，由五张表通过版本号串联：

| 环节 | 表 | 不变量 |
| --- | --- | --- |
| 契约登记 | `semantic_contracts` | 按来源版本登记：计量对象、物理量、单位、累计/区间/瞬时口径、采样代表时段、符号方向、倍率引用、单位换算因子与方向、有效期；只追加，更正用“收口旧有效期 + 新版本” |
| 发布锁定 | `contract_releases` | 把当前有效契约固化为不可变解释版本（内容指纹）；新契约只影响新运行，历史产品按其引用版本复演 |
| 原始受理 | `raw_readings` | 保留厂商不可变原载荷与来源版本；数值不可解析也保留并标记 |
| 标准化事实 | `standard_interval_facts` | 仅证据完整的读数进入；统一到标准单位与“区间用量”口径，记录逐步换算步骤、来源契约与解释版本 |
| 待核隔离 | `quarantine_records` | 无法证明语义的读数隔离为“待解释”，**不填零**，并阻断窗口进入确认版 |

**编译器强制规则（`services/semantics.py`）**

- **量纲**：功率（kW）不得直接与电量（kWh）相加；瞬时功率仅在“代表时段可积分”时按 `kWh = kW × 小时` 换算，否则 `dimension_mismatch` / `instant_interval_unsupported`。
- **时间语义**：累计值只能在可确认的连续读数间求差（缺基线 `cumulative_baseline`、回退 `cumulative_reset`、跨未覆盖槽位 `bridge_gap`）；区间读数不再二次累计（`interval_reaccumulation`）。
- **单位换算**：必须有明确因子与方向（注册表内建 L→m³ ×0.001、Wh→kWh ×0.001 等）；未登记单位（如 gal）或与契约声明单位不一致 → `unit_factor_unknown`，不靠猜。
- **符号方向**：负向读数只在 `bidirectional` 契约且具备可验证回送含义时接受，否则 `sign_feedback_unverified`。
- **有效期交界**：同一设备在交界处更换单位或计量模式时窗口按边界拆分（`boundary_split`），跨契约的累计差不直接相减，绝不用当前配置解释整天。
- **来源声明缺失 / 契约同期冲突**：分别隔离为 `missing_contract`（`numeric_parseable=true` 仍隔离）与 `contract_conflict`（命中两个契约）。

**两条落地路线都被支持**：接入时把合法批次规范化成不可变标准区间表（默认路径，产品计算快、同一原记录只有一种已批准解释）；契约更正后通过 `/api/semantics/replay` 重放受影响历史——新版本只影响新编译，旧版本标准事实保留，因此历史产品仍可按旧解释版本复演。

**与既有 SLO 的关系**：语义闸门只在窗口内存在“原始受理”时介入；没有原始受理的来源完全沿用第 5.1–5.7 节的 Sample 链路结论。待核槽位计入质量槽位并令 `certified/confirmable=false`，但不改动覆盖分母与既有违约事实。

## 6. 界面

| 页面 | 内容 |
| --- | --- |
| 产品健康 | 窗口 × 来源的分子/分母、覆盖率条、新鲜度、核验状态；表计下钻（频率分段、维护扣除、核验缺口、槽位矩阵）；带截止点的曲线 |
| 产品运行 | 近实时曲线与核验日汇总的运行版本、版本谱系、合格范围、消费者引用与判定 |
| 来源登记 | 表计安装期/频率/允许迟到/维护窗口/依赖；登记维护与变更频率后自动重算 |
| 消费影响 | 三个消费者的卡片与窗口判定、依赖分解、按楼栋结果、**阈值敏感性预览**（只改判定不改事实） |
| 事故指挥 | 事故列表、状态机、恢复评估记录、影响区间、时间线；执行恢复评估 / 尝试关闭 / 记录备注 |
| 语义契约 | 契约登记与解释版本谱系、重放语义验收、逐场景下钻每个标准区间的来源语义/换算步骤/边界拆分与待核隔离原因 |
| 模拟与验收 | 推进时钟、延迟/丢弃样本、迟到补传、登记维护、换频、审计时间线、重置基线 |
| 验收报告 | 从模拟表计实际到达重放的 15 个步骤，每步给出数据库读回的观测 |

## 7. 验证方式

### 7.1 后端测试（37 项）

```bash
cd backend
DATABASE_URL=postgresql+psycopg://ccdev:ccdev@127.0.0.1:5432/ccdev_test \
  ../.venv/bin/python -m pytest tests -q
```

测试会自建 `ccdev_test` 库（若不存在）。覆盖：

- 预期槽位与维护扣除、未到截止槽位不填零、声明零用量有效
- 重复到达只占一槽、频率变更拆段、安装期中断拆段
- 事后补录维护：分母扣除但违约事实保留并标注
- 累计表回零、换表倍率不衔接 → 核验缺口、无负节能
- 迟到重传同时更新曲线与完整性
- 同版本不同容忍度 → 不同判定；缺失不牵连无关楼栋
- 多产品同一缺口合并一次、阈值预览只改判定
- 无恢复评估拒绝关闭、关闭前新缺口重开（含跨窗口新缺口）、恢复后再发关联既往事故、区间不含中间正常时段
- 语义契约（15 项，`tests/test_semantics.py`）：kWh/kW 混入量纲隔离与功率积分、累计表连续求差/重置/跨缺口/缺基线、升↔立方米显式因子与边界拆窗、未登记单位不猜测、双向回送符号校验、同日协议升级拆窗、契约同期冲突、来源声明缺失（数值可解析仍隔离）、数值不可解析、积分间隔不支持、契约更正重放且历史解释版本保留、待核阻断确认版、标准事实携带逐步换算与解释版本

最近一次实际执行结果：`37 passed`（既有 22 项 + 语义 15 项，无回归）。

### 7.2 前端渲染断言（35 项）

```bash
# 先启动后端（8000）
cd frontend
API_BASE=http://127.0.0.1:8000 npm run test:ui
```

脚本先调用 `/api/acceptance/run` 重放验收，再用 jsdom 挂载各页面到真实 API，断言页面出现真实数据（表计编号、消费者名、事故编号等），点击「预览判定变化」验证阈值预览，并挂载「语义契约」页点击「重放语义验收」断言编译总览、标准区间事实、待核隔离与量纲原因真实渲染。最近一次实际执行结果：`UI smoke OK (35 assertions)`。

> 说明：容器内缺少 Chromium 所需的 `libatk` 等系统库且无 root 权限安装，因此未做浏览器截图验证；改用 jsdom 做真实数据渲染断言。前端 `npm run build`（tsc + vite）已实际执行通过，`/` 与前端路由均由后端 SPA 挂载实际返回 200。

### 7.3 手工验收（推荐路径）

1. 登录 `ops / ops123`，进入「验收报告」→「重放验收场景」，逐个展开 15 个步骤。
2. 重点核对：
   - `single_meter_delayed`：电力 `113/116`，物业 `restricted`、结算 `unavailable`；
   - `tolerance_split`：同一产品版本，同一 45 分钟超时槽位，物业（容忍 60）`ok`、结算（容忍 30）`unavailable`、考核 `ok`（B4 不在考核依赖范围）；
   - `missing_interval` / `late_backfill`：缺口解除后违约事实仍保留；补传后物业曲线恢复 `ok`，而结算/考核因该窗口迟到未获核验资格仍为 `unavailable`（影响槽位为 0，原因是核验资格而非缺口）；
   - `cumulative_reset`：蒸汽核验缺口，负差未计入总量；
   - `close_blocked_new_gap`：关闭被拒绝且事故回到 `open`；
   - `recurrence_after_recovery`：新事故关联既往事故，区间只有 `13:00–14:00`。
3. 在「事故指挥」手动执行恢复评估 → 关闭，观察时间线与恢复证据。
4. 在「消费影响」调整容忍范围：以 12:00–18:00 窗口为例，把容忍调到 600 分钟后，物业从 `restricted`（4 槽）变为 `ok`（0 槽），结算/考核仍为 `unavailable` 但影响槽位由 5 降到 1——判定随阈值变化，原始质量事实不变。
5. 进入「语义契约」→「重放语义验收」，逐个展开 8 个场景，重点核对：
   - `kwh_kw_mixing`：25 kWh 区间读数直接成立；把 1500 kW 错标成电量区间时被**量纲隔离**；正确的 1500 kW 按 15 分钟积分得 **375 kWh**，换算步骤含 `integrate`；
   - `cumulative_reset`：连续读数 108−100=8 m³ 成立，回退到 5 触发 `cumulative_reset`，负差不计节能；
   - `unit_switch`：5000 L × 0.001 = 5 m³（含明确因子方向），00:30 切回 m³ 时 `boundary_split=true`，加仑 gal 无因子被隔离；
   - `feedback_power`：双向契约下 -200 kW 凭可验证回送含义积分为 -50 kWh；单向表计出现负值被 `sign_feedback_unverified` 隔离；
   - `protocol_upgrade`：累计→区间在 00:30 交界拆窗，升级后 6.5 kWh 直接采用不二次求差；
   - `contract_conflict` / `missing_contract` / `instant_interval`：冲突、无契约、无代表时段全部进待核；其中无契约记录 `numeric_parseable=true`——**数值可解析但不可用于产品**。

### 7.4 API 验收（curl）

```bash
curl -s -X POST localhost:8000/api/acceptance/reset
curl -s -X POST localhost:8000/api/acceptance/run -H 'content-type: application/json' -d '{}' | python3 -m json.tool | head -60
curl -s localhost:8000/api/incidents | python3 -m json.tool | head -40

# 语义契约治理：重放八类语义样例并回读标准事实 / 待核隔离
curl -s -X POST localhost:8000/api/semantics/acceptance/run -H 'content-type: application/json' -d '{}' | python3 -m json.tool | head -80
curl -s "localhost:8000/api/semantics/quarantine?source=electricity&window_start=2026-11-10T00:00:00" | python3 -m json.tool
curl -s "localhost:8000/api/semantics/facts?source=water&window_start=2026-11-10T00:00:00" | python3 -m json.tool
curl -s localhost:8000/api/semantics/releases | python3 -m json.tool
```

## 8. 数据契约（主要接口）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/auth/login` | 演示登录 |
| GET | `/api/meta` | 时钟、楼栋、来源、产品、消费者阈值 |
| GET | `/api/sources` | 表计、采样承诺、维护窗口、产品依赖 |
| POST | `/api/sources/{code}/maintenance` | 登记维护（`recorded_at` 晚于槽位截止即事后补录） |
| POST | `/api/sources/{code}/commitments` | 变更采样频率（按段计算分母） |
| GET | `/api/health/evaluations` | 窗口评估列表（分子/分母/状态/核验） |
| GET | `/api/health/evaluations/{id}` | 逐表计明细、槽位状态、核验缺口、revision 链、违约事实 |
| POST | `/api/health/reevaluate` | 触发当前日完整重算 |
| GET | `/api/products` · `/api/products/{id}` | 两类产品的运行版本与合格范围、消费者引用 |
| GET | `/api/consumers` · `/api/consumers/{code}` | 消费目录与逐窗口影响 |
| POST | `/api/consumers/{code}/preview` | 阈值预览（不落库） |
| GET | `/api/incidents` · `/api/incidents/{id}` | 事故列表 / 时间线与证据 |
| POST | `/api/incidents/{id}/recovery-check` | 执行恢复评估 |
| POST | `/api/incidents/{id}/close` | 尝试关闭（校验恢复评估与新缺口） |
| POST | `/api/sim/action` | 样本到达/延迟/丢弃/补传/维护/换频 |
| POST | `/api/sim/clock` | 推进/设置模拟时钟（表计继续发射） |
| GET | `/api/sim/timeline` | 审计时间线 |
| GET/POST | `/api/semantics/contracts` | 语义契约列表 / 登记来源版本契约（量纲、单位、口径、符号、倍率、有效期） |
| GET/POST | `/api/semantics/releases` · `/api/semantics/release` | 解释版本谱系 / 锁定新解释版本 |
| GET/POST | `/api/semantics/raw` | 不可变原始受理查询 / 受理一条来源读数 |
| GET | `/api/semantics/facts` · `/api/semantics/quarantine` | 标准区间事实（含换算步骤）/ 待核隔离记录 |
| POST | `/api/semantics/compile` · `/api/semantics/replay` | 编译窗口 / 契约更正后重放受影响历史（旧版本保留） |
| GET | `/api/semantics/units` | 单位量纲注册表与各物理量标准单位 |
| POST | `/api/semantics/acceptance/run` · `/reset` | 八类语义样例重放 / 重置语义探针 |
| POST | `/api/acceptance/run` · `/api/acceptance/reset` | 重放验收 / 重置基线 |

完整 OpenAPI 3.1 定义见 `/openapi.json`，Swagger UI 见 `/docs`。

## 9. 目录结构

```
backend/
  app/
    config.py            # 数据库地址、窗口/槽位常量
    db.py                # 引擎、会话、初始化与种子入口
    models.py            # 来源/承诺/维护/样本/评估/事实/产品/消费者/影响/事故/恢复/审计 + 语义契约五表
    seed.py              # 楼栋、来源、表计、消费者依赖、演示账号
    timeutil.py          # 槽位/窗口对齐工具
    routers/api.py       # 全部 REST 接口（含 /api/semantics/*）
    spa.py               # 单容器模式下挂载前端构建产物
    services/
      ingest.py          # 模拟表计发射与样本生成
      health.py          # 窗口完整重算（核心评估引擎，含语义闸门）
      products.py        # 近实时曲线 / 核验日汇总（固定引用解释版本）
      impact.py          # 消费影响与阈值预览
      incidents.py       # 事故状态机、恢复评估、关闭守卫
      simulator.py       # 时钟、事件入口、调度检查
      acceptance.py      # 验收叙事重放与观测读回
      semantics.py       # 语义契约编译器：量纲/时间语义/单位换算/符号/边界/冲突校验与转换
      semantic_demo.py   # 八类语义验收样例（探针表计，不扰动主 SLO 覆盖）
  tests/                 # 37 项规则测试（含 15 项语义治理）
frontend/
  src/pages/             # 产品健康、产品运行、来源登记、消费影响、事故指挥、语义契约、模拟、验收、登录
  src/components/        # 布局、状态标签、SVG 曲线、槽位矩阵
  scripts/ui-smoke.tsx   # jsdom 真实数据渲染断言（35 项）
Dockerfile               # 两阶段：Node 构建前端 → Python 运行 API + 静态资源
docker-compose.yml       # PostgreSQL 18 + 应用
```

## 10. 边界说明

- 平台只管理数据质量与消费影响；不控制楼宇设备、不调整真实负荷、不办理结算。
- 鉴权为演示级别，不用于生产。
- 模拟时钟为平台状态，推进时钟时表计按确定性模型继续发射样本，用于复现验收叙事。
- 语义契约只裁决“读数能否被证明并用于能耗产品”，不替代表计校准或计量检定；待核记录需人工补登记契约或更正后重放，平台不自动猜测语义、不静默填零。
- 契约更正产生新解释版本，只影响此后新编译的标准事实；历史产品引用的旧版本事实永久保留、可复演，不会被覆盖。
