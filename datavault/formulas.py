"""Spreadsheet-style formula fields for custom sections.

    price * quantity
    IF(status = "Owned", price, 0)
    DAYS_BETWEEN(purchased, TODAY())
    ROUND(total / people, 2)
    first & " " & last

Formulas are parsed with Python's `ast` module and evaluated by walking a
**whitelist** of node types — there is no eval(), no attribute access, no
subscripting, no comprehensions, so a formula can only do arithmetic, text and
date work on the record's own fields. Numbers use Decimal so money math is
exact. Blank inputs propagate as blank (like SQL NULL); runtime errors such as
division by zero also produce a blank rather than failing the save.
"""
from __future__ import annotations

import ast
import math
import re
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MAX_EXPR_LEN = 500
MAX_NODES = 300
MAX_TEXT = 10_000
RESULT_TYPES = ("number", "money", "text", "date", "boolean")


class FormulaError(ValueError):
    """Invalid formula (reported when the field is defined)."""


class _Blank(Exception):
    """Internal: evaluation produced no value."""


# ------------------------------------------------------------- functions

def _num(x):
    if x is None:
        raise _Blank
    if isinstance(x, bool):
        return Decimal(int(x))
    if isinstance(x, Decimal):
        return x
    if isinstance(x, (int, float)):
        return Decimal(str(x))
    try:
        return Decimal(str(x).replace(",", "").replace("$", "").strip())
    except InvalidOperation:
        raise _Blank from None


def _text(x) -> str:
    if x is None:
        return ""
    if isinstance(x, bool):
        return "Yes" if x else "No"
    if isinstance(x, Decimal):
        return format(x.normalize(), "f") if x == x.to_integral() else format(x, "f").rstrip("0")
    if isinstance(x, date):
        return x.isoformat()
    return str(x)


def _date(x) -> date:
    if isinstance(x, date):
        return x
    if x is None:
        raise _Blank
    try:
        return date.fromisoformat(str(x)[:10])
    except ValueError:
        raise _Blank from None


def _round(x, n=0):
    n = int(_num(n))
    return _num(x).quantize(Decimal(1).scaleb(-n), rounding=ROUND_HALF_UP)


def _add_months(d, n):
    d, n = _date(d), int(_num(n))
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    return date(y, m + 1, min(d.day, monthrange(y, m + 1)[1]))


def _values(args):
    return [a for a in args if a is not None]


def _avg(*a):
    vals = [_num(x) for x in _values(a)]
    if not vals:
        raise _Blank
    return sum(vals) / len(vals)


def _minmax(fn):
    def f(*a):
        vals = _values(a)
        if not vals:
            raise _Blank
        if all(isinstance(v, date) for v in vals):
            return fn(vals)
        return fn(_num(v) for v in vals)
    return f


def _sqrt(x):
    x = _num(x)
    if x < 0:
        raise _Blank
    return x.sqrt()


