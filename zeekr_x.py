#!/usr/bin/env python3
"""Live Zeekr X garage, charging monitor and last-drive replay. Run with python3 zeekr_x.py."""

import argparse
import bisect
import collections
import contextlib
import curses
import fcntl
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import struct
import sys
import termios
import textwrap
import threading
import time
import http.client
import gzip
import io
import functools
import urllib.request

TAU = 2.0 * math.pi
PERIOD = 30.0
STEP = TAU / 72
FPS = 25
CELL_ASPECT = 2.05  # Character height / width in a typical monospace font.
ELEVATION = math.radians(8)

# Each finish has its own short value ramp within the scene's tonal range.
# The first colour is an xterm 256-colour index, the second an ANSI fallback.
FINISHES = {
    "pearl": ("=+*#%@", 252, curses.COLOR_WHITE),
    "facet": ("-=+*#%", 249, curses.COLOR_WHITE),
    "sill": (":-=+", 242, curses.COLOR_WHITE),
    "glass": (":-=+", 67, curses.COLOR_CYAN),
    "reflection": (":-=+", 75, curses.COLOR_CYAN),
    "tint": (".:-=", 60, curses.COLOR_BLUE),
    "roof": (":-=+*", 244, curses.COLOR_WHITE),
    "palace-beige": ("+*#%@", 187, curses.COLOR_WHITE),
    "sunroof": (".:-=", 60, curses.COLOR_BLUE),
    "black": (".:-", 239, curses.COLOR_WHITE),
    "rubber": (":-=+*", 244, curses.COLOR_WHITE),
    "rim": ("+*#%@", 253, curses.COLOR_WHITE),
    "drum": (".:-", 238, curses.COLOR_WHITE),
    "lamp": ("#%@", 231, curses.COLOR_WHITE),
    "tail": ("##@", 196, curses.COLOR_RED),
    "open": ("!!!", 215, curses.COLOR_YELLOW),
    "plate": ("::-", 240, curses.COLOR_WHITE),
}


def lerp(a, b, t):
    return a + (b - a) * t


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def normal(a, b, c):
    u = tuple(x - y for x, y in zip(b, a))
    v = tuple(x - y for x, y in zip(c, a))
    n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
    length = math.sqrt(dot(n, n)) or 1
    return tuple(x / length for x in n)


class Mesh:
    def __init__(self):
        self.vertices = []
        self.indices = {}
        self.triangles = []
        self.lines = []

    def vertex(self, p):
        if p not in self.indices:
            self.indices[p] = len(self.vertices)
            self.vertices.append(p)
        return self.indices[p]

    def face(self, points, finish, outward):
        # Fan triangulation gives adjacent facets a shared diagonal.
        ids = [self.vertex(p) for p in points]
        for i in range(1, len(ids) - 1):
            n = normal(points[0], points[i], points[i + 1])
            if dot(n, outward) < 0:
                n = tuple(-x for x in n)
            self.triangles.append((ids[0], ids[i], ids[i + 1], n, finish))

    def line(self, points, finish, glyph, outward):
        for a, b in zip(points, points[1:]):
            self.lines.append((self.vertex(a), self.vertex(b), finish, glyph, outward))


def section(table, x):
    """Linear interpolation across longitudinal design stations."""
    i = max(1, min(len(table) - 1, bisect.bisect_right(table, (x, math.inf))))
    a, b = table[i - 1], table[i]
    t = max(0, min(1, (x - a[0]) / (b[0] - a[0])))
    return tuple(lerp(u, v, t) for u, v in zip(a[1:], b[1:]))


# x runs from tail to nose, y is up, z is across the car. Dimensions in metres.
# Body station: outer half-width, shoulder height, crown above the shoulder.
BODY = [
    (-2.20, 0.760, 1.015, 0.020),
    (-2.10, 0.858, 1.077, 0.025),
    (-1.76, 0.922, 1.102, 0.026),
    (-1.35, 0.932, 1.095, 0.025),
    (-0.91, 0.906, 1.050, 0.036),
    (-0.59, 0.895, 0.998, 0.048),
    (0.12, 0.897, 0.996, 0.060),
    (0.83, 0.906, 1.002, 0.069),
    (1.37, 0.932, 0.983, 0.054),
    (1.70, 0.918, 0.944, 0.057),
    (1.96, 0.867, 0.853, 0.072),
    (2.105, 0.816, 0.785, 0.078),
    (2.215, 0.742, 0.758, 0.060),
]
AXLES = (-1.38, 1.37)
HUB = 0.357
ARCH = 0.435
BOTTOM = 0.214
# A narrow bevel breaks the hood into its centre stamping and two blade shelves.
DECK = [(0, 1), (0.61, 0.91), (0.82, 0.74), (1, 0)]


def deck(x, frac, side=1, lift=0):
    width, shoulder, crown = section(BODY, x)
    return (x, shoulder + crown * section(DECK, frac)[0] + lift, side * frac * width)


def sideskin(x, y, side, offset=0):
    width, shoulder, _ = section(BODY, x)
    t = (y - BOTTOM) / (shoulder - BOTTOM)
    inset = section([(0, 0.070), (0.22, 0.035), (0.70, 0), (1, 0.010)], t)[0]
    return (x, y, side * (width - inset + offset))


def body(mesh):
    xs = {r[0] for r in BODY}
    for axle in AXLES:
        xs.update(axle + ARCH * math.cos(i * math.pi / 14) for i in range(15))
    xs = sorted(xs)
    for side in (-1, 1):
        outward = (0, 0, side)
        for x0, x1 in zip(xs, xs[1:]):
            axle = min(AXLES, key=lambda a: abs((x0 + x1) / 2 - a))
            in_arch = abs((x0 + x1) / 2 - axle) < ARCH
            strips = []
            for x in (x0, x1):
                width, shoulder, _ = section(BODY, x)
                base = HUB + math.sqrt(max(0, ARCH**2 - (x - axle) ** 2)) if in_arch else BOTTOM
                heights = [
                    base,
                    max(base, lerp(BOTTOM, shoulder, 0.22)),
                    max(base, lerp(BOTTOM, shoulder, 0.70)),
                    shoulder,
                ]
                strips.append([sideskin(x, y, side) for y in heights] + [deck(x, 1, side)])
            for j, finish in enumerate(("facet", "pearl", "pearl", "pearl")):
                a, b = strips
                mesh.face([a[j], b[j], b[j + 1], a[j + 1]], finish, outward)
            for (f0, _), (f1, _) in zip(DECK, DECK[1:]):
                mesh.face(
                    [deck(x0, f0, side), deck(x1, f0, side), deck(x1, f1, side), deck(x0, f1, side)],
                    "black" if x1 <= -2.10 else "pearl",
                    (0, 1, 0),
                )

        # Wheel opening mouldings, dark returns and recessed well back plates.
        for axle in AXLES:
            arcs = []
            for r, offset in ((ARCH + 0.025, 0.006), (ARCH - 0.017, 0.017), (ARCH - 0.023, -0.18)):
                points = [
                    (axle + r * math.cos(i * math.pi / 14), HUB + r * math.sin(i * math.pi / 14))
                    for i in range(15)
                ]
                points = [(axle + r, BOTTOM)] + points + [(axle - r, BOTTOM)]
                arcs.append([sideskin(x, y, side, offset) for x, y in points])
            for outer, inner in zip(arcs, arcs[1:]):
                for i in range(len(outer) - 1):
                    mesh.face([outer[i], outer[i + 1], inner[i + 1], inner[i]], "black", outward)
            mesh.face(arcs[-1], "drum", outward)

        # A shallow pressed diagonal facet rises from the rocker toward the
        # rear haunch. Subdivide the crease at the underlying body stations.
        crease = [(-0.93, 0.59), (-0.6, 0.62), (-0.20, 0.55), (0.18, 0.46), (0.52, 0.36), (0.82, 0.31)]
        for (a, ya), (b, yb) in zip(crease, crease[1:]):
            mesh.face(
                [
                    sideskin(a, 0.29, side, 0.008),
                    sideskin(b, 0.29, side, 0.008),
                    sideskin(b, yb, side, 0.008),
                    sideskin(a, ya, side, 0.008),
                ],
                "facet",
                outward,
            )
        # The triangular upkick below the doors is visible in the side reference.
        lo, hi = AXLES[0] + ARCH, AXLES[1] - ARCH
        for a, b in zip(
            [(lo, 0.331), (-0.46, 0.422), (0.25, 0.306)], [(-0.46, 0.422), (0.25, 0.306), (hi, 0.275)]
        ):
            mesh.face(
                [
                    sideskin(a[0], BOTTOM, side, 0.016),
                    sideskin(b[0], BOTTOM, side, 0.016),
                    sideskin(*b, side, 0.016),
                    sideskin(*a, side, 0.016),
                ],
                "black",
                outward,
            )
        for x0 in (-0.86, -0.03):
            x1, y0, y1 = x0 + 0.12, 0.825, 0.844
            mesh.face(
                [
                    sideskin(x0, y0, side, 0.004),
                    sideskin(x1, y0, side, 0.004),
                    sideskin(x1, y1, side, 0.004),
                    sideskin(x0, y1, side, 0.004),
                ],
                "sill",
                outward,
            )
        # Very fine shut lines use actual facets so they diminish with distance.
        for path in [
            [(-0.16, 0.996), (-0.16, 0.72), (-0.20, 0.40)],
            [(0.84, 1.0), (0.89, 0.80), (0.88, 0.50), (0.84, 0.272)],
        ]:
            for (x0, y0), (x1, y1) in zip(path, path[1:]):
                mesh.face(
                    [
                        sideskin(x0, y0, side, 0.004),
                        sideskin(x0 + 0.012, y0, side, 0.004),
                        sideskin(x1 + 0.012, y1, side, 0.004),
                        sideskin(x1, y1, side, 0.004),
                    ],
                    "sill",
                    outward,
                )
    mesh.face(
        [(-2.20, BOTTOM, -0.70), (2.215, BOTTOM, -0.70), (2.215, BOTTOM, 0.70), (-2.20, BOTTOM, 0.70)],
        "black",
        (0, -1, 0),
    )


