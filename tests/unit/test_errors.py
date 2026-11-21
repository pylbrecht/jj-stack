from __future__ import annotations

from jj_stack.errors import (
    EXIT_FAILURE,
    EXIT_FORGE,
    EXIT_NO_STACK,
    EXIT_USAGE,
    CliError,
    UnsupportedStackError,
    UsageError,
    error_message,
    resolve_exit_code,
)
from jj_stack.forge.errors import ForgeError
from jj_stack.ui import plain_text


def test_error_message_appends_forge_cause_reason() -> None:
    cause = ForgeError("request failed (Connection refused)", kind="unavailable")
    try:
        raise CliError("Could not load pull request #7") from cause
    except CliError as error:
        assert (
            plain_text(error_message(error))
            == "Could not load pull request #7: request failed (Connection refused)"
        )
        assert str(error) == "Could not load pull request #7: request failed (Connection refused)"


def test_error_message_uses_forge_cause_reason_when_message_is_empty() -> None:
    cause = ForgeError("request failed (GitHub 404: Not Found)", kind="not_found")
    try:
        raise CliError("") from cause
    except CliError as error:
        assert plain_text(error_message(error)) == "request failed (GitHub 404: Not Found)"


def test_resolve_exit_code_prefers_the_error_category_over_the_cause() -> None:
    generic = CliError("failed")
    assert resolve_exit_code(generic) == EXIT_FAILURE

    categorized = UsageError("bad flag")
    assert resolve_exit_code(categorized) == EXIT_USAGE

    # A categorized error keeps its own code even with a forge cause.
    try:
        raise UnsupportedStackError("unsupported") from ForgeError("boom", kind="unavailable")
    except CliError as error:
        assert resolve_exit_code(error) == EXIT_NO_STACK


def test_resolve_exit_code_inherits_forge_code_from_wrapped_cause() -> None:
    try:
        raise CliError("Could not load pull request #7") from ForgeError(
            "boom", kind="unavailable"
        )
    except CliError as error:
        assert resolve_exit_code(error) == EXIT_FORGE


def test_resolve_exit_code_inherits_cli_code_from_wrapped_cause() -> None:
    try:
        raise CliError("Local history does not form a linear stack.") from UnsupportedStackError(
            "merge changes are not supported"
        )
    except CliError as error:
        assert resolve_exit_code(error) == EXIT_NO_STACK