# name -> (callable, min args, max args (None = variadic), description)
FUNCTIONS = {
    "ROUND": (_round, 1, 2, "ROUND(x, digits) — round half up"),
    "ABS": (lambda x: abs(_num(x)), 1, 1, "ABS(x)"),
    "CEIL": (lambda x: Decimal(math.ceil(_num(x))), 1, 1, "CEIL(x) — round up to an integer"),
    "FLOOR": (lambda x: Decimal(math.floor(_num(x))), 1, 1, "FLOOR(x) — round down to an integer"),
    "SQRT": (_sqrt, 1, 1, "SQRT(x)"),
    "MIN": (_minmax(min), 1, None, "MIN(a, b, …) — ignores blanks"),
    "MAX": (_minmax(max), 1, None, "MAX(a, b, …) — ignores blanks"),
    "SUM": (lambda *a: sum((_num(x) for x in _values(a)), Decimal(0)), 1, None, "SUM(a, b, …) — blanks count as 0"),
    "AVG": (_avg, 1, None, "AVG(a, b, …) — ignores blanks"),
    "PERCENT": (lambda a, b: _num(a) / _num(b) * 100, 2, 2, "PERCENT(part, whole)"),
    "LEN": (lambda s: Decimal(len(_text(s))), 1, 1, "LEN(text)"),
    "UPPER": (lambda s: _text(s).upper(), 1, 1, "UPPER(text)"),
    "LOWER": (lambda s: _text(s).lower(), 1, 1, "LOWER(text)"),
    "TRIM": (lambda s: _text(s).strip(), 1, 1, "TRIM(text)"),
    "LEFT": (lambda s, n: _text(s)[: max(0, int(_num(n)))], 2, 2, "LEFT(text, n)"),
    "RIGHT": (lambda s, n: _text(s)[-int(_num(n)):] if int(_num(n)) > 0 else "", 2, 2, "RIGHT(text, n)"),
    "CONTAINS": (lambda s, sub: _text(sub).lower() in _text(s).lower(), 2, 2, "CONTAINS(text, part) — case-insensitive"),
    "CONCAT": (lambda *a: "".join(_text(x) for x in a), 1, None, "CONCAT(a, b, …)"),
    "TEXT": (_text, 1, 1, "TEXT(x) — convert to text"),
    "NUMBER": (_num, 1, 1, "NUMBER(text) — convert to a number"),
    "TODAY": (date.today, 0, 0, "TODAY() — recalculated daily"),
    "DATE": (lambda y, m, d: date(int(_num(y)), int(_num(m)), int(_num(d))), 3, 3, "DATE(year, month, day)"),
    "YEAR": (lambda d: Decimal(_date(d).year), 1, 1, "YEAR(date)"),
    "MONTH": (lambda d: Decimal(_date(d).month), 1, 1, "MONTH(date)"),
    "DAY": (lambda d: Decimal(_date(d).day), 1, 1, "DAY(date)"),
    "DAYS_BETWEEN": (lambda a, b: Decimal((_date(b) - _date(a)).days), 2, 2, "DAYS_BETWEEN(start, end)"),
    "ADD_DAYS": (lambda d, n: _date(d) + timedelta(days=int(_num(n))), 2, 2, "ADD_DAYS(date, n)"),
    "ADD_MONTHS": (_add_months, 2, 2, "ADD_MONTHS(date, n) — clamps to month end"),
    "COALESCE": (lambda *a: next((x for x in a if x not in (None, "")), None), 1, None, "COALESCE(a, b, …) — first non-blank"),
    "ISBLANK": (lambda x: x in (None, ""), 1, 1, "ISBLANK(x)"),
}
# evaluated lazily so the branch not taken can't blank the result
SPECIAL = {
    "IF": "IF(condition, then, else)",
    "IFERROR": "IFERROR(x, fallback) — fallback when x is blank or errors",
    "AND": "AND(a, b, …)", "OR": "OR(a, b, …)", "NOT": "NOT(x)",
}
VOLATILE = {"TODAY"}

_ALLOWED = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.Call, ast.Name,
            ast.Constant, ast.Load, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
            ast.BitAnd, ast.BitXor, ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or, ast.Eq, ast.NotEq,
            ast.Lt, ast.LtE, ast.Gt, ast.GtE)


def _normalize(expr: str) -> str:
    """Accept spreadsheet habits: leading '=', '=' for equality, '<>' for
    not-equal. String literals are left untouched."""
    expr = expr.strip()
    if expr.startswith("="):
        expr = expr[1:]
    parts = re.split(r"(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')", expr)
    for i in range(0, len(parts), 2):  # even indexes are outside string literals
        p = parts[i].replace("<>", "!=")
        parts[i] = re.sub(r"(?<![=!<>])=(?!=)", "==", p)
    return "".join(parts)


@dataclass
class Formula:
    key: str
    expr: str
    result: str
    tree: ast.Expression
    refs: set[str] = field(default_factory=set)
    volatile: bool = False


def compile_formula(key: str, expr: str, result: str, known: set[str]) -> Formula:
    if not isinstance(expr, str) or not expr.strip():
        raise FormulaError("formula is empty")
    if len(expr) > MAX_EXPR_LEN:
        raise FormulaError(f"formula is longer than {MAX_EXPR_LEN} characters")
    if result not in RESULT_TYPES:
        raise FormulaError(f"result type must be one of {', '.join(RESULT_TYPES)}")
    try:
        tree = ast.parse(_normalize(expr), mode="eval")
    except SyntaxError as e:
        raise FormulaError(f"syntax error near column {e.offset or '?'}") from None
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_NODES:
        raise FormulaError("formula is too complex")
    refs, volatile = set(), False
    for node in nodes:
        if not isinstance(node, _ALLOWED):
            raise FormulaError(f"{type(node).__name__} is not allowed in formulas")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.keywords:
                raise FormulaError("only plain function calls like ROUND(x, 2) are allowed")
            name = node.func.id.upper()
            if name in SPECIAL:
                continue
            if name not in FUNCTIONS:
                raise FormulaError(f"unknown function {node.func.id}()")
            _, lo, hi, _ = FUNCTIONS[name]
            n = len(node.args)
            if n < lo or (hi is not None and n > hi):
                want = f"{lo}" if lo == hi else f"{lo}+" if hi is None else f"{lo}–{hi}"
                raise FormulaError(f"{name}() takes {want} argument(s), got {n}")
            volatile |= name in VOLATILE
        elif isinstance(node, ast.Constant) and not isinstance(node.value, (int, float, str, bool, type(None))):
            raise FormulaError("unsupported literal")
    for node in nodes:
        if isinstance(node, ast.Name) and not _is_func(tree, node):
            ref = node.id.lower()
            if ref in ("true", "false", "blank"):
                continue
            if ref == key:
                raise FormulaError("a formula can't refer to itself")
            if ref not in known:
                raise FormulaError(f"unknown field {node.id!r}")
            refs.add(ref)
    return Formula(key, expr, result, tree, refs, volatile)


