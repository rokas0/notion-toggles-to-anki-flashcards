"""Parser to extract Anki note data from HTML."""

import hashlib
import html
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote

from bs4 import BeautifulSoup

from .helpers import BLOCK_ID_RE, get_logger


@dataclass
class AnkiImage:
    """An image from HTML document."""

    #: `src` attribute as is in HTML document
    src: str
    #: Filename to be stored as
    filename: str
    #: Absolute path to the image
    abs_path: Path
    #: Image data
    data: bytes


@dataclass
class AnkiNote:
    """Anki note model."""

    #: Front side
    front: str
    #: Back side (can be empty for cloze note)
    back: Optional[str] = None
    #: Tags
    tags: List[str] = field(default_factory=list)
    #: Link to Notion page
    source: Optional[str] = None
    #: Note images
    images: List[AnkiImage] = field(default_factory=list)
    #: Notion block id of the toggle (32 hex chars, no dashes), if known
    block_id: Optional[str] = None


class NoteDataExtractor(HTMLParser):
    """Parser to extract Anki note data from HTML.

    For each toggle block Notion generates HTML like this:
    <ul id=<guid> class="toggle">
        <li>
            <details open="">
                <summary>Front side of a note</summary>
                <p>Back side paragraph 1.</p>
                <p>Back side paragraph 2.</p>
            </details>
        </li>
    </ul>

    LaTeX blocks follow MathML notation with a lot of generated HTML tags,
    but parser only cares about <annotation> tag which contains LaTeX code
    and <div class="equation-container"> wrapper tag to differ inline and
    block LaTeX.
    """

    #: Allowed tags
    ALLOWED_TAGS = {
        'h1',
        'h2',
        'h3',
        'h4',
        'h5',
        'p',
        'br', # Os change
        'strong',
        'summary',
        'em',
        'del',
        'pre',
        'code',
        'mark',
        'ul',
        'ol',
        'li',
        'div',
        'span',
        'blockquote',
        'hr',
        'figure',
        'a',
        'img',
        'figcaption',
        'svg',
        'path',
        'table',
        'thead',
        'tr',
        'th',
        'tbody',
        'td',
    }
    #: Ignore toggles starting with this symbol (White Exclamation Mark
    #: Ornament)
    IGNORE_MARK = '\u2755'
    #: Do not save these attributes
    SKIP_ATTRIBUTES = {'id', 'data-token-index'}
    #: Notion inline LaTeX class
    INLINE_LATEX_CLASS = 'notion-text-equation-token'
    #: Notion block LaTeX class
    BLOCK_LATEX_CLASS = 'equation'
    #: Anki block LaTeX tags
    ANKI_BLOCK_LATEX_TAGS = ('\\[', '\\]')
    #: Anki LaTeX tags
    ANKI_INLINE_LATEX_TAGS = ('\\(', '\\)')
    #: Empty paragraphs regex
    EMPTY_P_RE = re.compile(r'<p [^>]*></p>')
    #: Cloze regex
    CLOZE_RE = re.compile(r'{{c\d+::.*?}}')
    #: Warning text then Notion block was not exported properly and is empty
    EMPTY_BLOCK_WARNING = (
        '<p class="empty_block">Block was empty on export</p>'
    )

    #: Tags without a closing tag
    VOID_TAGS = {'br', 'hr', 'img', 'col', 'input', 'wbr'}
    #: Tags whose content is dropped entirely
    DROP_CONTENT_TAGS = {'script', 'style', 'title'}
    #: Extra tags kept on the back side so nested toggles stay collapsible
    NESTED_TOGGLE_TAGS = {'details', 'summary'}
    #: A paragraph consisting only of #tags, e.g. "#aws #iam"
    TAGS_LINE_RE = re.compile(r'^\s*(#[^\s#]+\s*)+$')

    #: Parser phases
    _PRE, _FRONT, _BACK, _DONE = range(4)

    def __init__(self, base_dir: Path, debug: bool = False) -> None:
        """Init extractor.

        :param base_dir: base dir of a file being parsed
        :param debug: debug mode
        """
        super().__init__(convert_charrefs=True)
        self.logger = get_logger(self.__class__.__name__, debug)
        self.base_dir = base_dir
        self.note_data: Dict[str, Any] = {}
        self._buffer: List[str] = []
        self._phase = self._PRE
        #: Open elements as (tag, text to emit when it closes or None)
        self._stack: List[Tuple[str, Optional[str]]] = []
        #: Stack depth of the toggle's own <summary>
        self._root_depth = 0
        #: Stack depth of the LaTeX wrapper we are inside of, 0 if none
        self._latex_depth = 0
        self._in_annotation = False
        #: Stack depth of a dropped element (<script> etc.), 0 if none
        self._drop_depth = 0

    def _get_attr_by_name(
        self, name: str, attrs: Iterable[Tuple[str, Optional[str]]]
    ) -> Optional[str]:
        """Get an attribute value by its name.

        :param name: name of an attribute
        :param attrs: attributes
        :returns: value of the attribute or None if no such attribute
        """
        for attr, value in attrs:
            if attr == name:
                return value
        return None

    def _check_if_latex(
        self, tag: str, attrs: Iterable[Tuple[str, Optional[str]]]
    ) -> Optional[Tuple[str, str]]:
        """Check if tag manifests start of either inline or block LaTeX.

        :param tag: a tag
        :param attrs: attributes
        :returns: inline or block Anki LaTeX tags or None if not a LaTeX block
        """
        class_ = self._get_attr_by_name('class', attrs)
        if tag in ('span', 'figure'):
            if class_ == self.BLOCK_LATEX_CLASS:
                return self.ANKI_BLOCK_LATEX_TAGS
            elif class_ == self.INLINE_LATEX_CLASS:
                return self.ANKI_INLINE_LATEX_TAGS
        return None

    def _push(self, tag: str, closing: Optional[str] = None) -> None:
        """Remember an open element and what to emit when it closes."""
        if tag not in self.VOID_TAGS:
            self._stack.append((tag, closing))

    def _inside(self, *tags: str) -> bool:
        return any(open_tag in tags for open_tag, _ in self._stack)

    def _render_start_tag(
        self, tag: str, attrs: Iterable[Tuple[str, Optional[str]]]
    ) -> str:
        skip = set(self.SKIP_ATTRIBUTES)
        if tag == 'details':
            skip.add('open')  # nested toggles start collapsed on the card
        filtered = [
            (attr, value) for attr, value in attrs
            if attr not in skip and not (
                # Internal Notion links make no sense outside Notion
                tag == 'a' and attr == 'href' and value
                and value.startswith(('open:', '#'))
            )
        ]
        # Attribute values arrive unescaped (convert_charrefs), so
        # re-escape them or a `"` inside a value breaks the markup
        rendered = ' '.join(
            f'{attr}="{html.escape(value or "", quote=True)}"'
            for attr, value in filtered
        )
        return f'<{tag} {rendered}>' if rendered else f'<{tag}>'

    def _track_image(self, attrs: Iterable[Tuple[str, Optional[str]]]) -> None:
        """Remember a local image so it can be uploaded to Anki's media."""
        src = self._get_attr_by_name('src', attrs)
        if not src:
            return
        if src.startswith(('http://', 'https://')):
            self.logger.warning(f'Skipping remote image: {src}')
            return
        abs_path = self.base_dir / unquote(src)
        if not abs_path.exists():
            self.logger.warning(f'Image file not found: {abs_path}')
            return
        # Name the media file after its content: identical images are
        # stored once, and a changed image gets a new name so it is
        # actually re-uploaded (Anki skips names it already has)
        data = abs_path.read_bytes()
        digest = hashlib.sha1(data).hexdigest()[:16]
        self.note_data.setdefault('images', []).append(AnkiImage(
            src=src,
            filename=f'notion_{digest}{abs_path.suffix.lower()}',
            abs_path=abs_path,
            data=data,
        ))

    def handle_startendtag(
        self, tag: str, attrs: Iterable[Tuple[str, Optional[str]]]
    ) -> None:
        """Handle `<tag/>`: a start tag that never gets an end tag."""
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID_TAGS:
            self.handle_endtag(tag)

    def handle_starttag(
        self, tag: str, attrs: Iterable[Tuple[str, Optional[str]]]
    ) -> None:
        """Handle a start tag.

        :param tag: a tag
        :param attrs: tag attributes
        """
        if self._phase == self._DONE:
            return
        # Everything before the toggle's own <summary> is wrapper markup
        if self._phase == self._PRE:
            self._push(tag)
            if tag == 'summary':
                self._phase = self._FRONT
                self._root_depth = len(self._stack)
            return
        # Inside LaTeX only the text of <annotation> (the TeX source) matters
        if self._latex_depth or self._drop_depth:
            self._push(tag)
            if self._latex_depth and tag == 'annotation':
                self._in_annotation = True
            return
        if tag in self.DROP_CONTENT_TAGS:
            self._push(tag)
            self._drop_depth = len(self._stack)
            return
        if (latex_tags := self._check_if_latex(tag, attrs)) is not None:
            self._buffer.append(latex_tags[0])
            self._push(tag, latex_tags[1])
            self._latex_depth = len(self._stack)
            return
        allowed = tag in self.ALLOWED_TAGS or (
            self._phase == self._BACK and tag in self.NESTED_TOGGLE_TAGS
        )
        if not allowed:
            # Drop the tag itself but keep its text (e.g. <time>, <u>)
            self._push(tag)
            return
        self._buffer.append(self._render_start_tag(tag, attrs))
        self._push(tag, f'</{tag}>')
        if tag == 'img':
            self._track_image(attrs)

    def handle_data(self, data: str) -> None:
        """Handle data.

        :param data: data
        """
        if self._phase in (self._PRE, self._DONE) or self._drop_depth:
            return
        if self._latex_depth:
            if self._in_annotation:
                self._buffer.append(html.escape(data, quote=False))
            return
        # A body paragraph made only of #tags sets the note's tags (once).
        # Titles, code blocks and ordinary text starting with "#" are content.
        if (
            self._phase == self._BACK
            and 'tags' not in self.note_data
            and self._stack and self._stack[-1][0] == 'p'
            and not self._inside('pre', 'code')
            and self.TAGS_LINE_RE.match(data)
        ):
            self.note_data['tags'] = [
                tag.strip() for tag in data.split('#') if tag.strip()
            ]
            return
        # Text arrives unescaped (convert_charrefs), so escape it again:
        # otherwise a note *about* `<div>` would inject a real tag
        self._buffer.append(html.escape(data, quote=False))

    def handle_endtag(self, tag: str) -> None:
        """Handle end tag.

        :param tag: tag
        """
        if self._phase == self._DONE or tag in self.VOID_TAGS:
            return
        if not any(open_tag == tag for open_tag, _ in self._stack):
            return  # stray end tag
        # Close everything up to and including the matching element
        while self._stack:
            depth = len(self._stack)
            open_tag, closing = self._stack.pop()
            if depth == self._latex_depth:
                self._latex_depth, self._in_annotation = 0, False
            elif open_tag == 'annotation':
                self._in_annotation = False
            if depth == self._drop_depth:
                self._drop_depth = 0
            if depth == self._root_depth and self._phase == self._FRONT:
                # The toggle's own </summary>: the front side is complete
                self.note_data['front'] = ''.join(self._buffer)
                self._buffer.clear()
                self._phase = self._BACK
            elif closing is not None:
                self._buffer.append(closing)
            if open_tag == tag:
                break
        if self._phase == self._BACK and len(self._stack) < self._root_depth - 1:
            # The element that held the <summary> has closed: toggle is over
            self._phase = self._DONE

    def get_data(self) -> Optional[AnkiNote]:
        """Collect the rest of the data.

        :returns: Note model
        """
        # Pop accidental trailing new lines
        while self._buffer and self._buffer[-1] in '\n\r':
            self._buffer.pop()
        back = ''.join(self._buffer)
        # Rewrite images src
        for image in self.note_data.get('images', []):
            back = back.replace(
                html.escape(image.src, quote=True), image.filename
            )
        # Remove empty paragraphs
        back = self.EMPTY_P_RE.sub('', back)
        # Remove empty classes
        back = back.replace('class=""', '').replace('<p >', '<p>')
        self.note_data['back'] = back
        try:
            note = AnkiNote(**self.note_data)
        except TypeError as exc:
            self.logger.error('Parsing error', exc_info=exc)
        else:
            return note
        return None

    @classmethod
    def extract_note(
        cls, html: str, base_dir: Path, debug: bool = False
    ) -> Optional[AnkiNote]:
        """Extract Note from HTML fragment.

        :param html: HTML
        :param base_dir: directory of HTML file (for construct absolute path
            of images)
        :param debug: debug mode
        :returns: note object
        """
        parser = cls(base_dir, debug)
        parser.feed(html)
        note = parser.get_data()
        # Check corresponding block was exported properly: if the backside of a
        # note is empty but the front side does not contain a cloze then Notion
        # failed to export that block
        if note and not note.back and not cls.CLOZE_RE.search(note.front):
            parser.logger.warning(
                'Empty backside but no cloze: %s', note.front
            )
            note.back = cls.EMPTY_BLOCK_WARNING
        # Skip if note starts with the ignore mark
        if note and note.front.startswith(cls.IGNORE_MARK):
            return None
        return note


