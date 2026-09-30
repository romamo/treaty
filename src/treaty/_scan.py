"""Registration-time scan of the calls a handler makes on its ``ctx`` parameter.

Handlers are plain functions, so the only place to find ``ctx.run("git log")`` or an
undeclared ``ctx.open_url`` before the command ever runs is its source. A handler
without source (a REPL, ``exec``, a C extension) is not scanned; the same checks run
again when the call happens.
"""

from __future__ import annotations

import ast
import collections
import functools
import importlib.metadata
import importlib.util
import inspect
import json
import os
import pkgutil
import subprocess
import sys
import sysconfig
import textwrap
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ._errors import Exit
from ._types import signature


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


def source_tree(fn: Callable[..., object]) -> ast.Module | None:
    """The syntax tree of ``fn``'s source, lines numbered from its first; None without
    source (a REPL, ``exec``, a C extension), or when the source is not a whole statement,
    as for a lambda written inside a call. A nested function holding a multi-line string at
    column 0 cannot be dedented, so it is parsed inside an ``if`` block. Cached per
    function, since the audit reads the same ones for several rules; an unhashable
    callable, such as a doctor check instance, is parsed each time"""
    try:
        return _cached_tree(fn)
    except TypeError:
        return _tree(fn)


@functools.lru_cache(maxsize=4096)
def _cached_tree(fn: Callable[..., object]) -> ast.Module | None:
    return _tree(fn)


def _tree(fn: Callable[..., object]) -> ast.Module | None:
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
    params = list(signature(fn).parameters)
    tree = None if len(params) < 2 else source_tree(fn)
    if tree is None:
        return []
    carried = _carried(params[0], tree)
    copies = _copies(params[0], tree)

    def reads(*nodes: ast.expr) -> tuple[str, ...]:
        """The fields ``nodes`` read, directly or through a local that holds one"""
        through = (f for node in nodes for f in _through(node, carried, copies))
        return tuple(dict.fromkeys((*_reads(params[0], *nodes), *through)))

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
        fields = reads(*node.args, *(k.value for k in node.keywords))
        first_arg = node.args[0] if node.args else None
        literal = _literal(first_arg)
        argv = None
        if method == "run" and isinstance(first_arg, (ast.List, ast.Tuple)):
            argv = tuple(ArgvItem(_literal(e), reads(e)) for e in first_arg.elts)
        calls.append(CtxCall(method, node.lineno, shell, fields, literal, argv))
    return calls


def ctx_attribute(fn: Callable[..., object], name: str) -> int | None:
    """The first line of the handler reading ``<ctx>.<name>``, such as ``ctx.http``"""
    params = list(signature(fn).parameters)
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


def _carried(args_name: str, tree: ast.AST) -> dict[str, tuple[str, ...]]:
    """Each local the handler fills from an expression that reads a field, with the
    fields it carries: ``extra = list(args.extra)``, ``cmd.extend(args.extra)``,
    ``cmd[0] = args.ref``, ``(extra := args.extra)``, and ``with open(args.path) as f``
    all make the local carry the field, and a local built from such a local carries them
    on. A value from anywhere else carries none"""
    assigned: list[tuple[str, ast.expr]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            assigned += [(name, node.value) for t in targets for name in _bound(t)]
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            assigned += [(name, node.iter) for name in _bound(node.target)]
        elif isinstance(node, ast.NamedExpr):
            assigned.append((node.target.id, node.value))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            assigned += [
                (name, item.context_expr)
                for item in node.items
                if item.optional_vars is not None
                for name in _bound(item.optional_vars)
            ]
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _FILLS
        ):
            # cmd.append(x) and the rest put x into cmd, or into self.cmd; insert's first
            # argument is a position, not a value
            put = node.args[1:] if node.func.attr == "insert" else node.args
            assigned += [
                (name, arg)
                for name in _bound(node.func.value)
                for arg in (*put, *(k.value for k in node.keywords))
            ]
    copies = _copies(args_name, tree)
    carried: dict[str, tuple[str, ...]] = {}
    changed = True
    while changed:
        changed = False
        for name, value in assigned:
            through = _through(value, carried, copies)
            fields = tuple(
                dict.fromkeys((*carried.get(name, ()), *_reads(args_name, value), *through))
            )
            if fields != carried.get(name, ()):
                carried[name] = fields
                changed = True
    return {k: v for k, v in carried.items() if v}


_REPLACES = frozenset({"replace", "dataclasses.replace", "copy.replace"})


