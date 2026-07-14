import { useMemo, useState } from 'react'
import { buildTree, type TopologyNode, type TreeNode } from '@/lib/topology'

const NODE_W = 160
const NODE_H = 42
const H_GAP = 28
const V_GAP = 58

interface Positioned {
  node: TreeNode
  x: number
  y: number
}

function layout(roots: TreeNode[], collapsed: Set<string>) {
  const positioned: Positioned[] = []
  const edges: { x1: number; y1: number; x2: number; y2: number }[] = []
  let nextX = 0

  const place = (node: TreeNode): Positioned => {
    const y = node.depth * (NODE_H + V_GAP)
    const children = collapsed.has(node.namespace) ? [] : node.childNodes
    let x: number
    if (children.length === 0) {
      x = nextX
      nextX += NODE_W + H_GAP
    } else {
      const placedChildren = children.map(place)
      x = (placedChildren[0].x + placedChildren[placedChildren.length - 1].x) / 2
      for (const child of placedChildren) {
        edges.push({
          x1: x + NODE_W / 2,
          y1: y + NODE_H,
          x2: child.x + NODE_W / 2,
          y2: child.y,
        })
      }
    }
    const placed = { node, x, y }
    positioned.push(placed)
    return placed
  }

  for (const root of roots) place(root)
  const width = Math.max(nextX - H_GAP, NODE_W)
  const maxY = positioned.reduce((value, item) => Math.max(value, item.y), 0)
  return { positioned, edges, width, height: maxY + NODE_H }
}

function statusColor(node: TreeNode): string {
  if (!node.online) return '#71717a'
  if (!node.healthy) return '#f59e0b'
  if (node.config_status === 'rejected' || node.config_status === 'rolled_back') return '#ef4444'
  if (node.config_status === 'ok') return '#22c55e'
  return '#2dd4bf'
}

export function TopologyMap({
  nodes,
  onSelect,
  selected,
  compact = false,
}: {
  nodes: TopologyNode[]
  onSelect?: (namespace: string) => void
  selected?: string | null
  compact?: boolean
}) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const { roots } = useMemo(() => buildTree(nodes), [nodes])
  const drawing = useMemo(() => layout(roots, collapsed), [roots, collapsed])

  if (nodes.length === 0) {
    return <p className="text-sm text-zinc-500">No routers discovered yet.</p>
  }

  const toggle = (namespace: string) => {
    setCollapsed(previous => {
      const next = new Set(previous)
      if (next.has(namespace)) next.delete(namespace)
      else next.add(namespace)
      return next
    })
  }
  const pad = 12

  return (
    <div
      className="overflow-auto rounded-md border border-zinc-200 dark:border-white/10 bg-white dark:bg-[#111113]"
      style={{ maxHeight: compact ? 260 : 600 }}
    >
      <svg
        width={drawing.width + pad * 2}
        height={drawing.height + pad * 2}
        className="min-w-full"
        role="img"
        aria-label="Router federation topology"
      >
        <g transform={`translate(${pad},${pad})`}>
          {drawing.edges.map((edge, index) => (
            <line
              key={index}
              x1={edge.x1}
              y1={edge.y1}
              x2={edge.x2}
              y2={edge.y2}
              stroke="currentColor"
              className="text-zinc-300 dark:text-white/15"
              strokeWidth={1.5}
            />
          ))}
          {drawing.positioned.map(({ node, x, y }) => {
            const hasChildren = node.childNodes.length > 0
            const isCollapsed = collapsed.has(node.namespace)
            const isSelected = selected === node.namespace
            return (
              <g
                key={node.namespace}
                transform={`translate(${x},${y})`}
                className={onSelect ? 'cursor-pointer' : undefined}
                onClick={() => onSelect?.(node.namespace)}
              >
                <title>{node.namespace} — {node.online ? (node.healthy ? 'online' : 'degraded') : 'offline'}</title>
                <rect
                  width={NODE_W}
                  height={NODE_H}
                  rx={6}
                  className={isSelected ? 'fill-teal-50 dark:fill-teal-950/50' : 'fill-zinc-100 dark:fill-[#1a1a1d]'}
                  stroke={statusColor(node)}
                  strokeWidth={isSelected ? 3 : 2}
                />
                <circle cx={14} cy={NODE_H / 2} r={5} fill={statusColor(node)} />
                <text
                  x={26}
                  y={NODE_H / 2 - 3}
                  className="fill-zinc-800 dark:fill-zinc-200"
                  fontSize={11}
                  fontFamily="JetBrains Mono, monospace"
                  dominantBaseline="middle"
                >
                  {node.namespace.length > 17 ? `${node.namespace.slice(0, 16)}…` : node.namespace}
                </text>
                <text x={26} y={NODE_H / 2 + 11} className="fill-zinc-500" fontSize={9} dominantBaseline="middle">
                  {node.role === 'hq' ? 'HQ' : node.online ? (node.healthy ? 'online' : 'degraded') : 'offline'}
                </text>
                {hasChildren && (
                  <g onClick={event => { event.stopPropagation(); toggle(node.namespace) }}>
                    <rect
                      x={NODE_W - 21}
                      y={NODE_H / 2 - 8}
                      width={16}
                      height={16}
                      rx={3}
                      className="fill-zinc-200 dark:fill-white/10"
                    />
                    <text
                      x={NODE_W - 13}
                      y={NODE_H / 2}
                      textAnchor="middle"
                      dominantBaseline="middle"
                      fontSize={12}
                      className="fill-zinc-700 dark:fill-zinc-300"
                    >
                      {isCollapsed ? '+' : '−'}
                    </text>
                  </g>
                )}
              </g>
            )
          })}
        </g>
      </svg>
    </div>
  )
}
