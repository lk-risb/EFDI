import { cn } from '@/lib/utils'

// 'neutral' extends TAK's original 3-tone pill for EFDI's tone-less
// informational badges (e.g. "immediate-parent authority") — same bracket
// motif and border-only styling, no color-coded semantics attached.
export function StatusPill({ text, tone }: { text: string; tone: 'ok' | 'warn' | 'bad' | 'neutral' }) {
  const cls = tone === 'ok'
    ? 'border-green-500/40 text-green-600 dark:text-green-400'
    : tone === 'warn'
    ? 'border-yellow-500/40 text-yellow-600 dark:text-yellow-400'
    : tone === 'bad'
    ? 'border-red-500/40 text-red-600 dark:text-red-400'
    : 'border-zinc-300 dark:border-white/10 text-zinc-600 dark:text-zinc-400'
  return (
    <span className={cn('inline-flex items-center text-[10px] tracking-[0.06em] uppercase border px-1.5 py-0.5', cls)}>
      [ {text} ]
    </span>
  )
}
