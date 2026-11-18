import { useEffect, useState } from 'react'
import { api, fmtTime, JSONObj, QUARANTINE_REASON_LABEL, SEMANTICS_LABEL } from '../lib/api'
import { useApp } from '../lib/store'
import { PageHead, StatusPill } from '../components/ui'

const WINDOW_START = '2026-11-10T00:00:00'

export default function SemanticsPage() {
  const { run, toast, refreshKey, bump } = useApp()
  const [data, setData] = useState<JSONObj | null>(null)
  const [contracts, setContracts] = useState<JSONObj[]>([])
  const [releases, setReleases] = useState<JSONObj[]>([])
  const [open, setOpen] = useState<string>('')
  const [showRegister, setShowRegister] = useState(false)
  const [meters, setMeters] = useState<JSONObj[]>([])

  const loadRegistry = () =>
    Promise.all([
      api.get<JSONObj[]>('/api/semantics/contracts'),
      api.get<JSONObj[]>('/api/semantics/releases'),
      api.get<JSONObj[]>('/api/sources'),
    ]).then(([cs, rs, src]) => {
      setContracts(cs)
      setReleases(rs)
      setMeters(src.flatMap((s) => s.meters.map((m: JSONObj) => ({ ...m, source: s.code, source_unit: s.unit }))))
    })

  useEffect(() => {
    loadRegistry().catch((err) => toast(String(err), 'bad'))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey])

  const execute = async () => {
    const res = await run(api.post<JSONObj>('/api/semantics/acceptance/run', {}), '语义验收已重放：标准事实与待核隔离已重新编译')
    if (res) {
      setData(res)
      setOpen(res.scenarios?.[0]?.key ?? '')
      await loadRegistry()
      bump()
    }
  }

  const release = async () => {
    const res = await run(api.post<JSONObj>('/api/semantics/release', {}), '已锁定新的契约解释版本')
    if (res) loadRegistry()
  }

  const summary = data?.summary
  const reasons = (summary?.quarantine_reasons ?? {}) as Record<string, number>

  return (
    <>
      <PageHead
        title="语义契约治理"
        lead="为每个来源版本登记计量对象、物理量、单位、累计/区间/瞬时含义、采样代表时段、符号方向、倍率引用与有效期。编译器只把证据完整的读数转为标准区间事实，无法证明语义的记录隔离为待核而非填零；功率不得与电量相加，累计只在连续读数间求差，单位换算必须有明确因子与方向。"
        actions={
          <>
            <button className="btn" onClick={release}>
              锁定解释版本
            </button>
            <button className="btn" onClick={() => setShowRegister(true)}>
              登记契约
            </button>
            <button className="btn primary" onClick={execute}>
              重放语义验收
            </button>
          </>
        }
      />

      {data && (
        <div className="panel">
          <div className="panel-head">
            <h2>编译总览</h2>
            <span className="hint">
              窗口 {fmtTime(data.window?.start)} → {fmtTime(data.window?.end)} · 解释版本 v{data.contract_release_version}
            </span>
          </div>
          <div className="panel-body">
            <div className="row wrap" style={{ gap: 10 }}>
              <span className={`pill ${data.ok ? 'good' : 'bad'}`}>{data.ok ? '全部场景通过' : '存在失败场景'}</span>
              <span className="tag">标准区间事实 {summary?.standard_facts ?? 0}</span>
              <span className="pill bad">待核隔离 {summary?.quarantined ?? 0}</span>
              {Object.entries(reasons).map(([code, n]) => (
                <span key={code} className="tag">
                  {QUARANTINE_REASON_LABEL[code] ?? code} × {n}
                </span>
              ))}
            </div>
            <div className="note" style={{ marginTop: 10 }}>
              “数值可解析”不等于“可用于能耗产品”：被隔离的读数数值往往合法，只是语义无法证明，因此不填零并阻止进入确认版。
            </div>
          </div>
        </div>
      )}

      {data && (
        <div className="panel" style={{ marginTop: 14 }}>
          <div className="panel-head">
            <h2>验收场景（{data.scenarios.length}）· 来源语义 / 换算步骤 / 可用槽位 / 隔离记录</h2>
          </div>
          <div className="panel-body" style={{ display: 'grid', gap: 8 }}>
            {data.scenarios.map((s: JSONObj) => {
              const isOpen = open === s.key
              return (
                <div key={s.key} className="panel" style={{ boxShadow: 'none' }}>
                  <div
                    className="panel-head"
                    style={{ cursor: 'pointer', borderBottom: isOpen ? '1px solid var(--line)' : 'none' }}
                    onClick={() => setOpen(isOpen ? '' : s.key)}
                  >
                    <div className="row">
                      <StatusPill status={s.passed ? 'qualified' : 'failed'} label={s.passed ? '通过' : '失败'} />
                      <h3>{s.title}</h3>
                      <span className="note mono">{s.meter}</span>
                    </div>
                    <span className="note">
                      标准 {s.standard_facts.length} · 隔离 {s.quarantined.length} · {isOpen ? '收起' : '展开'}
                    </span>
                  </div>
                  {isOpen && <ScenarioBody scenario={s} />}
                </div>
              )
            })}
          </div>
        </div>
      )}

      <div className="grid split-2" style={{ marginTop: 14 }}>
        <div className="panel">
          <div className="panel-head">
            <h2>语义契约登记</h2>
            <span className="hint">{contracts.length} 条（探针 + 手动）</span>
          </div>
          <div className="scroll-x">
            <table className="tbl">
              <thead>
                <tr>
                  <th>表计 / 来源版本</th>
                  <th>物理量</th>
                  <th>单位 → 标准</th>
                  <th>口径</th>
                  <th>符号</th>
                  <th>有效期</th>
                </tr>
              </thead>
              <tbody>
                {contracts.map((c: JSONObj) => (
                  <tr key={c.id}>
                    <td>
                      <div style={{ fontWeight: 540 }}>{c.meter_code}</div>
                      <div className="note mono">{c.source_version}</div>
                    </td>
                    <td>{c.physical_quantity}</td>
                    <td className="num">
                      {c.raw_unit} → {c.standard_unit}
                      <div className="note">×{c.unit_factor}</div>
                    </td>
                    <td>
                      <span className="tag">{SEMANTICS_LABEL[c.reading_semantics] ?? c.reading_semantics}</span>
                      <div className="note">{c.representative_minutes} 分钟</div>
                    </td>
                    <td>{c.sign_direction === 'bidirectional' ? '双向' : '正向'}</td>
                    <td className="mono note">
                      {c.valid_from?.slice(5, 16).replace('T', ' ')} → {c.valid_to ? c.valid_to.slice(5, 16).replace('T', ' ') : '长期'}
                    </td>
                  </tr>
                ))}
                {!contracts.length && (
                  <tr>
                    <td colSpan={6} className="empty">
                      暂无契约，先重放语义验收或手动登记
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="panel">
          <div className="panel-head">
            <h2>解释版本谱系</h2>
            <span className="hint">新契约只影响新运行，历史产品按旧版本复演</span>
          </div>
          <div className="scroll-x">
            <table className="tbl">
              <thead>
                <tr>
                  <th>版本</th>
                  <th>状态</th>
                  <th>契约数</th>
                  <th>指纹</th>
                  <th>生成时间</th>
                </tr>
              </thead>
              <tbody>
                {releases.map((r: JSONObj) => (
                  <tr key={r.version}>
                    <td className="num">v{r.version}</td>
                    <td>
                      <span className="tag">{r.status === 'locked' ? '已锁定' : r.status}</span>
                    </td>
                    <td className="num">{r.contract_count}</td>
                    <td className="mono note">{r.fingerprint?.slice(0, 10)}</td>
                    <td className="mono note">{r.created_at?.slice(5, 16).replace('T', ' ')}</td>
                  </tr>
                ))}
                {!releases.length && (
                  <tr>
                    <td colSpan={5} className="empty">
                      尚未发布解释版本
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {showRegister && (
        <RegisterDialog
          meters={meters}
          onClose={() => setShowRegister(false)}
          onSubmit={async (payload) => {
            const res = await run(api.post('/api/semantics/contracts', payload), '契约已登记，发布后产生新解释版本')
            if (res) {
              setShowRegister(false)
              loadRegistry()
            }
          }}
        />
      )}
    </>
  )
}

function ScenarioBody({ scenario }: { scenario: JSONObj }) {
  return (
    <div className="panel-body" style={{ display: 'grid', gap: 12 }}>
      <div>
        <h3 style={{ marginBottom: 6 }}>核验断言</h3>
        <table className="tbl">
          <tbody>
            {scenario.expectations.map((e: JSONObj, i: number) => (
              <tr key={i}>
                <td style={{ width: 36 }}>
                  <StatusPill status={e.passed ? 'qualified' : 'failed'} label={e.passed ? '✓' : '✕'} />
                </td>
                <td>{e.label}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div>
        <h3 style={{ marginBottom: 6 }}>标准区间事实（证据完整，可用槽位）</h3>
        <table className="tbl">
          <thead>
            <tr>
              <th>槽位</th>
              <th>标准值</th>
              <th>口径</th>
              <th>边界拆分</th>
              <th>换算步骤</th>
            </tr>
          </thead>
          <tbody>
            {scenario.standard_facts.map((f: JSONObj, i: number) => (
              <tr key={i}>
                <td className="mono note">{f.slot?.slice(11, 16)}</td>
                <td className="num">
                  {f.standard_value} {f.standard_unit}
                </td>
                <td>
                  <span className="tag">{SEMANTICS_LABEL[f.source_semantics] ?? f.source_semantics}</span>
                </td>
                <td>{f.boundary_split ? <span className="pill warn">边界</span> : '—'}</td>
                <td className="note mono-wrap" style={{ maxWidth: 460 }}>
                  {f.conversion_steps.map((s: JSONObj, j: number) => (
                    <span key={j} className="tag" style={{ marginRight: 4 }}>
                      {stepText(s)}
                    </span>
                  ))}
                </td>
              </tr>
            ))}
            {!scenario.standard_facts.length && (
              <tr>
                <td colSpan={5} className="empty">
                  无标准事实（全部待核）
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div>
        <h3 style={{ marginBottom: 6 }}>待核隔离（不填零，阻断确认版）</h3>
        <table className="tbl">
          <thead>
            <tr>
              <th>槽位</th>
              <th>原因</th>
              <th>数值可解析</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>
            {scenario.quarantined.map((q: JSONObj, i: number) => (
              <tr key={i}>
                <td className="mono note">{q.slot?.slice(11, 16)}</td>
                <td>
                  <span className="pill bad">{QUARANTINE_REASON_LABEL[q.reason_code] ?? q.reason_code}</span>
                </td>
                <td>{q.numeric_parseable ? '是（但语义不可用）' : '否'}</td>
                <td className="note">{q.reason_detail}</td>
              </tr>
            ))}
            {!scenario.quarantined.length && (
              <tr>
                <td colSpan={4} className="empty">
                  无隔离记录
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function stepText(s: JSONObj): string {
  switch (s.step) {
    case 'raw':
      return `原始 ${s.value}${s.unit}`
    case 'difference':
      return `求差 Δ=${s.delta}`
    case 'integrate':
      return `积分 ×${s.hours}h`
    case 'multiply':
      return `倍率 ×${s.factor}`
    case 'unit':
      return `单位 ×${s.factor} → ${s.to}`
    case 'interval':
      return '区间不二次累计'
    case 'sign':
      return `符号:${s.feedback_meaning}`
    default:
      return s.step
  }
}

function RegisterDialog({
  meters,
  onClose,
  onSubmit,
}: {
  meters: JSONObj[]
  onClose: () => void
  onSubmit: (payload: JSONObj) => void
}) {
  const [meter, setMeter] = useState(meters[0]?.code ?? '')
  const [sourceVersion, setSourceVersion] = useState('manual-v1')
  const [physicalQuantity, setPhysicalQuantity] = useState('electricity')
  const [rawUnit, setRawUnit] = useState('kWh')
  const [readingSemantics, setReadingSemantics] = useState('interval')
  const [validFrom, setValidFrom] = useState(WINDOW_START)
  const [rep, setRep] = useState(15)
  const [bidirectional, setBidirectional] = useState(false)
  const [feedback, setFeedback] = useState('反向馈送园区电网')

  return (
    <div
      style={{ position: 'fixed', inset: 0, background: 'rgba(16,19,24,0.42)', display: 'grid', placeItems: 'center', zIndex: 30 }}
      onClick={onClose}
    >
      <div style={{ width: 480, background: '#fff', borderRadius: 12, boxShadow: '0 20px 60px rgba(0,0,0,0.2)' }} onClick={(e) => e.stopPropagation()}>
        <div className="panel-head">
          <h2>登记来源版本语义契约</h2>
          <button className="btn ghost sm" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="panel-body" style={{ display: 'grid', gap: 10 }}>
          <label className="field">
            表计
            <select className="input" value={meter} onChange={(e) => setMeter(e.target.value)}>
              {meters.map((m: JSONObj) => (
                <option key={m.code} value={m.code}>
                  {m.code} · {m.name}
                </option>
              ))}
            </select>
          </label>
          <div className="row">
            <label className="field" style={{ flex: 1 }}>
              来源版本
              <input className="input" value={sourceVersion} onChange={(e) => setSourceVersion(e.target.value)} />
            </label>
            <label className="field" style={{ flex: 1 }}>
              物理量
              <select className="input" value={physicalQuantity} onChange={(e) => setPhysicalQuantity(e.target.value)}>
                <option value="electricity">electricity / 电量</option>
                <option value="water">water / 水量</option>
                <option value="steam">steam / 蒸汽</option>
                <option value="pv">pv / 光伏</option>
              </select>
            </label>
          </div>
          <div className="row">
            <label className="field" style={{ flex: 1 }}>
              原始单位
              <input className="input" value={rawUnit} onChange={(e) => setRawUnit(e.target.value)} />
            </label>
            <label className="field" style={{ flex: 1 }}>
              读数口径
              <select className="input" value={readingSemantics} onChange={(e) => setReadingSemantics(e.target.value)}>
                <option value="interval">interval 区间</option>
                <option value="cumulative">cumulative 累计</option>
                <option value="instant">instant 瞬时</option>
              </select>
            </label>
          </div>
          <div className="row">
            <label className="field" style={{ flex: 1 }}>
              生效时间
              <input className="input" value={validFrom} onChange={(e) => setValidFrom(e.target.value)} />
            </label>
            <label className="field" style={{ flex: 1 }}>
              代表时段（分钟）
              <input className="input" type="number" value={rep} onChange={(e) => setRep(Number(e.target.value))} />
            </label>
          </div>
          <label className="row" style={{ fontSize: 12.5 }}>
            <input type="checkbox" checked={bidirectional} onChange={(e) => setBidirectional(e.target.checked)} />
            双向符号（允许负值，需可验证回送含义）
          </label>
          {bidirectional && (
            <label className="field">
              回送含义
              <input className="input" value={feedback} onChange={(e) => setFeedback(e.target.value)} />
            </label>
          )}
          <div className="note">单位换算因子按注册表单自动推导（如同量纲 L→m³ ×0.001）；跨量纲 kW→kWh 必须走瞬时积分。</div>
          <div className="row" style={{ justifyContent: 'flex-end' }}>
            <button className="btn" onClick={onClose}>
              取消
            </button>
            <button
              className="btn primary"
              onClick={() =>
                onSubmit({
                  meter,
                  source_version: sourceVersion,
                  physical_quantity: physicalQuantity,
                  raw_unit: rawUnit,
                  reading_semantics: readingSemantics,
                  representative_minutes: rep,
                  valid_from: validFrom,
                  sign_direction: bidirectional ? 'bidirectional' : 'forward',
                  feedback_meaning: bidirectional ? feedback : '',
                })
              }
            >
              登记契约
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
