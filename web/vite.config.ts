import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import fs from "fs";
import path from "path";

const certDir = path.resolve(__dirname, "../certs");
const hasCerts =
  fs.existsSync(path.join(certDir, "key.pem")) &&
  fs.existsSync(path.join(certDir, "cert.pem"));

// Allow the dev backend host/port to be overridden so e2e tests can run
// in parallel with a live dev server on the default ports. Tests pass
// VITE_BACKEND_HOST + VITE_BACKEND_PORT pointing at a fresh test server.
// VITE_DISABLE_HTTPS=1 forces http/ws even when certs/ exists (the test
// backend doesn't serve TLS to keep startup simple).
const backendHost = process.env.VITE_BACKEND_HOST ?? "localhost";
const backendPort = process.env.VITE_BACKEND_PORT ?? "3456";
const useHttps = hasCerts && process.env.VITE_DISABLE_HTTPS !== "1";
const httpScheme = useHttps ? "https" : "http";
const wsScheme = useHttps ? "wss" : "ws";

export default defineConfig({
  // Relative asset paths so the same build works at the hub root AND when
  // vended by a node under /nodes/{id}/ui/ (contract ui/v1).
  base: "./",
  plugins: [react(), tailwindcss()],
  test: {
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    exclude: ["node_modules/**", "dist/**", "tests/e2e/**"],
  },
  server: {
    host: "0.0.0.0",
    port: 5174,
    allowedHosts: true,
    ...(useHttps && {
      https: {
        key: fs.readFileSync(path.join(certDir, "key.pem")),
        cert: fs.readFileSync(path.join(certDir, "cert.pem")),
      },
    }),
    proxy: {
      "/api": {
        target: `${httpScheme}://${backendHost}:${backendPort}`,
        secure: false,
      },
      "/ws": {
        target: `${wsScheme}://${backendHost}:${backendPort}`,
        ws: true,
        secure: false,
        configure: (proxy) => {
          // Suppress TLS socket errors when backend closes WS connections
          proxy.on("error", () => {});
          proxy.on("proxyReqWs", (_proxyReq, _req, socket) => {
            socket.on("error", () => {});
          });
        },
      },
      // Node-vended UI proxy: forward every /nodes/{id}/{ui,api,ws}/
      // to the hub backend, which tunnels it through to the node's own
      // loopback server. Without the /ui/ line here, Vite's SPA
      // catch-all would answer with the shell's own bundle, hiding
      // whatever HTML the node actually vends.
      "^/nodes/[^/]+/(ui|api)/": {
        target: `${httpScheme}://${backendHost}:${backendPort}`,
        secure: false,
      },
      "^/nodes/[^/]+/ws/": {
        target: `${wsScheme}://${backendHost}:${backendPort}`,
        ws: true,
        secure: false,
        configure: (proxy) => {
          proxy.on("error", () => {});
          proxy.on("proxyReqWs", (_proxyReq, _req, socket) => {
            socket.on("error", () => {});
          });
        },
      },
    },
  },
});
