import { useState, useEffect, useCallback, useRef } from "react";
import { api } from "../api.js";

interface TokenInfo {
  id: string;
  name: string;
  createdAt: number;
  lastUsedAt: number | null;
}

interface ScreenInfo {
  clientId: string;
  pairedUser: string;
  pairedAt: number;
  enabled: boolean;
  online: boolean;
  name?: string;
}

function formatDate(ts: number | null): string {
  if (!ts) return "Never";
  return new Date(ts * 1000).toLocaleString();
}

function timeAgo(ts: number | null): string {
  if (!ts) return "Never";
  const seconds = Math.floor(Date.now() / 1000 - ts);
  if (seconds < 60) return "Just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function PairDeviceDialog({ onClose, onPaired }: { onClose: () => void; onPaired: () => void }) {
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => { inputRef.current?.focus(); }, []);

  const confirm = async () => {
    const trimCode = code.replace(/\s/g, "");
    if (trimCode.length !== 6 || !/^\d{6}$/.test(trimCode)) {
      setError("Enter the 6-digit code from your device");
      return;
    }
    if (!name.trim()) {
      setError("Enter a name for this device");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      await api.confirmPairing(trimCode, name.trim());
      setSuccess(true);
      setTimeout(() => { onPaired(); onClose(); }, 1500);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Pairing failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={onClose}>
      <div className="bg-cc-bg border border-cc-border rounded-xl p-6 w-full max-w-sm shadow-xl" onClick={(e) => e.stopPropagation()}>
        <h3 className="text-lg font-semibold mb-1">Pair Device</h3>
        <p className="text-sm text-cc-muted mb-5">
          Enter the 6-digit code shown on your device or second screen.
        </p>

        {success ? (
          <div className="text-center py-4">
            <div className="text-2xl mb-2">Paired</div>
            <p className="text-sm text-cc-muted">Your device is now connected.</p>
          </div>
        ) : (
          <>
            <div className="mb-3">
              <input
                ref={inputRef}
                type="text"
                inputMode="numeric"
                maxLength={7}
                value={code}
                onChange={(e) => {
                  const raw = e.target.value.replace(/\D/g, "").slice(0, 6);
                  setCode(raw.length > 3 ? `${raw.slice(0, 3)} ${raw.slice(3)}` : raw);
                }}
                onKeyDown={(e) => e.key === "Enter" && confirm()}
                placeholder="000 000"
                className="w-full text-center text-3xl font-mono tracking-[0.3em] px-4 py-3 rounded-lg bg-cc-bg border border-cc-border text-cc-fg placeholder-cc-muted/30 focus:outline-none focus:ring-1 focus:ring-cc-primary"
              />
            </div>
            <div className="mb-4">
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && confirm()}
                placeholder="Device name (e.g. Pixel Watch, Living Room TV)"
                className="w-full px-3 py-2 text-sm rounded-lg bg-cc-bg border border-cc-border text-cc-fg placeholder-cc-muted focus:outline-none focus:ring-1 focus:ring-cc-primary"
              />
            </div>

            {error && (
              <div className="mb-4 px-3 py-2 text-sm rounded-lg bg-red-500/10 text-red-400 border border-red-500/20">
                {error}
              </div>
            )}

            <div className="flex gap-2 justify-end">
              <button
                onClick={onClose}
                className="px-4 py-2 text-sm font-medium rounded-lg border border-cc-border hover:bg-cc-hover transition-colors cursor-pointer"
              >
                Cancel
              </button>
              <button
                onClick={confirm}
                disabled={loading}
                className="px-4 py-2 text-sm font-medium rounded-lg bg-cc-primary text-white hover:opacity-90 disabled:opacity-50 transition-opacity cursor-pointer"
              >
                {loading ? "Pairing..." : "Pair"}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export function Devices() {
  const [tokens, setTokens] = useState<TokenInfo[]>([]);
  const [screens, setScreens] = useState<ScreenInfo[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [showPairing, setShowPairing] = useState(false);

  const loadAll = useCallback(async () => {
    try {
      const [tokenRes, screenList] = await Promise.all([
        api.listDeviceTokens(),
        api.secondScreenList(),
      ]);
      setTokens(tokenRes.tokens);
      setScreens(screenList);
    } catch {
      // ignore load errors — one or both may fail if auth disabled
    }
  }, []);

  useEffect(() => { loadAll(); }, [loadAll]);

  const revokeToken = async (tokenId: string, tokenName: string) => {
    if (!confirm(`Revoke "${tokenName}"? The device will lose access.`)) return;
    try {
      await api.revokeDeviceToken(tokenId);
      loadAll();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to revoke");
    }
  };

  const unpairScreen = async (clientId: string, screenName: string) => {
    if (!confirm(`Unpair "${screenName}"?`)) return;
    try {
      await api.secondScreenUnpair(clientId);
      loadAll();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to unpair");
    }
  };

  const hasDevices = tokens.length > 0 || screens.length > 0;

  return (
    <div className="p-6 max-w-2xl">
      <h2 className="text-lg font-semibold mb-1">Paired Devices</h2>
      <p className="text-sm text-cc-muted mb-5">
        Native clients and second screens paired with this hub.
      </p>

      <div className="mb-5">
        <button
          onClick={() => setShowPairing(true)}
          className="px-4 py-2 text-sm font-medium rounded-lg bg-cc-primary text-white hover:opacity-90 transition-opacity cursor-pointer"
        >
          Pair Device
        </button>
      </div>

      {error && (
        <div className="mb-4 px-3 py-2 text-sm rounded-lg bg-red-500/10 text-red-400 border border-red-500/20">
          {error}
        </div>
      )}

      {showPairing && (
        <PairDeviceDialog
          onClose={() => setShowPairing(false)}
          onPaired={loadAll}
        />
      )}

      {hasDevices && (
        <div className="border border-cc-border rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-cc-hover text-cc-muted text-xs uppercase tracking-wider">
                <th className="text-left px-4 py-2 font-medium">Name</th>
                <th className="text-left px-4 py-2 font-medium">Type</th>
                <th className="text-left px-4 py-2 font-medium">Status</th>
                <th className="px-4 py-2 font-medium w-20"></th>
              </tr>
            </thead>
            <tbody>
              {/* Native clients */}
              {tokens.map((t) => (
                <tr key={`t-${t.id}`} className="border-t border-cc-border hover:bg-cc-hover/50">
                  <td className="px-4 py-2.5 font-medium text-cc-fg">{t.name}</td>
                  <td className="px-4 py-2.5 text-cc-muted">Native</td>
                  <td className="px-4 py-2.5 text-cc-muted" title={formatDate(t.lastUsedAt)}>
                    {t.lastUsedAt ? `Last used ${timeAgo(t.lastUsedAt)}` : `Created ${timeAgo(t.createdAt)}`}
                  </td>
                  <td className="px-4 py-2.5 text-right">
                    <button
                      onClick={() => revokeToken(t.id, t.name)}
                      className="px-2.5 py-1 text-xs font-medium rounded border border-red-500/30 text-red-400 hover:bg-red-500/10 transition-colors cursor-pointer"
                    >
                      Revoke
                    </button>
                  </td>
                </tr>
              ))}
              {/* Second screens */}
              {screens.map((s) => (
                <tr key={`s-${s.clientId}`} className="border-t border-cc-border hover:bg-cc-hover/50">
                  <td className="px-4 py-2.5 font-medium text-cc-fg">{s.name || s.clientId.slice(0, 8)}</td>
                  <td className="px-4 py-2.5 text-cc-muted">Screen</td>
                  <td className="px-4 py-2.5">
                    <span className={`inline-flex items-center gap-1.5 text-sm ${s.online ? "text-cc-success" : "text-cc-muted"}`}>
                      <span className={`w-1.5 h-1.5 rounded-full ${s.online ? "bg-cc-success" : "bg-cc-muted opacity-40"}`} />
                      {s.online ? "Online" : "Offline"}
                    </span>
                  </td>
                  <td className="px-4 py-2.5 text-right">
                    <button
                      onClick={() => unpairScreen(s.clientId, s.name || s.clientId.slice(0, 8))}
                      className="px-2.5 py-1 text-xs font-medium rounded border border-red-500/30 text-red-400 hover:bg-red-500/10 transition-colors cursor-pointer"
                    >
                      Unpair
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!hasDevices && (
        <p className="text-sm text-cc-muted">No paired devices yet.</p>
      )}
    </div>
  );
}

// Keep old export name for backward compat with SettingsPage import
export { Devices as DeviceTokens };
