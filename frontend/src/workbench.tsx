import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { api, errorText } from "./api";
import { ErrorBox, Messages, Notice, Status } from "./components";
import { usePush } from "./push";
import type {
  Account,
  Audit,
  Detail,
  Message,
  OrderDetail,
  Page,
  Ticket,
} from "./types";
import { money, reasonName, routeName, time } from "./types";
interface Stats {
  queue_length: number;
  average_wait_seconds: number;
  sla_overdue: number;
  agents: { actor: string; resolved: number }[];
}
const actionName: Record<string, string> = {
  created: "创建工单",
  claimed: "领取工单",
  released: "退回队列",
  resolved: "解决工单",
  message_created: "发送消息",
  information_supplied: "补充订单信息",
  order_viewed: "查看关联订单",
  lease_renewed: "续期领取租约",
  sla_warning: "SLA 临近到期",
  sla_overdue: "SLA 已到期",
};
export function Workbench({ account }: { account: Account }) {
  const [tab, setTab] = useState<"queue" | "stats">("queue"),
    [mine, setMine] = useState(false),
    [offset, setOffset] = useState(0),
    [queue, setQueue] = useState<Ticket[]>([]);
  const [detail, setDetail] = useState<Detail | null>(null),
    [order, setOrder] = useState<OrderDetail | null>(null),
    [reply, setReply] = useState(""),
    [stats, setStats] = useState<Stats | null>(null);
  const [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [loading, setLoading] = useState(false),
    [now, setNow] = useState(Date.now());
  const selected = useRef<string | null>(null),
    current = useRef<Ticket | null>(null),
    chain = useRef<Promise<void>>(Promise.resolve());
  const enqueue = useCallback((work: () => Promise<void>) => {
    const job = chain.current.then(work);
    chain.current = job.catch(() => {});
    return job;
  }, []);
  const loadQueue = useCallback(async () => {
    const r = await api.get<Page<Ticket>>(
      `/workbench/tickets?limit=20&offset=${offset}&state=${mine ? "claimed" : "waiting_human"}${mine ? "&mine=true" : ""}`,
    );
    setQueue(r.items);
  }, [offset, mine]);
  const loadDetail = useCallback(async (id: string) => {
    setLoading(true);
    try {
      const r = await api.get<Detail>("/workbench/tickets/" + id);
      while (r.messages.length > 0 && r.messages.length % 50 === 0) {
        const p = await api.get<Page<Message> & { ticket_revision: number }>(
          `/workbench/tickets/${id}/messages?limit=50&offset=${r.messages.length}`,
        );
        r.messages.push(...p.items);
        r.ticket.revision = p.ticket_revision;
        if (p.items.length < 50) break;
      }
      if (selected.current === id) {
        current.current = r.ticket;
        setDetail(r);
      }
    } finally {
      setLoading(false);
    }
  }, []);
  const refresh = useCallback(async () => {
    await enqueue(async () => {
      await loadQueue();
      if (selected.current) await loadDetail(selected.current);
      if (tab === "stats" && account.role === "admin")
        setStats(await api.get<Stats>("/admin/support-stats"));
    });
  }, [enqueue, loadQueue, loadDetail, tab, account.role]);
  const live = usePush(refresh);
  useEffect(() => {
    void enqueue(loadQueue).catch((e) => setError(errorText(e)));
  }, [enqueue, loadQueue]);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  useEffect(() => {
    if (tab === "stats")
      void api
        .get<Stats>("/admin/support-stats")
        .then(setStats)
        .catch((e) => setError(errorText(e)));
  }, [tab]);
  function select(t: Ticket) {
    selected.current = t.ticket_id;
    current.current = null;
    setDetail(null);
    setOrder(null);
    setReply("");
    setError("");
    void enqueue(() => loadDetail(t.ticket_id)).catch((e) =>
      setError(errorText(e)),
    );
  }
  function action(operation: "claim" | "reply" | "resolve" | "release") {
    if (busy) return;
    void enqueue(async () => {
      const t = current.current;
      if (!t) return;
      setBusy(true);
      setError("");
      try {
        const updated = await api.mutate<Ticket>(
          `/workbench/tickets/${t.ticket_id}/${operation}`,
          {
            expected_revision: t.revision,
            ...(operation === "reply" ? { message: reply } : {}),
          },
        );
        current.current = updated;
        if (operation === "reply") setReply("");
        await loadDetail(t.ticket_id);
        await loadQueue();
      } catch (e) {
        setError(errorText(e));
        if (selected.current)
          await loadDetail(selected.current).catch(() => {});
      } finally {
        setBusy(false);
      }
    }).catch((e) => setError(errorText(e)));
  }
  function viewOrder() {
    void enqueue(async () => {
      if (!selected.current) return;
      setBusy(true);
      setError("");
      try {
        const r = await api.get<{
          ticket_revision: number;
          order: OrderDetail;
        }>(`/workbench/tickets/${selected.current}/order`);
        setOrder(r.order);
        if (current.current) current.current.revision = r.ticket_revision;
        await loadDetail(selected.current);
      } catch (e) {
        setError(errorText(e));
      } finally {
        setBusy(false);
      }
    });
  }
  function moreEvents() {
    void enqueue(async () => {
      if (!detail || !selected.current) return;
      const r = await api.get<Page<Audit> & { ticket_revision: number }>(
        `/workbench/tickets/${selected.current}/events?limit=50&offset=${detail.events.length}`,
      );
      if (current.current) current.current.revision = r.ticket_revision;
      setDetail((d) =>
        d
          ? {
              ...d,
              ticket: { ...d.ticket, revision: r.ticket_revision },
              events: [...d.events, ...r.items],
            }
          : d,
      );
    }).catch((e) => setError(errorText(e)));
  }
  const t = detail?.ticket;
  const owned = t?.state === "claimed" && t.assigned_account_id === account.id;
  const validLease = owned && new Date(t.lease_expires_at || 0).getTime() > now;
  return (
    <div className="desk">
      <aside className="desk-sidebar">
        <a className="brand light" href="/workbench">
          <b>A</b> Agent Ops
        </a>
        <span className="sidebar-label">服务工作台</span>
        <button
          className={tab === "queue" ? "active" : ""}
          onClick={() => setTab("queue")}
        >
          ▤ 服务队列
        </button>
        {account.role === "admin" && (
          <button
            className={tab === "stats" ? "active" : ""}
            onClick={() => setTab("stats")}
          >
            ▦ 服务概览
          </button>
        )}
        <div className="sidebar-bottom">
          <span className="live-dot">{live}</span>
          <p>{account.role === "admin" ? "管理员" : "客服"} · 已登录</p>
          <button onClick={() => void api.logout().catch(() => {})}>
            退出登录
          </button>
        </div>
      </aside>
      <div className="desk-main">
        <header className="desk-top">
          <div>
            <span className="eyebrow">SERVICE OPERATIONS</span>
            <h1>{tab === "queue" ? "人工服务工作台" : "服务概览"}</h1>
          </div>
          <span className="muted">仅本机访问 · 操作留痕</span>
        </header>
        <ErrorBox value={error} />
        {tab === "queue" && (
          <div className="desk-grid">
            <section className="panel queue-panel">
              <header className="queue-heading">
                <div>
                  <h2>工单队列</h2>
                  <small className="muted">按优先级与等待时间排序</small>
                </div>
                <button
                  aria-label="刷新工作台"
                  className="icon-button"
                  onClick={() =>
                    void refresh().catch((e) => setError(errorText(e)))
                  }
                >
                  ↻
                </button>
              </header>
              <div className="segmented">
                <button
                  aria-pressed={!mine}
                  onClick={() => {
                    setMine(false);
                    setOffset(0);
                  }}
                >
                  待领取
                </button>
                <button
                  aria-pressed={mine}
                  onClick={() => {
                    setMine(true);
                    setOffset(0);
                  }}
                >
                  我领取的
                </button>
              </div>
              <div className="queue-meta">
                当前页 {queue.length} 张 · 实时{live}
              </div>
              <div className="queue-list" aria-label="待处理工单">
                {queue.map((row) => (
                  <button
                    className={`queue-item ${selected.current === row.ticket_id ? "selected" : ""}`}
                    key={row.ticket_id}
                    data-testid="queue-ticket"
                    onClick={() => select(row)}
                  >
                    <div>
                      <b>#{row.ticket_id.slice(0, 8)}</b>
                      <Status value={row.state} />
                    </div>
                    <p>{routeName[row.route] || row.route}</p>
                    <footer>
                      <span className={row.priority === 2 ? "urgent" : ""}>
                        {row.priority === 2 ? "高优先级" : "常规"}
                      </span>
                      <span>
                        {row.sla_overdue
                          ? "SLA 已到期"
                          : row.sla_warned
                            ? "SLA 临近到期"
                            : "SLA 正常"}
                      </span>
                    </footer>
                  </button>
                ))}
                {!queue.length && (
                  <div className="empty">
                    <span>✓</span>
                    <h3>
                      {mine ? "暂时没有已领取工单" : "当前没有待领取工单"}
                    </h3>
                    <p>新工单会自动出现在这里。</p>
                  </div>
                )}
              </div>
              <div className="pagination">
                <button
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - 20))}
                >
                  上一页
                </button>
                <span>第 {Math.floor(offset / 20) + 1} 页</span>
                <button
                  disabled={queue.length < 20}
                  onClick={() => setOffset(offset + 20)}
                >
                  下一页
                </button>
              </div>
            </section>
            <div className="ticket-workspace">
              {!t ? (
                <section className="panel select-empty">
                  <div className="round-mark">↗</div>
                  <h2>
                    {loading ? "正在读取工单…" : "选择一张工单，开始处理"}
                  </h2>
                  <p className="muted">
                    会话、关联订单与操作记录会在这里展示。
                  </p>
                </section>
              ) : (
                <>
                  <section className="panel ticket-summary">
                    <div className="panel-title">
                      <div>
                        <span className="eyebrow">TICKET DETAIL</span>
                        <h2>工单 #{t.ticket_id.slice(0, 8)}</h2>
                      </div>
                      <Status value={t.state} />
                    </div>
                    <div className="ticket-facts">
                      <div>
                        <small>系统路由</small>
                        <strong>{routeName[t.route] || t.route}</strong>
                      </div>
                      <div>
                        <small>判断理由</small>
                        <span>{reasonName[t.reason_code] || "状态已更新"}</span>
                      </div>
                      <div>
                        <small>SLA 截止</small>
                        <span className={t.sla_overdue ? "urgent" : ""}>
                          {time(t.sla_due_at)}
                        </span>
                      </div>
                      <div>
                        <small>领取租约</small>
                        <span>
                          {t.lease_expires_at
                            ? time(t.lease_expires_at)
                            : "尚未领取"}
                        </span>
                      </div>
                    </div>
                    <div className="button-row">
                      <button
                        className="primary"
                        disabled={
                          busy || loading || t.state !== "waiting_human"
                        }
                        onClick={() => action("claim")}
                      >
                        领取工单
                      </button>
                      <button
                        className="secondary"
                        disabled={busy || loading || !validLease}
                        onClick={() => action("resolve")}
                      >
                        解决工单
                      </button>
                      <button
                        className="secondary"
                        disabled={busy || loading || !validLease}
                        onClick={() => action("release")}
                      >
                        退回队列
                      </button>
                      <small className="muted">
                        版本 {t.revision}
                        {owned && !validLease ? " · 租约已过期，等待回收" : ""}
                      </small>
                    </div>
                  </section>
                  <div className="workspace-columns">
                    <section className="panel desk-conversation">
                      <header className="panel-title">
                        <h2>会话历史</h2>
                        <small className="muted">顾客 ↔ 客服</small>
                      </header>
                      <Messages items={detail.messages} />
                      <form
                        className="composer"
                        onSubmit={(e: FormEvent) => {
                          e.preventDefault();
                          action("reply");
                        }}
                      >
                        <label htmlFor="reply">回复顾客</label>
                        <textarea
                          id="reply"
                          aria-label="回复顾客"
                          rows={3}
                          maxLength={2000}
                          value={reply}
                          onChange={(e) => setReply(e.target.value)}
                          disabled={!validLease}
                          placeholder={
                            validLease
                              ? "输入处理进展或回复内容…"
                              : "领取工单后可回复顾客"
                          }
                        />
                        <button
                          className="primary"
                          disabled={
                            busy || loading || !validLease || !reply.trim()
                          }
                        >
                          发送回复
                        </button>
                      </form>
                    </section>
                    <aside className="ticket-side">
                      <section className="panel aside-card">
                        <h3>关联订单</h3>
                        {t.order_id ? (
                          <>
                            <p>订单 #{t.order_id}</p>
                            <button
                              className="secondary full"
                              disabled={busy || loading}
                              onClick={viewOrder}
                            >
                              查看关联订单
                            </button>
                            <small className="muted">
                              每次查看均记录审计事件
                            </small>
                          </>
                        ) : (
                          <p className="muted">
                            尚未关联订单，可请顾客在会话中补充。
                          </p>
                        )}
                        {order && (
                          <div
                            className="order-panel"
                            aria-label="关联订单详情"
                          >
                            <Status value={order.status} />
                            <h3>{money(order.total_cents)}</h3>
                            {order.items.map((line, i) => (
                              <p key={i}>
                                商品 #{line.product_id} × {line.quantity}
                              </p>
                            ))}
                            <small>
                              {order.shipment
                                ? `${order.shipment.carrier} · ${order.shipment.tracking_no}`
                                : "暂无物流记录"}
                            </small>
                          </div>
                        )}
                      </section>
                      <section className="panel aside-card audit-panel">
                        <h3>操作记录</h3>
                        <ol>
                          {detail.events.map((event) => (
                            <li key={event.revision}>
                              <b>{actionName[event.type] || "工单更新"}</b>
                              <small>
                                {event.actor === "system"
                                  ? "系统"
                                  : event.actor.replace(
                                      "account:",
                                      "账号 #",
                                    )}{" "}
                                · v{event.revision}
                              </small>
                              <time>{time(event.created_at)}</time>
                            </li>
                          ))}
                        </ol>
                        {detail.events.length > 0 &&
                          detail.events.length % 50 === 0 && (
                            <button
                              className="text-button"
                              onClick={moreEvents}
                            >
                              加载更多记录
                            </button>
                          )}
                      </section>
                    </aside>
                  </div>
                </>
              )}
            </div>
          </div>
        )}
        {tab === "stats" && (
          <>
            <div className="stats-grid">
              {[
                ["待处理队列", stats?.queue_length, "张"],
                [
                  "平均等待",
                  stats
                    ? Math.round(stats.average_wait_seconds / 60)
                    : undefined,
                  "分钟",
                ],
                ["SLA 到期", stats?.sla_overdue, "张"],
              ].map(([label, value, unit]) => (
                <section className="panel stat" key={String(label)}>
                  <small>{label}</small>
                  <strong>
                    {value ?? "—"}
                    <em>{unit}</em>
                  </strong>
                </section>
              ))}
            </div>
            <section className="panel">
              <header className="panel-title">
                <h2>客服处理量</h2>
              </header>
              <table>
                <thead>
                  <tr>
                    <th>处理账号</th>
                    <th>已解决工单</th>
                  </tr>
                </thead>
                <tbody>
                  {stats?.agents.map((agent) => (
                    <tr key={agent.actor}>
                      <td>{agent.actor.replace("account:", "账号 #")}</td>
                      <td>{agent.resolved}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {stats?.agents.length === 0 && (
                <p className="empty">暂无已解决工单记录。</p>
              )}
            </section>
          </>
        )}
      </div>
    </div>
  );
}