def _copies(args_name: str, tree: ast.AST) -> frozenset[str]:
    """Locals that hold the arguments object itself: every value bound to the name is
    ``args``, another copy, or ``replace(<one of those>, ...)``. A name also bound to
    anything else, such as ``load_settings(args)`` on one branch, is not a copy"""
    bound: dict[str, list[ast.expr]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    bound.setdefault(target.id, []).append(node.value)
                else:
                    for name in _bound(target):
                        bound.setdefault(name, []).append(ast.Constant(None))
        elif isinstance(node, ast.NamedExpr):
            bound.setdefault(node.target.id, []).append(node.value)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            for name in _bound(node.target):
                bound.setdefault(name, []).append(ast.Constant(None))

    def is_copy(value: ast.expr, copies: set[str]) -> bool:
        if isinstance(value, ast.Name):
            return value.id == args_name or value.id in copies
        return (
            isinstance(value, ast.Call)
            and dotted(value.func) in _REPLACES
            and bool(value.args)
            and is_copy(value.args[0], copies)
        )

    copies: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, values in bound.items():
            if name not in copies and all(is_copy(v, copies) for v in values):
                copies.add(name)
                changed = True
    return frozenset(copies)


def _through(
    node: ast.expr, carried: Mapping[str, tuple[str, ...]], copies: frozenset[str]
) -> list[str]:
    """The fields ``node`` reads through locals: what each local it names carries, and,
    for a copy of the arguments object (``clean = replace(args, ref=args.base)``), the field
    a ``clean.ref`` reads plus the fields replaced into the copy, rather than every field"""
    by_field = {
        id(n.value): n.attr
        for n in ast.walk(node)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in copies
    }
    found: list[str] = []
    for n in ast.walk(node):
        if not isinstance(n, ast.Name):
            continue
        if id(n) in by_field:
            found.append(by_field[id(n)])
            found.extend(f for f in carried.get(n.id, ()) if f != EVERY_FIELD)
        else:
            found.extend(carried.get(n.id, ()))
    return found


_FILLS = frozenset({"append", "extend", "insert", "add", "update", "setdefault"})


def _bound(target: ast.expr) -> list[str]:
    """The local names an assignment target binds: a name, each name of a tuple or list, or
    the base name of ``cmd[0]`` or ``opts.depth``, never a name only read inside it"""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _bound(element)]
    if isinstance(target, ast.Starred):
        return _bound(target.value)
    if isinstance(target, (ast.Subscript, ast.Attribute)):
        return _bound(target.value)
    return []


EVERY_FIELD = "*"
"""What an expression reads when it passes the arguments object whole, as ``flags(args)``
does: any field may reach the call"""


