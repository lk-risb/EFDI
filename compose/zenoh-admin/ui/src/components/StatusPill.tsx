import { cn } from '@/lib/utils'

// 'neutral' extends TAK's original 3-tone pill for EFDI's tone-less
// informational badges (e.g. "immediate-parent authority") — same bracket
// motif and border-only styling, no color-coded semantics attached.
export function StatusPill({ text, tone }: { text: string; tone: 'ok' | 'warn' | 'bad' | 'neutral' }) {
  const cls = tone === 'ok'
    ? 'border-nominal-border text-nominal'
    : tone === 'warn'
    ? 'border-warning-border text-warning'
    : tone === 'bad'
    ? 'border-critical-border text-critical'
    : 'border-zinc-300 dark:border-white/10 text-zinc-600 dark:text-zinc-400'
  return (
    <span className={cn('inline-flex items-center text-[10px] tracking-[0.06em] uppercase border px-1.5 py-0.5', cls)}>
      [ {text} ]
    </span>
  )
}
