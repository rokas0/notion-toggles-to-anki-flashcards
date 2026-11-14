"""Matching of toggles to existing notes, with a fake collection."""

import types

from n2a.notes_manager import NotesManager
from n2a.parser import AnkiNote

ID_A, ID_B = 'a' * 32, 'b' * 32
BASIC = {'id': 1, 'flds': [
    {'name': 'Front', 'ord': 0}, {'name': 'Back', 'ord': 1},
    {'name': 'Source', 'ord': 2}]}
CLOZE = {'id': 2, 'flds': [{'name': 'Front', 'ord': 0}, {'name': 'Source', 'ord': 1}]}


def link(block_id=None):
    url = 'https://notion.so/me/page' + (f'#{block_id}' if block_id else '')
    return f'<a href="{url}">{url}</a>'


def manager(rows):
    """rows: {note id: (model id, [fields])}"""
    nm = NotesManager.__new__(NotesManager)
    nm.logger = types.SimpleNamespace(debug=lambda *a: None, info=lambda *a: None)
    nm.collection = types.SimpleNamespace(
        models=types.SimpleNamespace(
            by_name=lambda name: BASIC if name == NotesManager.MODEL_NAME else CLOZE),
        db=types.SimpleNamespace(
            list=lambda sql, *args: list(rows),
            all=lambda sql: [(nid, mid, '\x1f'.join(f)) for nid, (mid, f) in rows.items()],
        ),
    )
    nm.deck, nm.deck_name = 7, 'Deck'
    nm.reset_index()
    return nm


def note(front, block_id=None):
    return AnkiNote(front=front, back='b', block_id=block_id)


def test_match_by_block_id_survives_rewording():
    nm = manager({10: (1, ['Old question', 'b', link(ID_A)])})
    assert nm.find_note(note('New question', ID_A)) == 10


def test_same_question_different_toggle_is_a_new_note():
    nm = manager({10: (1, ['Q', 'b', link(ID_A)])})
    assert nm.find_note(note('Q', ID_B)) is None


def test_legacy_note_matched_by_question_once():
    nm = manager({10: (1, ['Q', 'b', link()]), 11: (2, ['C {{c1::x}}', link()])})
    assert nm.find_note(note('Q', ID_A)) == 10
    assert nm.find_note(note('C x', ID_B)) == 11     # cloze, Source is field 1
    nm._remember(10, note('Q', ID_A))
    # A second toggle with the same question must not grab the same card
    assert nm.find_note(note('Q', ID_B)) is None
    assert nm.find_note(note('Reworded', ID_A)) == 10


def test_toggle_without_id_matches_any_note_by_question():
    nm = manager({10: (1, ['Q', 'b', link(ID_A)])})
    assert nm.find_note(note('Q')) == 10


def test_forget_removes_note_from_all_indexes():
    nm = manager({10: (1, ['Q', 'b', link(ID_A)])})
    nm.find_note(note('Q', ID_A))
    nm.forget(10)
    assert nm.find_note(note('Q', ID_A)) is None
    assert nm.find_note(note('Q')) is None
