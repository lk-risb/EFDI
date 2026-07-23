/**
 * Corner-bracket framing, retired in the 2026 restyle.
 *
 * Panels now carry their identity through soft elevation, mica surfaces and the
 * accent ring instead of reticle brackets. This renders nothing on purpose: it
 * is referenced by 13 routes, so keeping the component as a no-op drops the
 * brackets everywhere from one place rather than editing every page. The
 * surrounding `hud-frame` class stays meaningful — it establishes the
 * positioning context those panels rely on.
 */
export function HudCorners() {
  return null
}