def extract_notes_data(
    source: Path, notion_namespace: str, debug: bool = False
) -> List[AnkiNote]:
    """Extract notes data from HTML source.

    :param source: HTML path
    :param notion_namespace: Notion namespace (to form `source` fields)
    :param debug: debug mode
    :returns: notes
    """
    notes = []
    html_doc = source.read_text(encoding='utf8')
    soup = BeautifulSoup(html_doc, 'html.parser')
    article = soup.find_all('article')[0]
    article_id = article['id'].replace('-', '')
    # Notion used to export toggles as <ul class="toggle"><li><details>…
    # Modern Notion exports toggles directly as <details class="toggle">…
    # Support both formats.
    toggle_nodes = soup.find_all('ul', 'toggle') or soup.find_all('details', 'toggle')
    page_url = (
        f'https://notion.so/{notion_namespace}/{article_id}'
        if notion_namespace else f'https://notion.so/{article_id}'
    )
    for note_node in toggle_nodes:
        note = NoteDataExtractor.extract_note(
            html=str(note_node), base_dir=source.parent, debug=debug
        )
        if not note:
            continue
        note.block_id = toggle_block_id(note_node)
        # The block anchor makes the link jump to the toggle in Notion, and
        # lets later syncs recognise the card even if its question changes
        url = f'{page_url}#{note.block_id}' if note.block_id else page_url
        note.source = f'<a href="{url}">{url}</a>'
        notes.append(note)
    return notes


def toggle_block_id(node: Any) -> Optional[str]:
    """Notion block id of a toggle element, without dashes.

    The id sits on the toggle's outer element (`<ul id=...>` in the classic
    export format, `<details id=...>` in the newer one).
    """
    # Only the toggle's own wrapper: in the classic format also its direct
    # <li>. A <details> child may be a nested toggle with an id of its own.
    candidates = [node]
    if node.name == 'ul':
        candidates += node.find_all('li', recursive=False)
    for element in candidates:
        block_id = element.get('id') or ''
        if BLOCK_ID_RE.fullmatch(block_id) or re.fullmatch(r'[0-9a-f]{32}', block_id):
            return block_id.replace('-', '')
    return None


if __name__ == '__main__':
    import sys
    from pprint import pprint

    source = sys.argv[1]
    notes = extract_notes_data(Path(source), 'test')
    pprint(notes)
