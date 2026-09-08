"""Joining a caller-supplied name onto a directory, without leaving it.

One helper, because the check was written twice — in ``dashboard`` for asset
requests and in ``isos`` for ISO filenames — and both copies had the same bug.
A security check that exists in two places is a security check that will be
fixed in one of them.

**The bug both copies had.** The obvious way to write this is::

    resolved = (root / relative).resolve()
    resolved.relative_to(root.resolve())          # raises if it escaped

which is correct about *where the path ends up* and wrong about *when it looks*.
``Path.__truediv__`` does not concatenate: a rooted right-hand side replaces the
left entirely, so ``Path("C:/app") / "//host/share"`` is ``\\\\host\\share`` and
not a subdirectory of anything. ``.resolve()`` then runs on that — and on
Windows resolving a UNC path asks the network for it. The containment check
does reject the result, correctly and far too late: the outbound SMB connection
has already been made, to a host the caller chose.

For the dashboard that route is unauthenticated, and a browser will issue the
request from any page the user happens to be visiting, which makes it a
request-forgery primitive rather than a curiosity. See DECISIONS.

**So containment is established before the filesystem is touched.** Three steps,
in this order, and the order is the whole point:

1. Reject a join that changed the anchor. This is what catches UNC and a
   different drive letter, and it is pure string comparison — no I/O.
2. Collapse ``..`` with ``normpath``, which is lexical, and require the result
   to be under the base. Also no I/O.
3. Only now resolve, and check containment *again* — because step 2 cannot see
   a symlink or a junction, and the second check is what catches one pointing
   out of the tree.

Steps 1 and 2 are not input filtering in disguise. Neither one looks for a bad
character; both ask whether the joined path is still inside the base, which is
the same question step 3 asks, asked earlier and without a syscall.
"""

from __future__ import annotations

import os
from pathlib import Path


def resolve_within(root: Path, relative: str) -> Path | None:
    """The path ``relative`` names inside ``root``, or None if it is not inside.

    Returns the fully resolved path. Existence is *not* checked — callers care
    about different things (a file, an ``.iso``, a directory) and say so
    themselves.
    """
    base = root.resolve()
    joined = base / relative

    # 1. A rooted right-hand side replaced the base. Never resolve this: on
    #    Windows a UNC anchor is a network location and resolving it dials out.
    if joined.anchor != base.anchor:
        return None

    # 2. `..` collapsed on the string, so containment can be decided without
    #    asking the filesystem anything.
    lexical = Path(os.path.normpath(joined))
    if not _is_within(lexical, base):
        return None

    # 3. Now it is safe to look: whatever this resolves through is already
    #    inside our own directory. Re-checked afterwards because a symlink or
    #    junction under the base can still point out of it.
    try:
        resolved = lexical.resolve()
    except OSError:
        return None
    return resolved if _is_within(resolved, base) else None


def _is_within(candidate: Path, base: Path) -> bool:
    try:
        candidate.relative_to(base)
    except ValueError:
        return False
    return True
