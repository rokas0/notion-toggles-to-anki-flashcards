"""Background worker exporting all configured Notion pages in one batch.

Instead of one thread per page each polling Notion every couple of seconds
(which quickly hits Notion's rate limit), a single worker:

1. optionally skips pages whose ``last_edited_time`` has not changed,
2. enqueues export tasks (spaced by ``stagger`` seconds, at most
   ``max_in_flight`` at a time),
3. polls *all* running tasks with a single ``getTasks`` request, backing the
   poll interval off from 2s to 10s,
4. downloads, unzips and parses each export as soon as it is ready and emits
   the notes for the main thread to write to the collection.
"""

import re
import time
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal

from .helpers import get_logger, safe_path
from .notion_client import AuthError, NotionClient, NotionClientError, RateLimited
from .parser import AnkiNote, extract_notes_data


@dataclass
class PageJob:
    """One configured Notion page."""

    page_id: str
    deck: str
    recursive: bool
    #: Put each subpage's notes into a subdeck named after the subpage
    subdecks: bool = False
    #: `last_edited_time` fetched before export (for incremental sync)
    last_edited: Optional[int] = None
    #: Notion workspace of the page (needed to route task requests)
    space_id: Optional[str] = None


class SyncSignals(QObject):
    """Signals emitted by `SyncWorker` (delivered on the main thread)."""

    #: (deck, page's target deck, List[AnkiNote]) - notes for one deck
    result = pyqtSignal(str, str, object)
    #: error message for one page (other pages continue)
    error = pyqtSignal(str)
    #: (page_id, deck) - page unchanged since last sync, not exported
    page_skipped = pyqtSignal(str, str)
    #: (page_id, last_edited_time) - page exported and parsed successfully
    page_done = pyqtSignal(str, object)
    #: config key of a rejected token; the whole sync stops
    auth_failed = pyqtSignal(str)
    #: worker is done
    finished = pyqtSignal()