def _reads(args_name: str, *nodes: ast.expr) -> tuple[str, ...]:
    """The ``<args>.<field>`` reads under ``nodes``, each once, in order, and
    ``EVERY_FIELD`` when ``<args>`` itself is passed on rather than read by field"""
    found: dict[str, None] = {}
    for node in nodes:
        bases = {id(n.value) for n in ast.walk(node) if isinstance(n, ast.Attribute)}
        for n in ast.walk(node):
            if (
                isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name)
                and n.value.id == args_name
            ):
                found[n.attr] = None
            elif isinstance(n, ast.Name) and n.id == args_name and id(n) not in bases:
                found[EVERY_FIELD] = None
    return tuple(found)


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
    when it is not a module-level name or goes through something other than a module. A
    module's attributes are read statically, so a lazy module's ``__getattr__`` never runs"""
    root, *rest = name.split(".")
    scope: dict[str, object] = {**module_scope(fn), **_closure(fn)}
    target = scope.get(root)
    for part in rest:
        if not isinstance(target, types.ModuleType):
            return None
        try:
            target = inspect.getattr_static(target, part)
        except AttributeError:
            return None
    return target


def clear_caches() -> None:
    """Forget parsed sources and followed helpers, so a new audit sees the code as it is"""
    _cached_tree.cache_clear()
    _cached_reach.cache_clear()
    _shipped.cache_clear()
    _editable_roots.cache_clear()
    _ships.cache_clear()


def _unwrapped(target: object) -> object:
    """A decorated helper's function, through each ``__wrapped__`` that decorator set on it;
    an object that answers any attribute, such as a lazy proxy or a mock, is kept as is"""
    for _ in range(32):
        found = getattr(target, "__dict__", None)
        if not isinstance(found, dict) or "__wrapped__" not in found:
            return target
        target = found["__wrapped__"]
    return target


FOLLOW_DEPTH = 3
"""How many calls from the handler the audit follows into another first-party module"""


@dataclass(frozen=True, slots=True)
class Reached:
    """A function the audit reads for a command: the handler, or a helper it calls"""

    fn: Callable[..., object]
    via: tuple[str, ...] = ()
    """The helpers from the handler to ``fn``, each ``module.name (file:line)``; empty for
    the handler itself"""

    @property
    def where(self) -> str:
        """``; found via helpers.enter (helpers.py:6)``, or nothing for the handler"""
        return f"; found via {' -> '.join(self.via)}" if self.via else ""


def reached_functions(fn: Callable[..., object]) -> tuple[Reached, ...]:
    """The handler and the first-party functions it calls, each once, the handler first
    and then by distance. A function of the handler's own module is followed however deep,
    as a fetch() beside the handler runs as part of it; one in another first-party module
    within ``FOLLOW_DEPTH`` calls of the handler. First-party is every top-level package of
    the distribution that ships the handler's (``importlib.metadata`` maps it, for an
    editable install too), else the handler's own top-level package, or, for an app that is
    a top-level module no distribution ships, a file under its directory. treaty, the
    standard library, and other distributions are never followed.

    A call is followed by name (``fetch(...)``), through modules and classes
    (``helpers.enter(...)``, ``Store.load(...)``) read without running their code, and
    through a module the function imports itself; a decorated helper through the
    ``__wrapped__`` its decorator set. Methods of objects, lambdas, ``functools.partial``,
    and callbacks are not. Cached, since several rules ask for the same command; an
    unhashable callable is followed each time"""
    try:
        return _cached_reach(fn)
    except TypeError:
        return _reach(fn)


@functools.lru_cache(maxsize=4096)
def _cached_reach(fn: Callable[..., object]) -> tuple[Reached, ...]:
    return _reach(fn)


def _reach(fn: Callable[..., object]) -> tuple[Reached, ...]:
    module = getattr(fn, "__module__", None)
    home = _home(fn)
    found = [Reached(fn)]
    seen = {id(fn)}
    queue = collections.deque([(found[0], 0)])
    while queue:
        unit, depth = queue.popleft()
        for target in _callees(unit.fn):
            if id(target) in seen:
                continue
            if target.__module__ != module and (
                depth >= FOLLOW_DEPTH or home is None or not _first_party(target, home)
            ):
                continue
            seen.add(id(target))
            following = Reached(target, (*unit.via, _label(target, home)))
            found.append(following)
            queue.append((following, depth + 1))
    return tuple(found)


@dataclass(frozen=True, slots=True)
class _Home:
    packages: frozenset[str] | None
    """The top-level packages that are first-party: every one of the distribution that
    ships the handler's, else the handler's own; None for a top-level module that no
    distribution ships, whose first-party code is the files under ``root``"""
    root: Path
    """The directory that holds the package, or the module when it has none"""
    unread: tuple[str, ...] = ()
    """The installed distributions whose ``direct_url.json`` could not be read while
    looking for an editable install that puts ``root`` on ``sys.path``: skipped, so an
    editable install among them is not followed"""

    @property
    def names(self) -> str:
        """``apppkg, libpkg``, or ``files under /src/app`` for a top-level module"""
        if self.packages is None:
            return f"files under {self.root}"
        return ", ".join(sorted(self.packages))


_INSTALLED = frozenset({"site-packages", "dist-packages"})
_STDLIB = Path(sysconfig.get_paths()["stdlib"]).resolve()


def first_party(fn: Callable[..., object]) -> str | None:
    """What the audit follows from the handler ``fn`` besides its own module: its
    first-party packages, or the files under its directory; None for treaty's own"""
    home = _home(fn)
    return None if home is None else home.names


def unread_records(fn: Callable[..., object]) -> tuple[str, ...]:
    """The distributions the audit skipped for the handler ``fn`` because their
    ``direct_url.json`` is not a PEP 610 record"""
    home = _home(fn)
    return () if home is None else home.unread


def _home(fn: Callable[..., object]) -> _Home | None:
    """Where the handler's first-party code lives; None for treaty's own or when unknown"""
    module = sys.modules.get(getattr(fn, "__module__", None) or "")
    file = getattr(module, "__file__", None)
    if module is None or not isinstance(file, str) or module.__name__.startswith("treaty."):
        return None
    path = Path(file).resolve()
    if module.__name__ != "__main__" and ("." in module.__name__ or path.stem == "__init__"):
        top = module.__name__.partition(".")[0]
        top_file = getattr(sys.modules.get(top), "__file__", None)
        if isinstance(top_file, str):
            root = Path(top_file).resolve().parent.parent
        else:
            # A namespace package has no single directory: its modules are known by name,
            # and the handler's own portion of it names where a helper is
            root = path.parents[module.__name__.count(".") + (path.stem == "__init__")]
        packages, unread = _distribution_packages(top, root, path)
        return _Home(packages or frozenset({top}), root, unread)
    if module.__name__ != "__main__":
        packages, unread = _distribution_packages(module.__name__, None, path)
        return _Home(packages, path.parent, unread)
    return _Home(None, path.parent)