def _is_func(tree, name_node) -> bool:
    return any(isinstance(n, ast.Call) and n.func is name_node for n in ast.walk(tree))


def order_formulas(formulas: list[Formula]) -> list[Formula]:
    """Topological sort so a formula can use another formula's result;
    raises on a cycle (a -> b -> a)."""
    by_key = {f.key: f for f in formulas}
    out, state = [], {}

    def visit(f, path):
        if state.get(f.key) == "done":
            return
        if state.get(f.key) == "active":
            raise FormulaError(f"circular reference: {' → '.join(path + [f.key])}")
        state[f.key] = "active"
        for r in sorted(f.refs):
            if r in by_key:
                visit(by_key[r], path + [f.key])
        state[f.key] = "done"
        out.append(f)

    for f in formulas:
        visit(f, [])
    return out


# ------------------------------------------------------------- evaluation

class _Evaluator:
    def __init__(self, env: dict):
        self.env = env

    def ev(self, n):
        meth = getattr(self, f"_{type(n).__name__}")
        return meth(n)

    def _Expression(self, n):
        return self.ev(n.body)

    def _Constant(self, n):
        v = n.value
        if isinstance(v, bool) or v is None or isinstance(v, str):
            return v
        return Decimal(str(v))

    def _Name(self, n):
        k = n.id.lower()
        if k == "true":
            return True
        if k == "false":
            return False
        if k == "blank":
            return None
        return self.env.get(k)

    def _UnaryOp(self, n):
        v = self.ev(n.operand)
        if isinstance(n.op, ast.Not):
            return not _truthy(v)
        if v is None:
            raise _Blank
        return -_num(v) if isinstance(n.op, ast.USub) else _num(v)

    def _BinOp(self, n):
        a, b = self.ev(n.left), self.ev(n.right)
        op = n.op
        if isinstance(op, ast.BitAnd):  # spreadsheet & = text concatenation
            s = _text(a) + _text(b)
            if len(s) > MAX_TEXT:
                raise _Blank
            return s
        if a is None or b is None:
            raise _Blank
        if isinstance(op, ast.Add):
            if isinstance(a, str) or isinstance(b, str):
                s = _text(a) + _text(b)
                if len(s) > MAX_TEXT:
                    raise _Blank
                return s
            if isinstance(a, date) and not isinstance(b, date):
                return a + timedelta(days=int(_num(b)))
            if isinstance(b, date) and not isinstance(a, date):
                return b + timedelta(days=int(_num(a)))
            return _num(a) + _num(b)
        if isinstance(op, ast.Sub):
            if isinstance(a, date) and isinstance(b, date):
                return Decimal((a - b).days)
            if isinstance(a, date):
                return a - timedelta(days=int(_num(b)))
            return _num(a) - _num(b)
        a, b = _num(a), _num(b)
        if isinstance(op, ast.Mult):
            return a * b
        if isinstance(op, (ast.Div, ast.FloorDiv, ast.Mod)):
            if b == 0:
                raise _Blank  # #DIV/0 -> blank
            return a / b if isinstance(op, ast.Div) else a // b if isinstance(op, ast.FloorDiv) else a % b
        if isinstance(op, (ast.Pow, ast.BitXor)):  # ^ is power, as in spreadsheets
            if abs(b) > 100 or abs(a) > Decimal(10) ** 12:
                raise _Blank
            return a ** b if b == b.to_integral() else Decimal(str(float(a) ** float(b)))
        raise _Blank

    def _BoolOp(self, n):
        if isinstance(n.op, ast.And):
            return all(_truthy(self.ev(v)) for v in n.values)
        return any(_truthy(self.ev(v)) for v in n.values)

    def _Compare(self, n):
        left = self.ev(n.left)
        for op, comp in zip(n.ops, n.comparators):
            right = self.ev(comp)
            if isinstance(op, (ast.Eq, ast.NotEq)):
                eq = _same(left, right)
                ok = eq if isinstance(op, ast.Eq) else not eq
            else:
                if left is None or right is None:
                    raise _Blank
                l, r = _comparable(left, right)
                ok = {ast.Lt: l < r, ast.LtE: l <= r, ast.Gt: l > r, ast.GtE: l >= r}[type(op)]
            if not ok:
                return False
            left = right
        return True

    def _IfExp(self, n):
        return self.ev(n.body) if _truthy(self.ev(n.test)) else self.ev(n.orelse)

    def _Call(self, n):
        name = n.func.id.upper()
        if name == "IF":
            if len(n.args) not in (2, 3):
                raise _Blank
            if _truthy(self.ev(n.args[0])):
                return self.ev(n.args[1])
            return self.ev(n.args[2]) if len(n.args) == 3 else None
        if name == "IFERROR":
            try:
                v = self.ev(n.args[0])
                if v is None:
                    raise _Blank
                return v
            except (_Blank, ArithmeticError, ValueError, OverflowError):
                return self.ev(n.args[1]) if len(n.args) > 1 else None
        if name == "AND":
            return all(_truthy(self.ev(a)) for a in n.args)
        if name == "OR":
            return any(_truthy(self.ev(a)) for a in n.args)
        if name == "NOT":
            return not _truthy(self.ev(n.args[0])) if n.args else None
        fn = FUNCTIONS[name][0]
        return fn(*[self.ev(a) for a in n.args])


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "no")
    return bool(v)


