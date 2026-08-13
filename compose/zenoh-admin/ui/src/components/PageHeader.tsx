interface PageHeaderProps {
  title: string
  /** Scout-style uppercase eyebrow above the title, e.g. "SYSTEM / OVERVIEW".
     Falls back to a generic section marker when omitted. */
  eyebrow?: string
  count?: number
  countLabel?: string
  actions?: React.ReactNode
}

export function PageHeader({ title, eyebrow = 'EFDI CONTROL', count, countLabel, actions }: PageHeaderProps) {
  return (
    <div className="flex items-end justify-between mb-8 border-b border-zinc-200 dark:border-white/10 pb-5">
      <div>
        <p className="font-mono text-[11px] uppercase tracking-[0.2em] text-zinc-500 mb-2">{eyebrow}</p>
        <div className="flex items-baseline gap-3">
          <h1 className="font-display text-4xl font-bold tracking-tight">{title}</h1>
          {count !== undefined && (
            <span className="hud-label text-xs text-zinc-500">{count} {countLabel}</span>
          )}
        </div>
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  )
}
