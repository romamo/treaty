"""Shell completion generated from the manifest: ``<app> completion bash`` or ``zsh``
prints a static script holding the command tree, every flag, and the values enum and path
flags take, so a tab press runs no Python and writes no audit log entry"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from ._envelope import visible
from ._values import CommandPath

COMPLETION_PATH = CommandPath("completion")
FORMAT_FLAG = "format"
_DESCRIPTION_CHARS = 72
_UNSAFE = re.compile(r"[^A-Za-z0-9_]")


class Shell(StrEnum):
    BASH = "bash"
    ZSH = "zsh"


@dataclass(frozen=True, slots=True)
class Takes:
    """The value a flag or positional takes: one of ``choices``, a path, or free text"""

    choices: tuple[str, ...] = ()
    files: bool = False


@dataclass(frozen=True, slots=True)
class Option:
    names: tuple[str, ...]
    """``--scope``, then ``-s`` when the flag has a short name"""
    description: str
    takes: Takes | None
    """None for a switch, which takes no value"""


@dataclass(frozen=True, slots=True)
class Node:
    """A place in the command tree: the root, a group, or a command"""

    words: tuple[str, ...]
    children: tuple[tuple[str, str], ...]
    """Each child's word and description"""
    options: tuple[Option, ...]
    """The command's own flags; the global ones apply everywhere"""
    slots: tuple[Takes, ...]
    """What each positional takes, in order"""
    variadic: bool
    """The last positional takes every remaining word"""
    strict: bool
    """Options end at the first positional (REQ-C-027)"""
    formats: Takes | None = None
    """The ``--format`` values of a command that offers its own, beside the app's (#209)"""

    @property
    def key(self) -> str:
        return " ".join(self.words)


@dataclass(frozen=True, slots=True)
class Tree:
    globals: tuple[Option, ...]
    nodes: tuple[Node, ...]


def tree(manifest: Mapping[str, object], groups: Mapping[CommandPath, str]) -> Tree:
    """The completion tree of ``manifest``; ``groups`` describes the groups, which have no
    manifest entry. ``--version`` is the root's own alias for the ``version`` command"""
    commands = manifest["commands"]
    flags = manifest["flags"]
    assert isinstance(commands, dict) and isinstance(flags, dict)
    described: dict[CommandPath, str] = {p: _short(d) for p, d in groups.items()}
    for key, entry in commands.items():
        described[CommandPath(key)] = _short(str(entry["description"]))
    for path in list(described):
        parent = path.parent
        while parent is not None:
            described.setdefault(parent, "")
            parent = parent.parent
    paths = set(described)
    root = _node((), paths, described, None, ())
    version = Option(("--version",), "Print the tool name and version", None)
    nodes = [Node(root.words, root.children, (version,), (), False, False)]
    format_flag = flags.get(FORMAT_FLAG)
    offered = tuple(format_flag["enum_values"]) if isinstance(format_flag, dict) else ()
    nodes += [
        _node(p.parts, paths, described, commands.get(p.value), offered)
        for p in sorted(paths, key=str)
    ]
    return Tree(_options(flags), tuple(nodes))


def script(app_name: str, version: str, completion_tree: Tree, shell: Shell) -> str:
    render = _bash if shell is Shell.BASH else _zsh
    return render(app_name, version, completion_tree)


def _node(
    words: tuple[str, ...],
    paths: set[CommandPath],
    described: Mapping[CommandPath, str],
    entry: Mapping[str, object] | None,
    formats: tuple[str, ...],
) -> Node:
    children = tuple(
        (p.parts[-1], described[p]) for p in sorted(paths, key=str) if p.parts[:-1] == words
    )
    if entry is None:
        return Node(words, children, (), (), False, False)
    flags = entry["flags"]
    assert isinstance(flags, dict)
    positionals = entry.get("positionals", [])
    assert isinstance(positionals, list)
    if entry.get("option_placement") == "strict" and not positionals:
        # #35: strict without the variadic positional it needs is a passthrough command;
        # every word after the path is the delegated tool's, offered as a file path
        return Node(words, children, (), (Takes((), True),), True, True)
    slots = tuple(_takes({**flags.get(p["name"], {}), **p}) for p in positionals)
    variadic = bool(positionals) and bool(positionals[-1].get("variadic"))
    strict = entry.get("option_placement") == "strict"
    beyond = entry.get("output_formats", [])
    assert isinstance(beyond, list)
    own = tuple(str(f) for f in beyond if f not in formats)
    takes = Takes((*formats, *own)) if own else None
    return Node(words, children, _options(flags), slots, variadic, strict, takes)


def _options(flags: Mapping[str, object]) -> tuple[Option, ...]:
    options: list[Option] = []
    for name, entry in sorted(flags.items()):
        assert isinstance(entry, dict)
        names = (f"--{name}",) + ((f"-{entry['short']}",) if "short" in entry else ())
        takes = None if entry["type"] == "boolean" else _takes(entry)
        options.append(Option(names, _short(str(entry["description"])), takes))
    return tuple(options)


