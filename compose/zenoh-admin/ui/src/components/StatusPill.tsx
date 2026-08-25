import { AlertTriangle, CheckCircle2, HelpCircle, XCircle } from 'lucide-react'
import { cn } from '@/lib/utils'

// Ported from Scout's real Status component (packages/ui/src/components/status.tsx):
// filled tone surface + border + icon, not a bracket-wrapped border-only pill —
// 'neutral' extends TAK's original 3-tone pill for EFDI's tone-less
// informational badges (e.g. "immediate-parent authority").
const TONE = {
  ok: { cls: 'border-nominal-border bg-nominal-surface text-nominal', Icon: CheckCircle2 },
  warn: { cls: 'border-warning-border bg-warning-surface text-warning', Icon: AlertTriangle },
  bad: { cls: 'border-critical-border bg-critical-surface text-critical', Icon: XCircle },
  neutral: { cls: 'border-zinc-300 dark:border-white/10 text-zinc-600 dark:text-zinc-400', Icon: HelpCircle },
} as const

export function StatusPill({ text, tone }: { text: string; tone: 'ok' | 'warn' | 'bad' | 'neutral' }) {
  const { cls, Icon } = TONE[tone]
  return (
    <span className={cn('inline-flex h-6 items-center gap-1.5 border px-2.5 font-mono text-[11px] tracking-wide whitespace-nowrap uppercase', cls)}>
      <Icon size={13} />
      {text}
    </span>
  )
}
