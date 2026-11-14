from pathlib import Path

from n2a.parser import NoteDataExtractor, extract_notes_data

PAGE = """<html><body><article id="8a775ee4-82ab-4373-2abc-9319add819c5">
<ul id="t1" class="toggle"><li><details open=""><summary>What is <strong>IAM</strong>?</summary>
<p>#aws #security</p>
<p>Identity &amp; Access Management</p>
</details></li></ul>
<details class="other"><summary>not a toggle</summary><p>x</p></details>
<ul id="t2" class="toggle"><li><details open=""><summary>❕ ignored</summary><p>nope</p></details></li></ul>
</article></body></html>"""


def _write(tmp_path: Path, body: str) -> Path:
    page = tmp_path / 'page.html'
    page.write_text(body, encoding='utf8')
    return page


def test_basic_toggle(tmp_path):
    notes = extract_notes_data(_write(tmp_path, PAGE), 'me')
    assert len(notes) == 1
    note = notes[0]
    assert note.front == 'What is <strong>IAM</strong>?'
    assert note.tags == ['aws', 'security']
    assert 'Identity &amp; Access Management' in note.back
    assert note.source == (
        '<a href="https://notion.so/me/8a775ee482ab43732abc9319add819c5">'
        'https://notion.so/me/8a775ee482ab43732abc9319add819c5</a>'
    )


def test_text_is_escaped_not_injected(tmp_path):
    html = (
        '<ul class="toggle"><li><details><summary>List&lt;String&gt;</summary>'
        '<p>Use <code>&lt;div&gt;</code> &amp; friends</p></details></li></ul>'
    )
    note = NoteDataExtractor.extract_note(html, tmp_path)
    assert note.front == 'List&lt;String&gt;'
    assert '<code>&lt;div&gt;</code> &amp; friends' in note.back
    assert '<div>' not in note.back


def test_attribute_values_are_escaped(tmp_path):
    html = (
        '<ul class="toggle"><li><details><summary>Q</summary>'
        '<p><a href="https://x.test/?a=1&amp;b=&quot;2&quot;">link</a></p>'
        '</details></li></ul>'
    )
    note = NoteDataExtractor.extract_note(html, tmp_path)
    assert 'href="https://x.test/?a=1&amp;b=&quot;2&quot;"' in note.back


def test_modern_details_toggle_format(tmp_path):
    body = (
        '<html><body><article id="8a775ee4-82ab-4373-2abc-9319add819c5">'
        '<details class="toggle"><summary>Front</summary><p>Back</p></details>'
        '</article></body></html>'
    )
    notes = extract_notes_data(_write(tmp_path, body), 'me')
    assert [(n.front, n.back) for n in notes] == [('Front', '<p>Back</p>')]


def test_empty_back_without_cloze_gets_warning(tmp_path):
    html = '<ul class="toggle"><li><details><summary>Q</summary></details></li></ul>'
    note = NoteDataExtractor.extract_note(html, tmp_path)
    assert note.back == NoteDataExtractor.EMPTY_BLOCK_WARNING


def test_cloze_note_has_empty_back(tmp_path):
    html = (
        '<ul class="toggle"><li><details><summary>Capital of LT is '
        '{{c1::Vilnius}}</summary></details></li></ul>'
    )
    note = NoteDataExtractor.extract_note(html, tmp_path)
    assert note.back == ''


def test_inline_latex(tmp_path):
    html = (
        '<ul class="toggle"><li><details><summary>Q</summary><p>'
        '<span class="notion-text-equation-token"><span><math><semantics>'
        '<mrow><mi>a</mi></mrow><annotation encoding="application/x-tex">'
        'a &lt; b</annotation></semantics></math></span></span>'
        '</p></details></li></ul>'
    )
    note = NoteDataExtractor.extract_note(html, tmp_path)
    assert '\\(a &lt; b\\)' in note.back


def _image_note(tmp_path, data: bytes):
    folder = tmp_path / 'Page abc'
    folder.mkdir(exist_ok=True)
    (folder / 'img.png').write_bytes(data)
    html = (
        '<ul class="toggle"><li><details><summary>Q</summary>'
        '<figure><a href="Page%20abc/img.png"><img src="Page%20abc/img.png"/>'
        '</a></figure></details></li></ul>'
    )
    return NoteDataExtractor.extract_note(html, tmp_path)


def test_images_named_by_content(tmp_path):
    first = _image_note(tmp_path, b'one')
    same = _image_note(tmp_path, b'one')
    changed = _image_note(tmp_path, b'two')
    name = first.images[0].filename
    assert name.startswith('notion_') and name.endswith('.png')
    assert same.images[0].filename == name
    assert changed.images[0].filename != name
    assert 'Page%20abc' not in first.back
    assert first.back.count(name) == 2  # <a href> and <img src>


def _toggle(body: str, title: str = 'Q') -> str:
    return (
        f'<ul class="toggle"><li><details open=""><summary>{title}</summary>'
        f'{body}</details></li></ul>'
    )