def _distribution_packages(
    top: str, root: Path | None, file: Path
) -> tuple[frozenset[str] | None, tuple[str, ...]]:
    """Every top-level package of the one distribution that ships ``top``, which the
    distribution's installed file list names, when the handler's ``file`` is that
    distribution's: a local ``top`` that shadows an installed one is not it. An editable
    install's list names only its ``.pth`` file, so for one whose ``.pth`` puts ``root`` on
    ``sys.path``, the packages in ``root``. None when no distribution, or several, ship
    ``top``, or when the one that does is not where the handler was loaded from. With
    them, the distributions skipped for an unreadable ``direct_url.json`` when no
    editable install was found"""
    shipped = _shipped()
    owners = shipped.get(top, ())
    if len(owners) == 1:
        if not _ships(owners[0], file):
            return None, ()
        packages = frozenset(name for name, dists in shipped.items() if owners[0] in dists)
        return packages - {"treaty"}, ()
    if owners or root is None:
        return None, ()
    editables = _editable_roots()
    if root not in editables.roots:
        return None, editables.unread
    beside = {m.name for m in pkgutil.iter_modules([str(root)]) if m.ispkg}
    return frozenset(beside | {top}) - {"treaty"}, ()


@functools.lru_cache(maxsize=1)
def _shipped() -> dict[str, tuple[str, ...]]:
    """Each top-level package to the distributions that ship it, read once per audit"""
    return {
        name: tuple(dict.fromkeys(dists))
        for name, dists in importlib.metadata.packages_distributions().items()
    }


@functools.lru_cache(maxsize=64)
def _ships(name: str, file: Path) -> bool:
    """Whether a distribution called ``name`` installed ``file``, a resolved path: its
    file list names it, or it is an editable install whose project directory, or a
    directory its ``.pth`` puts on ``sys.path``, holds it"""
    key = os.path.normcase(file.name)
    for dist in importlib.metadata.distributions(name=name):
        if any(
            os.path.normcase(f.name) == key and Path(str(dist.locate_file(f))).resolve() == file
            for f in dist.files or ()
        ):
            return True
        project = _editable_project(dist)
        if project is not None and any(
            file.is_relative_to(d) for d in (project, *_pth_roots(dist))
        ):
            return True
    return False


@dataclass(frozen=True, slots=True)
class _Editables:
    roots: frozenset[Path]
    """The directories the editable installs put on ``sys.path`` through a ``.pth``"""
    unread: tuple[str, ...]
    """The distributions skipped because their ``direct_url.json`` is not a PEP 610
    record"""


@functools.lru_cache(maxsize=1)
def _editable_roots() -> _Editables:
    """The directories the editable installs (PEP 610 ``dir_info.editable``) put on
    ``sys.path`` through a ``.pth`` file; an install that hooks the import system
    instead names none. Every installed distribution is read, so one whose record is
    malformed is skipped and named rather than stopping the audit"""
    found: set[Path] = set()
    unread: list[str] = []
    for dist in importlib.metadata.distributions():
        try:
            project = _read_project(dist)
        except _MalformedRecord:
            unread.append(dist.metadata["Name"])
            continue
        if project is not None:
            found.update(_pth_roots(dist))
    return _Editables(frozenset(found), tuple(sorted(set(unread))))


def _pth_roots(dist: importlib.metadata.Distribution) -> list[Path]:
    """The directories ``dist``'s ``.pth`` files put on ``sys.path``"""
    found: list[Path] = []
    for file in dist.files or ():
        if file.suffix != ".pth":
            continue
        pth = Path(str(dist.locate_file(file)))
        if not pth.is_file():
            continue
        for line in pth.read_text(encoding="utf-8").splitlines():
            entry = line.strip()
            if entry and not entry.startswith(("#", "import ", "import\t")):
                found.append((pth.parent / entry).resolve())
    return found


class _MalformedRecord(ValueError):
    """A ``direct_url.json`` that is not the record PEP 610 describes"""


