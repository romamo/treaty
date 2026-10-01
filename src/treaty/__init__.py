"""treaty: zero-dependency CLI framework implementing the CLI Agent Spec."""

from importlib.metadata import version

from ._adapters import OutputAdapter
from ._app import App, Group, NoArgs
from ._args_adapter import ArgsAdapter
from ._auth import Credentials, Expired
from ._batch import Batch, Item, ItemError
from ._cache import CachePolicy
from ._command import DangerLevel, Example, Renderer
from ._context import Ctx
from ._declare import Background, SideEffect, Subprocess
from ._deprecation import Deprecated
from ._deps import Check, Dependency, endpoint
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
from ._http import HttpResponse
from ._init import Init
from ._jobs import Job, JobStore
from ._journal import AuditLog
from ._mode import Format
from ._out import Binary, Out
from ._page import Page, PageRequest
from ._retry import Retry
from ._rules import DefaultWhenAbsent, Excludes, RequiredWhen, RequiresAny, RequiresOne
from ._scalars import ScalarSpec
from ._stdout import intercept_stdout
from ._steps import Rollback, StepName
from ._subprocess import Completed, Spawned
from ._table import table
from ._timeout import Timeout
from ._update import UpdateCheck
from ._values import CommandPath, ExitCode, ExitCodeName, SchemaVersion, Scope
from ._walk import WalkEntry

__version__ = version("treaty")

__all__ = [
    "Affects",
    "App",
    "Arg",
    "ArgsAdapter",
    "AuditLog",
    "Background",
    "Batch",
    "Binary",
    "CachePolicy",
    "Check",
    "CliExit",
    "HttpResponse",
    "CommandPath",
    "DefaultWhenAbsent",
    "Dependency",
    "Deprecated",
    "Completed",
    "Credentials",
    "Ctx",
    "DangerLevel",
    "Envelope",
    "ErrorDetail",
    "Example",
    "Excludes",
    "Expired",
    "Exit",
    "ExitCode",
    "ExitCodeEntry",
    "ExitCodeName",
    "Flag",
    "FrameworkCode",
    "Group",
    "Init",
    "Item",
    "ItemError",
    "Job",
    "JobStore",
    "Meta",
    "NetworkContext",
    "NoArgs",
    "Out",
    "OutputAdapter",
    "Format",
    "Page",
    "PageRequest",
    "Redirect",
    "ParseError",
    "RegistrationError",
    "Renderer",
    "RequiredWhen",
    "RequiresAny",
    "RequiresOne",
    "Retry",
    "RetryStrategy",
    "Rollback",
    "ScalarSpec",
    "SchemaError",
    "SchemaVersion",
    "Scope",
    "SideEffect",
    "SideEffects",
    "Spawned",
    "StepName",
    "Subprocess",
    "Timeout",
    "TreatyError",
    "UpdateCheck",
    "WalkEntry",
    "WarningDetail",
    "already_exists",
    "endpoint",
    "intercept_stdout",
    "table",
]
