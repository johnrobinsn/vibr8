import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import fs from "fs";
import path from "path";

const certDir = path.resolve(__dirname, "../certs");
const hasCerts =
  fs.existsSync(path.join(certDir, "key.pem")) &&
  fs.existsSync(path.join(certDir, "cert.pem"));

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: "0.0.0.0",
    port: 5174,
    allowedHosts: true,
    ...(hasCerts && {
      https: {
        key: fs.readFileSync(path.join(certDir, "key.pem")),
        cert: fs.readFileSync(path.join(certDir, "cert.pem")),
      },
    }),
    proxy: {
      "/api": "http://localhost:3456",
      "/ws": {
        target: "ws://localhost:3456",
        ws: true,
        configure: (proxy) => {
          // Suppress TLS socket errors when backend closes WS connections
          proxy.on("error", () => {});
          proxy.on("proxyReqWs", (_proxyReq, _req, socket) => {
            socket.on("error", () => {});
          });
        },
      },
    },
  },
});