def test_nested_toggle_stays_inside_parent_back(tmp_path):
    html = (
        '<details class="toggle"><summary>Outer</summary><p>body</p>'
        '<details class="toggle" open=""><summary>Inner</summary>'
        '<p>inner body</p></details><p>after</p></details>'
    )
    note = NoteDataExtractor.extract_note(html, tmp_path)
    assert note.front == 'Outer'
    assert note.back == (
        '<p>body</p><details class="toggle"><summary>Inner</summary>'
        '<p>inner body</p></details><p>after</p>'
    )


def test_nested_toggle_is_also_its_own_note(tmp_path):
    body = (
        '<html><body><article id="8a775ee4-82ab-4373-2abc-9319add819c5">'
        '<details class="toggle"><summary>Outer</summary>'
        '<details class="toggle"><summary>Inner</summary><p>x</p></details>'
        '</details></article></body></html>'
    )
    notes = extract_notes_data(_write(tmp_path, body), 'me')
    assert [n.front for n in notes] == ['Outer', 'Inner']
    assert notes[1].back == '<p>x</p>'


def test_unknown_tags_keep_their_text(tmp_path):
    note = NoteDataExtractor.extract_note(_toggle(
        '<p>first</p><p>Due <time>@Jan 1</time> later</p>'
        '<p>a <u>b</u> c</p><p>second</p>'
    ), tmp_path)
    assert note.back == (
        '<p>first</p><p>Due @Jan 1 later</p><p>a b c</p><p>second</p>'
    )


def test_script_and_style_content_is_dropped(tmp_path):
    note = NoteDataExtractor.extract_note(_toggle(
        '<p>a<style>p{color:red}</style><script>x()</script>b</p>'
    ), tmp_path)
    assert note.back == '<p>ab</p>'


def test_hash_in_title_is_not_tags(tmp_path):
    note = NoteDataExtractor.extract_note(
        _toggle('<p>answer</p>', title='#1 rule of X'), tmp_path)
    assert note.front == '#1 rule of X'
    assert note.tags == []


def test_hash_comment_in_code_is_not_tags(tmp_path):
    note = NoteDataExtractor.extract_note(_toggle(
        '<pre class="code"><code># install\npip install x</code></pre>'
        '<p>#python #tools</p>'
    ), tmp_path)
    assert '# install\npip install x' in note.back
    assert note.tags == ['python', 'tools']


def test_sentence_starting_with_hash_is_not_tags(tmp_path):
    note = NoteDataExtractor.extract_note(
        _toggle('<p>#1 priority is safety</p>'), tmp_path)
    assert note.back == '<p>#1 priority is safety</p>'
    assert note.tags == []


def test_self_closing_and_void_tags(tmp_path):
    note = NoteDataExtractor.extract_note(_toggle(
        '<p>a<br/>b<br>c</p><hr/><p>d</p>'
    ), tmp_path)
    assert note.back == '<p>a<br>b<br>c</p><hr><p>d</p>'


def test_block_latex(tmp_path):
    note = NoteDataExtractor.extract_note(_toggle(
        '<figure class="equation"><div class="equation-container">'
        '<span class="katex-display"><math><semantics><mrow><mi>x</mi></mrow>'
        '<annotation encoding="application/x-tex">x^2</annotation>'
        '</semantics></math></span></div></figure><p>after</p>'
    ), tmp_path)
    assert note.back == '\\[x^2\\]<p>after</p>'


BID = '8a775ee4-82ab-4373-2abc-9319add819c5'
BID_HEX = BID.replace('-', '')


def _page(toggles: str) -> str:
    return (
        '<html><body><article id="11111111-2222-3333-4444-555555555555">'
        f'{toggles}</article></body></html>'
    )


def test_block_id_from_classic_toggle(tmp_path):
    notes = extract_notes_data(_write(tmp_path, _page(
        f'<ul id="{BID}" class="toggle"><li><details open="">'
        '<summary>Q</summary><p>A</p></details></li></ul>'
    )), 'me')
    assert notes[0].block_id == BID_HEX
    url = f'https://notion.so/me/11111111222233334444555555555555#{BID_HEX}'
    assert notes[0].source == f'<a href="{url}">{url}</a>'


def test_block_id_from_details_toggle_and_no_namespace(tmp_path):
    notes = extract_notes_data(_write(tmp_path, _page(
        f'<details id="{BID}" class="toggle"><summary>Q</summary><p>A</p></details>'
    )), '')
    assert notes[0].block_id == BID_HEX
    assert f'https://notion.so/11111111222233334444555555555555#{BID_HEX}' in notes[0].source


def test_parent_toggle_does_not_take_nested_toggle_id(tmp_path):
    notes = extract_notes_data(_write(tmp_path, _page(
        '<details class="toggle"><summary>Outer</summary>'
        f'<p>x</p><details id="{BID}" class="toggle"><summary>Inner</summary>'
        '<p>y</p></details></details>'
    )), 'me')
    outer, inner = notes
    assert outer.block_id is None and inner.block_id == BID_HEX
