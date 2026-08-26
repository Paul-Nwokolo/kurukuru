"""
The Kurukuru command-line interface.

A client of the HTTP API, shipped from the backend package so that
``pip install -e backend`` puts the command on PATH. See
:mod:`kurukuru.cli.main` for the root app and :mod:`kurukuru.cli.naming` for the one
place the command's name is written down.
"""

from kurukuru.cli.naming import CLI_NAME

__all__ = ["CLI_NAME"]