def _takes(entry: Mapping[str, object]) -> Takes:
    choices = entry.get("enum_values", [])
    assert isinstance(choices, list)
    return Takes(tuple(str(c) for c in choices), entry.get("pattern_type") == "filepath")


def _short(description: str) -> str:
    """The first clause on one line, cut at a word to fit a completion menu; a control
    character or bidi override shows as its escape, as in --help (#203)"""
    text = visible(" ".join(re.split(r"; |\. ", description, maxsplit=1)[0].split()))
    if len(text) <= _DESCRIPTION_CHARS:
        return text
    return text[: _DESCRIPTION_CHARS - 3].rsplit(" ", 1)[0] + "..."


def _q(word: str) -> str:
    return shlex.quote(word)


def _words(words: Sequence[str]) -> str:
    return " ".join(_q(w) for w in words)


def _prefix(app_name: str) -> str:
    return "_" + _UNSAFE.sub("_", app_name) + "_complete"


def _header(app_name: str, version: str, shell: Shell) -> list[str]:
    return [
        f"# {shell.value} completion for {app_name} {version}, generated by treaty from its",
        f"# manifest. Regenerate it after an upgrade: {app_name} completion {shell.value}"
        " --format plain",
    ]


def _takes_table(fn: str, tree_: Tree, shell: Shell) -> list[str]:
    """``<fn>_values NODE WORD`` completes the value of flag WORD, or of positional ``#N``;
    a node's own flags come before the global ones, which match on any node"""
    lines = [f"{fn}_values() {{", '    case "$1|$2" in']
    files = "        _files ;;" if shell is Shell.ZSH else f'        {fn}_files "$3" ;;'

    def emit(patterns: list[str], takes: Takes) -> None:
        if takes.choices:
            if shell is Shell.ZSH:
                body = f"        compadd -- {_words(takes.choices)} ;;"
            else:
                body = f'        {fn}_words "$3" {_words(takes.choices)} ;;'
        elif takes.files:
            body = files
        else:
            return
        lines.append(f"    {'|'.join(patterns)})")
        lines.append(body)

    for node in tree_.nodes:
        for option in node.options:
            if option.takes is not None:
                emit([_q(f"{node.key}|{n}") for n in option.names], option.takes)
        for index, slot in enumerate(node.slots):
            emit([_q(f"{node.key}|#{index}")], slot)
        if node.formats is not None:
            emit([_q(f"{node.key}|--{FORMAT_FLAG}")], node.formats)
    for option in tree_.globals:
        if option.takes is not None:
            emit(["*" + _q(f"|{n}") for n in option.names], option.takes)
    lines += ["    esac", "}"]
    return lines


def _node_table(fn: str, tree_: Tree, shell: Shell) -> list[str]:
    """``<fn>_node NODE`` sets the caller's kids, opts, takes, slots, tail, and strict"""
    lines = [f"{fn}_node() {{", "    case $1 in"]
    for node in tree_.nodes:
        valued = [n for o in node.options if o.takes is not None for n in o.names]
        if shell is Shell.ZSH:
            kids = [_describe(w, d) for w, d in node.children]
            opts = [_describe(n, o.description) for o in node.options for n in o.names]
        else:
            kids = [w for w, _ in node.children]
            opts = [n for o in node.options for n in o.names]
        lines += [
            f"    {_q(node.key)})",
            f"        kids=({_words(kids)})",
            f"        opts=({_words(opts)})",
            f"        takes={_q(' ' + ' '.join(valued) + ' ')}",
            f"        slots={len(node.slots)} tail={int(node.variadic)}"
            f" strict={int(node.strict)} ;;",
        ]
    lines += ["    esac", "}"]
    return lines


def _describe(word: str, description: str) -> str:
    """One ``_describe`` spec: a colon in the word is escaped, the first one separates"""
    return word.replace(":", r"\:") + (f":{description}" if description else "")


def _globals(tree_: Tree, shell: Shell) -> tuple[str, str]:
    options = tree_.globals
    if shell is Shell.ZSH:
        words = [_describe(n, o.description) for o in options for n in o.names]
    else:
        words = [n for o in options for n in o.names]
    valued = [n for o in options if o.takes is not None for n in o.names]
    return _words(words), _q(" " + " ".join(valued) + " ")


# The children's words, space-separated: a zsh kid is a _describe spec, word:description
_BASH_NAMES = "${kids[*]}"
_ZSH_NAMES = "${(j: :)${(@)kids%%:*}}"

