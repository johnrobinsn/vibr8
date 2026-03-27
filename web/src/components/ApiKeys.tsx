import { useState, useEffect, useCallback } from "react";
import { api } from "../api.js";

interface KeyInfo {
  id: string;
  name: string;
  keyPrefix: string;
  createdAt: number;
  lastUsedAt: number;
}

function formatDate(ts: number): string {
  if (!ts) return "Never";
  return new Date(ts * 1000).toLocaleString();
}

function timeAgo(ts: number): string {
  if (!ts) return "Never";
  const seconds = Math.floor(Date.now() / 1000 - ts);
  if (seconds < 60) return "Just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function ApiKeys() {
  const [keys, setKeys] = useState<KeyInfo[]>([]);
  const [newKey, setNewKey] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const loadKeys = useCallback(async () => {
    try {
      const list = await api.listNodeKeys();
      setKeys(list);
    } catch {
      // ignore load errors
    }
  }, []);

  useEffect(() => { loadKeys(); }, [loadKeys]);

  const generate = async () => {
    if (!name.trim()) {
      setError("Enter a name for this key");
      return;
    }
    setLoading(true);
    setError(null);
    setNewKey(null);
    try {
      const res = await api.generateNodeKey(name.trim());
      setNewKey(res.apiKey);
      setName("");
      loadKeys();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to generate key");
    } finally {
      setLoading(false);
    }
  };

  const revoke = async (keyId: string, keyName: string) => {
    if (!confirm(`Revoke key "${keyName}"? Nodes using this key will no longer be able to register.`)) return;
    try {
      await api.revokeNodeKey(keyId);
      loadKeys();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to revoke key");
    }
  };

  const copyKey = () => {
    if (!newKey) return;
    navigator.clipboard.writeText(newKey);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="p-6 max-w-2xl">
      <h2 className="text-lg font-semibold mb-1">Node API Keys</h2>
      <p className="text-sm text-cc-muted mb-5">
        API keys for remote node registration. Each key authenticates a node connecting to this hub.
      </p>

      {/* Generate form */}
      <div className="flex items-center gap-2 mb-4">
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && generate()}
          placeholder="Key name (e.g. cloud-dev-1)"
          className="flex-1 px-3 py-2 text-sm rounded-lg bg-cc-bg border border-cc-border text-cc-fg placeholder-cc-muted focus:outline-none focus:ring-1 focus:ring-cc-primary"
        />
        <button
          onClick={generate}
          disabled={loading}
          className="shrink-0 px-4 py-2 text-sm font-medium rounded-lg bg-cc-primary text-white hover:opacity-90 disabled:opacity-50 transition-opacity cursor-pointer"
        >
          {loading ? "Generating..." : "Generate"}
        </button>
      </div>

      {error && (
        <div className="mb-4 px-3 py-2 text-sm rounded-lg bg-red-500/10 text-red-400 border border-red-500/20">
          {error}
        </div>
      )}

      {newKey && (
        <div className="mb-5 p-4 rounded-lg bg-cc-hover border border-cc-border">
          <div className="text-xs text-cc-muted mb-2 font-medium">
            Copy this key now — it won't be shown again
          </div>
          <div className="flex items-center gap-2">
            <code className="flex-1 text-sm font-mono bg-cc-bg px-3 py-2 rounded border border-cc-border text-cc-fg select-all break-all">
              {newKey}
            </code>
            <button
              onClick={copyKey}
              className="shrink-0 px-3 py-2 text-xs font-medium rounded-lg border border-cc-border hover:bg-cc-hover transition-colors cursor-pointer"
            >
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
        </div>
      )}

      {/* Key list */}
      {keys.length > 0 && (
        <div className="border border-cc-border rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-cc-hover text-cc-muted text-xs uppercase tracking-wider">
                <th className="text-left px-4 py-2 font-medium">Name</th>
                <th className="text-left px-4 py-2 font-medium">Key</th>
                <th className="text-left px-4 py-2 font-medium">Created</th>
                <th className="text-left px-4 py-2 font-medium">Last Used</th>
                <th className="px-4 py-2 font-medium w-20"></th>
              </tr>
            </thead>
            <tbody>
              {keys.map((k) => (
                <tr key={k.id} className="border-t border-cc-border hover:bg-cc-hover/50">
                  <td className="px-4 py-2.5 font-medium text-cc-fg">{k.name}</td>
                  <td className="px-4 py-2.5 font-mono text-xs text-cc-muted">{k.keyPrefix}</td>
                  <td className="px-4 py-2.5 text-cc-muted" title={formatDate(k.createdAt)}>
                    {timeAgo(k.createdAt)}
                  </td>
                  <td className="px-4 py-2.5 text-cc-muted" title={formatDate(k.lastUsedAt)}>
                    {timeAgo(k.lastUsedAt)}
                  </td>
                  <td className="px-4 py-2.5 text-right">
                    <button
                      onClick={() => revoke(k.id, k.name)}
                      className="px-2.5 py-1 text-xs font-medium rounded border border-red-500/30 text-red-400 hover:bg-red-500/10 transition-colors cursor-pointer"
                    >
                      Revoke
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {keys.length === 0 && (
        <p className="text-sm text-cc-muted">No API keys generated yet.</p>
      )}
    </div>
  );
}
