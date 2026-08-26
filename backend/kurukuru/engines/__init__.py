"""
Compute engine package.

The application talks to hypervisors exclusively through the
:class:`~kurukuru.engines.base.ComputeEngine` interface. This package holds the ABC
(``base``) plus one module per concrete driver, and the registry that maps an
engine *name* to a process-wide singleton.

QEMU is currently the only driver. The indirection is kept deliberately: the
registry, the ABC and the per-row dispatch are what let a Hyper-V or HVF engine
be added later by writing one module and one factory entry, with no change to
routers, models or the reconciler. Multipass was removed by deleting exactly
those two things, which is the evidence the seam holds.

Import surface: everything the rest of the app needs is re-exported here.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from kurukuru.engines.base import (
    ComputeEngine,
    ComputeEngineError,
    ComputeTimeoutError,
    HypervisorUnavailableError,
    InstanceInfo,
    InstanceNotFoundError,
    LaunchOptions,
    SnapshotInfo,
)
from kurukuru.engines.qemu import QemuEngine
from kurukuru.models import DEFAULT_ENGINE, KNOWN_ENGINES

logger = logging.getLogger("kurukuru.engines")

__all__ = [
    "ComputeEngine",
    "ComputeEngineError",
    "ComputeTimeoutError",
    "HypervisorUnavailableError",
    "InstanceInfo",
    "InstanceNotFoundError",
    "LaunchOptions",
    "SnapshotInfo",
    "QemuEngine",
    "EngineRegistry",
    "UnknownEngineError",
    "get_engine_registry",
]


class UnknownEngineError(ComputeEngineError):
    """The caller asked for an engine name we have no driver for.

    Raised for retired engines too — a row can name a driver that no longer
    exists, and callers must handle that rather than assume every row is
    actionable.
    """


# Name -> zero-arg constructor. Instantiation is lazy so that merely importing
# this module never touches the filesystem or probes a hypervisor.
_FACTORIES: dict[str, Callable[[], ComputeEngine]] = {
    "qemu": QemuEngine,
}


class EngineRegistry:
    """Resolves engine names to lazily-created :class:`ComputeEngine` singletons.

    One registry serves the whole process. Routers hold a registry rather than a
    single engine because a row's ``engine`` column decides which driver handles
    it — including rows naming a driver that no longer exists, which
    :meth:`supports` lets callers detect without catching an exception.
    """

    def __init__(self, factories: dict[str, Callable[[], ComputeEngine]] | None = None) -> None:
        self._factories = dict(factories if factories is not None else _FACTORIES)
        self._instances: dict[str, ComputeEngine] = {}

    def names(self) -> list[str]:
        """Known engine names, in catalog order."""
        return list(self._factories)

    def supports(self, name: str | None) -> bool:
        """Whether a driver exists for ``name``.

        The reconciler asks this before touching a row: a legacy row naming a
        retired engine has no driver to consult, and must be left alone rather
        than errored.
        """
        return (name or DEFAULT_ENGINE).strip().lower() in self._factories

    def get(self, name: str | None) -> ComputeEngine:
        """Return the singleton driver for ``name`` (default engine when None)."""
        key = (name or DEFAULT_ENGINE).strip().lower()
        if key not in self._factories:
            raise UnknownEngineError(
                f"No driver for compute engine '{name}' "
                f"(available: {', '.join(self._factories)})"
            )
        engine = self._instances.get(key)
        if engine is None:
            engine = self._factories[key]()
            self._instances[key] = engine
            logger.info("Instantiated compute engine '%s' -> %s", key, type(engine).__name__)
        return engine


_registry: EngineRegistry | None = None


def get_engine_registry() -> EngineRegistry:
    """FastAPI dependency returning the process-wide :class:`EngineRegistry`."""
    global _registry
    if _registry is None:
        _registry = EngineRegistry()
    return _registry


# Guard against the tuple in models.py and the factory table drifting apart.
assert set(_FACTORIES) == set(KNOWN_ENGINES), (
    "kurukuru.models.KNOWN_ENGINES and kurukuru.engines._FACTORIES disagree"
)
