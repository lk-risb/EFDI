"""Polygon helpers shared by the bridges that draw regions (dangausakis zones, deepstate)."""


def _perp(p, a, b):
    (x, y), (x1, y1), (x2, y2) = p, a, b
    dx, dy = x2 - x1, y2 - y1
    if dx == dy == 0:
        return ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5
    return abs(dy * x - dx * y + x2 * y1 - y2 * x1) / (dx * dx + dy * dy) ** 0.5


def _douglas_peucker(points: list, eps: float) -> list:
    """Indices of the points kept; `forced` indices are always kept."""
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        lo, hi = stack.pop()
        far, far_d = None, eps
        for i in range(lo + 1, hi):
            d = _perp(points[i], points[lo], points[hi])
            if d > far_d:
                far, far_d = i, d
        if far is not None:
            keep[far] = True
            stack.extend([(lo, far), (far, hi)])
    return [i for i, k in enumerate(keep) if k]


def _orient(a, b, c) -> float:
    return (c[1] - a[1]) * (b[0] - a[0]) - (b[1] - a[1]) * (c[0] - a[0])


def _crossings(points: list) -> list:
    """Index pairs (i, j) of closed-ring segments (points[i]->points[i+1]) that properly cross, found by a
    sweep over x so a ring of a few thousand vertices stays cheap."""
    n = len(points)
    segs = sorted(range(n), key=lambda i: min(points[i][0], points[(i + 1) % n][0]))
    found = []
    for pos, i in enumerate(segs):
        a, b = points[i], points[(i + 1) % n]
        right = max(a[0], b[0])
        for j in segs[pos + 1:]:
            c, d = points[j], points[(j + 1) % n]
            if min(c[0], d[0]) > right:
                break
            if (i + 1) % n == j or (j + 1) % n == i:
                continue
            if _orient(a, b, c) * _orient(a, b, d) < 0 and _orient(c, d, a) * _orient(c, d, b) < 0:
                found.append((i, j))
    return found


def simplify_ring(ring: list, max_vertices: int = 150) -> list:
    """Closed ring of about max_vertices points, shape preserved by Douglas-Peucker. Douglas-Peucker can leave
    the ring crossing itself (a coastline with a thin inlet), which draws as stray bands and broken fills, so
    the original vertices are put back along any segment that crosses another until the ring is simple."""
    points = [list(p[:2]) for p in (ring[:-1] if ring[0] == ring[-1] else ring)]
    eps, kept = 0.0005, list(range(len(points)))
    while len(kept) > max_vertices - 1 and eps < 5:
        kept = _douglas_peucker(points, eps)
        eps *= 1.6
    if len(kept) < len(points):
        for _ in range(8):
            simplified = [points[i] for i in kept]
            crossed = {k for pair in _crossings(simplified) for k in pair}
            if not crossed:
                break
            forced = set(kept)
            for k in crossed:
                lo, hi = kept[k], kept[(k + 1) % len(kept)]
                forced.update(range(lo, hi + 1) if hi > lo else range(lo, len(points)))
            kept = sorted(forced)
    simplified = [points[i] for i in kept]
    return simplified + [simplified[0]]


def ring_area(ring: list) -> float:
    """Shoelace area in square degrees — only used to rank and filter parts."""
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:] + ring[:1]))) / 2
