"""treaty: zero-dependency CLI framework implementing the CLI Agent Spec."""

from ._app import App, ExecArgs, Group, NoArgs
from ._command import DangerLevel, Example, Renderer
from ._context import Ctx
from ._effect import Affects
from ._envelope import Envelope, ErrorDetail, WarningDetail
from ._errors import CliExit, Exit, ParseError, RegistrationError, SchemaError, TreatyError
from ._exit import ExitCodeEntry, FrameworkCode, SideEffects
from ._flags import Arg, Flag
from ._mode import Format
from ._page import Page, PageRequest
from ._scalars import ScalarSpec
from ._subprocess import Completed
from ._timeout import Timeout
from ._values import CommandPath, ExitCode, ExitCodeName, Scope

__all__ = [
    "Affects",
    "App",
    "Arg",
    "CliExit",
    "CommandPath",
    "Completed",
    "Ctx",
    "DangerLevel",
    "Envelope",
    "ErrorDetail",
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
    "Format",
    "Page",
    "PageRequest",
    "ParseError",
    "RegistrationError",
    "Renderer",
    "ScalarSpec",
    "SchemaError",
    "Scope",
    "SideEffects",
    "Timeout",
    "TreatyError",
    "WarningDetail",
]
