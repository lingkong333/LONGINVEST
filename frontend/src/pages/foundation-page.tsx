import { ArrowUpRight, ChartSpline, CircleCheck, DatabaseZap } from "lucide-react"

export function FoundationPage() {
  return (
    <main className="foundation-page">
      <header className="foundation-page__header">
        <div>
          <p className="eyebrow">工作台总览</p>
          <h1>把长周期判断，建立在可验证的数据上。</h1>
          <p>
            登录与应用基础已经接通。业务页面将按监控、策略、回测和运维的顺序逐批接入。
          </p>
        </div>
        <div className="foundation-page__seal" aria-label="基础环境正常">
          <CircleCheck aria-hidden="true" />
          <span>基础环境</span>
          <strong>READY</strong>
        </div>
      </header>

      <section className="foundation-grid" aria-label="当前建设状态">
        <article className="foundation-card foundation-card--primary">
          <DatabaseZap aria-hidden="true" />
          <p>后端能力</p>
          <strong>核心闭环已完成</strong>
          <span>行情、目标、信号、策略、通知、回测与运维接口已进入主干。</span>
        </article>
        <article className="foundation-card">
          <ChartSpline aria-hidden="true" />
          <p>当前施工</p>
          <strong>前端统一接入</strong>
          <span>先完成认证、导航与页面状态，再接入各业务模块。</span>
        </article>
        <article className="foundation-card">
          <ArrowUpRight aria-hidden="true" />
          <p>下一批</p>
          <strong>仪表盘与监控</strong>
          <span>使用真实接口展示系统状态和监控股票，不使用演示数据。</span>
        </article>
      </section>
    </main>
  )
}
