"""Helper functions."""

import html
import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional

#: Plugin base dir
BASE_DIR = Path(__file__).parent
#: Persistent per-user data (Anki keeps `user_files` across add-on updates)
USER_FILES_DIR = BASE_DIR / 'user_files'
#: Block id regex
BLOCK_ID_RE = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
)
_CLOZE_RE = re.compile(r'\{\{c\d+::(.*?)\}\}', re.DOTALL)
_TAG_RE = re.compile(r'<[^>]+>')

#: Root logger of the add-on. A NullHandler is attached once so nothing
#: leaks to stderr unless file logging is enabled.
_ROOT_LOGGER = logging.getLogger('notion_flashcards')
_ROOT_LOGGER.addHandler(logging.NullHandler())
_ROOT_LOGGER.propagate = False


def safe_path(original_path: Path) -> Path:
    """Get WinAPI path compatible with long paths if on Windows.

    :param original_path: original path
    :returns: WinAPI path on Windows or original path otherwise
    """
    if os.name != 'nt':
        return original_path
    path = str(original_path.absolute())
    if path.startswith('\\\\'):
        path = f'\\\\?\\UNC\\{path[2:]}'
    else:
        path = f'\\\\?\\{path}'
    return Path(path)


def enable_logging_to_file() -> None:
    """Enable logging to `log.txt` (idempotent)."""
    if any(
        isinstance(h, logging.FileHandler) for h in _ROOT_LOGGER.handlers
    ):
        return
    handler = logging.FileHandler(
        BASE_DIR / 'log.txt', mode='w', encoding='utf8'
    )
    handler.setFormatter(logging.Formatter(
        fmt=(
            '%(asctime)s - %(name)s - %(filename)s:%(lineno)d - '
            '%(levelname)s - %(message)s'
        )
    ))
    _ROOT_LOGGER.addHandler(handler)


def get_logger(name: str, debug: bool = False) -> logging.Logger:
    """Get a child logger of the add-on root logger.

    :param name: logger name
    :param debug: DEBUG logging level
    :returns: logger
    """
    logger = logging.getLogger(f'notion_flashcards.{name}')
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    return logger


def normalize_block_id(block_id: str) -> str:
    """Normalize Notion block id.

    I.e. 'd151217ae85f4e79a05406f7db2bb0da'
    -> 'd151217a-e85f-4e79-a054-06f7db2bb0da'

    :param block_id: Notion block id
    :returns: normalized block id
    """
    if not BLOCK_ID_RE.match(block_id):
        return (
            f'{block_id[:8]}-{block_id[8:12]}-{block_id[12:16]}-'
            f'{block_id[16:20]}-{block_id[20:]}'
        )
    return block_id


def normalize_front(text: str) -> str:
    """Normalize a note's front side for matching against existing notes.

    Strips cloze markers (``{{c1::foo}}`` -> ``foo``), unescapes HTML
    entities, strips tags and collapses whitespace. Unescaping happens
    *before* stripping tags so that notes stored before text was escaped
    (``List<String>``) and after (``List&lt;String&gt;``) normalize the same.

    :param text: front field HTML
    :returns: normalized text
    """
    text = _CLOZE_RE.sub(r'\1', text)
    text = html.unescape(text)
    text = _TAG_RE.sub('', text)
    return ' '.join(text.split())


def local_timezone() -> str:
    """Best-effort IANA name of the local time zone, 'UTC' if unknown."""
    tz = os.environ.get('TZ')
    if tz and '/' in tz:
        return tz.lstrip(':')
    try:
        link = os.path.realpath('/etc/localtime')
    except OSError:
        return 'UTC'
    if 'zoneinfo/' in link:
        return link.split('zoneinfo/', 1)[1]
    return 'UTC'


def safe_str(string: Optional[str]) -> str:
    """Get safe string for logging with system encoding.

    :param string: string to be sanitized
    :returns: sanitized string
    """
    if not string:
        return ''
    encoding = sys.getdefaultencoding()
    return string.encode(encoding, errors='backslashreplace').decode(encoding)
