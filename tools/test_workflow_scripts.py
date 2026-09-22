"""The shell scripts embedded in GitHub Actions workflows have to be valid.

A `run:` block is a program that nothing checks until CI runs it, and a
release workflow runs rarely — so a mistake in one is found at the worst
possible moment, in the least readable place. These tests are the cheap half
of that: they do not prove a workflow works, only that its scripts are
syntactically real and will not be mangled on the way to the shell.

**Both checks come from one bug**, which cost three CI round-trips to find.
`release.yml` had an em-dash in a PowerShell comment. Written as UTF-8 and
decoded as ANSI on the way to the parser, U+2014 becomes three characters
ending in `"` — a stray quote, inside a comment, which opened a string that
was never closed. PowerShell reported it as "the string is missing the
terminator" **seventy lines later**, at the last quote in the file, and the
step failed with no output at all: no annotations, nothing but "Process
completed with exit code 1". The raw log lives in blob storage that was not
reachable from the machine doing the debugging.

A typographic character in a comment is the smallest possible cause for that
effect, which is exactly why it is worth a test rather than a habit.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

try:
    import yaml
except ImportError:  # pragma: no cover - the backend's own dependency
    pytest.skip("PyYAML is not installed", allow_module_level=True)

WORKFLOWS = sorted((Path(__file__).resolve().parent.parent / ".github" / "workflows").glob("*.yml"))

#: ``${{ … }}`` is substituted by Actions before any shell sees it. Replaced
#: with a plausible literal so the script can be parsed as the shell will
#: actually receive it, rather than rejected for syntax Actions removes.
_EXPRESSION = re.compile(r"\$\{\{[^}]*\}\}")


def _steps():
    for workflow in WORKFLOWS:
        data = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        for job_name, job in (data.get("jobs") or {}).items():
            default_shell = ((job.get("defaults") or {}).get("run") or {}).get("shell")
            for index, step in enumerate(job.get("steps") or []):
                if not step.get("run"):
                    continue
                yield (
                    f"{workflow.name}:{job_name}:{step.get('name') or index}",
                    step["run"],
                    step.get("shell") or default_shell or "pwsh",
                )


ALL_STEPS = list(_steps())
assert ALL_STEPS, "no run: steps were found — has the workflow layout changed?"


@pytest.mark.parametrize(
    ("label", "script", "shell"),
    ALL_STEPS,
    ids=[label for label, _, _ in ALL_STEPS],
)
def test_a_run_block_is_plain_ascii(label: str, script: str, shell: str) -> None:
    """No typographic characters in a script. See this module's docstring.

    The rule is deliberately "ASCII", not "no em-dashes": the failure is about
    how bytes survive the trip to a shell, and every non-ASCII character takes
    the same trip. Prose belongs in the YAML comments *around* the block,
    which are never executed and keep their typography.
    """
    offenders = sorted({character for character in script if ord(character) > 127})
    assert not offenders, (
        f"{label} contains non-ASCII characters in its run: block — "
        f"{[f'U+{ord(c):04X} {c!r}' for c in offenders]}.\n"
        f"These are silently re-encoded on the way to the shell and can open "
        f"a string that is never closed, failing the step with no usable "
        f"error. Put the prose in a YAML comment outside the block instead."
    )


@pytest.mark.skipif(sys.platform != "win32", reason="needs the PowerShell parser")
@pytest.mark.parametrize(
    ("label", "script", "shell"),
    [s for s in ALL_STEPS if s[2] in ("pwsh", "powershell")],
    ids=[label for label, _, shell in ALL_STEPS if shell in ("pwsh", "powershell")],
)
def test_a_powershell_run_block_parses(label: str, script: str, shell: str) -> None:
    """Parse it with PowerShell's own parser, which is the only real authority.

    Parsed rather than executed: running a release build from a unit test
    would be absurd, and a parse catches the whole class of defect this file
    exists for.
    """
    probe = (
        "$e = $null; $t = $null; "
        "$src = [Console]::In.ReadToEnd(); "
        "[System.Management.Automation.Language.Parser]::ParseInput("
        "$src, [ref]$t, [ref]$e) | Out-Null; "
        "if ($e -and $e.Count) { "
        "  $e | ForEach-Object { "
        "    Write-Output (\"line \" + $_.Extent.StartLineNumber + \": \" + $_.Message) }; "
        "  exit 1 } else { exit 0 }"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", probe],
        input=_EXPRESSION.sub("substituted-by-actions", script),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, (
        f"{label} is not valid PowerShell:\n{completed.stdout}{completed.stderr}"
    )