def _same(a, b) -> bool:
    if a is None or b is None:
        return a in (None, "") and b in (None, "")
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()  # spreadsheet-style, case-insensitive
    try:
        l, r = _comparable(a, b)
        return l == r
    except _Blank:
        return False


def _comparable(a, b):
    if isinstance(a, date) or isinstance(b, date):
        return _date(a), _date(b)
    if isinstance(a, str) and isinstance(b, str):
        return a.lower(), b.lower()
    return _num(a), _num(b)


# ------------------------------------------------------- record plumbing

def to_env(fields: list[dict], data: dict) -> dict:
    """Stored JSON values -> formula values (money cents -> Decimal dollars)."""
    env = {}
    for f in fields:
        v = data.get(f["key"])
        t = f["type"]
        if t == "formula":
            t = (f["options"] or {}).get("result", "number") if isinstance(f["options"], dict) else "number"
        if v is None:
            env[f["key"]] = None
        elif t == "money":
            env[f["key"]] = Decimal(int(v)) / 100
        elif t == "number":
            env[f["key"]] = _num(v)
        elif t == "date":
            try:
                env[f["key"]] = date.fromisoformat(v)
            except (TypeError, ValueError):
                env[f["key"]] = None
        elif t == "boolean":
            env[f["key"]] = bool(v)
        elif t in ("picture", "contact", "code"):
            env[f["key"]] = Decimal(int(v))
        else:
            env[f["key"]] = str(v)
    return env


def evaluate(formula: Formula, env: dict):
    """Evaluate and convert to the storable JSON value for the result type."""
    try:
        v = _Evaluator(env).ev(formula.tree)
        if v is None:
            return None
        r = formula.result
        if r == "money":
            return int(_num(v).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)
        if r == "number":
            d = _num(v)
            if not d.is_finite():
                return None
            return int(d) if d == d.to_integral() else float(round(d, 10))
        if r == "date":
            return _date(v).isoformat()
        if r == "boolean":
            return _truthy(v)
        s = _text(v)
        return s[:MAX_TEXT]
    except (_Blank, ArithmeticError, ValueError, TypeError, OverflowError, InvalidOperation, RecursionError):
        return None


def compile_section(fields: list[dict]) -> list[Formula]:
    """Compile every formula field of a section, in dependency order."""
    known = {f["key"] for f in fields}
    compiled = []
    for f in fields:
        if f["type"] != "formula":
            continue
        opts = f["options"] if isinstance(f["options"], dict) else {}
        try:
            compiled.append(compile_formula(f["key"], opts.get("expr", ""), opts.get("result", "number"), known))
        except FormulaError as e:
            raise FormulaError(f"{f['label']}: {e}") from None
    return order_formulas(compiled)


def apply(fields: list[dict], data: dict, compiled: list[Formula] | None = None) -> dict:
    """Return `data` with every formula field (re)computed."""
    compiled = compile_section(fields) if compiled is None else compiled
    if not compiled:
        return data
    out = dict(data)
    env = to_env(fields, out)
    by_key = {f["key"]: f for f in fields}
    for fm in compiled:
        out[fm.key] = evaluate(fm, env)
        env.update(to_env([by_key[fm.key]], out))  # later formulas see this result
    return out


def function_help() -> list[dict]:
    items = [{"name": k, "help": v[3]} for k, v in FUNCTIONS.items()]
    items += [{"name": k, "help": v} for k, v in SPECIAL.items()]
    return sorted(items, key=lambda x: x["name"])
