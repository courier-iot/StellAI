# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
# ----------------------------------------------------------------------------- Server tools
# Tools Stella can call that run here on the server (the phone app forwards them).
# Calculator and electronics helpers are exact; diagrams are rendered to PNG.

import ast
import math
import operator
import os
import subprocess
import tempfile

SI_PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "": 1.0,
             "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "R": 1.0}
UNIT_WORDS = r"(?:Hz|ohms?|Ohms?|Ω|V|A|W|F|H|s)"
# 4.7k, 10uF, 4k7, 4R7, 100mA, 3.3V, 1kHz, 2.2µF, 47Ω
QUANTITY_RE = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)(?:([pnuµμmkKMGR])(\d+)?)?" + UNIT_WORDS + r"?(?![\w(.])"
)


def _quantity_sub(m):
    whole, prefix, frac = m.group(1), m.group(2) or "", m.group(3)
    if frac and "." not in whole and "e" not in whole.lower():
        whole = f"{whole}.{frac}"  # 4k7 -> 4.7k
    return repr(float(whole) * SI_PREFIX[prefix])


def parse_quantity(v):
    """Number, or a string like '4.7k', '10uF', '4k7', '100 mA', '3.3V'."""
    if isinstance(v, bool):
        raise ValueError("expected a number")
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(" ", "")
    if not s:
        raise ValueError("empty value")
    m = QUANTITY_RE.fullmatch(s)
    if m:
        return float(_quantity_sub(m))
    return float(s)


def eng(x, unit=""):
    """Engineering notation with SI prefix: 0.00047 -> '470 µ' + unit."""
    if x == 0 or not math.isfinite(x):
        return f"{x:g} {unit}".strip()
    exp3 = int(math.floor(math.log10(abs(x)) / 3) * 3)
    exp3 = max(-12, min(9, exp3))
    prefix = {-12: "p", -9: "n", -6: "µ", -3: "m", 0: "", 3: "k", 6: "M", 9: "G"}[exp3]
    return f"{x / 10 ** exp3:.4g} {prefix}{unit}".strip()


_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _parallel(*xs):
    return 1.0 / sum(1.0 / x for x in xs)


_FUNCS = {
    "sqrt": math.sqrt, "ln": math.log, "log": math.log, "log10": math.log10, "log2": math.log2,
    "exp": math.exp, "sin": math.sin, "cos": math.cos, "tan": math.tan, "asin": math.asin,
    "acos": math.acos, "atan": math.atan, "atan2": math.atan2, "sinh": math.sinh, "cosh": math.cosh,
    "tanh": math.tanh, "abs": abs, "round": round, "min": min, "max": max, "pow": math.pow,
    "ceil": math.ceil, "floor": math.floor, "hypot": math.hypot, "degrees": math.degrees,
    "radians": math.radians, "parallel": _parallel, "db": lambda r: 20 * math.log10(r),
    "db10": lambda r: 10 * math.log10(r),
}
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


def safe_eval(expr):
    """Evaluate a maths expression without running arbitrary code."""
    text = QUANTITY_RE.sub(_quantity_sub, expr.replace("^", "**").replace("×", "*").replace("÷", "/"))
    tree = ast.parse(text, mode="eval")

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 1000:
                raise ValueError("exponent too large")
            return _BIN[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Name) and node.id in _CONSTS:
            return _CONSTS[node.id]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
            return _FUNCS[node.func.id](*[ev(a) for a in node.args])
        raise ValueError(f"not allowed in a calculation: {ast.unparse(node) if hasattr(ast, 'unparse') else type(node).__name__}")

    result = ev(tree)
    if isinstance(result, complex):
        raise ValueError("result is a complex number")
    return float(result)