class SyncWorker(QRunnable):
    """Export and parse all pages."""

    POLL_MIN = 2.0
    POLL_MAX = 10.0
    POLL_GROWTH = 1.5
    RATE_LIMIT_MIN = 5.0
    RATE_LIMIT_MAX = 120.0
    #: Give up on a single export after this long, sec
    TASK_TIMEOUT = 15 * 60
    #: Give up after this many consecutive failed getTasks calls
    MAX_POLL_FAILURES = 5
    #: Give up enqueuing after this many rate limits in a row
    MAX_ENQUEUE_RATE_LIMITS = 8

    def __init__(
        self,
        jobs: List[PageJob],
        notion_token: str,
        file_token: str,
        notion_namespace: str,
        timezone: str,
        max_in_flight: int,
        stagger: float,
        known_last_edited: Optional[Dict[str, int]] = None,
        debug: bool = False,
    ) -> None:
        super().__init__()
        self.jobs = jobs
        self.notion_token = notion_token
        self.file_token = file_token
        self.notion_namespace = notion_namespace
        self.timezone = timezone
        self.max_in_flight = max(1, max_in_flight)
        self.stagger = max(0.0, stagger)
        #: page_id -> last_edited_time from the previous successful sync;
        #: None disables skipping
        self.known_last_edited = known_last_edited
        self.debug = debug
        self.cancelled = False
        self.signals = SyncSignals()
        self.logger = get_logger('SyncWorker', debug)
        self.client: Optional[NotionClient] = None

    # -- helpers ----------------------------------------------------------

    def cancel(self) -> None:
        """Ask the worker to stop as soon as possible."""
        self.cancelled = True

    def _sleep(self, seconds: float) -> None:
        """Sleep in small steps so cancellation is honoured quickly."""
        end = time.monotonic() + seconds
        while not self.cancelled and time.monotonic() < end:
            time.sleep(min(0.5, end - time.monotonic()))

    def _fail(self, job: PageJob, exc: Exception) -> None:
        self.logger.error('Page %s failed: %s', job.page_id, exc)
        self.signals.error.emit(f'Cannot export {job.page_id}:\n{exc}')

    # -- stages -----------------------------------------------------------

    def _filter_unchanged(self, jobs: List[PageJob]) -> List[PageJob]:
        """Drop pages that have not changed since the last sync.

        Only non-recursive pages are skipped: editing a subpage does not
        change its parent's `last_edited_time`.
        """
        assert self.client
        remaining = []
        for job in jobs:
            if self.known_last_edited is None or job.recursive:
                remaining.append(job)
                continue
            try:
                job.last_edited = self.client.get_last_edited(job.page_id)
            except AuthError:
                raise
            except NotionClientError as exc:
                self.logger.warning(
                    'Cannot get last_edited_time of %s: %s', job.page_id, exc
                )
            known = self.known_last_edited.get(job.page_id)
            if job.last_edited is not None and job.last_edited == known:
                self.logger.info('Page %s unchanged, skipping', job.page_id)
                self.signals.page_skipped.emit(job.page_id, job.deck)
            else:
                remaining.append(job)
        return remaining

    def _extract(self, archive: Path, destination: Path) -> None:
        """Extract a zip, skipping entries the file system can't hold."""
        with zipfile.ZipFile(archive) as zip_file:
            for member in zip_file.infolist():
                try:
                    zip_file.extract(member, destination)
                except OSError as exc:
                    # e.g. file names too long for the file system
                    self.logger.warning('Skipping %s: %s', member.filename, exc)

    def _process_export(self, job: PageJob, url: str) -> None:
        """Download, unzip and parse one export, then emit its notes."""
        assert self.client
        with TemporaryDirectory() as tmp_dir:
            tmp_path = safe_path(Path(tmp_dir))
            archive = tmp_path / 'export.zip'
            self.client.download(url, archive)
            self._extract(archive, tmp_path)
            archive.unlink()
            # Large exports come split into parts: zips inside the zip
            for _ in range(3):
                inner = list(tmp_path.rglob('*.zip'))
                if not inner:
                    break
                for part in inner:
                    self._extract(part, part.parent)
                    part.unlink()
            html_paths = sorted(tmp_path.rglob('*.html'))
            # The page itself is the shallowest file; subpages sit in folders
            # named after their parent pages
            root_dir = min(
                html_paths, key=lambda p: len(p.parts), default=tmp_path / 'x'
            ).parent
            by_deck: Dict[str, List[AnkiNote]] = defaultdict(list)
            parse_errors = 0
            for html_path in html_paths:
                deck = (
                    subdeck_for(html_path, root_dir, job.deck)
                    if job.subdecks else job.deck
                )
                try:
                    by_deck[deck] += extract_notes_data(
                        source=html_path,
                        notion_namespace=self.notion_namespace,
                        debug=self.debug,
                    )
                except Exception:  # one bad page must not lose the rest
                    parse_errors += 1
                    self.logger.exception('Cannot parse %s', html_path.name)
        self.logger.info(
            'Page %s: %d notes in %d deck(s) (%d files failed to parse)',
            job.page_id, sum(len(n) for n in by_deck.values()), len(by_deck),
            parse_errors,
        )
        for deck, notes in by_deck.items():
            self.signals.result.emit(deck, job.deck, notes)
        if parse_errors:
            self.signals.error.emit(
                f'{parse_errors} file(s) of page {job.page_id} could not be '
                f'parsed, see log'
            )
        else:
            self.signals.page_done.emit(job.page_id, job.last_edited)

    # -- main loop --------------------------------------------------------

    def run(self) -> None:
        try:
            self.client = NotionClient(
                self.notion_token, self.debug, self.file_token
            )
            self._run()
        except AuthError as exc:
            # Every other page would fail the same way: stop and say why
            self.logger.error('%s', exc)
            self.signals.auth_failed.emit(exc.token_key)
        except Exception as exc:  # never let the thread die silently
            self.logger.exception('Sync worker crashed')
            self.signals.error.emit(f'Sync failed: {exc}')
        finally:
            self.signals.finished.emit()

    def _run(self) -> None:
        assert self.client
        pending = self._filter_unchanged(list(self.jobs))
        #: task id -> (job, enqueued at)
        in_flight: Dict[str, Tuple[PageJob, float]] = {}
        poll_interval = self.POLL_MIN
        rate_limit_wait = self.RATE_LIMIT_MIN
        poll_failures = 0
        enqueue_rate_limits = 0

        while (pending or in_flight) and not self.cancelled:
            # 1. Enqueue new exports while there is capacity
            while pending and len(in_flight) < self.max_in_flight:
                job = pending[0]
                try:
                    if job.space_id is None:
                        job.space_id = self.client.get_space_id(job.page_id)
                    task_id = self.client.enqueue_export_task(
                        job.page_id, job.recursive, self.timezone, job.space_id
                    )
                except RateLimited as exc:
                    enqueue_rate_limits += 1
                    if enqueue_rate_limits >= self.MAX_ENQUEUE_RATE_LIMITS:
                        for job in pending:
                            self._fail(job, exc)
                        pending.clear()
                        break
                    wait = exc.wait or rate_limit_wait
                    self.logger.warning('Rate limited on enqueue, %.0fs', wait)
                    self._sleep(wait)
                    rate_limit_wait = min(
                        rate_limit_wait * 2, self.RATE_LIMIT_MAX
                    )
                    break  # poll what is running, retry this page later
                except AuthError:
                    raise
                except NotionClientError as exc:
                    pending.pop(0)
                    self._fail(job, exc)
                    continue
                pending.pop(0)
                enqueue_rate_limits = 0
                in_flight[task_id] = (job, time.monotonic())
                poll_interval = self.POLL_MIN
                if pending and self.stagger:
                    self._sleep(self.stagger)
            if not in_flight:
                continue

            # 2. Poll every running task with a single request
            self._sleep(poll_interval)
            if self.cancelled:
                break
            try:
                # One request per workspace (almost always just one)
                by_space: Dict[Optional[str], List[str]] = defaultdict(list)
                for task_id, (job, _) in in_flight.items():
                    by_space[job.space_id].append(task_id)
                tasks: Dict[str, Dict] = {}
                for space_id, task_ids in by_space.items():
                    tasks.update(self.client.get_tasks(task_ids, space_id))
            except RateLimited as exc:
                wait = exc.wait or rate_limit_wait
                self.logger.warning('Rate limited on poll, waiting %.0fs', wait)
                self._sleep(wait)
                rate_limit_wait = min(rate_limit_wait * 2, self.RATE_LIMIT_MAX)
                continue
            except AuthError:
                raise
            except NotionClientError as exc:
                poll_failures += 1
                self.logger.warning(
                    'Poll failed (%d/%d): %s',
                    poll_failures, self.MAX_POLL_FAILURES, exc,
                )
                if poll_failures >= self.MAX_POLL_FAILURES:
                    for job, _ in in_flight.values():
                        self._fail(job, exc)
                    in_flight.clear()
                continue
            poll_failures = 0
            rate_limit_wait = self.RATE_LIMIT_MIN

            # 3. Handle finished / failed / timed out tasks
            for task_id, (job, started) in list(in_flight.items()):
                try:
                    task = tasks.get(task_id)
                    url = (
                        self.client.export_url_from_task(task)
                        if task else None
                    )
                    if url:
                        del in_flight[task_id]
                        self._process_export(job, url)
                    elif time.monotonic() - started > self.TASK_TIMEOUT:
                        del in_flight[task_id]
                        self._fail(job, NotionClientError('Export timed out'))
                except AuthError:
                    raise
                except Exception as exc:
                    in_flight.pop(task_id, None)
                    self._fail(job, exc)
            poll_interval = min(
                poll_interval * self.POLL_GROWTH, self.POLL_MAX
            )


