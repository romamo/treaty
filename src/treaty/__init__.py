"""treaty: zero-dependency CLI framework implementing the CLI Agent Spec."""

from importlib.metadata import version

from ._app import App, ExecArgs, Group, NoArgs
from ._auth import Credentials
from ._command import DangerLevel, Example, Renderer
from ._context import Ctx
from ._effect import Affects
from ._envelope import Envelope, ErrorDetail, Meta, WarningDetail
from ._errors import CliExit, Exit, ParseError, RegistrationError, SchemaError, TreatyError
from ._exit import ExitCodeEntry, FrameworkCode, SideEffects
from ._flags import Arg, Flag
from ._jobs import Job, JobStore
from ._mode import Format
from ._page import Page, PageRequest
from ._retry import Retry
from ._scalars import ScalarSpec
from ._subprocess import Completed
from ._table import table
from ._timeout import Timeout
from ._values import CommandPath, ExitCode, ExitCodeName, SchemaVersion, Scope

__version__ = version("treaty")

__all__ = [
    "Affects",
    "App",
    "Arg",
    "CliExit",
    "CommandPath",
    "Completed",
    "Credentials",
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
    "Job",
    "JobStore",
    "Meta",
    "NoArgs",
    "Format",
    "Page",
    "PageRequest",
    "ParseError",
    "RegistrationError",
    "Renderer",
    "Retry",
    "ScalarSpec",
    "SchemaError",
    "SchemaVersion",
    "Scope",
    "SideEffects",
    "Timeout",
    "TreatyError",
    "WarningDetail",
    "table",
]
