"use client";
import { DataView } from "./common";
type Analytics = {
  pv?: number;
  uv?: number;
  sessions?: number;
  clicks?: number;
  start?: string;
  end?: string;
  daily?: { date: string; pv: number; uv: number }[];
  pages?: {
    path: string;
    pv: number;
    uv: number;
    clicks: number;
    click_users: number;
    content_views: number;
  }[];
};
export function AnalyticsReport({ value }: { value: unknown }) {
  const data = value as Analytics;
  const days = data.daily || [];
  const maximum = Math.max(1, ...days.map((d) => d.pv));
  return (
    <>
      <div className="metric-grid">
        {[
          ["页面浏览", data.pv],
          ["独立访客", data.uv],
          ["访问会话", data.sessions],
          ["核心点击", data.clicks],
        ].map(([label, amount]) => (
          <div className="metric" key={label}>
            <p className="metric-label">{label}</p>
            <p className="metric-value">{amount ?? "未知"}</p>
            <span className="metric-foot">
              {data.start} — {data.end}
            </span>
          </div>
        ))}
      </div>
      <section className="panel">
        <div className="section-head">
          <h2>访问趋势</h2>
          <span className="muted">上海时区 · 页面浏览</span>
        </div>
        {days.length ? (
          <div
            className="analytics-chart"
            role="img"
            aria-label={days
              .map((d) => `${d.date}：${d.pv}次浏览，${d.uv}位访客`)
              .join("；")}
          >
            {days.map((d) => (
              <div key={d.date} className="analytics-bar">
                <span>{d.pv}</span>
                <div
                  style={{ height: `${Math.max(2, (d.pv / maximum) * 130)}px` }}
                />
                <time>{d.date.slice(5)}</time>
              </div>
            ))}
          </div>
        ) : (
          <p className="empty-state">这个时段还没有公开访问记录。</p>
        )}
        <p className="muted">
          管理、预览与测试访问由服务端排除；没有曝光分母，不显示 CTR。
        </p>
      </section>
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th>页面</th>
              <th>浏览</th>
              <th>访客</th>
              <th>内容阅读</th>
              <th>点击 / 人数</th>
            </tr>
          </thead>
          <tbody>
            {data.pages?.map((p) => (
              <tr key={p.path}>
                <td>{p.path}</td>
                <td>{p.pv}</td>
                <td>{p.uv}</td>
                <td>{p.content_views}</td>
                <td>
                  {p.clicks} / {p.click_users}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <details style={{ marginTop: 24 }}>
        <summary>查看采集明细</summary>
        <DataView value={value} />
      </details>
    </>
  );
}
