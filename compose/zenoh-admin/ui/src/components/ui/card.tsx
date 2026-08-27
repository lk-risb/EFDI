import type * as React from 'react'
import {cn} from '@/lib/utils'

/**
 * Corner-bracket frame — the "instrument panel" motif, ported verbatim from
 * Scout's real CardBrackets (packages/ui/src/components/card.tsx): four
 * positioned corner spans, not a CSS ::before gradient trick. Renders inside
 * a `.hud-frame` container, which supplies the positioning context. This is
 * the same component TAK's HudCorners.tsx already delegates to.
 */
export function CardBrackets({className}: {className?: string}) {
  const corner = 'pointer-events-none absolute size-[10px] border-[color:var(--bracket-color)]'
  return (
    <span aria-hidden="true" className={cn('pointer-events-none absolute inset-0', className)} data-slot="card-brackets">
      <span className={cn(corner, '-top-px -left-px border-t border-l')} />
      <span className={cn(corner, '-top-px -right-px border-t border-r')} />
      <span className={cn(corner, '-bottom-px -left-px border-b border-l')} />
      <span className={cn(corner, '-bottom-px -right-px border-b border-r')} />
    </span>
  )
}

export function Card({
  className,
  bracketed = true,
  inset = false,
  children,
  ...props
}: React.ComponentProps<'div'> & {
  /** Corner-bracket frame (default true) — the universal Scout panel treatment. */
  bracketed?: boolean
  /** Sunken surface for wells and nested panels. */
  inset?: boolean
}) {
  return (
    <div
      data-slot="card"
      data-bracketed={bracketed || undefined}
      className={cn(
        'hud-frame hud-glass relative rounded-md border border-zinc-200 p-5 dark:border-white/10',
        inset && 'bg-zinc-100 dark:bg-black/20',
        className
      )}
      {...props}
    >
      {bracketed ? <CardBrackets /> : null}
      {children}
    </div>
  )
}

export function CardHeader({
  className,
  label,
  title,
  action,
  children,
  ...props
}: Omit<React.ComponentProps<'div'>, 'title'> & {
  /** Mono uppercase eyebrow above the title. */
  label?: React.ReactNode
  title?: React.ReactNode
  /** Right-aligned slot (Status pill, button, …). */
  action?: React.ReactNode
}) {
  return (
    <div data-slot="card-header" className={cn('mb-4 flex items-start justify-between gap-3 border-b border-zinc-200 pb-4 dark:border-white/10', className)} {...props}>
      <div className="flex min-w-0 flex-col gap-1">
        {label != null && <span className="hud-label text-[11px] font-semibold text-zinc-600 dark:text-zinc-300">{label}</span>}
        {title != null && <CardTitle>{title}</CardTitle>}
        {children}
      </div>
      {action != null && <div className="shrink-0">{action}</div>}
    </div>
  )
}

export function CardTitle({className, ...props}: React.ComponentProps<'h3'>) {
  return <h3 data-slot="card-title" className={cn('flex items-center gap-2 text-sm font-semibold', className)} {...props} />
}

export function CardDescription({className, ...props}: React.ComponentProps<'p'>) {
  return <p data-slot="card-description" className={cn('mt-1 text-xs text-zinc-500', className)} {...props} />
}

export function CardContent({className, ...props}: React.ComponentProps<'div'>) {
  return <div data-slot="card-content" className={cn(className)} {...props} />
}

export function CardFooter({className, ...props}: React.ComponentProps<'div'>) {
  return <div data-slot="card-footer" className={cn('mt-4 flex items-center gap-2', className)} {...props} />
}
