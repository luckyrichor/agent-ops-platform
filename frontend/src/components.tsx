import type { FormEvent, ReactNode } from "react";
import { useState } from "react";
import { api } from "./api";
import type { Account, Message } from "./types";
import { stateName, time, reasonName } from "./types";
export function Status({ value }: { value: string }) {
  return (
    <span className={`badge badge-${value}`}>{stateName[value] || value}</span>
  );
}
export function Notice({ children }: { children: ReactNode }) {
  return (
    <div className="notice" role="status">
      {children}
    </div>
  );
}
export function ErrorBox({ value }: { value: string }) {
  return value ? (
    <div className="error" role="alert">
      {value}
    </div>
  ) : null;
}
export function Login({
  staff,
  onLogin,
}: {
  staff: boolean;
  onLogin: (account: Account) => void;
}) {
  const [email, setEmail] = useState(""),
    [password, setPassword] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const account = await api.login(email, password);
      if (
        (staff && account.role === "customer") ||
        (!staff && account.role !== "customer")
      ) {
        api.clear(false);
        setError("账号角色与当前入口不符，请选择对应入口。");
        return;
      }
      setPassword("");
      setEmail("");
      onLogin(account);
    } catch {
      setError("登录未完成，请检查账号、密码及本机服务。");
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="login-layout">
      <section className="login-intro">
        <div className="brand light">
          <b>A</b> Agent Ops
        </div>
        <span className="eyebrow">
          {staff ? "SERVICE DESK" : "CUSTOMER PORTAL"}
        </span>
        <h1>{staff ? "把每次服务，\n接稳。" : "选购与服务，\n清楚地连接。"}</h1>
        <p>
          {staff
            ? "查看队列、跟进会话、核对订单。每一步都有记录。"
            : "浏览商品，管理订单，随时接入人工服务。"}
        </p>
        <div className="intro-rule" />
        <small>本机服务 · 通过安全隧道访问</small>
      </section>
      <section className="login-card">
        <span className="eyebrow">欢迎回来</span>
        <h2>{staff ? "登录人工工作台" : "登录客户网页"}</h2>
        <p className="muted">
          使用已配置的{staff ? "客服或管理员" : "顾客"}账号。
        </p>
        <form onSubmit={submit}>
          <label>
            邮箱
            <input
              data-private
              type="email"
              autoComplete="username"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </label>
          <label>
            密码
            <input
              data-private
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
          <ErrorBox value={error} />
          <button className="primary full" disabled={busy}>
            {busy ? "正在登录…" : "登录"}
          </button>
        </form>
        <p className="login-foot">
          {staff ? "顾客访问" : "客服与管理员"}{" "}
          <a href={staff ? "/customer" : "/workbench"}>
            {staff ? "客户网页" : "人工工作台"} →
          </a>
        </p>
        <small className="muted">
          会话仅保存在当前标签页，刷新后需重新登录。注册尚未开通。
        </small>
      </section>
    </main>
  );
}
export function Messages({ items }: { items: Message[] }) {
  return (
    <div className="messages" aria-label="会话历史">
      {!items.length && <p className="muted">暂时没有消息。</p>}
      {items.map((m) => (
        <article
          key={`${m.origin}-${m.conversation_id || 0}-${m.id}`}
          className={`message message-${m.sender_role}`}
        >
          <header>
            <b>
              {(
                {
                  customer: "顾客",
                  agent: "客服",
                  system: "系统",
                  source: "历史记录 · 角色未标注",
                } as Record<string, string>
              )[m.sender_role] || "历史记录"}
            </b>
            <time>{time(m.created_at)}</time>
          </header>
          <p>
            {m.sender_role === "system"
              ? reasonName[m.content] ||
                (/^[A-Z_]+$/.test(m.content) ? "服务状态已更新" : m.content)
              : m.content}
          </p>
        </article>
      ))}
    </div>
  );
}
