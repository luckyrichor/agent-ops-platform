export type Role = "customer" | "agent" | "admin";
export interface Account {
  id: number;
  role: Role;
  customer_user_id: number | null;
}
export interface Tokens {
  access_token: string;
  refresh_token: string;
  expires_in: number;
  account: Account;
}
export interface Ticket {
  ticket_id: string;
  route: string;
  state: "needs_information" | "waiting_human" | "claimed" | "resolved";
  revision: number;
  reason_code: string;
  order_id: number | null;
  assigned_account_id: number | null;
  lease_expires_at: string | null;
  priority: number;
  sla_due_at: string | null;
  sla_warned: boolean;
  sla_overdue: boolean;
}
export interface Message {
  id: number;
  origin: "seed" | "live";
  sender_role: string;
  content: string;
  created_at: string;
  conversation_id?: number | null;
}
export interface Audit {
  revision: number;
  type: string;
  actor: string;
  created_at: string;
}
export interface Detail {
  ticket: Ticket;
  messages: Message[];
  events: Audit[];
}
export interface Product {
  id: number;
  sku: string;
  title: string;
  category_id: number;
  price_cents: number;
  freight_cents: number;
  available: number;
}
export interface Order {
  order_id: number;
  status: string;
  total_cents: number;
  revision: number;
  created_at: string;
}
export interface OrderDetail {
  id: number;
  user_id: number;
  status: string;
  total_cents: number;
  revision: number;
  items: { product_id: number; quantity: number; unit_price_cents: number }[];
  shipment: { carrier: string; tracking_no: string; status: string } | null;
}
export interface Page<T> {
  items: T[];
  next_cursor?: string | null;
  offset?: number;
  limit?: number;
}
export interface Push {
  sequence: number;
  kind: string;
  payload: { ticket_id: string; revision: number; change?: string };
}
export const stateName: Record<string, string> = {
  needs_information: "待补充信息",
  waiting_human: "排队中",
  claimed: "客服处理中",
  resolved: "已解决",
  pending_payment: "待支付",
  shipped: "已发货",
  delivered: "已送达",
  canceled: "已取消",
  processing: "处理中",
};
export const routeName: Record<string, string> = {
  human: "人工服务",
  order_status: "订单与物流",
  return_request: "退货申请",
  clarify: "待澄清",
};
export const reasonName: Record<string, string> = {
  ORDER_INFORMATION_SUPPLIED: "订单信息已补充",
  HUMAN_APPROVAL_REQUIRED: "需要人工核实处理",
  ORDER_ID_REQUIRED_OR_UNKNOWN: "请补充属于您的订单号",
  ORDER_STATUS_FOUND: "已查到订单状态",
  CLARIFY_REQUEST: "请补充问题或转人工",
  HUMAN_CLAIMED: "客服已领取",
  HUMAN_RESOLVED: "客服已完成处理",
  HUMAN_RELEASED: "已退回待处理队列",
  CLAIM_LEASE_EXPIRED: "领取超时，已返回队列",
};
export const money = (cent: number) =>
  new Intl.NumberFormat("zh-CN", { style: "currency", currency: "CNY" }).format(
    cent / 100,
  );
export const time = (value?: string | null) =>
  value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—";
