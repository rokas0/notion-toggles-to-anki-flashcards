"""`NotionSyncPlugin._resolve_decks`: find moved decks, ask when ambiguous."""

import importlib.util
import json
import logging
import sys
from types import SimpleNamespace

import pytest

from conftest import ADDON_DIR


def _load_entry_point():
    spec = importlib.util.spec_from_file_location(
        'n2a_resolve', ADDON_DIR / '__init__.py',
        submodule_search_locations=[str(ADDON_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules['n2a_resolve'] = module
    spec.loader.exec_module(module)  # mw is None: no GUI setup
    return module


n2a = _load_entry_point()
PageJob = n2a.PageJob


class FakeDecks:
    def __init__(self, names):
        self.by_name = {name: i for i, name in enumerate(names, 1)}

    def all_names_and_ids(self, include_filtered=True):
        return [SimpleNamespace(name=n, id=i) for n, i in self.by_name.items()]

    def id(self, name, create=False):
        if name not in self.by_name and create:
            self.by_name[name] = len(self.by_name) + 1
        return self.by_name.get(name)


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    def make(deck_names, answers=()):
        monkeypatch.setattr(n2a, 'mw', None)
        p = n2a.NotionSyncPlugin()  # while mw is None: no GUI setup
        decks = FakeDecks(deck_names)
        monkeypatch.setattr(n2a, 'mw', SimpleNamespace(col=SimpleNamespace(decks=decks)))
        monkeypatch.setattr(n2a, 'STATE_FILE', tmp_path / 'sync_state.json')
        monkeypatch.setattr(n2a, 'USER_FILES_DIR', tmp_path)
        p.logger = logging.getLogger('test')
        asked = []
        answers = list(answers)

        def ask(target, names):
            asked.append((target, names))
            return answers.pop(0)
        p._ask_deck = ask
        return p, decks, asked
    return make


def job(deck, page='p1'):
    return PageJob(page_id=page, deck=deck, recursive=True)


def test_moved_deck_is_reused(plugin):
    p, decks, asked = plugin(['Daily', 'Daily::AWS', 'Daily::AWS::awssap2a'])
    [resolved] = p._resolve_decks([job('Daily::awssap2a')])
    assert resolved.deck == 'Daily::AWS::awssap2a'
    assert 'Daily::awssap2a' not in decks.by_name
    assert not asked


def test_bare_name(plugin):
    p, _, _ = plugin(['Daily::AWS::awssap2a'])
    assert p._resolve_decks([job('awssap2a')])[0].deck == 'Daily::AWS::awssap2a'


def test_missing_deck_is_created_at_configured_path(plugin):
    p, decks, _ = plugin(['Daily'])
    assert p._resolve_decks([job('Daily::new')])[0].deck == 'Daily::new'
    assert 'Daily::new' in decks.by_name


def test_ambiguous_asks_once_and_remembers(plugin):
    names = ['Daily::awssap2a', 'Daily::AWS::awssap2a']
    p, _, asked = plugin(names, answers=['Daily::AWS::awssap2a'])
    jobs = [job('Daily::awssap2a', 'p1'), job('Daily::awssap2a', 'p2')]
    assert [j.deck for j in p._resolve_decks(jobs)] == ['Daily::AWS::awssap2a'] * 2
    assert asked == [('Daily::awssap2a', names)]  # exact path offered first
    # Next sync: remembered, even after the deck is moved again
    p2, decks, asked2 = plugin([])
    decks.by_name = {'Daily::awssap2a': 1, 'Archive::awssap2a': 2}
    assert p2._resolve_decks(jobs)[0].deck == 'Archive::awssap2a'
    assert not asked2


def test_changed_target_deck_resolves_again(plugin, tmp_path):
    p, _, _ = plugin(['A::x', 'B::y'])
    p._resolve_decks([job('x')])
    assert p._resolve_decks([job('y')])[0].deck == 'B::y'


def test_cancel_aborts(plugin):
    p, _, _ = plugin(['A::x', 'B::x'], answers=[None])
    assert p._resolve_decks([job('x')]) is None


def test_last_edited_state_survives(plugin, tmp_path):
    p, _, _ = plugin(['x'])
    p._save_state({'p1': 5})
    p._resolve_decks([job('x')])
    p._save_state({'p2': 6})
    data = json.loads((tmp_path / 'sync_state.json').read_text())
    assert data['last_edited'] == {'p1': 5, 'p2': 6}
    assert data['decks']['p1']['target'] == 'x'