# E-series standard resistor values (one decade)
E_SERIES = {
    "E12": [1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2],
    "E24": [1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2.0, 2.2, 2.4, 2.7, 3.0, 3.3, 3.6, 3.9, 4.3, 4.7, 5.1,
            5.6, 6.2, 6.8, 7.5, 8.2, 9.1],
    "E96": [1.00, 1.02, 1.05, 1.07, 1.10, 1.13, 1.15, 1.18, 1.21, 1.24, 1.27, 1.30, 1.33, 1.37, 1.40,
            1.43, 1.47, 1.50, 1.54, 1.58, 1.62, 1.65, 1.69, 1.74, 1.78, 1.82, 1.87, 1.91, 1.96, 2.00,
            2.05, 2.10, 2.15, 2.21, 2.26, 2.32, 2.37, 2.43, 2.49, 2.55, 2.61, 2.67, 2.74, 2.80, 2.87,
            2.94, 3.01, 3.09, 3.16, 3.24, 3.32, 3.40, 3.48, 3.57, 3.65, 3.74, 3.83, 3.92, 4.02, 4.12,
            4.22, 4.32, 4.42, 4.53, 4.64, 4.75, 4.87, 4.99, 5.11, 5.23, 5.36, 5.49, 5.62, 5.76, 5.90,
            6.04, 6.19, 6.34, 6.49, 6.65, 6.81, 6.98, 7.15, 7.32, 7.50, 7.68, 7.87, 8.06, 8.25, 8.45,
            8.66, 8.87, 9.09, 9.31, 9.53, 9.76],
}


def _series_values(series):
    base = E_SERIES.get(series.upper())
    if not base:
        raise ValueError("series must be E12, E24 or E96")
    return [round(b * 10 ** d, 10) for d in range(-1, 8) for b in base]


def nearest_standard(value, series="E24"):
    vals = _series_values(series)
    below = max((v for v in vals if v <= value * (1 + 1e-9)), default=vals[0])
    above = min((v for v in vals if v >= value * (1 - 1e-9)), default=vals[-1])
    nearest = min((below, above), key=lambda v: abs(v - value))
    return below, nearest, above


def _need(args, keys, count):
    got = {k: parse_quantity(args[k]) for k in keys if args.get(k) not in (None, "")}
    if len(got) < count:
        raise ValueError(f"give at least {count} of: {', '.join(keys)}")
    return got


def tool_calculate(args):
    expr = str(args.get("expression", "")).strip()
    if not expr:
        raise ValueError("no expression")
    value = safe_eval(expr)
    unit = str(args.get("unit", "")).strip()
    return {"expression": expr, "value": value, "formatted": eng(value, unit)}, f"{expr} = {eng(value, unit)}"


def tool_ohms_law(args):
    g = _need(args, ["voltage", "current", "resistance", "power"], 2)
    v, i, r, p = g.get("voltage"), g.get("current"), g.get("resistance"), g.get("power")
    if v is not None and i is not None:
        r, p = v / i, v * i
    elif v is not None and r is not None:
        i = v / r; p = v * i
    elif v is not None and p is not None:
        i = p / v; r = v / i
    elif i is not None and r is not None:
        v = i * r; p = v * i
    elif i is not None and p is not None:
        v = p / i; r = v / i
    else:  # r and p
        v = math.sqrt(p * r); i = v / r
    res = {"voltage": v, "current": i, "resistance": r, "power": p}
    summary = f"{eng(v, 'V')}, {eng(i, 'A')}, {eng(r, 'Ω')}, {eng(p, 'W')}"
    return res, summary


