import { test, expect, type Page, type WebSocketRoute } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
type Credential = {
  email: string;
  password: string;
  role: string;
  customer_user_id?: number;
};
const roster = JSON.parse(fs.readFileSync(process.env.P5_ROSTER_PATH!, "utf8"))
  .accounts as Credential[];
const customers = roster.filter((a) => a.role === "customer"),
  seat = roster.find((a) => a.role === "agent")!,
  admin = roster.find((a) => a.role === "admin")!;
const shots = path.join(process.env.P5_ARTIFACTS!, "screenshots");
fs.mkdirSync(shots, { recursive: true });
async function shot(page: Page, name: string) {
  await page.screenshot({
    path: path.join(shots, name + ".png"),
    fullPage: true,
    mask: [
      page.locator("[data-private],input[type=password],input[type=email]"),
    ],
    maskColor: "#cad5dc",
  });
}
async function login(page: Page, account: Credential, staff = false) {
  await page.goto(staff ? "/workbench" : "/customer");
  // Arguments never appear in the checked-in test or Playwright call logs/traces.
  await page.evaluate(
    ({ email, password }) => {
      const set = (selector: string, value: string) => {
        const el = document.querySelector<HTMLInputElement>(selector)!;
        Object.getOwnPropertyDescriptor(
          HTMLInputElement.prototype,
          "value",
        )!.set!.call(el, value);
        el.dispatchEvent(new Event("input", { bubbles: true }));
      };
      set("input[type=email]", email);
      set("input[type=password]", password);
    },
    { email: account.email, password: account.password },
  );
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("button", { name: "退出登录" })).toBeVisible();
}
async function navigate(page: Page, name: string) {
  await page
    .getByRole("navigation", { name: "客户导航" })
    .getByRole("button", { name, exact: true })
    .click();
}

test("顾客提问到实时人工回复和解决，含重连及完整操作记录", async ({
  browser,
}) => {
  const context = await browser.newContext();
  const customer = await context.newPage(),
    agent = await context.newPage();
  const sockets: WebSocketRoute[] = [];
  await customer.routeWebSocket("**/api/ws", (ws) => {
    ws.connectToServer();
    sockets.push(ws);
  });
  const errors: string[] = [];
  customer.on("pageerror", (e) => errors.push(e.name));
  agent.on("pageerror", (e) => errors.push(e.name));
  await customer.goto("/customer");
  await shot(customer, "01-customer-login");
  await login(customer, customers[0]);
  await login(agent, seat, true);
  await expect(
    agent.getByText("已连接", { exact: true }).first(),
  ).toBeVisible();
  await customer.getByLabel("您的问题").fill("我想申请退货，请人工协助");
  await customer.getByLabel("关联订单号", { exact: true }).fill("501");
  await customer.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect(
    customer.getByText("排队中", { exact: true }).first(),
  ).toBeVisible();
  await expect(agent.getByTestId("queue-ticket")).toHaveCount(1);
  await shot(customer, "02-customer-queued");
  await shot(agent, "03-workbench-queue");
  await agent.getByTestId("queue-ticket").first().click();
  await expect(
    agent.getByRole("heading", { name: "会话历史", exact: true }),
  ).toBeVisible();
  const otherAgent = await context.newPage();
  await login(otherAgent, roster.filter((a) => a.role === "agent")[1], true);
  await otherAgent.getByTestId("queue-ticket").first().click();
  await agent.getByRole("button", { name: "领取工单", exact: true }).click();
  await expect(agent.getByLabel("回复顾客", { exact: true })).toBeEnabled();
  await expect(
    agent.getByRole("button", { name: "领取工单", exact: true }),
  ).toHaveCount(0);
  await expect(
    agent.getByRole("button", { name: "已由我领取", exact: true }),
  ).toBeDisabled();
  await expect(
    customer.getByText("客服处理中", { exact: true }).first(),
  ).toBeVisible();
  const heldByOther = otherAgent.getByRole("button", {
    name: /客服 #\d+ 持有/,
  });
  await expect(heldByOther).toBeVisible();
  await expect(heldByOther).toBeDisabled();
  await expect(
    otherAgent.getByLabel("回复顾客", { exact: true }),
  ).toBeDisabled();
  await otherAgent.close();
  // Disconnect only this customer page's WebSocket; API and the second tab stay connected.
  await sockets.at(-1)!.close();
  await agent
    .getByLabel("回复顾客", { exact: true })
    .fill("已收到您的退货诉求，我正在为您核对订单。");
  await agent.getByRole("button", { name: "发送回复", exact: true }).click();
  await expect(
    customer.getByText("已收到您的退货诉求，我正在为您核对订单。", {
      exact: true,
    }),
  ).toBeVisible();
  await agent
    .getByRole("button", { name: "查看关联订单", exact: true })
    .click();
  await expect(
    agent
      .getByRole("region", { name: "关联订单详情" })
      .or(agent.locator('[aria-label="关联订单详情"]')),
  ).toBeVisible();
  await expect(
    agent.getByText("查看关联订单", { exact: true }).last(),
  ).toBeVisible();
  await shot(agent, "04-workbench-ticket");
  await shot(customer, "05-customer-reply");
  await agent.getByRole("button", { name: "解决工单", exact: true }).click();
  await expect(
    customer.getByText("已解决", { exact: true }).first(),
  ).toBeVisible();
  await expect(
    agent.locator(".audit-panel").getByText("解决工单", { exact: true }),
  ).toBeVisible();
  for (const text of [
    "创建工单",
    "领取工单",
    "发送消息",
    "查看关联订单",
    "解决工单",
  ])
    await expect(
      agent.locator(".audit-panel").getByText(text, { exact: true }).first(),
    ).toBeVisible();
  await shot(agent, "06-workbench-resolved");
  expect(errors).toEqual([]);
  await context.close();
});