def _editable_project(dist: importlib.metadata.Distribution) -> Path | None:
    """The project directory of an editable install, which its ``direct_url.json``
    (PEP 610) records; None for any other install. A malformed record of the
    distribution that ships the handler's package stops the audit: it cannot tell
    whether the handler is that distribution's"""
    try:
        return _read_project(dist)
    except _MalformedRecord as exc:
        name = dist.metadata["Name"]
        raise Exit.PRECONDITION(
            f"the direct_url.json of the installed distribution {name} is not a PEP 610 "
            f"record: {exc}",
            context={"distribution": name},
            fix_required=f"reinstall {name}, or uninstall it if the audit does not need it",
        ) from None


def _read_project(dist: importlib.metadata.Distribution) -> Path | None:
    """``_editable_project``, raising ``_MalformedRecord`` for a malformed record"""
    direct_url = dist.read_text("direct_url.json")
    if direct_url is None:
        return None
    try:
        record = json.loads(direct_url)
        if not isinstance(record, dict):
            raise TypeError("the record is not a JSON object")
        info = record.get("dir_info")
        if info is None:
            return None
        if not isinstance(info, dict):
            raise TypeError("dir_info is not a JSON object")
        if info.get("editable") is not True:
            return None
        url = record["url"]
        if not isinstance(url, str):
            raise TypeError("url is not a string")
        return Path.from_uri(url).resolve()
    except (ValueError, KeyError, TypeError) as exc:
        raise _MalformedRecord(str(exc)) from None


def _first_party(fn: types.FunctionType, home: _Home) -> bool:
    top = (fn.__module__ or "").partition(".")[0]
    if top == "treaty":
        return False
    if home.packages is not None:
        return top in home.packages
    path = Path(fn.__code__.co_filename).resolve()
    return (
        path.is_relative_to(home.root)
        and not _INSTALLED & set(path.parts)
        and not path.is_relative_to(_STDLIB)
    )


def _label(fn: types.FunctionType, home: _Home | None) -> str:
    """``helpers.enter (helpers.py:6)``: the helper, and where it is defined"""
    path = Path(fn.__code__.co_filename).resolve()
    if home is not None and path.is_relative_to(home.root):
        path = path.relative_to(home.root)
    return f"{fn.__module__}.{fn.__qualname__} ({path.as_posix()}:{fn.__code__.co_firstlineno})"


def _callees(fn: Callable[..., object]) -> list[types.FunctionType]:
    """The Python functions ``fn``'s calls name, in its module, closure, or own imports"""
    tree = source_tree(fn)
    if tree is None:
        return []
    scope: dict[str, object] = {**module_scope(fn), **_closure(fn), **_imported(fn, tree)}
    found: list[types.FunctionType] = []
    for node in ast.walk(tree):
        name = dotted(node.func) if isinstance(node, ast.Call) else None
        target = None if name is None else _static(scope, name.split("."))
        if isinstance(target, types.FunctionType) and target.__name__ != "<lambda>":
            found.append(target)
    return found


def _static(scope: Mapping[str, object], parts: list[str]) -> object:
    """``parts`` looked up through modules and classes without running their code, so a
    module's ``__getattr__`` or a proxy is never asked; a decorated function unwrapped"""
    target = _unwrapped(scope.get(parts[0]))
    for part in parts[1:]:
        if not isinstance(target, (types.ModuleType, type)):
            return None
        try:
            target = inspect.getattr_static(target, part)
        except AttributeError:
            return None
        if isinstance(target, (staticmethod, classmethod)):
            target = target.__func__
        target = _unwrapped(target)
    return target


def _imported(fn: Callable[..., object], tree: ast.AST) -> dict[str, object]:
    """The names ``fn`` binds by importing inside its body, from modules already loaded:
    the audit reads what the app imported and imports nothing itself"""
    found: dict[str, object] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name if alias.asname else alias.name.partition(".")[0]
                module = sys.modules.get(name)
                if module is not None:
                    found[alias.asname or name] = module
        elif isinstance(node, ast.ImportFrom):
            source = sys.modules.get(absolute_module(fn, node) or "")
            if source is None:
                continue
            for alias in node.names:
                try:
                    value = inspect.getattr_static(source, alias.name)
                except AttributeError:
                    value = sys.modules.get(f"{source.__name__}.{alias.name}")
                if value is not None:
                    found[alias.asname or alias.name] = value
    return found


def absolute_module(code: Callable[..., object] | type, node: ast.ImportFrom) -> str | None:
    """The module an ``from ... import`` names, a relative one resolved against the
    package ``code`` belongs to"""
    if not node.level:
        return node.module
    package = getattr(sys.modules.get(getattr(code, "__module__", "") or ""), "__package__", None)
    if not package:
        return None
    try:
        return importlib.util.resolve_name("." * node.level + (node.module or ""), package)
    except ImportError:
        return None  # beyond the top-level package: an import that never runs


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
