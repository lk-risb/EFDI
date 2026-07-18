import { apiJson } from '@/lib/api'

export interface TopologyNode {
  namespace: string
  router_zid: string | null
  parent_namespace: string | null
  role: 'hq' | 'pod' | 'peer'
  children: string[]
  healthy: boolean
  online: boolean
  last_seen_seconds: number
  reported?: boolean
  neighbors: TopologyNeighbor[]
  config_status: string | null
  config_status_version: number | null
  config_status_at: string | null
}

export interface TopologyNeighbor {
  router_zid: string
  whatami: string
  link_count: number | null
  protocols: string[]
}

export interface TopologyTransportEdge {
  source: string
  target: string
  protocols: string[]
  observers: string[]
}

export interface TopologyResponse {
  nodes: TopologyNode[]
  transport_edges: TopologyTransportEdge[]
  generated_at: number
  publish_interval_s: number
  stale_after_s: number
}

export interface TreeNode extends TopologyNode {
  childNodes: TreeNode[]
  depth: number
}

export function fetchTopology(): Promise<TopologyResponse> {
  return apiJson<TopologyResponse>('/api/topology')
}

/** Reconstruct a stable forest without dropping orphans or cyclic bad data. */
export function buildTree(nodes: TopologyNode[]): {
  roots: TreeNode[]
  byNamespace: Map<string, TreeNode>
} {
  const byNamespace = new Map<string, TreeNode>()
  for (const node of nodes) {
    byNamespace.set(node.namespace, { ...node, childNodes: [], depth: 0 })
  }

  function parentWouldCycle(node: TreeNode, parent: TreeNode): boolean {
    const visited = new Set<string>([node.namespace])
    let current: TreeNode | undefined = parent
    while (current) {
      if (visited.has(current.namespace)) return true
      visited.add(current.namespace)
      current = current.parent_namespace
        ? byNamespace.get(current.parent_namespace)
        : undefined
    }
    return false
  }

  const roots: TreeNode[] = []
  for (const node of byNamespace.values()) {
    const parent = node.parent_namespace
      ? byNamespace.get(node.parent_namespace)
      : undefined
    if (parent && !parentWouldCycle(node, parent)) {
      parent.childNodes.push(node)
    } else {
      roots.push(node)
    }
  }

  const visited = new Set<string>()
  const visit = (node: TreeNode, depth: number) => {
    if (visited.has(node.namespace)) return
    visited.add(node.namespace)
    node.depth = depth
    node.childNodes.sort((a, b) => a.namespace.localeCompare(b.namespace))
    for (const child of node.childNodes) visit(child, depth + 1)
  }
  roots.sort((a, b) => a.namespace.localeCompare(b.namespace))
  for (const root of roots) visit(root, 0)

  // Defensive fallback for a malformed graph that escaped the cycle check.
  for (const node of byNamespace.values()) {
    if (!visited.has(node.namespace)) {
      roots.push(node)
      visit(node, 0)
    }
  }
  return { roots, byNamespace }
}
