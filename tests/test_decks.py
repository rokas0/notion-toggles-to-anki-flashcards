from n2a.decks import deck_candidates, deck_leaf

DECKS = [
    ('Daily', 1),
    ('Daily::AWS', 2),
    ('Daily::AWS::awssap2a', 3),
    ('Daily::awssap2a+', 4),
    ('Daily::Anglu', 5),
]


def test_leaf():
    assert deck_leaf('Daily::AWS::awssap2a') == 'awssap2a'
    assert deck_leaf('awssap2a') == 'awssap2a'
    assert deck_leaf('Daily :: awssap2a ') == 'awssap2a'


def test_moved_deck_is_found_by_name():
    assert deck_candidates('Daily::awssap2a', DECKS) == [
        ('Daily::AWS::awssap2a', 3)
    ]


def test_bare_name_is_enough():
    assert deck_candidates('awssap2a', DECKS) == [('Daily::AWS::awssap2a', 3)]
    assert deck_candidates('anglu', DECKS) == [('Daily::Anglu', 5)]


def test_no_match():
    assert deck_candidates('Daily::awssap3', DECKS) == []


def test_exact_path_listed_first():
    decks = DECKS + [('Archive::awssap2a', 6), ('Daily::awssap2a', 7)]
    assert deck_candidates('Daily::awssap2a', decks) == [
        ('Daily::awssap2a', 7),
        ('Archive::awssap2a', 6),
        ('Daily::AWS::awssap2a', 3),
    ]
