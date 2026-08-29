# -*- coding: utf-8 -*-
"""Expose this checkout under its canonical plugin package name."""
from __future__ import annotations

import asyncio
import importlib.machinery
import importlib.util
import inspect
import os
import sys
import types
from pathlib import Path

import pytest


PACKAGE_NAME = "astrbot_plugin_private_companion"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]


# Load the real package before legacy unit tests get a chance to install
# ``setdefault`` stubs into the shared full-suite process.  The explicit CI
# stub mode remains self-contained for artifact-import smoke tests.
if os.environ.get("ASTRBOT_CI_STUBS") != "1":
    try:
        import astrbot  # noqa: F401
    except ImportError:
        pass


if PACKAGE_NAME not in sys.modules:
    package = types.ModuleType(PACKAGE_NAME)
    package.__file__ = str(PLUGIN_ROOT / "__init__.py")
    package.__package__ = PACKAGE_NAME
    package.__path__ = [str(PLUGIN_ROOT)]
    spec = importlib.machinery.ModuleSpec(PACKAGE_NAME, loader=None, is_package=True)
    spec.submodule_search_locations = [str(PLUGIN_ROOT)]
    package.__spec__ = spec
    sys.modules[PACKAGE_NAME] = package


if os.environ.get("ASTRBOT_CI_STUBS") == "1" and "astrbot" not in sys.modules:
    class _Dummy:
        def __call__(self, *_args, **_kwargs):
            return self

        def __getattr__(self, _name):
            return self

        def __iter__(self):
            return iter(())

        def __bool__(self):
            return False


    class _Logger:
        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: None


    def _module(name: str, *, package: bool = False) -> types.ModuleType:
        value = types.ModuleType(name)
        if package:
            value.__path__ = []
        sys.modules[name] = value
        return value


    astrbot = _module("astrbot", package=True)
    api = _module("astrbot.api", package=True)
    event = _module("astrbot.api.event")
    core = _module("astrbot.core", package=True)
    utils = _module("astrbot.core.utils", package=True)
    paths = _module("astrbot.core.utils.astrbot_path")
    api.logger = _Logger()
    event.MessageChain = _Dummy
    event.AstrMessageEvent = _Dummy
    event.filter = _Dummy()
    paths.get_astrbot_data_path = lambda: Path(".")
    astrbot.api = api
    core.utils = utils

    quart = _module("quart")
    quart.request = _Dummy()
    quart.send_file = _Dummy()


_HAS_PYTEST_ASYNCIO = importlib.util.find_spec("pytest_asyncio") is not None


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "asyncio: run this coroutine test in an event loop",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    """Run marked async tests when the optional pytest-asyncio plugin is absent."""
    if _HAS_PYTEST_ASYNCIO or "asyncio" not in pyfuncitem.keywords:
        return None
    test_function = pyfuncitem.obj
    if not inspect.iscoroutinefunction(test_function):
        return None
    fixture_names = pyfuncitem._fixtureinfo.argnames
    kwargs = {name: pyfuncitem.funcargs[name] for name in fixture_names}
    asyncio.run(test_function(**kwargs))
    return True
