"""Find a page's target deck, even after the deck was moved.

A deck is matched by the last part of its name, so `target_deck:
"Daily::awssap2a"` (or just `"awssap2a"`) still finds the deck after it was
dragged into `Daily::AWS::awssap2a`, instead of creating a new, empty one.
"""

from typing import Iterable, List, Tuple

#: (deck name, deck id)
Deck = Tuple[str, int]


def _parts(name: str) -> List[str]:
    return [part.strip() for part in name.split('::')]


def deck_leaf(name: str) -> str:
    """Last part of a deck name: `Daily::AWS::awssap2a` -> `awssap2a`."""
    return _parts(name)[-1]


def deck_candidates(target: str, decks: Iterable[Deck]) -> List[Deck]:
    """Existing decks a configured `target_deck` may refer to.

    Every deck whose last name part equals the target's is a candidate
    (case-insensitive, like Anki). The deck at exactly the configured path,
    if it exists, comes first; the rest are sorted by name.

    :param target: configured `target_deck`, a full path or just a name
    :param decks: all (non-filtered) decks of the collection
    :returns: matching decks
    """
    leaf = deck_leaf(target).casefold()
    path = '::'.join(_parts(target)).casefold()
    found = [d for d in decks if deck_leaf(d[0]).casefold() == leaf]
    found.sort(key=lambda d: (
        '::'.join(_parts(d[0])).casefold() != path, d[0].casefold()
    ))
    return found
