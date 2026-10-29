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
  Message,
  Order,
  OrderDetail,
  Page,
  Product,
  Ticket,
} from "./types";
import { money, reasonName, time } from "./types";
type View = "chat" | "products" | "orders" | "payment";
export function Customer({ account }: { account: Account }) {
  const initial = location.pathname.split("/")[2] as View;
  const [view, setView] = useState<View>(
    ["products", "orders", "payment"].includes(initial) ? initial : "chat",
  );
  const [catalogKeyword, setCatalogKeyword] = useState("");
  const [productSearch, setProductSearch] = useState("");
  const [shoppingEnabled, setShoppingEnabled] = useState(false);
  const [memoryEnabled, setMemoryEnabled] = useState(false);
  const [guideBudget, setGuideBudget] = useState("2000");
  const [guideProduct, setGuideProduct] = useState<string | null>(null);
  useEffect(() => {
    void api
      .get<{ catalog_enabled: boolean; memory_enabled: boolean }>(
        "/v1/commerce/availability",
      )
      .then((r) => {
        setShoppingEnabled(r.catalog_enabled);
        setMemoryEnabled(r.memory_enabled);
      })
      .catch(() => {});
  }, []);
  async function forgetMemory() {
    if (
      !window.confirm(
        "删除全部导购记忆？此操作无法撤销。订单和客服记录会保留。",
      )
    )
      return;
    await run(async () => {
      const result = await api.raw("/v1/commerce/memory", { method: "DELETE" });
      const data = (await result.json()) as { status: string };
      setGuide([
        data.status === "deleted"
          ? "全部导购记忆已删除。"
          : "记忆服务尚未开通。",
      ]);
      setGuideProduct(null);
    });
  }
  const [paymentsEnabled, setPaymentsEnabled] = useState(false);
  const [paymentMessage, setPaymentMessage] = useState("");
  const [paymentLink, setPaymentLink] = useState("");
  useEffect(() => {
    void api
      .get<{ enabled: boolean }>("/payments/availability")
      .then((r) => setPaymentsEnabled(r.enabled))
      .catch(() => {});
  }, []);
  async function pay() {
    if (!order || busy) return;
    setBusy(true);
    setError("");
    try {
      const r = await api.mutate<{ payment_url: string }>(
        `/orders/${order.id}/payment`,
        {},
      );
      const url = new URL(r.payment_url);
      if (url.protocol !== "https:" || !url.hostname.endsWith(".alipaydev.com"))
        throw new Error();
      setPaymentLink(r.payment_url);
      setPaymentMessage(
        "请在沙箱收银台完成付款，再刷新我的订单。跳转成功不代表已付款。",
      );
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  const [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const locked = useRef(false);
  const [products, setProducts] = useState<Product[]>([]),
    [productCursor, setProductCursor] = useState<string | null>(null),
    [item, setItem] = useState<Product | null>(null),
    [quantity, setQuantity] = useState(1);
  const [orders, setOrders] = useState<Order[]>([]),
    [orderCursor, setOrderCursor] = useState<string | null>(null),
    [order, setOrder] = useState<OrderDetail | null>(null);
  const [ticket, setTicket] = useState<Ticket | null>(null),
    [tickets, setTickets] = useState<Ticket[]>([]),
    [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState(""),
    [orderInput, setOrderInput] = useState(""),
    [guide, setGuide] = useState<string[]>([]),
    [streaming, setStreaming] = useState(false);
  const activeId = useRef<string | null>(null),
    streamAbort = useRef<AbortController | null>(null);
  useEffect(() => {
    if (!item) return;
    const previous = document.activeElement as HTMLElement | null;
    const dialog = document.querySelector<HTMLElement>('[role="dialog"]');
    const focusable = () =>
      Array.from(
        dialog?.querySelectorAll<HTMLElement>(
          "button:not(:disabled),input:not(:disabled)",
        ) || [],
      );
    focusable()[0]?.focus();
    const handle = (e: KeyboardEvent) => {
      if (e.key === "Escape") setItem(null);
      if (e.key === "Tab") {
        const list = focusable();
        if (!list.length) return;
        if (e.shiftKey && document.activeElement === list[0]) {
          e.preventDefault();
          list.at(-1)?.focus();
        } else if (!e.shiftKey && document.activeElement === list.at(-1)) {
          e.preventDefault();
          list[0].focus();
        }
      }
    };
    document.addEventListener("keydown", handle);
    return () => {
      document.removeEventListener("keydown", handle);
      previous?.focus();
    };
  }, [item]);
  const loadOrders = useCallback(async (cursor?: string) => {
    const r = await api.get<Page<Order>>(
      "/orders?limit=12" + (cursor ? "&cursor=" + cursor : ""),
    );
    setOrders(r.items);
    setOrderCursor(r.next_cursor || null);
  }, []);
  const loadTickets = useCallback(async () => {
    const r = await api.get<Page<Ticket>>("/support/tickets?limit=20&offset=0");
    setTickets(r.items);
  }, []);
  const loadTicket = useCallback(async (id: string) => {
    const t = await api.get<Ticket>("/support/tickets/" + id);
    const all: Message[] = [];
    let offset = 0;
    while (true) {
      const page = await api.get<Page<Message>>(
        `/support/tickets/${id}/messages?limit=100&offset=${offset}`,
      );
      all.push(...page.items);
      if (page.items.length < 100) break;
      offset += 100;
    }
    if (activeId.current !== id) return;
    setTicket((old) =>
      old?.ticket_id === id && old.revision > t.revision ? old : t,
    );
    setMessages(all);
  }, []);
  const refresh = useCallback(async () => {
    await loadTickets();
    if (activeId.current) await loadTicket(activeId.current);
  }, [loadTickets, loadTicket]);
  const live = usePush(refresh);
  useEffect(() => {
    void loadTickets().catch((e) => setError(errorText(e)));
  }, [loadTickets]);
  useEffect(() => {
    return () => streamAbort.current?.abort();
  }, []);
  useEffect(() => {
    if (view === "orders")
      void loadOrders().catch((e) => setError(errorText(e)));
    if (view === "products") {
      setProductSearch("");
      void api
        .get<Page<Product>>("/products?limit=12")
        .then((r) => {
          setProducts(r.items);
          setProductCursor(r.next_cursor || null);
        })
        .catch((e) => setError(errorText(e)));
    }
  }, [view, loadOrders]);
  async function run(action: () => Promise<void>) {
    if (locked.current) return;
    locked.current = true;
    setBusy(true);
    setError("");
    try {
      await action();
    } catch (e) {
      setError(errorText(e));
      if (activeId.current) void loadTicket(activeId.current).catch(() => {});
    } finally {
      locked.current = false;
      setBusy(false);
    }
  }
  function navigate(next: View) {
    setError("");
    setView(next);
    history.replaceState(null, "", "/customer/" + next);
  }
  async function selectTicket(t: Ticket) {
    activeId.current = t.ticket_id;
    setTicket(t);
    setMessages([]);
    setOrderInput(t.order_id ? String(t.order_id) : "");
    await loadTicket(t.ticket_id);
  }
  async function send(e: FormEvent) {
    e.preventDefault();
    if (!draft.trim()) return;
    const message = draft.trim();
    await run(async () => {
      let result: Ticket;
      if (ticket && ticket.state !== "resolved") {
        if (orderInput && ticket.order_id === null)
          result = await api.mutate<Ticket>(
            `/support/tickets/${ticket.ticket_id}/information`,
            {
              expected_revision: ticket.revision,
              order_id: Number(orderInput),
              message,
            },
          );
        else
          result = await api.mutate<Ticket>(
            `/support/tickets/${ticket.ticket_id}/messages`,
            { expected_revision: ticket.revision, message },
          );
      } else
        result = await api.mutate<Ticket>("/support/requests", {
          message,
          order_id: orderInput || null,
        });
      activeId.current = result.ticket_id;
      setTicket(result);
      setDraft("");
      await Promise.all([loadTicket(result.ticket_id), loadTickets()]);
    });
  }
  async function handoff() {
    await run(async () => {
      const result = await api.mutate<Ticket>("/support/requests", {
        message: "转人工",
        order_id: orderInput || null,
      });
      activeId.current = result.ticket_id;
      setTicket(result);
      await Promise.all([loadTicket(result.ticket_id), loadTickets()]);
    });
  }
  async function guideStream() {
    if (streaming || !draft.trim()) return;
    setStreaming(true);
    setError("");
    setGuide([]);
    setGuideProduct(null);
    const control = new AbortController();
    streamAbort.current = control;
    try {
      const r = await api.raw("/v1/commerce/stream", {
        method: "POST",
        signal: control.signal,
        body: JSON.stringify({
          request: draft.trim(),
          category: "kettle",
          budget_cent: Math.round(Number(guideBudget) * 100),
          region: "上海",
          run_id: crypto.randomUUID(),
        }),
      });
      if (!r.body) throw new Error();
      const reader = r.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const chunk = await reader.read();
        buffer += decoder.decode(chunk.value, { stream: !chunk.done });
        const frames = buffer.split(/\r?\n\r?\n/);
        buffer = frames.pop() || "";
        for (const frame of frames) {
          const event = frame
            .split("\n")
            .find((x) => x.startsWith("event:"))
            ?.slice(6)
            .trim();
          if (event === "started")
            setGuide((x) => [...x, "已连接，正在查询商品与库存。"]);
          if (event === "result") {
            const data = JSON.parse(
              frame
                .split("\n")
                .filter((x) => x.startsWith("data:"))
                .map((x) => x.slice(5))
                .join("\n"),
            ) as {
              outcome: string;
              recommendation: {
                selected: {
                  id: string;
                  title: string;
                  total_cent: number;
                } | null;
              } | null;
              memory_write_status: string;
            };
            if (data.outcome !== "succeeded") throw new Error();
            const chosen = data.recommendation?.selected;
            setGuide((x) => [
              ...x,
              chosen
                ? `${chosen.title} · 含运费 ${money(chosen.total_cent)}`
                : "未找到符合预算且有库存的商品，请调整关键词或预算。",
              data.memory_write_status === "succeeded"
                ? "本次导购偏好已保存。"
                : "本次未保存导购记忆。",
            ]);
            if (chosen) setGuideProduct(chosen.id);
          }
          if (event === "error" || event === "aborted") throw new Error();
        }
        if (chunk.done) break;
      }
    } catch (e) {
      if (!control.signal.aborted) setError(errorText(e));
    } finally {
      setStreaming(false);
      streamAbort.current = null;
    }
  }
  return (
    <div className="app customer-app">
      <header className="topbar">
        <a className="brand" href="/customer">
          <b>A</b> Agent Ops <span>客户中心</span>
        </a>
        <div className="top-actions">
          <span className="live-dot">{live}</span>
          <span>顾客</span>
          <button
            className="text-button"
            onClick={() => void api.logout().catch(() => {})}
          >
            退出登录
          </button>
        </div>
      </header>
      <nav className="customer-nav" aria-label="客户导航">
        {(
          [
            ["chat", "导购与客服"],
            ["products", "商品浏览"],
            ["orders", "我的订单"],
            ["payment", "支付"],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            aria-current={view === key ? "page" : undefined}
            onClick={() => navigate(key)}
          >
            {label}
            {key === "payment" && (
              <small>{paymentsEnabled ? "沙箱" : "尚未开通"}</small>
            )}
          </button>
        ))}
      </nav>
      <main className="customer-main">
        <ErrorBox value={error} />
        {view === "chat" && (
          <>
            <div className="page-heading">
              <div>
                <span className="eyebrow">CONVERSATION</span>
                <h1>导购与客户服务</h1>
                <p className="muted">
                  订单问题、退货咨询与人工服务，都在这里继续。
                </p>
              </div>
              <div className="heading-tag">服务记录可追溯</div>
            </div>
            <div className="chat-layout">
              <section className="panel conversation">
                <header className="panel-title">
                  <div>
                    <h2>服务会话</h2>
                    {ticket ? (
                      <small className="muted">
                        工单 {ticket.ticket_id.slice(0, 8)}
                      </small>
                    ) : (
                      <small className="muted">开始一个新的问题</small>
                    )}
                  </div>
                  {ticket && <Status value={ticket.state} />}
                </header>
                <div className="chat-body">
                  {!ticket && (
                    <div className="welcome">
                      <div className="round-mark">A</div>
                      <h3>有什么可以帮您？</h3>
                      <p>咨询订单、申请退货，或选择转人工。</p>
                    </div>
                  )}
                  {ticket && (
                    <>
                      <Notice>
                        {reasonName[ticket.reason_code] || "服务状态已更新"}
                        {ticket.state === "waiting_human" &&
                          " · 已进入人工队列，请等待客服接入。"}
                      </Notice>
                      <Messages items={messages} />
                    </>
                  )}
                  {guide.length > 0 && (
                    <section className="guide-result" aria-label="流式演示结果">
                      <b>导购结果</b>
                      {guide.map((line, i) => (
                        <p key={i}>{line}</p>
                      ))}
                    </section>
                  )}
                </div>
                <form className="composer" onSubmit={send}>
                  <label className="compact-label">
                    关联订单号（可选）
                    <input
                      aria-label="关联订单号"
                      type="number"
                      min="1"
                      placeholder="填写属于您的订单号"
                      value={orderInput}
                      onChange={(e) => setOrderInput(e.target.value)}
                      disabled={ticket?.order_id != null}
                    />
                  </label>
                  <label className="sr-only" htmlFor="question">
                    您的问题
                  </label>
                  <textarea
                    id="question"
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    placeholder="请输入订单或服务问题…"
                    maxLength={2000}
                    required
                    rows={3}
                  />
                  <div className="composer-actions">
                    <button
                      type="button"
                      className="secondary"
                      onClick={() => void handoff()}
                      disabled={
                        busy ||
                        ticket?.state === "waiting_human" ||
                        ticket?.state === "claimed"
                      }
                    >
                      转人工
                    </button>
                    <button
                      className="primary"
                      disabled={busy || !draft.trim()}
                    >
                      {busy ? "发送中…" : "发送消息"}
                    </button>
                  </div>
                </form>
              </section>
              <aside className="chat-aside">
                <section className="panel aside-card">
                  <span className="eyebrow">能力状态</span>
                  <h3>导购功能</h3>
                  <p>按关键词和预算查询商品，核对当前可用库存与运费。</p>
                  <span
                    className={
                      shoppingEnabled ? "badge" : "badge badge-unavailable"
                    }
                  >
                    {shoppingEnabled ? "已开通" : "尚未开通"}
                  </span>
                  <p className="muted small">
                    在问题输入框填写商品关键词。推荐时的价格和库存以下单时再次核对为准。
                  </p>
                  <label>
                    导购预算（元）
                    <input
                      aria-label="导购预算（元）"
                      type="number"
                      min="1"
                      max="1000000"
                      value={guideBudget}
                      onChange={(e) => setGuideBudget(e.target.value)}
                    />
                  </label>
                  <button
                    className="secondary full"
                    onClick={() => void guideStream()}
                    disabled={
                      streaming ||
                      !shoppingEnabled ||
                      !Number.isFinite(Number(guideBudget)) ||
                      Number(guideBudget) <= 0
                    }
                  >
                    {streaming ? "正在查询…" : "开始导购"}
                  </button>
                  {guideProduct && (
                    <button
                      className="secondary full"
                      onClick={() =>
                        void run(async () => {
                          setItem(
                            await api.get<Product>("/products/" + guideProduct),
                          );
                          setQuantity(1);
                          navigate("products");
                        })
                      }
                    >
                      查看推荐商品
                    </button>
                  )}
                  <button
                    className="secondary full"
                    disabled={!memoryEnabled || streaming || busy}
                    onClick={() => void forgetMemory()}
                  >
                    删除全部导购记忆
                  </button>
                </section>
                <section className="panel aside-card">
                  <h3>历史工单</h3>
                  {!tickets.length && <p className="muted">暂无服务记录。</p>}
                  {tickets.map((t) => (
                    <button
                      className="ticket-link"
                      key={t.ticket_id}
                      onClick={() => void run(() => selectTicket(t))}
                    >
                      <span>#{t.ticket_id.slice(0, 8)}</span>
                      <Status value={t.state} />
                    </button>
                  ))}
                  <button
                    className="text-button"
                    onClick={() => {
                      activeId.current = null;
                      setTicket(null);
                      setMessages([]);
                      setOrderInput("");
                    }}
                  >
                    开始新会话
                  </button>
                </section>
              </aside>
            </div>
          </>
        )}
        {view === "products" && (
          <>
            <div className="page-heading">
              <div>
                <span className="eyebrow">CATALOG</span>
                <h1>商品浏览</h1>
                <p className="muted">读取商品库中的实时价格与可用库存。</p>
              </div>
            </div>
            <form
              className="panel"
              onSubmit={(event) => {
                event.preventDefault();
                void run(async () => {
                  const search =
                    "/products/search?limit=12&available_only=true&query=" +
                    encodeURIComponent(catalogKeyword.trim());
                  const result = await api.get<Page<Product>>(search);
                  setProducts(result.items);
                  setProductCursor(result.next_cursor || null);
                  setProductSearch(search);
                });
              }}
            >
              <label>
                商品关键词
                <input
                  aria-label="商品关键词"
                  maxLength={2000}
                  value={catalogKeyword}
                  onChange={(event) => setCatalogKeyword(event.target.value)}
                />
              </label>
              <button className="secondary" disabled={busy}>
                搜索有货商品
              </button>
            </form>
            <div className="product-grid">
              {products.map((p) => (
                <article className="panel product-card" key={p.id}>
                  <div className="product-art">
                    <span>
                      CATALOG / {String(p.category_id).padStart(2, "0")}
                    </span>
                    <b>{p.sku.slice(0, 2).toUpperCase()}</b>
                    <small>商品 #{p.id}</small>
                  </div>
                  <div className="product-copy">
                    <small className="muted">SKU {p.sku}</small>
                    <h3>{p.title}</h3>
                    <div className="price-row">
                      <strong>{money(p.price_cents)}</strong>
                      <small>库存 {p.available}</small>
                    </div>
                    <button
                      className="secondary full"
                      onClick={() =>
                        void run(async () => {
                          setItem(await api.get<Product>("/products/" + p.id));
                          setQuantity(1);
                        })
                      }
                    >
                      查看商品 #{p.id}
                    </button>
                  </div>
                </article>
              ))}
            </div>
            {!products.length && (
              <div className="empty">暂无可售商品，请确认已配置商品数据。</div>
            )}
            {productCursor && (
              <button
                className="secondary"
                onClick={() =>
                  void run(async () => {
                    const r = await api.get<Page<Product>>(
                      (productSearch
                        ? productSearch + "&offset="
                        : "/products?limit=12&cursor=") + productCursor,
                    );
                    setProducts((x) => [...x, ...r.items]);
                    setProductCursor(r.next_cursor || null);
                  })
                }
              >
                加载更多商品
              </button>
            )}
            {item && (
              <div className="modal-backdrop">
                <section
                  className="modal panel"
                  role="dialog"
                  aria-modal="true"
                  aria-label="商品详情"
                >
                  <button
                    className="close"
                    aria-label="关闭商品详情"
                    onClick={() => setItem(null)}
                  >
                    ×
                  </button>
                  <span className="eyebrow">PRODUCT DETAIL</span>
                  <h2>{item.title}</h2>
                  <p className="muted">
                    商品 #{item.id} · SKU {item.sku}
                  </p>
                  <h2>{money(item.price_cents)}</h2>
                  <p>
                    运费 {money(item.freight_cents)} · 可用库存 {item.available}
                  </p>
                  <label>
                    购买数量
                    <input
                      aria-label="购买数量"
                      type="number"
                      min={1}
                      max={Math.min(10000, item.available)}
                      value={quantity}
                      onChange={(e) => setQuantity(Number(e.target.value))}
                    />
                  </label>
                  <Notice>
                    下单会真实预留库存。支付仅在配置沙箱后可用，不涉及真实资金。
                  </Notice>
                  <div className="button-row">
                    <button className="secondary" onClick={() => setItem(null)}>
                      取消
                    </button>
                    <button
                      className="primary"
                      disabled={
                        busy || quantity < 1 || quantity > item.available
                      }
                      onClick={() =>
                        void run(async () => {
                          const placed = await api.mutate<{ order_id: number }>(
                            "/orders",
                            { lines: [{ product_id: item.id, quantity }] },
                          );
                          setItem(null);
                          navigate("orders");
                          await loadOrders();
                          setOrder(
                            await api.get<OrderDetail>(
                              "/orders/" + placed.order_id,
                            ),
                          );
                        })
                      }
                    >
                      确认下单
                    </button>
                  </div>
                </section>
              </div>
            )}
          </>
        )}
        {view === "orders" && (
          <>
            <div className="page-heading">
              <div>
                <span className="eyebrow">YOUR ORDERS</span>
                <h1>我的订单</h1>
                <p className="muted">只展示当前顾客的订单与物流。</p>
              </div>
              <button
                className="secondary"
                onClick={() => void run(() => loadOrders())}
              >
                刷新订单
              </button>
            </div>
            <section className="panel">
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>订单</th>
                      <th>创建时间</th>
                      <th>状态</th>
                      <th>金额</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {orders.map((o) => (
                      <tr key={o.order_id}>
                        <td>
                          <b>订单 #{o.order_id}</b>
                        </td>
                        <td>{time(o.created_at)}</td>
                        <td>
                          <Status value={o.status} />
                        </td>
                        <td>{money(o.total_cents)}</td>
                        <td>
                          <button
                            className="text-button"
                            onClick={() =>
                              void run(async () =>
                                setOrder(
                                  await api.get<OrderDetail>(
                                    "/orders/" + o.order_id,
                                  ),
                                ),
                              )
                            }
                          >
                            查看订单 #{o.order_id}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {!orders.length && (
                <div className="empty">暂无订单，去商品浏览看看。</div>
              )}
            </section>
            {orderCursor && (
              <button
                className="secondary"
                onClick={() => void run(() => loadOrders(orderCursor))}
              >
                下一页订单
              </button>
            )}
            {order && (
              <section className="panel order-detail">
                <div className="panel-title">
                  <h2>订单详情 #{order.id}</h2>
                  <Status value={order.status} />
                </div>
                <div className="detail-grid">
                  <div>
                    <small>金额</small>
                    <strong>{money(order.total_cents)}</strong>
                  </div>
                  <div>
                    <small>物流</small>
                    <span>
                      {order.shipment
                        ? `${order.shipment.carrier} · ${order.shipment.tracking_no}`
                        : "暂无物流信息"}
                    </span>
                  </div>
                </div>
                {order.items.map((line, i) => (
                  <p key={i}>
                    商品 #{line.product_id} × {line.quantity} ·{" "}
                    {money(line.unit_price_cents)}
                  </p>
                ))}
                <div className="button-row">
                  <button
                    className="secondary"
                    onClick={() => {
                      setOrderInput(String(order.id));
                      activeId.current = null;
                      setTicket(null);
                      setMessages([]);
                      navigate("chat");
                    }}
                  >
                    咨询此订单
                  </button>
                  <button
                    className="secondary"
                    disabled={busy}
                    onClick={() =>
                      void run(async () => {
                        const t = await api.mutate<Ticket>("/support/returns", {
                          message: "申请退货",
                          order_id: String(order.id),
                        });
                        activeId.current = t.ticket_id;
                        setTicket(t);
                        navigate("chat");
                        await refresh();
                      })
                    }
                  >
                    申请退货 / 人工处理
                  </button>
                  <button
                    className="primary"
                    onClick={() => navigate("payment")}
                  >
                    查看支付状态
                  </button>
                </div>
              </section>
            )}
          </>
        )}
        {view === "payment" && (
          <section className="panel unavailable-page">
            <span className="eyebrow">PAYMENT</span>
            <h1>{paymentsEnabled ? "订单沙箱支付" : "支付尚未开通"}</h1>
            {paymentsEnabled ? (
              <>
                <p>
                  仅使用沙箱买家。请选择自己的待支付订单，付款结果以服务端核对为准。
                </p>
                <p>
                  {order
                    ? `当前订单 #${order.id} · ${money(order.total_cents)}`
                    : "请先到我的订单选择一张订单。"}
                </p>
                <button
                  className="primary"
                  disabled={
                    busy || !order || order.status !== "pending_payment"
                  }
                  onClick={() => void pay()}
                >
                  生成沙箱支付链接
                </button>
                {paymentLink && (
                  <a
                    className="primary"
                    href={paymentLink}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    打开沙箱收银台
                  </a>
                )}
                {paymentMessage && <p role="status">{paymentMessage}</p>}
              </>
            ) : (
              <p>当前未配置支付服务，不会创建支付或模拟成功。</p>
            )}
            <button className="secondary" onClick={() => navigate("orders")}>
              返回我的订单
            </button>
          </section>
        )}
      </main>
      <footer className="page-footer">Agent Ops · 客户服务与订单协作</footer>
    </div>
  );
}
