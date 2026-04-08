#
# Copyright (C) 2026-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.0
#

"""Terminal output utilities — color formatters and TTY detection."""

import sys
from typing import TYPE_CHECKING

import colorama

if TYPE_CHECKING:
    from typing import Any, Callable


output_is_a_tty = sys.stdout.isatty()


def create_formatter(*decorators) -> Callable[[Any], str]:
    """Return a function which decorates its argument with the given
    color/style if stdout is a tty, and leaves intact otherwise."""
    def color(arg: Any) -> str:
        return "".join(decorators) + str(arg) + colorama.Style.RESET_ALL

    def nocolor(arg: Any) -> str:
        return str(arg)
    return color if output_is_a_tty else nocolor


class palette:
    """Color palette for formatting terminal output"""
    fail = create_formatter(colorama.Fore.RED, colorama.Style.BRIGHT)
    diff_in = create_formatter(colorama.Fore.GREEN)
    diff_out = create_formatter(colorama.Fore.RED)
    diff_mark = create_formatter(colorama.Fore.MAGENTA)
