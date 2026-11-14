"""Smoke test: the add-on entry point and all its modules import cleanly."""

import importlib.util
import sys

from conftest import ADDON_DIR


def test_entry_point_imports_without_anki_window():
    spec = importlib.util.spec_from_file_location(
        'n2a_entry', ADDON_DIR / '__init__.py',
        submodule_search_locations=[str(ADDON_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules['n2a_entry'] = module
    try:
        spec.loader.exec_module(module)  # runs NotionSyncPlugin() with mw=None
    finally:
        for name in [n for n in sys.modules if n.startswith('n2a_entry')]:
            del sys.modules[name]
    assert module.NotionSyncPlugin._redacted(
        {'notion_token': 'secret', 'file_token': '', 'debug': True}
    ) == {'notion_token': '***', 'file_token': '', 'debug': True}
