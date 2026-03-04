import { useState } from "react";

interface Props {
  onLogin: () => void;
}

export function LoginPage({ onLogin }: Props) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError("");
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({ error: "Login failed" }));
        throw new Error(data.error || "Login failed");
      }
      onLogin();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Login failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="h-[100dvh] flex items-center justify-center bg-cc-bg">
      <div className="w-full max-w-sm mx-4">
        <div className="text-center mb-8">
          <img src="/logo.svg" alt="vibr8" className="w-10 h-10 mx-auto mb-3" />
          <h1 className="text-xl font-semibold text-cc-fg">vibr8</h1>
        </div>

        <form onSubmit={handleSubmit} className="bg-cc-card border border-cc-border rounded-xl p-6 shadow-sm">
          <div className="space-y-4">
            <div>
              <label htmlFor="username" className="block text-xs font-medium text-cc-muted mb-1.5">
                Username
              </label>
              <input
                id="username"
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoFocus
                autoComplete="username"
                className="w-full px-3 py-2 text-sm bg-cc-input-bg border border-cc-border rounded-lg text-cc-fg placeholder:text-cc-muted focus:outline-none focus:border-cc-primary/50"
              />
            </div>

            <div>
              <label htmlFor="password" className="block text-xs font-medium text-cc-muted mb-1.5">
                Password
              </label>
              <input
                id="password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                className="w-full px-3 py-2 text-sm bg-cc-input-bg border border-cc-border rounded-lg text-cc-fg placeholder:text-cc-muted focus:outline-none focus:border-cc-primary/50"
              />
            </div>

            {error && (
              <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-cc-error/5 border border-cc-error/20">
                <p className="text-xs text-cc-error">{error}</p>
              </div>
            )}

            <button
              type="submit"
              disabled={loading || !username || !password}
              className={`w-full py-2 rounded-lg text-sm font-medium transition-colors ${
                loading || !username || !password
                  ? "bg-cc-hover text-cc-muted cursor-not-allowed"
                  : "bg-cc-primary hover:bg-cc-primary-hover text-white cursor-pointer"
              }`}
            >
              {loading ? "Signing in..." : "Sign in"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