# Cabin stations describe the tumblehome, sloping screens and floating roof.
# x, lower glass height and width, upper glass height and width
CABIN = [
    (-2.145, 1.060, 0.793, 1.085, 0.774),
    (-1.815, 1.102, 0.872, 1.461, 0.693),
    (-1.605, 1.128, 0.881, 1.522, 0.687),
    (-1.235, 1.156, 0.888, 1.570, 0.678),
    (-0.72, 1.033, 0.886, 1.588, 0.675),
    (-0.16, 1.032, 0.890, 1.584, 0.681),
    (0.215, 1.037, 0.890, 1.541, 0.685),
    (0.44, 1.042, 0.887, 1.456, 0.705),
    (0.965, 1.040, 0.863, 1.068, 0.823),
]


def cab(x, t, side, offset=0):
    y0, w0, y1, w1 = section(CABIN, x)
    return (x, lerp(y0, y1, t), side * (lerp(w0, w1, t) + offset))


def crown(x, f, side=1, lift=0):
    _, _, y, w = section(CABIN, x)
    return (x, y + (1 - f) * 0.045 + lift, side * w * f)


def roof_panel(mesh, x0, x1, side, paint):
    # Half-widths along the glass opening. The end stations clip its corners.
    opening = [(-1.51, 0.65), (-1.40, 0.81), (-0.09, 0.81), (0.045, 0.68)]
    cuts = [x0] + [x for x, _ in opening if x0 < x < x1] + [x1]
    for a, b in zip(cuts, cuts[1:]):
        if opening[0][0] <= a and b <= opening[-1][0]:
            f, g = section(opening, a)[0], section(opening, b)[0]
            bands = [
                (0, 0, f, g, "sunroof"),
                (f, g, f + 0.025, g + 0.025, "black"),
                (f + 0.025, g + 0.025, 1, 1, paint),
            ]
        else:
            bands = [(0, 0, 1, 1, paint)]
        for inner_a, inner_b, outer_a, outer_b, finish in bands:
            mesh.face(
                [
                    crown(a, inner_a, side),
                    crown(b, inner_b, side),
                    crown(b, outer_b, side),
                    crown(a, outer_a, side),
                ],
                finish,
                (0, 1, 0),
            )


def cabin(mesh, roof):
    paint = "palace-beige" if roof == "palace-beige" else "roof"
    for side in (-1, 1):
        out = (0, 0, side)
        for a, b in zip(CABIN, CABIN[1:]):
            x0, x1 = a[0], b[0]
            mid = (x0 + x1) / 2
            below_roof = -1.815 < mid < 0.215
            mesh.face(
                [deck(x0, 1, side), deck(x1, 1, side), cab(x1, 0, side), cab(x0, 0, side)],
                "pearl",
                out,
            )
            for t, u, finish in [
                (0, 0.055, "black"),
                (0.055, 0.92, "tint" if mid < -0.16 else "glass"),
                (0.92, 1, paint if below_roof else "black"),
            ]:
                mesh.face(
                    [cab(x0, t, side), cab(x1, t, side), cab(x1, u, side), cab(x0, u, side)],
                    finish,
                    out,
                )
            if below_roof:
                roof_panel(mesh, x0, x1, side, paint)
                continue
            top = "tint" if mid < -1.815 else "glass"
            for f, g, finish in [(0, 0.943, top), (0.943, 1, "black")]:
                mesh.face(
                    [crown(x0, f, side), crown(x1, f, side), crown(x1, g, side), crown(x0, g, side)],
                    finish,
                    (0, 1, 0),
                )
        # A faint diagonal reflection follows each facet of the windscreen.
        glass = [(0.218, 0.52, 0.74), (0.44, 0.41, 0.63), (0.962, 0.12, 0.30)]
        if side == 1:
            for (a, f, g), (b, h, j) in zip(glass, glass[1:]):
                mesh.face(
                    [
                        crown(a, f, side, 0.003),
                        crown(a, g, side, 0.003),
                        crown(b, j, side, 0.003),
                        crown(b, h, side, 0.003),
                    ],
                    "reflection",
                    (1, 1, 0),
                )
        for x in (CABIN[0][0], CABIN[-1][0]):
            outward = (1 if x > 0 else -1, 0, 0)
            mesh.face(
                [crown(x, 0, side), crown(x, 1, side), deck(x, 1, side), deck(x, 0, side)],
                "black",
                outward,
            )
        # Painted, forward-raked C pillar and slim black B pillar.
        for coords, finish in [
            ([(-2.10, 0), (-1.37, 0), (-1.25, 1), (-1.60, 1)], "pearl"),
            ([(-0.24, 0), (-0.165, 0), (-0.24, 1), (-0.313, 1)], "black"),
            ([(0.905, 0), (0.964, 0), (0.28, 1), (0.215, 1)], "black"),
        ]:
            # Follow every change in the side glass contour under each pillar.
            (bl, _), (br, _), (tr, _), (tl, _) = coords
            for i in range(8):
                t, u = i / 8, (i + 1) / 8
                left, right = lerp(bl, tl, t), lerp(br, tr, t)
                l2, r2 = lerp(bl, tl, u), lerp(br, tr, u)
                cuts = [v for v in (r[0] for r in CABIN) if left < v < right]
                fractions = [0] + [(v - left) / (right - left) for v in cuts] + [1]
                for f, g in zip(fractions, fractions[1:]):
                    mesh.face(
                        [
                            cab(lerp(left, right, f), t, side, 0.009),
                            cab(lerp(left, right, g), t, side, 0.009),
                            cab(lerp(l2, r2, g), u, side, 0.009),
                            cab(lerp(l2, r2, f), u, side, 0.009),
                        ],
                        finish,
                        out,
                    )

        # Floating lip over the backlight, with a dark underside.
        mesh.face(
            [
                (-1.924, 1.472, side * 0.705),
                (-1.805, 1.497, side * 0.704),
                (-1.805, 1.520, 0),
                (-1.954, 1.492, 0),
            ],
            paint,
            (0, 1, 0),
        )
        mesh.face(
            [
                (-1.924, 1.472, side * 0.705),
                (-1.954, 1.492, 0),
                (-1.948, 1.453, 0),
                (-1.904, 1.439, side * 0.696),
            ],
            "black",
            (-1, 0, 0),
        )

        # Stem and faceted mirror cap.
        mesh.face(
            [
                (0.60, 0.982, side * 0.88),
                (0.70, 1.005, side * 0.88),
                (0.65, 1.068, side * 1.00),
                (0.56, 1.047, side * 1.00),
            ],
            "black",
            out,
        )
        base = [
            (0.515, 1.055, 0.970),
            (0.543, 1.052, 1.059),
            (0.657, 1.063, 1.075),
            (0.718, 1.076, 0.969),
        ]
        cap = [(x, y + 0.071, side * (z - 0.009)) for x, y, z in base]
        base = [(x, y, side * z) for x, y, z in base]
        mesh.face(cap, "pearl", (0, 1, 0))
        for i in range(4):
            j = (i + 1) % 4
            mesh.face([base[i], base[j], cap[j], cap[i]], "tint" if i == 0 else "roof", out)


def wheels(mesh):
    steps = 28
    for x in AXLES:
        for side in (-1, 1):
            out = (0, 0, side)

            def at(r, a, z):
                return (x + r * math.cos(a), HUB + r * math.sin(a), side * z)

            rings = [
                (0.31, 0.653),
                (0.353, 0.686),
                (0.357, 0.845),
                (0.344, 0.895),
                (0.304, 0.917),
                (0.252, 0.920),
                (0.244, 0.922),
                (0.230, 0.921),
                (0.218, 0.897),
            ]
            for (r, z), (s, w), finish in zip(
                rings,
                rings[1:],
                ("black", "rubber", "rubber", "rubber", "rubber", "rim", "rim", "black"),
            ):
                for i in range(steps):
                    a, b = TAU * i / steps, TAU * (i + 1) / steps
                    mesh.face(
                        [at(r, a, z), at(r, b, z), at(s, b, w), at(s, a, w)],
                        finish,
                        (math.cos((a + b) / 2) * 0.7, math.sin((a + b) / 2) * 0.7, side),
                    )
            mesh.face([at(0.31, TAU * i / steps, 0.653) for i in range(steps)], "black", (0, 0, -side))
            mesh.face([at(0.222, TAU * i / steps, 0.896) for i in range(steps)], "drum", out)
            ring = [at(0.240, TAU * i / steps, 0.925) for i in range(steps + 1)]
            mesh.line(ring, "rim", None, out)
            for i in range(5):
                a = TAU * i / 5 + 0.19
                for skew in (-1, 1):
                    spoke = [
                        (0.059, a + skew * 0.09),
                        (0.125, a + skew * 0.22),
                        (0.229, a + skew * 0.38),
                        (0.231, a + skew * 0.14),
                        (0.105, a + skew * 0.035),
                    ]
                    mesh.face([at(r, b, 0.923) for r, b in spoke], "rim", out)
            mesh.face([at(0.062, TAU * i / 10, 0.925) for i in range(10)], "roof", out)