#: Notion export names end with the page id: "My page 8a775ee4...c5"
_EXPORT_ID_SUFFIX_RE = re.compile(r'\s*[0-9a-f]{32}$')


def _title_from_export_name(name: str) -> str:
    """Page title from an exported file/folder name, safe as a deck name."""
    title = _EXPORT_ID_SUFFIX_RE.sub('', name).strip()
    # "::" separates subdecks and quotes break Anki searches
    title = title.replace('::', ':').replace('"', '')
    return title or 'Untitled'


def subdeck_for(html_path: Path, root_dir: Path, root_deck: str) -> str:
    """Deck for an exported page: subpages become nested subdecks.

    `Root abc.html` -> `root_deck`;
    `Root abc/Sub def.html` -> `root_deck::Sub`;
    `Root abc/Sub def/Leaf 123.html` -> `root_deck::Sub::Leaf`.
    """
    try:
        parts = html_path.relative_to(root_dir).parts
    except ValueError:
        return root_deck
    if len(parts) <= 1:
        return root_deck
    # parts[0] is the root page's folder; the rest are subpage names
    chain = [_title_from_export_name(p) for p in parts[1:-1]]
    chain.append(_title_from_export_name(Path(parts[-1]).stem))
    return '::'.join([root_deck, *chain])
