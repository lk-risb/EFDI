"""Polygon helpers shared by the bridges that draw regions (dangausakis zones, deepstate)."""


def _perp(p, a, b):
    (x, y), (x1, y1), (x2, y2) = p, a, b
    dx, dy = x2 - x1, y2 - y1
    if dx == dy == 0:
        return ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5
    return abs(dy * x - dx * y + x2 * y1 - y2 * x1) / (dx * dx + dy * dy) ** 0.5


def _douglas_peucker(points: list, eps: float) -> list:
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
    return [p for p, k in zip(points, keep) if k]


def simplify_ring(ring: list, max_vertices: int = 150) -> list:
    """Closed ring of at most max_vertices points, shape preserved by Douglas-Peucker."""
    points = [list(p[:2]) for p in (ring[:-1] if ring[0] == ring[-1] else ring)]
    eps, simplified = 0.0005, points
    while len(simplified) > max_vertices - 1 and eps < 5:
        simplified = _douglas_peucker(points, eps)
        eps *= 1.6
    return simplified + [simplified[0]]


def ring_area(ring: list) -> float:
    """Shoelace area in square degrees — only used to rank and filter parts."""
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:] + ring[:1]))) / 2