def ends(mesh):
    for front in (True, False):
        x = BODY[-1][0] if front else BODY[0][0]
        sign = 1 if front else -1
        w = section(BODY, x)[0]
        out = (sign, 0, 0)

        def panel(coords, finish, offset=0.003):
            mesh.face([(x + sign * offset, y, z) for z, y in coords], finish, out)

        for side in (-1, 1):
            shoulder = section(BODY, x)[1]
            profile = [sideskin(x, lerp(BOTTOM, shoulder, t), side) for t in (0, 0.22, 0.70, 1)]
            for a, b in zip(profile, profile[1:]):
                mesh.face([(x, a[1], 0), a, b, (x, b[1], 0)], "pearl", out)
            # Close the crown above the horizontal shoulder band.
            for (f, _), (g, _) in zip(DECK, DECK[1:]):
                mesh.face(
                    [
                        deck(x, f, side),
                        deck(x, g, side),
                        (x, shoulder, side * g * (w - 0.010)),
                        (x, shoulder, side * f * (w - 0.010)),
                    ],
                    "pearl" if front else "black",
                    out,
                )

            def half(coords, finish, offset=0.004):
                panel([(z * side, y) for z, y in coords], finish, offset)

            if front:
                half([(0, 0.222), (0.675, 0.222), (0.704, 0.325), (0.53, 0.408), (0, 0.413)], "black")
                half(
                    [(0.13, 0.692), (0.678, 0.688), (0.704, 0.648), (0.665, 0.624), (0.235, 0.641)],
                    "black",
                )
                half([(0.666, 0.654), (0.700, 0.669), (0.709, 0.412), (0.688, 0.365)], "black")
                mesh.line(
                    [
                        (x + 0.007, 0.659, side * 0.168),
                        (x + 0.007, 0.662, side * 0.677),
                        (x + 0.007, 0.419, side * 0.697),
                    ],
                    "black",
                    None,
                    out,
                )
                half([(0.526, 0.502), (0.698, 0.532), (0.697, 0.497)], "black")
                # Split, swept-back daytime running blades on the hood shoulders.
                blade = [
                    (1.625, 0.944, 0.967),
                    (1.70, 0.830, 0.962),
                    (1.83, 0.704, 0.942),
                    (1.96, 0.627, 0.884),
                    (2.055, 0.584, 0.775),
                    (2.14, 0.572, 0.628),
                ]
                for (a, f, g), (b, h, j) in zip(blade, blade[1:]):
                    mesh.face(
                        [
                            deck(a, f, side, 0.009),
                            deck(a, g, side, 0.009),
                            deck(b, j, side, 0.009),
                            deck(b, h, side, 0.009),
                        ],
                        "black",
                        (0, 1, 0),
                    )
                for points in [
                    [(2.128, 0.607), (2.020, 0.668), (1.990, 0.758), (1.835, 0.825)],
                    [(1.965, 0.841), (1.830, 0.896), (1.721, 0.933)],
                ]:
                    mesh.line(
                        [deck(a, f, side, 0.021) for a, f in points],
                        "lamp",
                        None,
                        (0.42, 0.86, side * 0.23),
                    )
                half([(0.41, 0.658), (0.613, 0.653), (0.615, 0.665), (0.418, 0.675)], "lamp", 0.007)
            else:
                half([(0, 0.220), (0.694, 0.220), (0.719, 0.397), (0.480, 0.525), (0, 0.480)], "black")
                half([(0, 0.572), (0.47, 0.572), (0.703, 0.715), (0.485, 0.537), (0, 0.536)], "facet")
                half([(0.669, 0.452), (0.687, 0.48), (0.707, 0.757), (0.687, 0.725)], "tail")
                half([(0, 0.947), (0.755, 0.958), (0.751, 1.023), (0, 1.032)], "black")
                half(
                    [(0, 0.970), (0.723, 0.980), (0.749, 1.011), (0.701, 0.995), (0, 0.991)],
                    "tail",
                    0.007,
                )
                mesh.line([(x - 0.010, 0.982, 0), (x - 0.010, 0.989, side * 0.715)], "tail", "=", out)
                wrap = [(-2.20, 0.988), (-2.085, 1.028), (-1.76, 1.082)]
                mesh.line(
                    [sideskin(a, y, side, 0.012) for a, y in wrap], "tail", None, (-0.55, 0, side)
                )
        lo, hi = (0.453, 0.600) if front else (0.335, 0.442)
        panel([(-0.24, lo), (0.24, lo), (0.24, hi), (-0.24, hi)], "plate", 0.009)
        for k in (-1, 1):
            mesh.line(
                [
                    (x + sign * 0.012, lo + 0.024, -k * 0.019),
                    (x + sign * 0.012, hi - 0.024, k * 0.019),
                ],
                "rim",
                None,
                out,
            )


def build_mesh(roof="palace-beige"):
    mesh = Mesh()
    body(mesh)
    cabin(mesh, roof)
    wheels(mesh)
    ends(mesh)
    return mesh


class Palette:
    """Cells are small integers so both shading and run comparisons are cheap."""

    def __init__(self):
        self.cells = [(" ", 0)]
        self.ramps = {}
        self.special = {}
        for name, (ramp, _, _) in FINISHES.items():
            self.ramps[name] = [self.add(ch, name) for ch in ramp]
            for ch in "-|/\\=.!":
                self.special[name, ch] = self.add(ch, name)

    def add(self, ch, name):
        i = len(self.cells)
        self.cells.append((ch, name))
        return i


def paint_triangle(a, b, c, cell, width, height, pixels, depths):
    # Scan-convert at character centres. Orthographic depth is linear along
    # both edges and scanlines. Half-open intervals join without double seams.
    top, mid, bot = sorted((a, b, c), key=lambda v: v[1])
    dy = bot[1] - top[1]
    if dy < 1e-8:
        return
    dx = (bot[0] - top[0]) / dy
    dz = (bot[2] - top[2]) / dy
    for p, q in ((top, mid), (mid, bot)):
        start = max(0, math.ceil(p[1]))
        stop = min(height, math.ceil(q[1]))
        if start >= stop:
            continue
        short_dy = q[1] - p[1]
        sx = (q[0] - p[0]) / short_dy
        sz = (q[2] - p[2]) / short_dy
        for row in range(start, stop):
            left, right = top[0] + (row - top[1]) * dx, p[0] + (row - p[1]) * sx
            z0, z1 = top[2] + (row - top[1]) * dz, p[2] + (row - p[1]) * sz
            if left > right:
                left, right, z0, z1 = right, left, z1, z0
            x0 = max(0, math.ceil(left))
            x1 = min(width, math.ceil(right))
            if x1 <= x0:
                continue
            delta = (z1 - z0) / (right - left)
            depth = z0 + (x0 - left) * delta
            for i in range(row * width + x0, row * width + x1):
                if depth > depths[i]:
                    depths[i] = depth
                    pixels[i] = cell
                depth += delta


def terminal_aspect():
    """Use the font cell size if the terminal reports its pixel dimensions."""
    try:
        size = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, bytes(8))
        rows, cols, x_pixels, y_pixels = struct.unpack("HHHH", size)
        if rows and cols and x_pixels and y_pixels:
            return (y_pixels / rows) / (x_pixels / cols)
    except OSError:
        pass
    return CELL_ASPECT


