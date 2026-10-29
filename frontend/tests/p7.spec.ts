import { test, expect, type Page } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
test.skip(
  !process.env.P7_ROSTER_PATH || !process.env.P7_KEYWORD_PATH,
  "P7 requires explicit P2 runtime configuration",
);
const roster = (
  process.env.P7_ROSTER_PATH
    ? JSON.parse(fs.readFileSync(process.env.P7_ROSTER_PATH, "utf8")).accounts
    : []
).filter((a: { role: string }) => a.role === "customer") as {
  email: string;
  password: string;
}[];
const keyword = process.env.P7_KEYWORD_PATH
  ? fs.readFileSync(process.env.P7_KEYWORD_PATH, "utf8").trim()
  : "";
async function login(page: Page, account: { email: string; password: string }) {
  await page.goto("/customer");
  await page.waitForLoadState("networkidle");
  await expect(page.locator("input[type=email]")).toBeVisible();
  await page.evaluate(({ email, password }) => {
    for (const [selector, value] of [
      ["input[type=email]", email],
      ["input[type=password]", password],
    ]) {
      const el = document.querySelector<HTMLInputElement>(selector)!;
      Object.getOwnPropertyDescriptor(
        HTMLInputElement.prototype,
        "value",
      )!.set!.call(el, value);
      el.dispatchEvent(new Event("input", { bubbles: true }));
    }
  }, account);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("button", { name: "退出登录" })).toBeVisible();
}
async function guide(page: Page) {
  await page.getByLabel("您的问题").fill(keyword);
  await page.getByLabel("导购预算（元）").fill("2000");
  await page.getByRole("button", { name: "开始导购", exact: true }).click();
  await expect(
    page.getByText("本次导购偏好已保存。", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "查看推荐商品" }),
  ).toBeVisible();
}
async function shot(page: Page, name: string) {
  const folder = path.join(process.env.P5_ARTIFACTS!, "screenshots");
  fs.mkdirSync(folder, { recursive: true });
  await page.screenshot({
    path: path.join(folder, name + ".png"),
    fullPage: true,
    mask: [
      page.locator("[data-private],input[type=password],input[type=email]"),
    ],
    maskColor: "#cad5dc",
  });
}

test("P2真实商品流式导购、商品面板和清除记忆", async ({ page }) => {
  await login(page, roster[0]);
  await guide(page);
  await shot(page, "p7-guide");
  await page.getByRole("button", { name: "查看推荐商品" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await shot(page, "p7-real-product");
  await page.keyboard.press("Escape");
  await page.getByLabel("商品关键词").fill(keyword);
  await page.getByRole("button", {name:"搜索有货商品"}).click();
  await expect(page.locator(".product-card").first()).toBeVisible();
  await page
    .getByRole("navigation", { name: "客户导航" })
    .getByRole("button", { name: "导购与客服", exact: true })
    .click();
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "删除全部导购记忆" }).click();
  await expect(
    page.getByText("全部导购记忆已删除。", { exact: true }),
  ).toBeVisible();
  await shot(page, "p7-forgotten");
});

test("同一浏览器两个标签页的顾客身份和记忆隔离", async ({ context }) => {
  const a = await context.newPage(),
    b = await context.newPage();
  await login(a, roster[0]);
  await login(b, roster[1]);
  const body = {
    request: keyword,
    category: "fixture",
    budget_cent: 200000,
    region: "上海",
    run_id: crypto.randomUUID(),
  };
  async function invoke(
    page: Page,
    input: typeof body & { memory_id?: string },
  ) {
    return page.evaluate(async (payload) => {
      const modulePath = "/src/api.ts";
      const { api } = await import(modulePath);
      return (
        await api.raw("/v1/commerce/runs", {
          method: "POST",
          body: JSON.stringify(payload),
        })
      ).json();
    }, input);
  }
  const first = await invoke(a, body);
  expect(first.memory_write_status).toBe("succeeded");
  const other = await invoke(b, {
    ...body,
    memory_id: first.memory_id,
    run_id: crypto.randomUUID(),
  });
  expect(other.memory_read_status).toBe("read_degraded");
  expect(other.memory_id).not.toBe(first.memory_id);
  expect((await invoke(a, body)).memory_id).toBe(first.memory_id);
  await a.evaluate(async () => {
    const modulePath = "/src/api.ts";
    const { api } = await import(modulePath);
    await api.raw("/v1/commerce/memory", { method: "DELETE" });
  });
  expect(
    (
      await invoke(b, {
        ...body,
        memory_id: other.memory_id,
        run_id: crypto.randomUUID(),
      })
    ).memory_read_status,
  ).toBe("available");
  await a.close();
  await b.close();
});
