from importlib.metadata import EntryPoint

import pytest

from diematic.core.events import EventBus
from diematic.core.state import StateStore
from diematic.plugins.base import Plugin, PluginContext, PluginError
from diematic.plugins.loader import create_plugins, load_plugin_classes
from diematic.registers.loader import parse_map


class GoodPlugin(Plugin):
    async def run(self) -> None:
        return None


class NotAPlugin:
    pass


class BrokenConfigPlugin(Plugin):
    def __init__(self, context):
        raise PluginError("bad settings")

    async def run(self) -> None:
        return None


def entry(name, target):
    return EntryPoint(name, f"{__name__}:{target}", "diematic.plugins")


def discover(*entries):
    return lambda: list(entries)


def make_context(name):
    regmap = parse_map({"system": "t", "name": "t"})
    return PluginContext(regmap, StateStore(regmap), EventBus(), "en", {"name": name})


def test_listed_plugins_are_loaded_by_name():
    classes = load_plugin_classes(["good"], discover(entry("good", "GoodPlugin")))
    assert classes == {"good": GoodPlugin}


def test_only_requested_plugins_are_loaded():
    found = discover(entry("good", "GoodPlugin"), entry("other", "does_not_exist"))
    assert list(load_plugin_classes(["good"], found)) == ["good"]


def test_unknown_plugin_lists_the_available_ones():
    with pytest.raises(PluginError, match=r"unknown plugin 'missing'.*good"):
        load_plugin_classes(["missing"], discover(entry("good", "GoodPlugin")))


def test_unknown_plugin_with_nothing_installed():
    with pytest.raises(PluginError, match="none installed"):
        load_plugin_classes(["missing"], discover())


def test_import_failures_are_wrapped():
    with pytest.raises(PluginError, match="could not be loaded"):
        load_plugin_classes(["bad"], discover(entry("bad", "does_not_exist")))


def test_classes_that_are_not_plugins_are_rejected():
    with pytest.raises(PluginError, match="Plugin subclass"):
        load_plugin_classes(["bad"], discover(entry("bad", "NotAPlugin")))


def test_each_plugin_gets_its_own_context():
    plugins = create_plugins(["good"], make_context, discover(entry("good", "GoodPlugin")))
    assert isinstance(plugins["good"], GoodPlugin)
    assert plugins["good"].context.config == {"name": "good"}


def test_configuration_errors_surface_at_creation():
    with pytest.raises(PluginError, match="bad settings"):
        create_plugins(["broken"], make_context, discover(entry("broken", "BrokenConfigPlugin")))


def test_no_plugins_is_fine():
    assert create_plugins([], make_context, discover()) == {}