test("同一浏览器两个标签页登录不同顾客，订单与工单数据互不串用", async ({
  browser,
}) => {
  const context = await browser.newContext();
  const a = await context.newPage(),
    b = await context.newPage();
  let authorization = "";
  a.on("request", (request) => {
    if (
      request.url().includes("/api/orders") &&
      request.headers()["authorization"]
    )
      authorization = request.headers()["authorization"];
  });
  await login(a, customers[0]);
  await login(b, customers[1]);
  await navigate(a, "我的订单");
  await navigate(b, "我的订单");
  await expect(a.getByText("订单 #501", { exact: true })).toBeVisible();
  await expect(a.getByText("订单 #502", { exact: true })).toHaveCount(0);
  await expect(b.getByText("订单 #502", { exact: true })).toBeVisible();
  await expect(b.getByText("订单 #501", { exact: true })).toHaveCount(0);
  await navigate(b, "导购与客服");
  await b.getByLabel("您的问题").fill("第二位顾客的独立咨询");
  await b.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect(
    b.getByText("第二位顾客的独立咨询", { exact: true }),
  ).toBeVisible();
  await navigate(a, "导购与客服");
  await expect(
    a.getByText("第二位顾客的独立咨询", { exact: true }),
  ).toHaveCount(0);
  const denied = await a.evaluate(async (header) => {
    return (
      await fetch("/api/orders/502", { headers: { Authorization: header } })
    ).status;
  }, authorization);
  expect(denied).toBe(404);
  for (const page of [a, b])
    expect(
      await page.evaluate(() => ({
        local: localStorage.length,
        session: sessionStorage.length,
      })),
    ).toEqual({ local: 0, session: 0 });
  await shot(b, "07-customer-isolated");
  await a.getByRole("button", { name: "退出登录" }).click();
  await expect(a.getByRole("heading", { name: "登录客户网页" })).toBeVisible();
  await expect(b.getByRole("button", { name: "退出登录" })).toBeVisible();
  await context.close();
});

