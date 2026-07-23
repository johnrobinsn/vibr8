// Feature flags — small constants that gate UI surfaces during
// transitions. The implementation on the other side of a false flag
// stays fully intact; only the render + affordance-that-opens-it
// disappear. Flip to true to bring the surface back.

/**
 * TaskPanel — the right-hand session-scoped panel that shows the
 * current session's task list (TodoWrite entries) plus usage limits.
 * Currently disabled — the plan is to surface the content somewhere
 * else in the future.
 *
 * When false:
 *   - App.tsx skips the TaskPanel render + its mobile-overlay backdrop.
 *   - The task-panel toggle key (currently unbound — no keyboard
 *     shortcut in App.tsx) stays a no-op naturally.
 *   - The `taskPanelOpen` store field is left intact; anything that
 *     reads it still gets the persisted value but no UI surface
 *     honors it while the flag is off.
 *
 * Nothing else depends on TaskPanel — TodoWrite state lives in the
 * store's `tasks` map (per-session) and can be surfaced by any
 * future component that subscribes to it.
 */
export const TASK_PANEL_ENABLED = false;
