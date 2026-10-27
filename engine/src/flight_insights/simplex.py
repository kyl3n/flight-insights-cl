"""A small linear program solver (spec: Propulsion, "Motor-out margin"): minimise c.x
subject to A x = b and x >= 0, by the two-phase simplex method on a dense tableau.

The motor-out margin solves one program per motor with at most a few dozen variables, so
a dense tableau is plenty, and numpy is all the engine depends on (no SciPy). Bland's
rule picks the entering and leaving variables, which rules out cycling.
"""

from __future__ import annotations

from typing import Any

import numpy as np

Array = np.ndarray[Any, Any]
TOL = 1e-9


class Unbounded(ValueError):
    """The objective decreases without limit."""


def _pivot(t: Array, row: int, col: int) -> None:
    t[row] /= t[row, col]
    for r in range(t.shape[0]):
        if r != row and abs(t[r, col]) > 0:
            t[r] -= t[r, col] * t[row]


def _run(t: Array, basis: list[int], cost: Array, allowed: list[int]) -> None:
    """Pivot until no allowed column lowers the cost (Bland's rule)."""
    m = t.shape[0]
    while True:
        reduced = cost[:-1] - cost[basis] @ t[:, :-1]
        entering = next((j for j in allowed if reduced[j] < -TOL), None)
        if entering is None:
            return
        rows = [(t[i, -1] / t[i, entering], basis[i], i) for i in range(m) if t[i, entering] > TOL]
        if not rows:
            raise Unbounded("the objective has no lower bound")
        _, _, row = min(rows)  # the least ratio; ties to the lowest basic variable
        _pivot(t, row, entering)
        basis[row] = entering


def linprog(c: Array, a: Array, b: Array) -> Array | None:
    """The x >= 0 that minimises c.x with a x = b, or None when no x satisfies them."""
    a = np.array(a, float)
    b = np.array(b, float)
    c = np.array(c, float)
    m, n = a.shape
    flip = b < 0
    a[flip], b[flip] = -a[flip], -b[flip]
    # phase 1: artificial variables, one per row, minimised to zero
    t = np.hstack([a, np.eye(m), b[:, None]])
    basis = list(range(n, n + m))
    phase1 = np.concatenate([np.zeros(n), np.ones(m), [0.0]])
    _run(t, basis, phase1, list(range(n + m)))
    if phase1[basis] @ t[:, -1] > 1e-7 * max(1.0, float(np.abs(b).max(initial=0.0))):
        return None
    # drive the artificials still basic (at zero) out, or drop their redundant rows
    keep: list[int] = []
    for i in range(m):
        if basis[i] >= n:
            col = next((j for j in range(n) if abs(t[i, j]) > TOL), None)
            if col is None:
                continue
            _pivot(t, i, col)
            basis[i] = col
        keep.append(i)
    t = t[keep]
    basis = [basis[i] for i in keep]
    # phase 2: the real objective, artificials barred
    _run(t, basis, np.concatenate([c, np.zeros(m), [0.0]]), list(range(n)))
    x = np.zeros(n)
    for i, j in enumerate(basis):
        x[j] = t[i, -1]
    return x
