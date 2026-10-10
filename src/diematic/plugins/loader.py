"""Discovers plugins registered under the `diematic.plugins` entry-point group."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from importlib.metadata import EntryPoint, entry_points

from diematic.plugins.base import Plugin, PluginContext, PluginError

GROUP = "diematic.plugins"

EntryPoints = Callable[[], Iterable[EntryPoint]]


def _installed() -> Iterable[EntryPoint]:
    return entry_points(group=GROUP)


def load_plugin_classes(
    names: Iterable[str], discover: EntryPoints = _installed
) -> dict[str, type[Plugin]]:
    available = {entry.name: entry for entry in discover()}
    classes: dict[str, type[Plugin]] = {}
    for name in names:
        entry = available.get(name)
        if entry is None:
            known = ", ".join(sorted(available)) or "none installed"
            raise PluginError(f"unknown plugin {name!r} (available: {known})")
        try:
            loaded = entry.load()
        except Exception as exc:
            raise PluginError(f"plugin {name!r} could not be loaded: {exc}") from exc
        if not (isinstance(loaded, type) and issubclass(loaded, Plugin)):
            raise PluginError(f"plugin {name!r} does not provide a Plugin subclass")
        classes[name] = loaded
    return classes


def create_plugins(
    names: Iterable[str],
    context_for: Callable[[str], PluginContext],
    discover: EntryPoints = _installed,
) -> dict[str, Plugin]:
    """Instantiate the named plugins, each with its own context."""
    classes = load_plugin_classes(list(names), discover)
    return {name: cls(context_for(name)) for name, cls in classes.items()}
