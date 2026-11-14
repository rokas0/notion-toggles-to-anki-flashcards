import json

import jsonschema
import pytest

from n2a.helpers import BASE_DIR, normalize_block_id, normalize_front


@pytest.mark.parametrize('stored, fresh', [
    # notes written before text was escaped vs. after
    ('List<String>', 'List&lt;String&gt;'),
    ('a < b', 'a &lt; b'),
    ('Q &amp; A', 'Q &amp; A'),
    # old cloze-format note vs. current HTML note
    ('kas yra {{c1::/s á v/}}', 'kas yra <code>/s á v/</code>'),
    ('two  spaces&nbsp;here', 'two spaces here'),
])
def test_normalize_front_matches_old_and_new(stored, fresh):
    assert normalize_front(stored) == normalize_front(fresh)


def test_normalize_front_keeps_distinct_text_distinct():
    assert normalize_front('<b>IAM</b> role') != normalize_front('IAM policy')


def test_normalize_block_id():
    assert normalize_block_id('d151217ae85f4e79a05406f7db2bb0da') == (
        'd151217a-e85f-4e79-a054-06f7db2bb0da'
    )
    dashed = 'd151217a-e85f-4e79-a054-06f7db2bb0da'
    assert normalize_block_id(dashed) == dashed


def test_default_config_matches_schema():
    schema = json.loads((BASE_DIR / 'schemas/config_schema.json').read_text())
    config = json.loads((BASE_DIR / 'config.json').read_text())
    jsonschema.validate(config, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**config, 'unknown_key': 1}, schema)


def test_page_without_id_is_rejected():
    schema = json.loads((BASE_DIR / 'schemas/config_schema.json').read_text())
    config = json.loads((BASE_DIR / 'config.json').read_text())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {**config, 'notion_pages': [{'target_deck': 'X'}]}, schema)