def tool_voltage_divider(args):
    vin = parse_quantity(args["vin"]) if args.get("vin") not in (None, "") else None
    vout = parse_quantity(args["vout"]) if args.get("vout") not in (None, "") else None
    r1 = parse_quantity(args["r1"]) if args.get("r1") not in (None, "") else None
    r2 = parse_quantity(args["r2"]) if args.get("r2") not in (None, "") else None
    series = str(args.get("series") or "E24")
    known = sum(x is not None for x in (vin, vout, r1, r2))
    if vin is not None and r1 is not None and r2 is not None:
        vout = vin * r2 / (r1 + r2)
    elif vout is not None and r1 is not None and r2 is not None:
        vin = vout * (r1 + r2) / r2
    elif vin is not None and vout is not None and r1 is not None:
        r2 = r1 * vout / (vin - vout)
    elif vin is not None and vout is not None and r2 is not None:
        r1 = r2 * (vin - vout) / vout
    elif vin is not None and vout is not None and known == 2:
        # Suggest a standard-value pair near 10k total
        if not 0 < vout < vin:
            raise ValueError("vout must be between 0 and vin")
        best = None
        for a in _series_values(series):
            if not 1e3 <= a <= 1e6:
                continue
            _, b, _ = nearest_standard(a * vout / (vin - vout), series)
            err = abs(vin * b / (a + b) - vout)
            score = (round(err, 6), abs(math.log10((a + b) / 2e4)))
            if best is None or score < best[0]:
                best = (score, a, b)
        r1, r2 = best[1], best[2]
        actual = vin * r2 / (r1 + r2)
        res = {"vin": vin, "vout_target": vout, "r1": r1, "r2": r2, "vout_actual": actual,
               "error_percent": (actual - vout) / vout * 100, "series": series}
        return res, f"R1 = {eng(r1, 'Ω')}, R2 = {eng(r2, 'Ω')} → {eng(actual, 'V')} ({res['error_percent']:+.2f}%)"
    else:
        raise ValueError("give three of vin, vout, r1, r2 (or just vin and vout to get a suggested pair)")
    current = vin / (r1 + r2)
    res = {"vin": vin, "vout": vout, "r1": r1, "r2": r2, "current": current}
    return res, f"Vin {eng(vin, 'V')}, Vout {eng(vout, 'V')}, R1 {eng(r1, 'Ω')}, R2 {eng(r2, 'Ω')}, I {eng(current, 'A')}"


def tool_led_resistor(args):
    vs = parse_quantity(args["supply_voltage"])
    vf = parse_quantity(args["led_voltage"])
    n = int(args.get("leds_in_series") or 1)
    i = parse_quantity(args["current_ma"]) / 1000.0
    drop = vs - vf * n
    if drop <= 0:
        raise ValueError("supply voltage is too low for that many LEDs")
    r = drop / i
    _, _, e12_up = nearest_standard(r, "E12")
    _, _, e24_up = nearest_standard(r, "E24")
    actual_i = drop / e24_up
    p = actual_i ** 2 * e24_up
    rating = next(w for w in (0.125, 0.25, 0.5, 1, 2, 5, 10, 1e9) if w >= p * 2)
    res = {"exact_resistance": r, "e24_resistor": e24_up, "e12_resistor": e12_up,
           "actual_current": actual_i, "resistor_power": p, "suggested_rating_w": rating}
    return res, (f"{eng(r, 'Ω')} exact → use {eng(e24_up, 'Ω')} (E24) or {eng(e12_up, 'Ω')} (E12); "
                 f"{eng(actual_i, 'A')}, {eng(p, 'W')} → {rating:g} W resistor")


def tool_rc_filter(args):
    g = _need(args, ["resistance", "capacitance", "frequency"], 2)
    r, c, f = g.get("resistance"), g.get("capacitance"), g.get("frequency")
    if r is not None and c is not None:
        f = 1 / (2 * math.pi * r * c)
    elif r is not None and f is not None:
        c = 1 / (2 * math.pi * r * f)
    else:
        r = 1 / (2 * math.pi * c * f)
    tau = r * c
    res = {"resistance": r, "capacitance": c, "cutoff_frequency": f, "time_constant": tau}
    return res, f"R {eng(r, 'Ω')}, C {eng(c, 'F')}, fc {eng(f, 'Hz')}, τ {eng(tau, 's')}"


def tool_combine_resistors(args):
    vals = [parse_quantity(v) for v in (args.get("values") or [])]
    if len(vals) < 2:
        raise ValueError("give at least two values")
    mode = str(args.get("mode", "parallel")).lower()
    total = sum(vals) if mode == "series" else _parallel(*vals)
    return {"mode": mode, "total": total}, f"{mode}: {eng(total, 'Ω')}"


def tool_standard_resistor(args):
    value = parse_quantity(args["value"])
    series = str(args.get("series") or "E24")
    below, nearest, above = nearest_standard(value, series)
    res = {"value": value, "series": series, "nearest": nearest, "below": below, "above": above,
           "error_percent": (nearest - value) / value * 100}
    return res, f"{series}: nearest {eng(nearest, 'Ω')} ({res['error_percent']:+.1f}%), range {eng(below, 'Ω')}–{eng(above, 'Ω')}"


