import { useEffect, useState } from 'react'
import { api, fmtTime, fmtWindow, JSONObj } from '../lib/api'
import { useApp } from '../lib/store'
import { PageHead, StatusPill } from '../components/ui'
import { RangeChips } from '../components/charts'

export default function AcceptancePage() {
  const { run, toast, bump, reloadMeta } = useApp()
  const [data, setData] = useState<JSONObj | null>(null)
  const [open, setOpen] = useState<string>('baseline')

  const [stepsMeta, setStepsMeta] = useState<JSONObj[]>([])

  useEffect(() => {
    api
      .get<JSONObj[]>('/api/acceptance/steps')
      .then(setStepsMeta)
      .catch((err) => toast(String(err), 'bad'))
  }, [toast])

  const execute = async () => {
    const res = await run(api.post<JSONObj>('/api/acceptance/run', {}))
    if (res) {
      setData(res)
      setOpen(res.steps?.[0]?.step ?? '')
      await reloadMeta()
      bump()
      toast('验收叙事已从模拟表计到达重放完毕')
    }
  }

  return (
    <>
      <PageHead
        title="验收报告"
        lead="从模拟表计实际到达开始重放：计划维护、换频、单表计延迟、迟到补传、重复到达、累计表复位、依赖分支、恢复评估、关闭前再缺失与恢复后再缺失。所有结论都从数据库中的评估、影响和事故读回。"
        actions={
          <button className="btn primary" onClick={execute}>
            重放验收场景
          </button>
        }
      />

      {!data && (
        <div className="panel">
          <div className="panel-body" style={{ display: 'grid', gap: 10 }}>
            <div className="banner info">
              <span>
                点击“重放验收场景”会先把模拟数据重置到 06:00 基线，再按真实事件顺序执行事件并完整重算每个受影响窗口。整个过程不会触碰真实楼宇设备或结算数据。
              </span>
            </div>
            <ol className="note" style={{ margin: 0, paddingLeft: 18, display: 'grid', gap: 3 }}>
              {stepsMeta.map((m: JSONObj) => (
                <li key={m.key}>{m.title}</li>
              ))}
            </ol>
          </div>
        </div>
      )}

      {data && (
        <>
          <div className="panel">
            <div className="panel-head">
              <h2>执行步骤（{data.steps.length}）</h2>
              <span className="hint">生成于 {fmtTime(data.generated_at)}</span>
            </div>
            <div className="panel-body" style={{ display: 'grid', gap: 8 }}>
              {data.steps.map((s: JSONObj, i: number) => {
                const isOpen = open === s.step
                return (
                  <div key={s.step} className="panel" style={{ boxShadow: 'none' }}>
                    <div
                      className="panel-head"
                      style={{ cursor: 'pointer', borderBottom: isOpen ? '1px solid var(--line)' : 'none' }}
                      onClick={() => setOpen(isOpen ? '' : s.step)}
                    >
                      <div className="row">
                        <span className="tag">{String(i + 1).padStart(2, '0')}</span>
                        <h3>{s.title}</h3>
                      </div>
                      <span className="note">{isOpen ? '收起' : '展开'}</span>
                    </div>
                    {isOpen && <StepBody step={s} />}
                  </div>
                )
              })}
            </div>
          </div>
        </>
      )}
    </>
  )
}

