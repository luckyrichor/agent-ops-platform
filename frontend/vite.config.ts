import { defineConfig } from "vite";
const target = process.env.P5_API_TARGET || "http://127.0.0.1:8003";
if (new URL(target).hostname !== "127.0.0.1")
  throw new Error("API target must be loopback");
const proxy = {
  "/api": {
    target,
    ws: true,
    rewrite: (path: string) => path.replace(/^\/api/, ""),
  },
};
export default defineConfig({
  server: { host: "127.0.0.1", port: 5173, strictPort: true, proxy },
  preview: { host: "127.0.0.1", port: 5173, strictPort: true, proxy },
  build: { sourcemap: false },
});
