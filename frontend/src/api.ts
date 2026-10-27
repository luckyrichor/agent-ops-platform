import type { Tokens } from "./types";
export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
  ) {
    super("请求未完成");
  }
}
export function errorText(error: unknown) {
  if (error instanceof ApiError) {
    if (error.status === 401) return "登录已过期，请重新登录。";
    if (error.status === 403) return "当前账号无权执行此操作。";
    if (error.status === 404) return "内容不存在，或不属于当前账号。";
    if (error.status === 409)
      return "状态或库存已更新，请刷新后重试，输入内容已保留。";
    if (error.status === 422) return "请检查输入内容和订单号。";
    if (error.status === 503) return "服务暂不可用，请稍后重试。";
  }
  return "连接未完成，请检查本机服务或 SSH 转发后重试。";
}
class MemorySession {
  private tokens: Tokens | null = null;
  private expires = 0;
  private generation = 0;
  private refreshing: Promise<void> | null = null;
  private keys = new Map<string, string>();
  onLost = () => {};
  private accept(tokens: Tokens) {
    this.tokens = tokens;
    this.expires = Date.now() + tokens.expires_in * 1000;
  }
  async login(email: string, password: string) {
    this.clear(false);
    const version = this.generation;
    const response = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
      cache: "no-store",
      credentials: "omit",
    });
    if (!response.ok) throw new ApiError(response.status, "LOGIN_FAILED");
    const tokens = (await response.json()) as Tokens;
    if (version !== this.generation) throw new ApiError(401, "SESSION_CHANGED");
    this.accept(tokens);
    return tokens.account;
  }
  clear(notify = true) {
    this.generation++;
    this.tokens = null;
    this.expires = 0;
    this.keys.clear();
    this.refreshing = null;
    if (notify) this.onLost();
  }
  async ensure(force = false) {
    if (!this.tokens) throw new ApiError(401, "AUTH_REQUIRED");
    if (!force && Date.now() < this.expires - 30000) return;
    if (!this.refreshing) {
      const generation = this.generation;
      const refresh = this.tokens.refresh_token;
      this.refreshing = (async () => {
        const r = await fetch("/api/auth/refresh", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: refresh }),
          cache: "no-store",
          credentials: "omit",
        });
        if (!r.ok) {
          if (generation === this.generation) this.clear();
          throw new ApiError(401, "AUTH_REQUIRED");
        }
        const tokens = (await r.json()) as Tokens;
        if (generation !== this.generation)
          throw new ApiError(401, "SESSION_CHANGED");
        this.accept(tokens);
      })().finally(() => {
        if (generation === this.generation) this.refreshing = null;
      });
    }
    await this.refreshing;
  }
  async token() {
    await this.ensure();
    return this.tokens!.access_token;
  }
  async raw(
    path: string,
    init: RequestInit = {},
    retry = true,
  ): Promise<Response> {
    await this.ensure();
    const r = await fetch("/api" + path, {
      ...init,
      cache: "no-store",
      credentials: "omit",
      headers: {
        "Content-Type": "application/json",
        ...init.headers,
        Authorization: "Bearer " + this.tokens!.access_token,
      },
    });
    if (r.status === 401 && retry) {
      await this.ensure(true);
      return this.raw(path, init, false);
    }
    if (!r.ok) {
      let code = "REQUEST_FAILED";
      try {
        const data = await r.json();
        if (typeof data.detail === "string") code = data.detail;
      } catch {
        /* no response payload is logged */
      }
      throw new ApiError(r.status, code);
    }
    return r;
  }
  async get<T>(path: string): Promise<T> {
    return (await this.raw(path)).json() as Promise<T>;
  }
  async mutate<T>(path: string, body: unknown): Promise<T> {
    const fingerprint = path + JSON.stringify(body);
    const key = this.keys.get(fingerprint) || crypto.randomUUID();
    this.keys.set(fingerprint, key);
    const r = await this.raw(path, {
      method: "POST",
      body: JSON.stringify(body),
      headers: { "Idempotency-Key": key },
    });
    this.keys.delete(fingerprint);
    return r.json() as Promise<T>;
  }
  async logout() {
    try {
      await this.raw("/auth/logout", { method: "POST" });
    } finally {
      this.clear();
    }
  }
}
export const api = new MemorySession();
