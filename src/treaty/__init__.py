"""treaty: zero-dependency CLI framework implementing the CLI Agent Spec."""

from importlib.metadata import version

from ._app import App, ExecArgs, Group, NoArgs
from ._auth import Credentials, Expired
from ._command import DangerLevel, Example, Renderer
from ._context import Ctx
from ._deprecation import Deprecated
from ._effect import Affects
from ._envelope import Envelope, ErrorDetail, Meta, NetworkContext, Redirect, WarningDetail
from ._errors import (
    CliExit,
    Exit,
    ParseError,
    RegistrationError,
    SchemaError,
    TreatyError,
    already_exists,
)
from ._exit import ExitCodeEntry, FrameworkCode, RetryStrategy, SideEffects
from ._flags import Arg, Flag
from ._init import Init
from ._jobs import Job, JobStore
from ._mode import Format
from ._out import Binary, Out
from ._page import Page, PageRequest
from ._retry import Retry
from ._rules import DefaultWhenAbsent, Excludes, RequiredWhen
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
    "Binary",
    "CliExit",
    "CommandPath",
    "DefaultWhenAbsent",
    "Deprecated",
    "Completed",
    "Credentials",
    "Ctx",
    "DangerLevel",
    "Envelope",
    "ErrorDetail",
    "Example",
    "Excludes",
    "ExecArgs",
    "Expired",
    "Exit",
    "ExitCode",
    "ExitCodeEntry",
    "ExitCodeName",
    "Flag",
    "FrameworkCode",
    "Group",
    "Init",
    "Job",
    "JobStore",
    "Meta",
    "NetworkContext",
    "NoArgs",
    "Out",
    "Format",
    "Page",
    "PageRequest",
    "Redirect",
    "ParseError",
    "RegistrationError",
    "Renderer",
    "RequiredWhen",
    "Retry",
    "RetryStrategy",
    "ScalarSpec",
    "SchemaError",
    "SchemaVersion",
    "Scope",
    "SideEffects",
    "Timeout",
    "TreatyError",
    "WarningDetail",
    "already_exists",
    "table",
]
