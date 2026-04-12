import { useState, useEffect, useCallback, useRef } from "react";
import { api } from "../api.js";
import type { AndroidDeviceInfo, DiscoveredDevice, MdnsDevice } from "../types.js";

function timeAgo(ts: number): string {
  if (!ts) return "Never";
  const seconds = Math.floor(Date.now() / 1000 - ts);
  if (seconds < 60) return "Just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

type ConnectionMode = "usb" | "ip" | "mdns";

interface AddDeviceDialogProps {
  onClose: () => void;
  onAdded: () => void;
  prefilledSerial?: string;
  prefilledMode?: ConnectionMode;
  prefilledIp?: string;
  prefilledPort?: number;
  prefilledName?: string;
}

function AddDeviceDialog({
  onClose,
  onAdded,
  prefilledSerial,
  prefilledMode,
  prefilledIp,
  prefilledPort,
  prefilledName,
}: AddDeviceDialogProps) {
  const [name, setName] = useState(prefilledName || "");
  const [mode, setMode] = useState<ConnectionMode>(prefilledMode || "usb");
  const [serial, setSerial] = useState(prefilledSerial || "");
  const [ip, setIp] = useState(prefilledIp || "");
  const [port, setPort] = useState(prefilledPort ? String(prefilledPort) : "5555");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);

  useEffect(() => { nameRef.current?.focus(); }, []);

  const submit = async () => {
    if (!name.trim()) { setError("Enter a name"); return; }
    if (mode === "usb" && !serial.trim()) { setError("Enter the device serial"); return; }
    if (mode === "ip" && !ip.trim()) { setError("Enter the IP address"); return; }

    setLoading(true);
    setError(null);
    try {
      const deviceId = mode === "usb" ? serial.trim() : `${ip.trim()}:${port || "5555"}`;
      await api.registerAndroidDevice({
        name: name.trim(),
        connectionMode: mode,
        deviceId,
        ...(mode !== "usb" ? { ip: ip.trim(), port: parseInt(port) || 5555 } : {}),
      });
      onAdded();
      onClose();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Registration failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={onClose}>
      <div className="bg-cc-bg border border-cc-border rounded-xl p-6 w-full max-w-md shadow-xl" onClick={(e) => e.stopPropagation()}>
        <h3 className="text-lg font-semibold mb-4">Add Android Device</h3>

        <div className="space-y-3">
          <div>
            <label className="block text-xs font-medium text-cc-muted mb-1">Name</label>
            <input
              ref={nameRef}
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && submit()}
              placeholder="e.g. Pixel 9 Pro"
              className="w-full px-3 py-2 text-sm rounded-lg bg-cc-bg border border-cc-border text-cc-fg placeholder-cc-muted focus:outline-none focus:ring-1 focus:ring-cc-primary"
            />
          </div>

          <div>
            <label className="block text-xs font-medium text-cc-muted mb-1">Connection</label>
            <div className="flex gap-1 p-0.5 bg-cc-hover rounded-lg">
              {(["usb", "ip", "mdns"] as const).map((m) => (
                <button
                  key={m}
                  onClick={() => setMode(m)}
                  className={`flex-1 px-3 py-1.5 text-xs font-medium rounded-md transition-colors cursor-pointer ${
                    mode === m ? "bg-cc-bg text-cc-fg shadow-sm" : "text-cc-muted hover:text-cc-fg"
                  }`}
                >
                  {m === "usb" ? "USB" : m === "ip" ? "IP" : "mDNS"}
                </button>
              ))}
            </div>
          </div>

          {mode === "usb" && (
            <div>
              <label className="block text-xs font-medium text-cc-muted mb-1">Serial</label>
              <input
                type="text"
                value={serial}
                onChange={(e) => setSerial(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && submit()}
                placeholder="Device serial (from adb devices)"
                className="w-full px-3 py-2 text-sm rounded-lg bg-cc-bg border border-cc-border text-cc-fg placeholder-cc-muted focus:outline-none focus:ring-1 focus:ring-cc-primary"
              />
            </div>
          )}

          {(mode === "ip" || mode === "mdns") && (
            <div className="flex gap-2">
              <div className="flex-1">
                <label className="block text-xs font-medium text-cc-muted mb-1">IP Address</label>
                <input
                  type="text"
                  value={ip}
                  onChange={(e) => setIp(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && submit()}
                  placeholder="192.168.1.100"
                  className="w-full px-3 py-2 text-sm rounded-lg bg-cc-bg border border-cc-border text-cc-fg placeholder-cc-muted focus:outline-none focus:ring-1 focus:ring-cc-primary"
                />
              </div>
              <div className="w-24">
                <label className="block text-xs font-medium text-cc-muted mb-1">Port</label>
                <input
                  type="text"
                  value={port}
                  onChange={(e) => setPort(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && submit()}
                  placeholder="5555"
                  className="w-full px-3 py-2 text-sm rounded-lg bg-cc-bg border border-cc-border text-cc-fg placeholder-cc-muted focus:outline-none focus:ring-1 focus:ring-cc-primary"
                />
              </div>
            </div>
          )}
        </div>

        {error && (
          <div className="mt-3 px-3 py-2 text-sm rounded-lg bg-red-500/10 text-red-400 border border-red-500/20">
            {error}
          </div>
        )}

        <div className="flex gap-2 justify-end mt-5">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm font-medium rounded-lg border border-cc-border hover:bg-cc-hover transition-colors cursor-pointer"
          >
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={loading}
            className="px-4 py-2 text-sm font-medium rounded-lg bg-cc-primary text-white hover:opacity-90 disabled:opacity-50 transition-opacity cursor-pointer"
          >
            {loading ? "Adding..." : "Add Device"}
          </button>
        </div>
      </div>
    </div>
  );
}

export function AndroidDevices() {
  const [devices, setDevices] = useState<AndroidDeviceInfo[]>([]);
  const [discovered, setDiscovered] = useState<{ usb: DiscoveredDevice[]; mdns: MdnsDevice[] } | null>(null);
  const [discovering, setDiscovering] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [addPrefill, setAddPrefill] = useState<Partial<AddDeviceDialogProps>>({});

  const loadDevices = useCallback(async () => {
    try {
      const list = await api.listAndroidDevices();
      setDevices(list);
    } catch {
      // ignore
    }
  }, []);

  useEffect(() => { loadDevices(); }, [loadDevices]);

  // Poll device status every 5s
  useEffect(() => {
    const interval = setInterval(loadDevices, 5000);
    return () => clearInterval(interval);
  }, [loadDevices]);

  const discover = async () => {
    setDiscovering(true);
    try {
      const result = await api.discoverAndroidDevices();
      setDiscovered(result);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Discovery failed");
    } finally {
      setDiscovering(false);
    }
  };

  const removeDevice = async (nodeId: string, name: string) => {
    if (!confirm(`Remove "${name}"? This will disconnect the device.`)) return;
    try {
      await api.deleteAndroidDevice(nodeId);
      loadDevices();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to remove");
    }
  };

  const reconnect = async (nodeId: string) => {
    try {
      await api.connectAndroidDevice(nodeId);
      loadDevices();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Reconnect failed");
    }
  };

  const addFromDiscovered = (dev: DiscoveredDevice) => {
    setAddPrefill({
      prefilledSerial: dev.serial,
      prefilledMode: "usb",
      prefilledName: dev.model || "",
    });
    setShowAdd(true);
  };

  const addFromMdns = (dev: MdnsDevice) => {
    setAddPrefill({
      prefilledMode: "mdns",
      prefilledIp: dev.ip,
      prefilledPort: dev.port,
      prefilledName: dev.name || "",
    });
    setShowAdd(true);
  };

  return (
    <div className="p-6 max-w-2xl">
      <h2 className="text-lg font-semibold mb-1">Android Devices</h2>
      <p className="text-sm text-cc-muted mb-5">
        Manage ADB-connected Android devices for screen viewing and computer-use.
      </p>

      <div className="flex gap-2 mb-5">
        <button
          onClick={() => { setAddPrefill({}); setShowAdd(true); }}
          className="px-4 py-2 text-sm font-medium rounded-lg bg-cc-primary text-white hover:opacity-90 transition-opacity cursor-pointer"
        >
          Add Device
        </button>
        <button
          onClick={discover}
          disabled={discovering}
          className="px-4 py-2 text-sm font-medium rounded-lg border border-cc-border hover:bg-cc-hover transition-colors disabled:opacity-50 cursor-pointer"
        >
          {discovering ? "Scanning..." : "Discover"}
        </button>
      </div>

      {error && (
        <div className="mb-4 px-3 py-2 text-sm rounded-lg bg-red-500/10 text-red-400 border border-red-500/20">
          {error}
          <button onClick={() => setError(null)} className="ml-2 text-red-400/60 hover:text-red-400 cursor-pointer">&times;</button>
        </div>
      )}

      {showAdd && (
        <AddDeviceDialog
          onClose={() => setShowAdd(false)}
          onAdded={loadDevices}
          {...addPrefill}
        />
      )}

      {/* Discovered devices */}
      {discovered && (discovered.usb.length > 0 || discovered.mdns.length > 0) && (
        <div className="mb-5">
          <h3 className="text-sm font-medium text-cc-muted mb-2">Discovered Devices</h3>
          <div className="border border-cc-border rounded-lg divide-y divide-cc-border">
            {discovered.usb.map((dev) => (
              <div key={dev.serial} className="flex items-center justify-between px-4 py-2.5">
                <div>
                  <span className="text-sm font-medium text-cc-fg">{dev.model || dev.serial}</span>
                  <span className="ml-2 text-xs text-cc-muted">USB</span>
                  {dev.model && <span className="ml-2 text-xs text-cc-muted font-mono">{dev.serial}</span>}
                </div>
                <button
                  onClick={() => addFromDiscovered(dev)}
                  className="px-3 py-1 text-xs font-medium rounded-lg bg-cc-primary/10 text-cc-primary hover:bg-cc-primary/20 transition-colors cursor-pointer"
                >
                  Add
                </button>
              </div>
            ))}
            {discovered.mdns.map((dev) => (
              <div key={`${dev.ip}:${dev.port}`} className="flex items-center justify-between px-4 py-2.5">
                <div>
                  <span className="text-sm font-medium text-cc-fg">{dev.name || dev.ip}</span>
                  <span className="ml-2 text-xs text-cc-muted">mDNS</span>
                  <span className="ml-2 text-xs text-cc-muted font-mono">{dev.ip}:{dev.port}</span>
                </div>
                <button
                  onClick={() => addFromMdns(dev)}
                  className="px-3 py-1 text-xs font-medium rounded-lg bg-cc-primary/10 text-cc-primary hover:bg-cc-primary/20 transition-colors cursor-pointer"
                >
                  Add
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {discovered && discovered.usb.length === 0 && discovered.mdns.length === 0 && (
        <div className="mb-5 px-3 py-2 text-sm text-cc-muted rounded-lg bg-cc-hover">
          No new devices found. Make sure USB debugging is enabled on your device.
        </div>
      )}

      {/* Registered devices */}
      {devices.length > 0 && (
        <div className="border border-cc-border rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-cc-hover text-cc-muted text-xs uppercase tracking-wider">
                <th className="text-left px-4 py-2 font-medium">Device</th>
                <th className="text-left px-4 py-2 font-medium">Connection</th>
                <th className="text-left px-4 py-2 font-medium">Status</th>
                <th className="px-4 py-2 font-medium w-28"></th>
              </tr>
            </thead>
            <tbody>
              {devices.map((d) => (
                <tr key={d.id} className="border-t border-cc-border hover:bg-cc-hover/50">
                  <td className="px-4 py-2.5">
                    <div className="font-medium text-cc-fg">{d.name}</div>
                    {d.capabilities.model && (
                      <div className="text-xs text-cc-muted">
                        {d.capabilities.manufacturer} {d.capabilities.model}
                        {d.capabilities.androidVersion && ` · Android ${d.capabilities.androidVersion}`}
                      </div>
                    )}
                  </td>
                  <td className="px-4 py-2.5">
                    <span className="text-xs font-medium uppercase text-cc-muted">{d.connectionMode}</span>
                    <div className="text-xs text-cc-muted font-mono">{d.deviceId}</div>
                  </td>
                  <td className="px-4 py-2.5">
                    <span className={`inline-flex items-center gap-1.5 text-sm ${d.status === "online" ? "text-cc-success" : "text-cc-muted"}`}>
                      <span className={`w-1.5 h-1.5 rounded-full ${d.status === "online" ? "bg-cc-success" : "bg-cc-muted opacity-40"}`} />
                      {d.status === "online" ? "Online" : d.status === "unauthorized" ? "Unauthorized" : "Offline"}
                    </span>
                    {d.lastSeen > 0 && d.status !== "online" && (
                      <div className="text-xs text-cc-muted">Last seen {timeAgo(d.lastSeen)}</div>
                    )}
                  </td>
                  <td className="px-4 py-2.5 text-right">
                    <div className="flex gap-1 justify-end">
                      {d.status !== "online" && d.connectionMode !== "usb" && (
                        <button
                          onClick={() => reconnect(d.id)}
                          className="px-2.5 py-1 text-xs font-medium rounded border border-cc-border text-cc-fg hover:bg-cc-hover transition-colors cursor-pointer"
                          title="Reconnect"
                        >
                          Retry
                        </button>
                      )}
                      <button
                        onClick={() => removeDevice(d.id, d.name)}
                        className="px-2.5 py-1 text-xs font-medium rounded border border-red-500/30 text-red-400 hover:bg-red-500/10 transition-colors cursor-pointer"
                      >
                        Remove
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {devices.length === 0 && (
        <p className="text-sm text-cc-muted">
          No Android devices registered. Click "Add Device" or "Discover" to get started.
        </p>
      )}
    </div>
  );
}
