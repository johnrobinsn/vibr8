import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App.js";
import { NODE_MODE } from "./nodeMode.js";
import { initShellBridge } from "./shellBridge.js";
import "./index.css";

initShellBridge();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>
);

// The service worker is registered at the origin root, which belongs to
// the hub shell — a node-vended iframe must not claim that scope.
if ("serviceWorker" in navigator && !NODE_MODE) {
  navigator.serviceWorker.register("/sw.js");
}
