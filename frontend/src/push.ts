import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { Push } from "./types";
export function usePush(refresh: () => Promise<void>) {
  const callback = useRef(refresh);
  callback.current = refresh;
  const [status, setStatus] = useState("连接中");
  useEffect(() => {
    let active = true,
      socket: WebSocket | null = null,
      cursor = 0,
      queue: Push[] = [],
      timer: ReturnType<typeof setTimeout> | undefined;
    let reconnect: ReturnType<typeof setTimeout> | undefined,
      working = false,
      retries = 0;
    async function drain() {
      if (!active || working || !queue.length) return;
      working = true;
      const batch = queue.slice();
      try {
        if (
          batch.some(
            (e) =>
              !["lease_renewed", "order_viewed"].includes(
                e.payload.change || "",
              ),
          )
        )
          await callback.current();
        if (active) {
          cursor = Math.max(cursor, ...batch.map((e) => e.sequence));
          queue = queue.filter((e) => e.sequence > cursor);
        }
      } catch {
        socket?.close();
      } finally {
        working = false;
        if (active && queue.length) timer = setTimeout(drain, 100);
      }
    }
    async function connect() {
      try {
        const token = await api.token();
        if (!active) return;
        socket = new WebSocket(
          `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/ws`,
        );
        socket.onopen = () => {
          socket?.send(JSON.stringify({ access_token: token, after: cursor }));
          socket?.send("ping");
        };
        socket.onmessage = (message: MessageEvent<string>) => {
          if (!active) return;
          try {
            const event = JSON.parse(message.data);
            if (event.type === "pong") {
              retries = 0;
              setStatus("已连接");
              return;
            }
            if (
              typeof event.sequence !== "number" ||
              event.sequence <= cursor ||
              queue.some((e) => e.sequence === event.sequence)
            )
              return;
            queue.push(event as Push);
            if (queue.length > 500) {
              socket?.close();
              return;
            }
            if (timer) clearTimeout(timer);
            timer = setTimeout(drain, 30);
          } catch {
            socket?.close();
          }
        };
        socket.onclose = () => {
          if (active) {
            setStatus("重连中");
            reconnect = setTimeout(
              connect,
              Math.min(500 * 2 ** retries++, 5000),
            );
          }
        };
        socket.onerror = () => socket?.close();
      } catch {
        if (active) {
          setStatus("等待登录");
          reconnect = setTimeout(connect, 2000);
        }
      }
    }
    void connect();
    return () => {
      active = false;
      if (timer) clearTimeout(timer);
      if (reconnect) clearTimeout(reconnect);
      socket?.close();
    };
  }, []);
  return status;
}