def render(mesh, palette, width, height, angle, aspect=CELL_ASPECT):
    ca, sa = math.cos(angle), math.sin(angle)
    ce, se = math.cos(ELEVATION), math.sin(ELEVATION)
    view = (ca * ce, se, sa * ce)
    # A fixed scale through the whole turn prevents the nose-on view zooming in.
    scale = min((width - 2) / 4.93, (height - 1) * aspect / 2.40)
    points = []
    for x, y, z in mesh.vertices:
        toward = x * ca + z * sa
        points.append(
            (
                (-x * sa + z * ca) * scale,
                (toward * se - y * ce) * scale / aspect,
                toward * ce + y * se,
            )
        )
    xx, yy, _ = zip(*points)
    cx = (width - 1 - min(xx) - max(xx)) * 0.5
    cy = (height - 1 - min(yy) - max(yy)) * 0.5
    screen = [(x + cx, y + cy, z) for x, y, z in points]
    pixels = [0] * (width * height)
    depths = [-math.inf] * len(pixels)
    light = (0.59 * ca + 0.16 * sa, 0.79, 0.59 * sa - 0.16 * ca)
    for i, j, k, n, finish in mesh.triangles:
        ramp = palette.ramps[finish]
        diffuse = max(0, n[0] * light[0] + n[1] * light[1] + n[2] * light[2])
        cell = ramp[int(diffuse * (len(ramp) - 1) + 0.5)]
        paint_triangle(screen[i], screen[j], screen[k], cell, width, height, pixels, depths)
    for i, j, finish, ch, n in mesh.lines:
        facing = dot(view, n)
        if facing <= 0.07:
            continue
        a, b = screen[i], screen[j]
        dx, dy, dz = (v - u for u, v in zip(a, b))
        if ch is None:
            if abs(dx) > 2.8 * abs(dy):
                ch = "-"
            elif abs(dx) < 0.65 * abs(dy):
                ch = "|"
            else:
                ch = "\\" if dx * dy > 0 else "/"
        cell = palette.special[finish, ch]
        steps = max(1, math.ceil(max(abs(dx), abs(dy))))
        for step in range(steps + 1):
            t = step / steps
            x, y = round(a[0] + dx * t), round(a[1] + dy * t)
            if 0 <= x < width and 0 <= y < height:
                idx = y * width + x
                z = a[2] + dz * t
                # Test at the actual sample on its supporting plane. The stored
                # surface depth was sampled at the cell centre, up to half a
                # character away from this fine piece of trim.
                h = (-n[0] * sa + n[2] * ca) * (x - a[0] - dx * t)
                v = (n[0] * ca * se - n[1] * ce + n[2] * sa * se) * (y - a[1] - dy * t) * aspect
                at_center = z - (h + v) / (scale * facing)
                if at_center + 0.012 >= depths[idx]:
                    pixels[idx] = cell
                    depths[idx] = max(depths[idx], at_center)
    return pixels


def colours(roof):
    attrs = {name: 0 for name in FINISHES}
    if curses.has_colors() and curses.COLORS >= 8:
        pairs = {}
        for name in ("tail", *FINISHES):
            _, rich, basic = FINISHES[name]
            if roof == "palace-beige" and name in ("pearl", "facet"):
                rich = FINISHES["palace-beige"][1]
            colour = rich if curses.COLORS >= 256 else basic
            if colour not in pairs:
                pair = len(pairs) + 1
                if pair >= curses.COLOR_PAIRS:
                    continue
                curses.init_pair(pair, colour, curses.COLOR_BLACK)
                pairs[colour] = curses.color_pair(pair)
            attrs[name] = pairs[colour]
    return attrs


def paint_car(win, pixels, width, height, palette, attrs, top=0):
    # erase() touches only the virtual screen. One differential doupdate()
    # commits the complete frame, so the physical screen never shows a wipe.
    chars = palette.cells
    for row in range(height):
        x = 0
        base = row * width
        while x < width:
            cell = pixels[base + x]
            if not cell:
                x += 1
                continue
            glyph, finish = chars[cell]
            start = x
            run = [glyph]
            x += 1
            while x < width and chars[pixels[base + x]][1] == finish:
                run.append(chars[pixels[base + x]][0])
                x += 1
            win.addstr(top + row, start, "".join(run), attrs[finish])


# The live data worker uses the CLI's Python environment. Only small,
# display-ready messages cross the pipe. Curses stays on the main thread.
PROJECT_DIR = Path(__file__).resolve().parent
POLL_SECONDS = 60
OPENINGS = {
    "driver front": "doorOpenStatusDriver",
    "passenger front": "doorOpenStatusPassenger",
    "driver rear": "doorOpenStatusDriverRear",
    "passenger rear": "doorOpenStatusPassengerRear",
    "hatch": "trunkOpenStatus",
    "bonnet": "engineHoodOpenStatus",
}


def number(value, low=0, high=math.inf):
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        if math.isfinite(n) and low <= n <= high:
            return n
    except (TypeError, ValueError):
        pass
    return None


def flag(value):
    return {"1": True, "true": True, "0": False, "false": False}.get(str(value).lower())


def epoch(value):
    n = number(value, 1)
    if n and n > 1e11:
        n /= 1000
    return n if n and 946684800 <= n <= 4102444800 else None


def garage_data(status):
    more = status.get("additionalVehicleStatus") or {}
    ev = more.get("electricVehicleStatus") or {}
    safety = more.get("drivingSafetyStatus") or {}
    opened = {name: flag(safety.get(key)) for name, key in OPENINGS.items()}
    eta = number(ev.get("timeToFullyCharged"), 0, 2046)
    charging = flag(ev.get("isCharging"))
    return dict(
        soc=number(ev.get("chargeLevel"), 0, 100),
        km=number(ev.get("distanceToEmptyOnBatteryOnly")),
        plugged=flag(ev.get("isPluggedIn")),
        charging=charging,
        locked=flag(safety.get("centralLockingStatus")),
        openings=opened,
        eta=eta,
        stamp=epoch(status.get("updateTime")),
    )


def power_data(data):
    return dict(
        state=number(data.get("chargerState")),
        kw=number(data.get("chargePower")),
        amps=number(data.get("chargeCurrent")),
        volts=number(data.get("chargeVoltage")),
        stamp=epoch(data.get("updateTime")),
    )


def target_data(data):
    # The configured limit is in tenths of a percent.
    tenth = number(data.get("soc"), 0, 1000)
    return {"percent": None if tenth is None else tenth / 10}


def rows_in(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("trackPoints", "list", "records", "rows", "data"):
            if isinstance(payload.get(key), (list, dict)):
                return rows_in(payload[key])
    return []


def route_points(payload, origin=None):
    points = []
    for item in rows_in(payload):
        if not isinstance(item, dict):
            continue
        lat = number(item.get("latitude"), -90, 90)
        lon = number(item.get("longitude"), -180, 180)
        if lat is not None and lon is not None and (lat or lon):
            points.append((epoch(item.get("systemTime")), lat, lon))
    if len(points) < 2:
        return []
    timed = all(t is not None for t, _, _ in points)
    if timed:
        points.sort(key=lambda p: p[0])
    t0, lat0, lon0 = points[0]
    timed = timed and points[-1][0] > t0
    cosine = math.cos(math.radians(lat0))
    if origin is not None:
        origin[:] = [lat0, lon0]
    # Use distance along the route as the clock if GPS timestamps are absent.
    result = []
    length = 0
    for stamp, lat, lon in points:
        x = ((lon - lon0 + 180) % 360 - 180) * cosine
        y = lat - lat0
        if result:
            length += math.hypot(x - result[-1][0], y - result[-1][1])
        moment = stamp - t0 if timed else length
        result.append((x, y, moment))
    if not length:
        return []
    span = result[-1][2]
    return [(x, y, t / span) for x, y, t in result]


def journey_page(vehicle, cursor=0):
    log = vehicle.get_journey_log(page_size=2, days_back=90, end_time=cursor)
    trips = [t for t in rows_in(log) if isinstance(t, dict)]
    if not trips:
        if log == [] or (isinstance(log, dict) and (
            log.get("total") in (0, "0") or any(log.get(k) == [] for k in ("list", "records", "rows", "data"))
        )):
            return [], 0
        raise ValueError("No journey log response")
    trips.sort(key=lambda t: epoch(t.get("endTime")) or epoch(t.get("startTime")) or 0, reverse=True)
    # lastId is a millisecond timestamp. Some responses omit it.
    stamp = number(log.get("lastId")) if isinstance(log, dict) else None
    if not stamp or len(trips) > 2:
        oldest = trips[min(1, len(trips)-1)]
        seconds = epoch(oldest.get("reportTime")) or epoch(oldest.get("endTime"))
        stamp = seconds * 1000 if seconds else 0
    next_cursor = int(stamp) - 1
    if len(trips) < 2 or next_cursor <= 0 or (cursor and next_cursor >= cursor):
        next_cursor = 0
    return trips[:2], next_cursor


def drive_detail(vehicle, trip):
    start, end = epoch(trip.get("startTime")), epoch(trip.get("endTime"))
    duration = end - start if start and end and end >= start else number(trip.get("duration"))
    km = number(trip.get("traveledDistance"))
    if km is None:
        km = number(trip.get("distance"))
    efficiency = number(trip.get("electricConsumption"))
    energy = number(trip.get("energyConsumption"))
    if efficiency is None and km and energy is not None:
        efficiency = energy * 100 / km
    origin = []
    points = route_points(trip.get("trackPoints"), origin)
    track_error = False
    if trip.get("reportTime") is not None and trip.get("tripId") is not None:
        try:
            track = vehicle.get_trip_trackpoints(trip["reportTime"], trip["tripId"])
            track_origin = []
            detailed = route_points(track, track_origin)
            if detailed:
                points, origin = detailed, track_origin
        except Exception:
            track_error = True
    return dict(
        key=[trip.get("tripId"), trip.get("reportTime"), start, end],
        origin=origin,
        start=start,
        end=end,
        km=km,
        seconds=duration,
        efficiency=efficiency,
        points=points,
        track_error=track_error and not points,
    )


def other_device(exc):
    message = f"{exc} {exc.__cause__}"
    return "079021" in message or "logged in on another device" in message


def connect_main_account(connect, restart):
    try:
        return connect("main")
    except SystemExit as exc:
        if not restart or not (other_device(exc) or "No working saved session" in str(exc)):
            raise
    # Only an explicit restart can submit the configured account's password.
    return connect("main", force_login=True, allow_login=True)


def api_worker():
    output = sys.stdout

    def emit(**message):
        print(json.dumps(message, allow_nan=False, separators=(",", ":")), file=output, flush=True)

    vehicle = None
    chosen_vin = None
    # The local helper normally prints connection diagnostics. Keep these out
    # of both the terminal and the JSON stream, including exception messages.
    with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet):
        for line in sys.stdin:
            command = line.strip()
            restart = command.startswith("restart ")
            group = command.partition(" ")[2] if restart else command
            cursor = 0
            if group.startswith("trip ") and group[5:].isdigit():
                cursor = int(group[5:])
                group = "trip"
            if group not in ("vehicle", "trip"):
                continue
            if restart:
                vehicle = None
            try:
                from zeekr_x_api.session_cache import connect, pick_vehicle

                if vehicle is None:
                    os.environ["ZEEKR_FORCE_LOGIN"] = "0"
                    client = connect_main_account(connect, restart)
                    vehicle = pick_vehicle(client)
                    wanted = chosen_vin or os.environ.get("ZEEKR_VIN")
                    if wanted:
                        vehicle = next((v for v in client.vehicles if v.vin == wanted), None)
                    if vehicle is None:
                        emit(error=group, reason="Configured vehicle unavailable on the main account.")
                        continue
                    chosen_vin = vehicle.vin
                if group == "trip":
                    trips, next_cursor = journey_page(vehicle, cursor)
                    for i, record in enumerate(trips):
                        emit(drive=drive_detail(vehicle, record), latest=not cursor and i == 0)
                    if not trips and not cursor:
                        emit(part="trip", data={"empty": True})
                    emit(trip_cursor=next_cursor)
                else:
                    for name, get, convert in (
                        ("garage", vehicle.get_status, garage_data),
                        ("power", vehicle.get_charging_status, power_data),
                        ("target", vehicle.get_charging_limit, target_data),
                    ):
                        try:
                            raw = get()
                            if not isinstance(raw, dict) or not raw:
                                raise ValueError("Missing vehicle response")
                            emit(part=name, data=convert(raw))
                        except Exception:
                            emit(error=name, reason=name.capitalize() + " reading unavailable.")
                            vehicle = None
                if restart and vehicle is not None:
                    emit(connected=group)
            except ImportError:
                emit(error=group, reason="API dependencies missing. Install the CLI as shown in README.md.")
            except FileNotFoundError:
                emit(error=group, reason="Zeekr configuration file missing. See README.md for setup.")
            except (Exception, SystemExit) as exc:
                vehicle = None
                if other_device(exc):
                    reason = "Another device signed in on the main account. Press S to restart its session."
                elif "No working saved session" in str(exc):
                    reason = "Saved main session unavailable. Press S to sign in."
                elif "9300" in f"{exc} {exc.__cause__}":
                    reason = "Zeekr declined main API sign-in (9300). A valid saved session is required."
                else:
                    reason = "Could not read the main account. Check the network and API settings."
                emit(error=group, reason=reason)
            finally:
                emit(done=group)


