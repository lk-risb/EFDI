import {CardBrackets} from '@/components/ui/card'

// Thin wrapper over the real ported CardBrackets (components/ui/card.tsx,
// itself a verbatim port of Scout's packages/ui/src/components/card.tsx) —
// every route already renders <HudCorners /> inside a `.hud-frame` container,
// so wiring this one file keeps every existing call site correct without
// touching them.
export function HudCorners() {
  return <CardBrackets />
}