test("真实商品浏览下单、订单详情、支付骨架及商品库 SSE 导购", async ({
  page,
}) => {
  await login(page, customers[2]);
  await navigate(page, "商品浏览");
  await expect(page.getByRole("heading", { name: "办公保温杯" })).toBeVisible();
  await shot(page, "08-products");
  await page
    .getByRole("button", { name: "查看商品 #101", exact: true })
    .click();
  await expect(page.getByRole("dialog", { name: "商品详情" })).toBeVisible();
  await shot(page, "09-product-detail");
  await page.getByLabel("购买数量", { exact: true }).fill("2");
  await page.getByRole("button", { name: "确认下单", exact: true }).click();
  await expect(page.getByRole("heading", { name: /订单详情 #/ })).toBeVisible();
  await expect(page.getByText("商品 #101 × 2")).toBeVisible();
  await shot(page, "10-orders");
  await navigate(page, "支付尚未开通");
  await expect(
    page.getByRole("heading", { name: "支付尚未开通", exact: true }),
  ).toBeVisible();
  await shot(page, "11-payment-not-enabled");
  await navigate(page, "导购与客服");
  await page.getByLabel("您的问题").fill("办公保温杯");
  await page.getByRole("button", { name: "开始导购" }).click();
  await expect(
    page.getByText("已连接，正在查询商品与库存。", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("本次未保存导购记忆。", { exact: true }),
  ).toBeVisible();
  await shot(page, "12-streaming-demo");
});

test("客服退回后重新领取，已领取列表可恢复并可查看统计", async ({
  browser,
}) => {
  const context = await browser.newContext();
  const c = await context.newPage(),
    a = await context.newPage();
  await login(c, customers[1]);
  await login(a, admin, true);
  await c.getByRole("button", { name: "转人工", exact: true }).click();
  await expect(
    a.getByTestId("queue-ticket").filter({ hasText: "人工服务" }),
  ).toHaveCount(1);
  await a.getByTestId("queue-ticket").filter({ hasText: "人工服务" }).click();
  await a.getByRole("button", { name: "领取工单", exact: true }).click();
  await expect(
    a.getByRole("button", { name: "退回队列", exact: true }),
  ).toBeEnabled();
  await a.getByRole("button", { name: "退回队列", exact: true }).click();
  await expect(c.getByText("排队中", { exact: true }).first()).toBeVisible();
  await a.getByRole("button", { name: "领取工单", exact: true }).click();
  await a.getByRole("button", { name: "我领取的", exact: true }).click();
  await expect(a.getByTestId("queue-ticket")).toHaveCount(1);
  await a.getByRole("button", { name: "▦ 服务概览" }).click();
  await expect(a.getByText("待处理队列", { exact: true })).toBeVisible();
  await shot(a, "13-admin-stats");
  await context.close();
});

test("移动端布局和登录遮罩截图", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/customer");
  await shot(page, "14-mobile-login");
  await login(page, customers[2]);
  await navigate(page, "商品浏览");
  await expect(
    page.getByRole("heading", { name: "商品浏览", exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await shot(page, "15-mobile-products");
});

test("P6 支付页契约：真实夹具订单生成链接，平台令牌不发送到网关", async ({
  browser,
}) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.route("**/api/payments/availability", (route) =>
    route.fulfill({ json: { enabled: true } }),
  );
  await page.route("**/api/orders/*/payment", (route) =>
    route.fulfill({
      json: { payment_url: "https://fixture.alipaydev.com/test-payment" },
    }),
  );
  let leaked = false;
  await context.route("https://fixture.alipaydev.com/**", (route) => {
    leaked = Boolean(route.request().headers()["authorization"]);
    return route.fulfill({
      contentType: "text/html",
      body: "<p>UI contract fixture only</p>",
    });
  });
  await login(page, customers[2]);
  await navigate(page, "商品浏览");
  await page
    .getByRole("button", { name: "查看商品 #101", exact: true })
    .click();
  await page.getByRole("button", { name: "确认下单", exact: true }).click();
  await expect(page.getByRole("heading", { name: /订单详情 #/ })).toBeVisible();
  await navigate(page, "支付沙箱");
  await page
    .getByRole("button", { name: "生成沙箱支付链接", exact: true })
    .click();
  const popup = context.waitForEvent("page");
  await page.getByRole("link", { name: "打开沙箱收银台", exact: true }).click();
  const external = await popup;
  await expect(external.getByText("UI contract fixture only")).toBeVisible();
  expect(leaked).toBe(false);
  await expect(page.getByText(/跳转成功不代表已付款/)).toBeVisible();
  await context.close();
});