class LiveFeed:
    def __init__(self):
        self.data = {}
        self.errors = {}
        self.pending = set()
        self.restart_waiting = set()
        self.restarting = set()
        self.connected_until = 0
        self.drives = []
        self.drive_index = 0
        self.cursor = 0
        self.advance = False
        self.history = collections.deque(maxlen=120)
        self.inbox = queue.Queue()
        self.process = None
        self.next_poll = 0

    def request(self, group, restart=False, cursor=0):
        if restart and (self.restarting or self.restart_waiting):
            return
        if group in self.pending:
            if restart:
                self.restart_waiting.add(group)
            return
        if restart:
            self.connected_until = 0
        if group == "vehicle":
            self.next_poll = time.monotonic() + POLL_SECONDS
        try:
            if self.process is None:
                interpreter = PROJECT_DIR / ".venv/bin/python"
                if not interpreter.is_file():
                    interpreter = sys.executable
                self.process = subprocess.Popen(
                    [str(interpreter), "-B", str(PROJECT_DIR / "zeekr_x.py"), "--api-worker"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    start_new_session=True,
                )
                threading.Thread(target=self.listen, args=(self.process,), daemon=True).start()
            command = ("restart " if restart else "") + group
            if cursor:
                command += f" {cursor}"
            self.process.stdin.write(command + "\n")
            self.process.stdin.flush()
            self.pending.add(group)
            if restart:
                self.restarting.add(group)
        except OSError:
            self.errors[group] = "API worker unavailable. Check your Python environment."

    def browse(self, direction):
        target = self.drive_index + direction
        if 0 <= target < len(self.drives):
            self.advance = False
            self.drive_index = target
            self.data["trip"] = self.drives[target]
            if direction > 0 and target == len(self.drives) - 1 and self.cursor:
                self.request("trip", cursor=self.cursor)
        elif direction > 0 and self.drives and (self.cursor or "trip" in self.pending):
            self.advance = True
            if self.cursor:
                self.request("trip", cursor=self.cursor)
        else:
            self.advance = False

    def listen(self, process):
        try:
            for line in process.stdout:
                self.inbox.put((process, json.loads(line)))
        except (OSError, ValueError):
            pass
        finally:
            self.inbox.put((process, None))

    def update(self):
        changed = False
        while True:
            try:
                process, msg = self.inbox.get_nowait()
            except queue.Empty:
                break
            if process is not self.process:
                continue
            changed = True
            if msg is None:
                for group in self.pending:
                    self.errors[group] = "API worker stopped. Press R to retry."
                self.pending.clear()
                self.close()
                self.process = None
                self.restarting.clear()
                for group in tuple(self.restart_waiting):
                    self.restart_waiting.remove(group)
                    self.request(group, restart=True)
            elif "drive" in msg:
                trip = msg["drive"]
                self.errors.pop("trip", None)
                if msg["latest"]:
                    self.drives = []
                    self.drive_index = 0
                    self.advance = False
                    self.cursor = 0
                    self.data["trip"] = trip
                if all(t["key"] != trip["key"] for t in self.drives):
                    self.drives.append(trip)
                    if self.advance:
                        self.drive_index = len(self.drives) - 1
                        self.data["trip"] = trip
                        self.advance = False
            elif "trip_cursor" in msg:
                self.cursor = msg["trip_cursor"]
                if not self.cursor:
                    self.advance = False
            elif "connected" in msg:
                group = msg["connected"]
                self.errors.pop(group, None)
                self.connected_until = time.monotonic() + 8
            elif "error" in msg:
                self.errors[msg["error"]] = msg["reason"]
                if msg["error"] == "trip":
                    self.advance = False
            elif "done" in msg:
                group = msg["done"]
                self.pending.discard(group)
                self.restarting.discard(group)
                if group in self.restart_waiting:
                    self.restart_waiting.remove(group)
                    self.advance = False
                    self.request(group, restart=True)
                elif group == "trip" and self.advance and self.cursor:
                    self.request(group, cursor=self.cursor)
            else:
                part, data = msg["part"], msg["data"]
                self.errors.pop(part, None)
                if part == "trip" and data.get("empty"):
                    self.drives.clear()
                    self.drive_index = 0
                if part != "trip":
                    self.errors.pop("vehicle", None)
                if part in ("power", "garage"):
                    data["received"] = time.time()
                self.data[part] = data
                if part == "power" and data["kw"] is not None:
                    stamp = data["stamp"] or data["received"]
                    if not self.history or stamp > self.history[-1][0]:
                        self.history.append((stamp, data["kw"]))
        if time.monotonic() >= self.next_poll and "vehicle" not in self.pending:
            self.request("vehicle")
            changed = True
        return changed

    def close(self):
        if self.process:
            # EOF lets an in-flight read and any session save finish normally.
            try:
                self.process.stdin.close()
            except BrokenPipeError:
                pass


def mark_openings(mesh, openings):
    # The Australian RWD has its driver on the right (+z).
    for name in openings:
        if name == "bonnet":
            for side in (-1, 1):
                mesh.line(
                    [
                        deck(1.0, 0.60, side, 0.018),
                        deck(1.68, 0.60, side, 0.018),
                        deck(2.10, 0.52, side, 0.018),
                    ],
                    "open",
                    "!",
                    (0.3, 1, 0),
                )
        elif name == "hatch":
            for side in (-1, 1):
                mesh.line(
                    [(-2.216, 0.94, side * 0.68), (-2.216, 0.58, side * 0.55), (-2.216, 0.55, 0)],
                    "open",
                    "!",
                    (-1, 0, 0),
                )
        else:
            side = 1 if name.startswith("driver") else -1
            left, right = (-0.96, -0.20) if name.endswith("rear") else (-0.16, 0.84)
            seam = [
                (left, 0.94),
                (right, 0.94),
                (right, 0.70),
                (right - 0.035, 0.41),
                (left + 0.075, 0.41),
                (left, 0.70),
                (left, 0.94),
            ]
            mesh.line([sideskin(x, y, side, 0.023) for x, y in seam], "open", "!", (0, 0, side))


def value(n, unit="", decimals=0):
    return "--" if n is None else f"{n:.{decimals}f}{unit}"


def duration(seconds):
    if seconds is None:
        return "--"
    minutes = round(seconds / 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02}m" if hours else f"{minutes} min"


def clock_label(stamp, fmt="%H:%M"):
    return time.strftime(fmt, time.localtime(stamp)) if stamp else "--"


def freshness(stamp):
    if not stamp:
        return "Vehicle timestamp unavailable"
    age = max(0, time.time() - stamp)
    if age < 60:
        return "Vehicle updated just now"
    if age < 600:
        return f"Vehicle updated {int(age//60)} min ago"
    return "Last reported " + clock_label(stamp, "%d %b %H:%M")


def charge_readings(feed):
    """Reconcile the asynchronous status and meter reports for both views."""
    now = time.time()
    def recent(part):
        record = feed.data.get(part, {})
        stamp = record.get("stamp") or record.get("received")
        if "vehicle" in feed.errors or part in feed.errors or not stamp:
            return {}
        return record if -POLL_SECONDS <= now - stamp <= 3 * POLL_SECONDS else {}

    g = recent("garage")
    power = recent("power")
    battery_time = g.get("stamp") or g.get("received", 0)
    meter_time = power.get("stamp") or power.get("received", 0)
    # Reports within a polling interval can describe the same charge event.
    # A later unplug report supersedes a retained positive meter reading.
    if g.get("plugged") is False and battery_time - meter_time > POLL_SECONDS:
        power = {}

    state = power.get("state")
    drawing = (power.get("kw") or 0) > 0 or (
        (power.get("amps") or 0) > 0 and (power.get("volts") or 0) > 0
    )
    if drawing or state in (1, 2):
        label = "Charging"
    elif state in (25, 26):
        label = "Plugged in" if g.get("plugged") is True else "Charging stopped"
    elif g.get("charging") is True:
        label = "Charging"
    elif g.get("plugged") is True:
        label = "Plugged in"
    elif g.get("plugged") is False:
        label = "Unplugged"
    else:
        label = "Charge state unknown"
    return label, power, g.get("eta") if label == "Charging" else None


def put(win, y, x, text, attr=0):
    h, w = win.getmaxyx()
    if 0 <= y < h and 0 <= x < w - 1 and text:
        win.addnstr(y, x, text, w - x - 1, attr)


def tab_bar(win, tab, attrs):
    _, w = win.getmaxyx()
    x = 1
    if w >= 54:
        put(win, 0, x, "ZEEKR X", curses.A_BOLD)
        x += 10
    tabs = (("g", "Garage"), ("c", "Charge"), ("t", "Last drive"))
    for key, label in tabs:
        tag = f" {key.upper()} {label} " if w >= 34 else (label if tab == key else key.upper())
        put(win, 0, x, tag, curses.A_REVERSE | curses.A_BOLD if tab == key else attrs["rim"])
        x += len(tag) + 1


def garage_panel(win, feed, attrs, h):
    g = feed.data.get("garage")
    if not g:
        put(
            win,
            h - 5,
            1,
            "Reading vehicle status..." if "vehicle" in feed.pending else "Vehicle status unavailable",
        )
        return
    lock = {True: "Locked", False: "Unlocked", None: "Lock unknown"}[g["locked"]]
    put(
        win,
        h - 5,
        1,
        "   ".join([value(g["soc"], "%") + " battery", value(g["km"], " km"), charge_readings(feed)[0], lock]),
        curses.A_BOLD,
    )
    opened = [k for k, v in g["openings"].items() if v]
    unknown = [k for k, v in g["openings"].items() if v is None]
    if opened:
        line = "Open: " + ", ".join(opened)
    elif unknown:
        line = "Closure data incomplete"
    else:
        line = "Doors, hatch and bonnet closed"
    put(win, h - 4, 1, line, attrs["open"] | curses.A_BOLD if opened else attrs["rim"])
    put(win, h - 3, 1, freshness(g["stamp"]), attrs["rim"])


def power_chart(win, samples, attrs):
    h, w = win.getmaxyx()
    if not samples:
        put(win, 8, 1, "Waiting for a power reading.")
        return

    first, last = samples[0][0], samples[-1][0]
    low = min(kw for _, kw in samples)
    peak = max(kw for _, kw in samples)
    count = len(samples)
    if count == 1:
        summary = "First reading"
    elif round(low, 1) == round(peak, 1):
        summary = f"Steady at {peak:.1f} kW"
    else:
        summary = f"{low:.1f} to {peak:.1f} kW"
    put(win, 8, 1, f"POWER HISTORY   {summary}", attrs["rim"] | curses.A_BOLD)

    # Integrate only between nearby samples. Long gaps have unknown power.
    energy = seconds = 0.0
    for (t0, kw0), (t1, kw1) in zip(samples, list(samples)[1:]):
        delta = t1 - t0
        if 0 < delta <= 3 * POLL_SECONDS:
            seconds += delta
            energy += (kw0 + kw1) * .5 * delta / 3600
    detail = f"{count} reading" + ("" if count == 1 else "s")
    if seconds:
        span = duration(seconds) if seconds >= 60 else f"{seconds:.0f} sec"
        detail += f"   ~{energy:.2f} kWh over {span} sampled"
    put(win, 9, 1, detail)

    x0, x1 = 9, w - 3
    top, bottom = 11, min(18, h - 6)
    if bottom - top < 2:
        return
    # Leave headroom so steady charging draws a visible horizontal line.
    ceiling = max(1, math.ceil(peak * 1.1))
    put(win, top, 1, f"{ceiling} kW")
    put(win, bottom, 1, "0")
    put(win, bottom, x0, "-" * (x1 - x0 + 1), attrs["roof"])
    points = []
    for stamp, kw in samples:
        x = x0 + round((x1 - x0) * (stamp - first) / max(1, last - first))
        y = bottom - round((bottom - top) * kw / ceiling)
        if points:
            prev_t, prev_x, prev_y = points[-1]
            if stamp - prev_t <= 3 * POLL_SECONDS:
                steps = max(x - prev_x, abs(y - prev_y))
                for j in range(1, steps):
                    t = j / steps
                    put(win, round(lerp(prev_y, y, t)), round(lerp(prev_x, x, t)),
                        "-" if y == prev_y else ".", attrs["reflection"])
        points.append((stamp, x, y))
    for _, x, y in points:
        put(win, y, x, "*", attrs["reflection"] | curses.A_BOLD)
    put(win, bottom + 1, x0, clock_label(first))
    if last > first and x1 - x0 >= 12:
        put(win, bottom + 1, x1 - 4, clock_label(last))


def charge_panel(win, feed, attrs):
    h, w = win.getmaxyx()
    g = feed.data.get("garage", {})
    label, power, eta = charge_readings(feed)
    target = feed.data.get("target", {}).get("percent")
    put(win, 2, 1, "BATTERY   " + value(g.get("soc"), "%") + "     Target "
        + value(target, "%"), curses.A_BOLD)
    width = min(100, w - 4)
    bar = ["-"] * width
    if g.get("soc") is not None:
        filled = round(width * g["soc"] / 100)
        bar[:filled] = ["="] * filled
    if target is not None:
        bar[round((width - 1) * target / 100)] = "|"
    put(win, 3, 1, "[" + "".join(bar) + "]", attrs["palace-beige"])

    meter = "   ".join(value(power[k], unit, decimals)
                       for k, unit, decimals in (("kw", " kW", 1), ("volts", " V", 0), ("amps", " A", 1))
                       if power.get(k) is not None)
    put(win, 5, 1, label + ("    " + meter if meter else ""), curses.A_BOLD)
    if label == "Charging":
        put(win, 6, 1, "Time to full  " + (duration(eta * 60) if eta is not None else "No estimate from car"))
    if h >= 16:
        power_chart(win, feed.history, attrs)
    stamp = feed.data.get("power", {}).get("stamp")
    put(win, h - 3, 1, "Power: " + freshness(stamp) + "   Battery: " + freshness(g.get("stamp")), attrs["rim"])


def map_bounds(trip):
    lat, lon = trip["origin"]
    cosine = max(0.01, math.cos(math.radians(lat)))
    xs, ys, _ = zip(*trip["points"])
    # Rounded map areas can serve several nearby drives. Only the box is sent.
    margin = max(.006, (max(ys)-min(ys)) * .35, (max(xs)-min(xs)) * .35)
    south = max(-90, math.floor((lat + min(ys) - margin) * 50) / 50)
    north = min(90, math.ceil((lat + max(ys) + margin) * 50) / 50)
    west = math.floor((lon + (min(xs) - margin) / cosine) * 50) / 50
    east = math.ceil((lon + (max(xs) + margin) / cosine) * 50) / 50
    return south, west, north, east


def varint(data, offset):
    value = 0
    for shift in range(0, 70, 7):
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError("Invalid vector tile integer")


def packed(data):
    offset = 0
    while offset < len(data):
        value, offset = varint(data, offset)
        yield value


def fields(data):
    """Read the protobuf wire types used in Mapbox vector tiles."""
    offset = 0
    while offset < len(data):
        tag, offset = varint(data, offset)
        kind = tag & 7
        if kind == 0:
            value, offset = varint(data, offset)
        else:
            if kind == 2:
                size, offset = varint(data, offset)
            else:
                size = {1: 8, 5: 4}[kind]
            end = offset + size
            if end > len(data):
                raise ValueError("Incomplete vector tile")
            value = data[offset:end]
            offset = end
        yield tag >> 3, value


def tile_lines(geometry, z, x, y, extent):
    commands = iter(packed(geometry))
    u = v = 0
    path = []
    for cmd in commands:
        op = cmd & 7
        if op not in (1, 2):
            raise ValueError("Invalid line command")
        for _ in range(cmd >> 3):
            if op == 1 and path:
                yield path
                path = []
            du, dv = next(commands, None), next(commands, None)
            if du is None or dv is None:
                raise ValueError("Incomplete geometry")
            u += (du >> 1) ^ -(du & 1)
            v += (dv >> 1) ^ -(dv & 1)
            lon = (x + u/extent) / (2**z) * 360 - 180
            my = 1 - 2*(y + v/extent) / (2**z)
            path.append((math.degrees(math.atan(math.sinh(math.pi*my))), lon))
    if path:
        yield path


def read_map_url(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Zeekr-X-Terminal/1.0"})
    with urllib.request.urlopen(request, timeout=10) as response:
        data = response.read(4_000_001)
    if len(data) > 4_000_000:
        raise ValueError("Oversized map response")
    if data.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            data = stream.read(4_000_001)
        if len(data) > 4_000_000:
            raise ValueError("Oversized tile")
    return data


@functools.lru_cache(maxsize=1)
def tile_template():
    return json.loads(read_map_url("https://tiles.openfreemap.org/planet"))["tiles"][0]


@functools.lru_cache(maxsize=12)
def street_tile(z, x, y):
    data = read_map_url(tile_template().format(z=z, x=x, y=y))
    roads = []
    for tag, layer in fields(data):
        if tag != 3:
            continue
        props = collections.defaultdict(list)
        for field, value in fields(layer):
            props[field].append(value)
        if props[1][0] not in (b"transportation", b"transportation_name"):
            continue
        keys = props[3]
        values = [dict(fields(raw)).get(1, b"") for raw in props[4]]
        for raw in props[2]:
            feature = dict(fields(raw))
            if feature.get(3) != 2:
                continue
            ids = iter(packed(feature.get(2, b"")))
            tags = {keys[k]: values[v] for k,v in zip(ids, ids)}
            if tags.get(b"class") not in (b"motorway", b"trunk", b"primary", b"secondary",
                                           b"tertiary", b"minor", b"service", b"residential"):
                continue
            name = tags.get(b"name", b"").decode("utf8", "replace")
            name = "".join(c for c in name if c.isascii() and c.isprintable())[:36]
            for path in tile_lines(feature[4], z, x, y, (props[5] or [4096])[0]):
                roads.append((name, path))
    return roads


def read_streets(bounds):
    south, west, north, east = bounds
    def tile(lat, lon, n):
        lat = math.radians(max(-85.05, min(85.05, lat)))
        x = math.floor((lon + 180)/360*n)
        y = math.floor((1-math.asinh(math.tan(lat))/math.pi)*n/2)
        return x, min(n-1,max(0,y))

    if east - west > 360:
        return [], "Street map unavailable at this scale"
    for z in range(14, -1, -1):
        n = 2**z
        left, top = tile(north, west, n)
        right, bottom = tile(south, east, n)
        if (right-left+1)*(bottom-top+1) <= 6:
            break
    roads = []
    for x in range(left, right+1):
        for y in range(top, bottom+1):
            roads.extend(street_tile(z, x % n, y))
    return roads, ("© OpenStreetMap contributors · OpenMapTiles · OpenFreeMap · M hide"
                   if roads else "No mapped streets at this scale · M hide")


class StreetMaps:
    def __init__(self):
        self.enabled = False
        self.cache = collections.OrderedDict()
        self.inflight = None
        self.results = queue.Queue()

    def fetch(self, bounds):
        try:
            entry = read_streets(bounds)
        except (OSError, KeyError, ValueError, TypeError, IndexError, EOFError, ArithmeticError, http.client.HTTPException):
            entry = [], "Street map unavailable · M retry"
        self.results.put((bounds, entry))

    def layer(self, bounds):
        if not self.results.empty():
            key, entry = self.results.get_nowait()
            self.cache[key] = entry
            if len(self.cache) > 6:
                self.cache.popitem(last=False)
            self.inflight = None
        if bounds in self.cache:
            return self.cache[bounds]
        if self.inflight is None:
            self.inflight = bounds
            threading.Thread(target=self.fetch, args=(bounds,), daemon=True).start()
        return None, "Loading streets…"


def street_rows(roads, origin, project, width, height, route):
    lat0, lon0 = origin
    cosine = math.cos(math.radians(lat0))
    grid = [[" "] * width for _ in range(height)]
    labels = []
    for name, points in roads:
        path = [project((lon-lon0)*cosine, lat-lat0) for lat, lon in points]
        best = None
        for (x, y), (u, v) in zip(path, path[1:]):
            dx, dy = u-x, v-y
            # Clip before sampling, including segments with both ends off screen.
            lo, hi = 0., 1.
            for start, delta, limit in ((x, dx, width-1), (y, dy, height-1)):
                if delta:
                    a, b = sorted((-start/delta, (limit-start)/delta))
                    lo, hi = max(lo, a), min(hi, b)
                elif not 0 <= start <= limit:
                    hi = -1.
            if hi < lo:
                continue
            n = max(1, math.ceil((hi-lo)*max(abs(dx),abs(dy))))
            char = "-" if abs(dx)>2*abs(dy) else "|" if abs(dy)>2*abs(dx) else "\\" if dx*dy>0 else "/"
            for j in range(n+1):
                t = lerp(lo, hi, j/n)
                col, row = round(x+t*dx), round(y+t*dy)
                grid[row][col] = char
            mid = (lo+hi)/2
            if best is None or n > best[0]:
                best = n, round(x+mid*dx), round(y+mid*dy)
        if best and name:
            labels.append((*best,name))
    used = set()
    occupied = set(route)
    for _, x, y, name in sorted(labels, reverse=True):
        if len(used) >= max(2,width*height//180) or name in used:
            continue
        x += 1
        if x+len(name) >= width or any((i,y) in occupied for i in range(x,x+len(name))):
            continue
        grid[y][x:x+len(name)] = name
        occupied.update((i,j) for i in range(x-2,x+len(name)+2) for j in range(y-2,y+3))
        used.add(name)
    return ["".join(row).rstrip() for row in grid]


class RoutePlot:
    def __init__(self, points, width, height, aspect, roads=None, origin=None):
        self.steps = []
        self.times = []
        xs, ys, _ = zip(*points)
        west, north = min(xs), max(ys)
        dx, dy = max(xs) - west, north - min(ys)
        scale = min(
            (width - 1) / dx if dx else math.inf, (height - 1) * aspect / dy if dy else math.inf
        )
        scale = min(scale * .86, width / .004)
        ox = (width - 1 - dx * scale) / 2
        oy = (height - 1 - dy * scale / aspect) / 2
        projected = [
            (ox + (x - west) * scale, oy + (north - y) * scale / aspect, t) for x, y, t in points
        ]
        grid = [[" "] * width for _ in range(height)]
        for (a, b, t0), (c, d, t1) in zip(projected, projected[1:]):
            n = max(1, math.ceil(max(abs(c - a), abs(d - b))))
            for j in range(n + 1):
                t = j / n
                x = min(width - 1, max(0, round(lerp(a, c, t))))
                y = min(height - 1, max(0, round(lerp(b, d, t))))
                if not self.steps or (x, y) != self.steps[-1]:
                    self.steps.append((x, y))
                    self.times.append(lerp(t0, t1, t))
                    grid[y][x] = "."
        self.rows = ["".join(r).rstrip() for r in grid]
        def project(x, y):
            return ox + (x-west)*scale, oy + (north-y)*scale/aspect
        self.background = street_rows(roads, origin, project, width, height, self.steps) if roads else []


    def draw(self, win, progress, attrs):
        if self.background:
            for y, row in enumerate(self.background):
                put(win, 6+y, 2, row, attrs["rim"] | curses.A_DIM)
            for x, y in self.steps:
                put(win, 6+y, 2+x, "*", attrs["reflection"] | curses.A_BOLD)
        else:
            for y, row in enumerate(self.rows):
                put(win, 6 + y, 2, row, attrs["reflection"] | curses.A_BOLD)
        i = max(0, bisect.bisect_right(self.times, progress) - 1)
        for x, y in self.steps[:i]:
            put(win, 6 + y, 2 + x, "+", attrs["palace-beige"])
        for (x, y), marker in ((self.steps[-1], "E"), (self.steps[0], "S")):
            put(win, 6 + y, 2 + x, marker, curses.A_BOLD)
        x, y = self.steps[i]
        put(win, 6 + y, 2 + x, "@", attrs["palace-beige"] | curses.A_REVERSE)


def trip_panel(win, trip, plot, progress, pending, attrs, error="", index=0):
    h, w = win.getmaxyx()
    if not trip or trip.get("empty"):
        if error and not pending:
            put(win, 2, 1, "Last drive could not refresh", curses.A_BOLD)
            for i, line in enumerate(textwrap.wrap(error, width=w-3)):
                put(win, 4+i, 1, line)
            return
        if trip and trip.get("empty"):
            message = "No uploaded journeys in the past 90 days."
        else:
            message = "Reading your latest drive..." if pending else "Journey data unavailable."
        put(win, 3, 1, message)
        return
    put(
        win,
        2,
        1,
        ("LAST DRIVE   " if index == 0 else f"{index} BACK   ")
        + clock_label(trip["start"], "%a %d %b %Y, %H:%M")
        + " - "
        + clock_label(trip["end"]),
        curses.A_BOLD,
    )
    put(
        win,
        3,
        1,
        "    ".join(
            (
                value(trip["km"], " km", 1),
                duration(trip["seconds"]),
                value(trip["efficiency"], " kWh/100 km", 1),
            )
        ),
    )
    if plot:
        plot.draw(win, progress, attrs)
        put(
            win,
            h - 3,
            1,
            "N up    S start   E end   @ replay    " + value(progress * 100, "%"),
            attrs["rim"],
        )
    else:
        put(
            win,
            6,
            1,
            (
                "Route read failed. Press R to retry."
                if trip["track_error"]
                else "No usable GPS trace for this drive."
            ),
        )


def alert_for(feed, tab):
    if tab == "t":
        return feed.errors.get("trip", "")
    keys = ("vehicle", "garage", "power", "target") if tab == "c" else ("vehicle", "garage")
    return "  ".join(feed.errors[k] for k in keys if k in feed.errors)


def animate(win, roof, feed):
    try:
        curses.curs_set(0)
    except curses.error:
        pass  # Some terminfo entries do not support cursor visibility.
    win.keypad(True)
    win.leaveok(True)
    if hasattr(curses, "set_escdelay"):
        curses.set_escdelay(60)
    mesh, palette, attrs = build_mesh(roof), Palette(), colours(roof)
    if attrs["pearl"]:
        win.bkgd(" ", attrs["pearl"])
    angle = math.radians(45)
    last = time.monotonic()
    paused = False
    dirty = True
    old_size = None
    tab = "g"
    paused_tabs = {"g": False, "c": False, "t": False}
    marked = ()
    progress = 0.0
    plot = plot_key = None
    last_trip = bounds = None
    maps = StreetMaps()
    last_draw_second = -1
    while True:
        now = time.monotonic()
        dirty = feed.update() or dirty
        if not paused:
            if tab == "g":
                angle = (angle + (now - last) * TAU / PERIOD) % TAU
            elif tab == "t":
                progress = (progress + (now - last) / 20) % 1
        last = now
        size = win.getmaxyx()
        if (
            dirty
            or (not paused and tab != "c")
            or size != old_size
            or int(now) != last_draw_second
        ):
            rows, cols = size
            if size != old_size:
                aspect = terminal_aspect()
            try:
                win.erase()
                if rows >= 13 and cols >= 26:
                    tab_bar(win, tab, attrs)
                    if tab == "g":
                        opened = tuple(
                            k for k, v in feed.data.get("garage", {}).get("openings", {}).items() if v
                        )
                        if opened != marked:
                            mesh = build_mesh(roof)
                            mark_openings(mesh, opened)
                            marked = opened
                        height = rows - 7
                        pixels = render(mesh, palette, cols, height, angle, aspect)
                        paint_car(win, pixels, cols, height, palette, attrs, top=2)
                        garage_panel(win, feed, attrs, rows)
                    elif tab == "c":
                        charge_panel(win, feed, attrs)
                    else:
                        trip = feed.data.get("trip")
                        if trip is not last_trip:
                            progress = 0
                            last_trip = trip
                            plot_key = None
                            bounds = map_bounds(trip) if trip and trip.get("points") else None
                        roads, caption = None, "M street map · shares map area with OpenFreeMap"
                        if bounds and maps.enabled:
                            roads, caption = maps.layer(bounds)
                        geometry_key = size, id(roads)
                        if plot_key != geometry_key:
                            pts = trip.get("points") if trip else None
                            plot = RoutePlot(pts, cols - 4, rows - 10, aspect, roads, trip["origin"]) if pts else None
                            plot_key = geometry_key
                        trip_panel(win, trip, plot, progress, "trip" in feed.pending, attrs, feed.errors.get("trip"), feed.drive_index)
                        if bounds:
                            put(win, 4, 1, caption)
                        if feed.drives:
                            older = "[ older" if feed.cursor or feed.drive_index + 1 < len(feed.drives) or "trip" in feed.pending else "Oldest available"
                            newer = "   ] newer" if feed.drive_index else ""
                            put(win, 1, 1, older + newer)
                    error = alert_for(feed, tab)
                    if tab == "t" and (
                        not feed.data.get("trip") or feed.data["trip"].get("empty")
                    ):
                        error = ""
                    group = "trip" if tab == "t" else "vehicle"
                    if feed.restarting or feed.restart_waiting:
                        status = "Restarting main session..."
                    elif error:
                        status = error
                    elif group in feed.pending:
                        status = "Refreshing..."
                    elif now < feed.connected_until:
                        status = "Main session connected."
                    elif tab == "c":
                        remaining = max(0, math.ceil(feed.next_poll - now))
                        status = f"Next check in {remaining}s"
                    else:
                        status = ""
                    put(
                        win,
                        rows - 2,
                        1,
                        status,
                        attrs["open"] if error else 0,
                    )
                    motion = "Space play" if paused else "Space pause"
                    move = "   Arrows " + ("scrub" if tab == "t" else "turn") if paused else ""
                    controls = "S restart session   R refresh   Q quit" if cols >= 40 else "S restart  R read  Q quit"
                    if tab == "t" and feed.drive_index:
                        controls = controls.replace("R refresh", "R latest").replace("R read", "R latest")
                    if tab != "c":
                        controls += "   " + motion + move
                    put(win, rows - 1, 1, controls)
                else:
                    if cols > 1:
                        win.addnstr(0, 0, "Enlarge terminal", cols - 1)
                win.noutrefresh()
                curses.doupdate()
            except curses.error:
                if size == win.getmaxyx():
                    raise
            dirty = False
            old_size = size
            last_draw_second = int(now)
        delay = -1 if paused else max(0, math.ceil(1000 * (now + 1 / FPS - time.monotonic())))
        if paused or tab == "c":
            delay = 150
        win.timeout(delay)
        key = win.getch()
        if key in (ord("q"), ord("Q"), 3):
            return
        choice = chr(key).lower() if 0 <= key < 128 else ""
        if choice in ("g", "c", "t"):
            paused_tabs[tab] = paused
            tab = choice
            paused = paused_tabs[tab]
            if tab == "t" and "trip" not in feed.data and "trip" not in feed.errors:
                feed.request("trip")
            dirty = True
        elif tab == "t" and choice == "m" and bounds:
            entry = maps.cache.get(bounds)
            if maps.enabled and entry and "retry" in entry[1]:
                del maps.cache[bounds]
            else:
                maps.enabled = not maps.enabled
            dirty = True
        elif tab == "t" and choice in ("[", "]"):
            feed.browse(1 if choice == "[" else -1)
            dirty = True
        elif choice in ("r", "s"):
            feed.request("trip" if tab == "t" else "vehicle", restart=choice == "s")
            dirty = True
        elif key == ord(" ") and tab != "c":
            paused = not paused
            last = time.monotonic()
            dirty = True
        elif paused and key in (curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_UP, curses.KEY_DOWN):
            direction = 1 if key in (curses.KEY_RIGHT, curses.KEY_UP) else -1
            if tab == "t":
                progress = min(1, max(0, progress + direction * 0.05))
            elif tab == "g":
                angle = (angle + direction * STEP) % TAU
            dirty = True
        elif key == curses.KEY_RESIZE:
            dirty = True


def main():
    parser = argparse.ArgumentParser(prog="zeekr-x", description="Open the live Zeekr X garage, charge watch and last-drive replay.")
    parser.add_argument(
        "--roof",
        choices=("palace-beige", "black"),
        default="palace-beige",
        help="roof paint around the glass sunroof (default: Palace Beige RWD)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="open the live dashboard (default)",
    )
    parser.epilog = ("Street maps use OpenFreeMap and OpenMapTiles. Map data © OpenStreetMap "
                     "contributors, ODbL: https://www.openstreetmap.org/copyright")
    parser.add_argument("--api-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.api_worker:
        try:
            api_worker()
        except (KeyboardInterrupt, BrokenPipeError):
            pass
        return 0
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        sys.exit("Run {} in an interactive terminal.".format(parser.prog))
    feed = LiveFeed()
    try:
        curses.wrapper(animate, args.roof, feed)
    except KeyboardInterrupt:
        pass
    finally:
        feed.close()


if __name__ == "__main__":
    sys.exit(main())
