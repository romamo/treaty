"""Registration-time scan of the calls a handler makes on its ``ctx`` parameter.

Handlers are plain functions, so the only place to find ``ctx.run("git log")`` or an
undeclared ``ctx.open_url`` before the command ever runs is its source. A handler
without source (a REPL, ``exec``, a C extension) is not scanned; the same checks run
again when the call happens.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CtxCall:
    method: str
    line: int
    """Line in the handler's source, 1 for the ``def`` or its first decorator"""
    shell: bool
    """A shell string where ``ctx.run`` or ``ctx.pipeline`` takes an argument list"""
    fields: tuple[str, ...] = ()
    """The ``args.<field>`` reads among the call's arguments"""


def ctx_calls(fn: Callable[..., object]) -> list[CtxCall]:
    """Every ``<ctx>.<method>(...)`` call in the handler, ``<ctx>`` its second parameter"""
    params = list(inspect.signature(fn).parameters)
    if len(params) < 2:
        return []
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except OSError, TypeError:
        return []
    calls: list[CtxCall] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == params[1]
        ):
            continue
        method = node.func.attr
        shell = False
        if method in ("run", "pipeline") and node.args:
            first = node.args[0]
            stages = first.elts if method == "pipeline" and isinstance(first, ast.List) else [first]
            shell = any(_text(stage) for stage in stages)
        fields = tuple(
            dict.fromkeys(
                n.attr
                for arg in (*node.args, *(k.value for k in node.keywords))
                for n in ast.walk(arg)
                if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name)
                and n.value.id == params[0]
            )
        )
        calls.append(CtxCall(method, node.lineno, shell, fields))
    return calls


def _text(node: ast.expr) -> bool:
    """A string literal, an f-string, or a concatenation or ``%`` format of one"""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp):
        return _text(node.left) or _text(node.right)
    return False
