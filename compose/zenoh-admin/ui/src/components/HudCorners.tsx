/**
 * Corner-bracket frame — the "instrument panel" motif, ported directly from
 * Scout's real CardBrackets component (packages/ui/src/components/card.tsx):
 * four positioned corner spans, not a CSS ::before gradient trick. Renders
 * inside a `.hud-frame` container, which supplies the positioning context.
 */
export function HudCorners() {
  const corner = "pointer-events-none absolute size-[10px] border-[color:var(--bracket-color)]"
  return (
    <span aria-hidden="true" className="pointer-events-none absolute inset-0">
      <span className={`${corner} -top-px -left-px border-t border-l`} />
      <span className={`${corner} -top-px -right-px border-t border-r`} />
      <span className={`${corner} -bottom-px -left-px border-b border-l`} />
      <span className={`${corner} -bottom-px -right-px border-b border-r`} />
    </span>
  )
}
