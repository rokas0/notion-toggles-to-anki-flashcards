"""Pytest bootstrap: lets the add-on be imported without Anki or Qt.

Minimal stand-ins for ``aqt``, ``anki`` and ``PyQt6`` are installed when
the real ones are missing (``aqt.mw`` is None, which makes the plugin skip
its GUI setup). The add-on is also aliased as package ``n2a``, because its folder
name may not be a valid identifier (e.g. an AnkiWeb id).
"""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

ADDON_DIR = Path(__file__).resolve().parents[1]


def _stub_module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    # Anything not defined explicitly is a MagicMock (PEP 562)
    module.__getattr__ = lambda attr: MagicMock(name=f'{name}.{attr}')
    sys.modules[name] = module
    return module


class _BoundSignal:
    def __init__(self):
        self.calls = []
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *args):
        self.calls.append(args)
        for slot in self._slots:
            slot(*args)


class _Signal:
    """Per-instance signal recording its emits (``signal.calls``)."""

    def __init__(self, *types_):
        pass

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        return obj.__dict__.setdefault(self.name, _BoundSignal())


def _install_stubs() -> None:
    try:
        import PyQt6.QtCore  # noqa: F401
    except ImportError:
        base = type('QtBase', (), {'__init__': lambda self, *a, **k: None})
        pyqt6 = _stub_module('PyQt6')
        pyqt6.QtCore = _stub_module(
            'PyQt6.QtCore',
            QObject=base,
            QRunnable=type('QRunnable', (base,), {}),
            pyqtSignal=_Signal,
        )
        pyqt6.QtGui = _stub_module('PyQt6.QtGui')
        pyqt6.QtWidgets = _stub_module('PyQt6.QtWidgets')
    try:
        import aqt  # noqa: F401
    except ImportError:
        _stub_module('aqt', mw=None)
        _stub_module('aqt.gui_hooks')
        _stub_module('aqt.utils')
        _stub_module('anki')
        _stub_module('anki.consts')
        _stub_module('anki.models')
        _stub_module('anki.notes')
        _stub_module('anki.utils')


_install_stubs()


def _skip_addon_package_setup() -> None:
    """Stop pytest importing the add-on root ``__init__.py`` as a package.

    Its folder name need not be importable (``644963660``), and importing it
    would run the Anki entry point. Tests import modules via ``n2a`` instead.
    """
    from _pytest.python import Package

    original = Package.setup

    def setup(self):
        if Path(self.path) != ADDON_DIR:
            original(self)

    Package.setup = setup


_skip_addon_package_setup()

_pkg = types.ModuleType('n2a')
_pkg.__path__ = [str(ADDON_DIR)]  # type: ignore[attr-defined]
sys.modules.setdefault('n2a', _pkg)
