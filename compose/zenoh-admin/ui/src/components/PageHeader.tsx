interface PageHeaderProps {
  title: string
  count?: number
  countLabel?: string
  actions?: React.ReactNode
}

export function PageHeader({ title, count, countLabel, actions }: PageHeaderProps) {
  return (
    <div className="flex items-center justify-between mb-6">
      <div className="flex items-center gap-3">
        <h1 className="text-2xl font-bold tracking-tight">{title}</h1>
        {count !== undefined && (
          <span className="hud-label text-xs text-zinc-500">{count} {countLabel}</span>
        )}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  )
}
