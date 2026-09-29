"""Registration-time scan of the calls a handler makes on its ``ctx`` parameter.

Handlers are plain functions, so the only place to find ``ctx.run("git log")`` or an
undeclared ``ctx.open_url`` before the command ever runs is its source. A handler
without source (a REPL, ``exec``, a C extension) is not scanned; the same checks run
again when the call happens.
"""

from __future__ import annotations

import ast
import functools
import inspect
import os
import subprocess
import sys
import textwrap
import types
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ArgvItem:
    """One element of a ``ctx.run([...])`` list literal"""

    literal: str | None = None
    """The element, when it is a string literal"""
    fields: tuple[str, ...] = ()
    """The ``args.<field>`` reads in it, such as ``args.target`` or ``*args.files``"""


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
    argv: tuple[ArgvItem, ...] | None = None
    """``ctx.run``'s argument list, when it is a list or tuple literal"""


@functools.cache
def source_tree(fn: Callable[..., object]) -> ast.Module | None:
    """The syntax tree of ``fn``'s source, lines numbered from its first; None without
    source (a REPL, ``exec``, a C extension), or when the source is not a whole statement,
    as for a lambda written inside a call. A nested function holding a multi-line string at
    column 0 cannot be dedented, so it is parsed inside an ``if`` block. Cached: the audit
    reads the same functions for several rules"""
    try:
        source = inspect.getsource(fn)
    except OSError, TypeError:
        return None
    try:
        return ast.parse(textwrap.dedent(source))
    except IndentationError:
        try:
            tree = ast.parse("if 1:\n" + source)
        except SyntaxError:
            return None
        return ast.increment_lineno(tree, -1)
    except SyntaxError:
        return None


def ctx_calls(fn: Callable[..., object]) -> list[CtxCall]:
    """Every ``<ctx>.<method>(...)`` call in the handler, ``<ctx>`` its second parameter"""
    params = list(inspect.signature(fn).parameters)
    tree = None if len(params) < 2 else source_tree(fn)
    if tree is None:
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
        fields = _reads(params[0], *node.args, *(k.value for k in node.keywords))
        first_arg = node.args[0] if node.args else None
        literal = _literal(first_arg)
        argv = None
        if method == "run" and isinstance(first_arg, (ast.List, ast.Tuple)):
            argv = tuple(ArgvItem(_literal(e), _reads(params[0], e)) for e in first_arg.elts)
        calls.append(CtxCall(method, node.lineno, shell, fields, literal, argv))
    return calls


def ctx_attribute(fn: Callable[..., object], name: str) -> int | None:
    """The first line of the handler reading ``<ctx>.<name>``, such as ``ctx.http``"""
    params = list(inspect.signature(fn).parameters)
    tree = None if len(params) < 2 else source_tree(fn)
    if tree is None:
        return None
    lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == name
        and isinstance(node.value, ast.Name)
        and node.value.id == params[1]
    ]
    return min(lines, default=None)


def _reads(args_name: str, *nodes: ast.expr) -> tuple[str, ...]:
    """The ``<args>.<field>`` reads under ``nodes``, each once, in order"""
    return tuple(
        dict.fromkeys(
            n.attr
            for node in nodes
            for n in ast.walk(node)
            if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name)
            and n.value.id == args_name
        )
    )


def _literal(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


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
    tree = source_tree(fn)
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


def _program_starters() -> tuple[object, ...]:
    """The functions and classes that start a program without ctx.run"""
    names = ("run", "call", "check_call", "check_output", "Popen")
    found: list[object] = [getattr(subprocess, n) for n in names]
    found += [
        getattr(os, n)
        for n in dir(os)
        if n.startswith(("exec", "spawn", "posix_spawn")) and callable(getattr(os, n))
    ]
    return tuple(found)


_STARTERS = _program_starters()


def _closure(fn: Callable[..., object] | type) -> dict[str, object]:
    """The names a handler defined inside a function sees from it, such as that function's
    imports; a cell not assigned yet, for a name bound later, holds nothing to resolve"""
    code = getattr(fn, "__code__", None)
    cells = getattr(fn, "__closure__", None) or ()
    if code is None:
        return {}
    found: dict[str, object] = {}
    for name, cell in zip(code.co_freevars, cells, strict=True):
        try:
            found[name] = cell.cell_contents
        except ValueError:
            # An empty cell: the enclosing function binds the name after this definition
            continue
    return found


def module_scope(fn: Callable[..., object] | type) -> dict[str, object]:
    """The globals ``fn`` sees: its own for a function, its module's for a class"""
    found = getattr(fn, "__globals__", None)
    if isinstance(found, dict):
        return found
    module = sys.modules.get(getattr(fn, "__module__", "") or "")
    return {} if module is None else vars(module)


def resolve_name(fn: Callable[..., object] | type, name: str) -> object:
    """What ``name`` refers to where ``fn`` is defined, as ``_resolve`` finds it"""
    return _resolve(fn, name)


def _resolve(fn: Callable[..., object] | type, name: str) -> object:
    """What ``name``, such as ``sp.run`` or ``run``, refers to in the handler's module; None
    when it is not a module-level name or goes through something other than a module"""
    root, *rest = name.split(".")
    scope: dict[str, object] = {**module_scope(fn), **_closure(fn)}
    target = scope.get(root)
    for part in rest:
        if not isinstance(target, types.ModuleType):
            return None
        target = getattr(target, part, None)
    return target


@functools.cache
def reached_functions(fn: Callable[..., object]) -> tuple[Callable[..., object], ...]:
    """The handler and every function of its own module it calls by a bare name,
    transitively: a fetch() helper beside the handler runs as part of it, while a function
    from another module is that module's to declare. A decorated helper is followed through
    ``__wrapped__``; methods, lambdas, and ``functools.partial`` are not. Each appears
    once, the handler first. Cached: four rules ask for the same command"""
    module = getattr(fn, "__module__", None)
    found: list[Callable[..., object]] = [fn]
    queue = [fn]
    while queue:
        current = queue.pop()
        tree = source_tree(current)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            found_name = _resolve(current, node.func.id)
            target = inspect.unwrap(found_name) if callable(found_name) else found_name
            if (
                isinstance(target, types.FunctionType)
                and target.__name__ != "<lambda>"
                and target.__module__ == module
                and all(target is not seen for seen in found)
            ):
                found.append(target)
                queue.append(target)
    return tuple(found)


def direct_subprocess_calls(fn: Callable[..., object]) -> list[ShellCall]:
    """Calls in the handler's source that start a program without ``ctx.run``:
    ``subprocess.run`` and its siblings, ``os.exec*``, ``os.spawn*``, and
    ``os.posix_spawn*``, however the handler's module imported them (``import subprocess as
    sp``, ``from subprocess import run``). A shell one is refused at registration already;
    this finds the argv lists. A call inside a helper the handler calls is not seen"""
    tree = source_tree(fn)
    if tree is None:
        return []
    found: list[ShellCall] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = dotted(node.func)
        if name is None:
            continue
        target = _resolve(fn, name)
        if any(target is starter for starter in _STARTERS):
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


def _text(node: ast.expr) -> bool:
    """A string literal, an f-string, or a concatenation or ``%`` format of one"""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp):
        return _text(node.left) or _text(node.right)
    return False