function StepBody({ step }: { step: JSONObj }) {
  const detail = step.detail ?? {}
  const obs = step.observation
  const windows = obs?.windows ?? []
  const latestWindow = windows[windows.length - 1]
  const sensitivity = detail.sensitivity as JSONObj[] | undefined

  return (
    <div className="panel-body" style={{ display: 'grid', gap: 12 }}>
      <div className="row wrap">
        {Object.entries(detail)
          .filter(([k]) => !['sensitivity', 'close_result'].includes(k))
          .map(([k, v]) => (
            <span key={k} className="tag">
              {k}: {typeof v === 'object' ? JSON.stringify(v) : String(v)}
            </span>
          ))}
        {detail.close_result && (
          <span className={`pill ${detail.close_result.ok ? 'good' : 'bad'}`}>
            关闭{detail.close_result.ok ? '成功' : '被拒绝'} {detail.close_result.error ?? ''}
          </span>
        )}
      </div>

      {latestWindow && (
        <div className="split-2">
          <div>
            <h3 style={{ marginBottom: 6 }}>窗口 {fmtWindow(latestWindow.window_start, latestWindow.window_end)} · 来源</h3>
            <table className="tbl">
              <thead>
                <tr>
                  <th>来源</th>
                  <th>状态</th>
                  <th>覆盖</th>
                  <th>核验</th>
                  <th>缺失/迟到</th>
                </tr>
              </thead>
              <tbody>
                {latestWindow.sources.map((s: JSONObj) => (
                  <tr key={s.source}>
                    <td>{s.source}</td>
                    <td>
                      <StatusPill status={s.status} />
                    </td>
                    <td className="num">
                      {s.coverage_num}/{s.coverage_den}
                    </td>
                    <td>{s.certified ? <span className="pill good">已核验</span> : <span className="pill warn">未核验</span>}</td>
                    <td className="num">
                      {s.counts?.missing ?? 0} / {s.counts?.late ?? 0}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div>
            <h3 style={{ marginBottom: 6 }}>消费者判定</h3>
            <table className="tbl">
              <thead>
                <tr>
                  <th>消费者</th>
                  <th>判定</th>
                  <th>影响槽位</th>
                  <th>容忍</th>
                </tr>
              </thead>
              <tbody>
                {latestWindow.consumers.map((c: JSONObj) => (
                  <tr key={c.consumer}>
                    <td>{c.consumer}</td>
                    <td>
                      <StatusPill status={c.status} />
                    </td>
                    <td className="num">{c.affected_slots}</td>
                    <td className="num">{c.thresholds?.tolerance_minutes} 分</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {sensitivity && (
        <div>
          <h3 style={{ marginBottom: 6 }}>阈值敏感性（同一窗口，仅改变容忍范围）</h3>
          <table className="tbl">
            <thead>
              <tr>
                <th>消费者</th>
                <th>当前容忍</th>
                <th>当前判定</th>
                <th>预览容忍</th>
                <th>预览判定</th>
                <th>影响槽位变化</th>
              </tr>
            </thead>
            <tbody>
              {sensitivity.map((x: JSONObj) => (
                <tr key={x.consumer}>
                  <td>{x.consumer}</td>
                  <td className="num">{x.baseline_tolerance} 分</td>
                  <td>
                    <StatusPill status={x.baseline_status} />
                  </td>
                  <td className="num">{x.preview_tolerance} 分</td>
                  <td>
                    <StatusPill status={x.preview_status} />
                  </td>
                  <td className="num">
                    {x.baseline_affected} → {x.preview_affected}
                    {x.changed ? <span className="pill warn" style={{ marginLeft: 6 }}>变化</span> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {obs?.incidents?.length ? (
        <div>
          <h3 style={{ marginBottom: 6 }}>事故状态</h3>
          <table className="tbl">
            <thead>
              <tr>
                <th>编号</th>
                <th>状态</th>
                <th>原因</th>
                <th>区间</th>
                <th>关联既往</th>
              </tr>
            </thead>
            <tbody>
              {obs.incidents.map((inc: JSONObj) => (
                <tr key={inc.id}>
                  <td className="mono">{inc.code}</td>
                  <td>
                    <StatusPill status={inc.status} />
                  </td>
                  <td>{inc.cause === 'data_gap' ? '缺失' : '核验'}</td>
                  <td>
                    <RangeChips ranges={inc.ranges} />
                  </td>
                  <td className="note">{inc.prior_incident_id ? `#${inc.prior_incident_id}` : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      <details className="raw">
        <summary>查看该步骤完整观测 JSON</summary>
        <pre>{JSON.stringify(obs, null, 2)}</pre>
      </details>
    </div>
  )
}