# The walk shared by both shells: it skips flags and their values, follows child words
# while no positional has been seen, counts positionals, and treats every word after --
# (or, on a strict command, after the first positional) as a positional
_WALK = """\
    local node= npos=0 expect= literal= w
    local -a kids opts
    local takes slots tail strict
    {fn}_node ''
    for w in "${{before[@]}}"; do
        if [[ -z $literal ]]; then
            if [[ $w == '=' ]]; then
                expect='='
                continue
            fi
            if [[ -n $expect ]]; then
                expect=
                continue
            fi
            case $w in
            --)
                literal=1
                continue ;;
            -*=*)
                continue ;;
            -*)
                [[ $takes == *" $w "* || $gtakes == *" $w "* ]] && expect=$w
                continue ;;
            esac
        fi
        if ((npos == 0)) && [[ " {names} " == *" $w "* ]]; then
            node=${{node:+$node }}$w
            {fn}_node "$node"
            continue
        fi
        ((npos++))
        ((strict)) && literal=1
    done
"""


def _bash(app_name: str, version: str, tree_: Tree) -> str:
    fn = _prefix(app_name)
    gwords, gtakes = _globals(tree_, Shell.BASH)
    main = f"""\
{fn}_words() {{
    local cur=$1 w
    shift
    for w in "$@"; do
        [[ $w == "$cur"* ]] && COMPREPLY+=("$w")
    done
}}

{fn}_files() {{
    local f
    compopt -o filenames 2>/dev/null
    while IFS= read -r f; do
        COMPREPLY+=("$f")
    done < <(compgen -f -- "$1")
}}

{fn}() {{
    COMPREPLY=()
    local cur=${{COMP_WORDS[COMP_CWORD]}} prev=${{COMP_WORDS[COMP_CWORD-1]}}
    local -a before=("${{COMP_WORDS[@]:1:COMP_CWORD-1}}") globals=({gwords})
    local gtakes={gtakes}
{_WALK.format(fn=fn, names=_BASH_NAMES)}
    # Bash 3.2 keeps --flag=value one word, though readline replaces only the value
    if [[ -z $literal && $cur == -*=* ]]; then
        {fn}_values "$node" "${{cur%%=*}}" "${{cur#*=}}"
        return
    fi
    # Later versions split it at the =: complete the value after it, not the = itself
    [[ $cur == '=' ]] && return
    if [[ -z $literal && $prev == '=' ]] && ((COMP_CWORD > 1)); then
        {fn}_values "$node" "${{COMP_WORDS[COMP_CWORD-2]}}" "$cur"
        return
    fi
    if [[ -n $expect ]]; then
        {fn}_values "$node" "$expect" "$cur"
        return
    fi
    if [[ -z $literal && $cur == -* ]]; then
        {fn}_words "$cur" "${{opts[@]}}" "${{globals[@]}}"
        return
    fi
    if ((npos == 0)) && ((${{#kids[@]}})); then
        {fn}_words "$cur" "${{kids[@]}}"
        return
    fi
    ((tail && npos >= slots)) && npos=$((slots - 1))
    ((npos < slots)) && {fn}_values "$node" "#$npos" "$cur"
}}

complete -F {fn} {_q(app_name)}
"""
    lines = [*_header(app_name, version, Shell.BASH), ""]
    lines += _node_table(fn, tree_, Shell.BASH) + [""]
    lines += _takes_table(fn, tree_, Shell.BASH) + [""]
    return "\n".join(lines) + "\n" + main


def _zsh(app_name: str, version: str, tree_: Tree) -> str:
    fn = _prefix(app_name)
    gwords, gtakes = _globals(tree_, Shell.ZSH)
    main = f"""\
{fn}() {{
    local cur=${{words[CURRENT]}}
    local -a before=("${{(@)words[2,CURRENT-1]}}") globals=({gwords})
    local gtakes={gtakes}
{_WALK.format(fn=fn, names=_ZSH_NAMES)}
    if [[ -z $literal && $cur == -*=* ]]; then
        compset -P '*='
        {fn}_values "$node" "${{cur%%=*}}"
        return
    fi
    if [[ -n $expect ]]; then
        {fn}_values "$node" "$expect"
        return
    fi
    if [[ -z $literal && $cur == -* ]]; then
        _describe -t options option opts -- globals
        return
    fi
    if ((npos == 0)) && ((${{#kids}})); then
        _describe -t commands command kids
        return
    fi
    ((tail && npos >= slots)) && npos=$((slots - 1))
    ((npos < slots)) && {fn}_values "$node" "#$npos"
}}

if [[ ${{zsh_eval_context[-1]}} == loadautofunc ]]; then
    {fn} "$@"
else
    compdef {fn} {_q(app_name)}
fi
"""
    lines = [f"#compdef {app_name}", *_header(app_name, version, Shell.ZSH), ""]
    lines += _node_table(fn, tree_, Shell.ZSH) + [""]
    lines += _takes_table(fn, tree_, Shell.ZSH) + [""]
    return "\n".join(lines) + "\n" + main
