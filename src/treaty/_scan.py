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
    literal: str | None = None
    """The first argument, when it is a string literal"""


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
        first_arg = node.args[0] if node.args else None
        literal = (
            first_arg.value
            if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str)
            else None
        )
        calls.append(CtxCall(method, node.lineno, shell, fields, literal))
    return calls


_SHELL_CALLS = frozenset(
    {"os.system", "os.popen", "system", "popen", "getoutput", "getstatusoutput"}
)
# Calls whose shell= keyword starts a shell: subprocess's and its lookalikes
_SHELL_KEYWORD = frozenset({"run", "call", "check_call", "check_output", "Popen"})


@dataclass(frozen=True, slots=True)
class ShellCall:
    name: str
    """The call as written, such as ``os.system`` or ``subprocess.run``"""
    line: int


def shell_calls(fn: Callable[..., object]) -> list[ShellCall]:
    """Calls in the handler's source that hand a string to a shell (REQ-F-044, REQ-C-019):
    ``os.system``, ``os.popen``, ``subprocess.getoutput``, and ``subprocess.run``,
    ``Popen``, and the rest with a ``shell=`` that is not a false constant"""
    tree = _tree(fn)
    if tree is None:
        return []
    found: list[ShellCall] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = dotted(node.func)
        if name is None:
            continue
        last = name.rpartition(".")[2]
        shell = last in _SHELL_KEYWORD and any(
            k.arg == "shell" and not (isinstance(k.value, ast.Constant) and not k.value.value)
            for k in node.keywords
        )
        if name in _SHELL_CALLS or last in ("getoutput", "getstatusoutput") or shell:
            found.append(ShellCall(name, node.lineno))
    return sorted(found, key=lambda c: c.line)


def dotted(node: ast.expr) -> str | None:
    """``a.b.c`` for a name or attribute chain, else None"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return None if base is None else f"{base}.{node.attr}"
    return None


def _tree(fn: Callable[..., object]) -> ast.AST | None:
    try:
        return ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except OSError, TypeError:
        return None  # no source to scan (REPL, exec, C extension)


def _text(node: ast.expr) -> bool:
    """A string literal, an f-string, or a concatenation or ``%`` format of one"""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp):
        return _text(node.left) or _text(node.right)
    return False
