"""treaty: zero-dependency CLI framework implementing the CLI Agent Spec."""

from ._app import App, ExecArgs, Group, NoArgs
from ._command import DangerLevel, Example
from ._context import Ctx
from ._errors import CliExit, Exit, ParseError, RegistrationError, SchemaError, TreatyError
from ._exit import ExitCodeEntry, FrameworkCode, SideEffects
from ._flags import Arg, Flag
from ._mode import OutputMode
from ._timeout import Timeout
from ._values import CommandPath, ExitCode, ExitCodeName, Scope

__all__ = [
    "App",
    "Arg",
    "CliExit",
    "CommandPath",
    "Ctx",
    "DangerLevel",
    "Example",
    "ExecArgs",
    "Exit",
    "ExitCode",
    "ExitCodeEntry",
    "ExitCodeName",
    "Flag",
    "FrameworkCode",
    "Group",
    "NoArgs",
    "OutputMode",
    "ParseError",
    "RegistrationError",
    "SchemaError",
    "Scope",
    "SideEffects",
    "Timeout",
    "TreatyError",
]
