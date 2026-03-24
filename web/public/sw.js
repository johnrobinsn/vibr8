self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {
  // Pass through — let the browser handle all fetches natively.
  // Previously this called e.respondWith(fetch(e.request)) which broke
  // WebSocket upgrades since fetch() can't handle them.
});

// Show notifications on behalf of the main thread (required on mobile)
self.addEventListener("message", (e) => {
  if (e.data && e.data.type === "show_notification") {
    self.registration.showNotification(e.data.title || "vibr8", {
      body: e.data.body || "",
    });
  }
});
