"""Tests for the batched export/poll loop with a fake Notion client."""

from typing import Dict, List

import pytest

from n2a.notion_client import NotionClient, NotionClientError, RateLimited
from n2a.sync_worker import PageJob, SyncWorker


class FakeClient:
    """Each task completes after `ready_after[page]` polls."""

    def __init__(self, ready_after: Dict[str, int], fail=(), edited=None,
                 rate_limit_polls=0):
        self.ready_after = ready_after
        self.fail = set(fail)
        self.edited = edited or {}
        self.rate_limit_polls = rate_limit_polls
        self.enqueued: List[str] = []
        self.polls: List[List[str]] = []
        self.task_page: Dict[str, str] = {}

    def get_space_id(self, page_id):
        return 'space-1'

    def enqueue_export_task(self, page_id, recursive, timezone, space_id=None):
        task_id = f'task-{page_id}'
        self.enqueued.append(page_id)
        self.task_page[task_id] = page_id
        return task_id

    def get_tasks(self, task_ids, space_id=None):
        if self.rate_limit_polls:
            self.rate_limit_polls -= 1
            raise RateLimited(1)
        ids = list(task_ids)
        self.polls.append(ids)
        out = {}
        for task_id in ids:
            page = self.task_page[task_id]
            if page in self.fail:
                out[task_id] = {'id': task_id, 'state': 'failure'}
                continue
            self.ready_after[page] -= 1
            done = self.ready_after[page] <= 0
            out[task_id] = {
                'id': task_id,
                'status': {'type': 'complete', 'exportURL': f'url-{page}'}
                if done else {'type': 'progress'},
            }
        return out

    export_url_from_task = staticmethod(NotionClient.export_url_from_task)

    def get_last_edited(self, page_id):
        return self.edited.get(page_id)


def make_worker(client, jobs, **kw):
    worker = SyncWorker(
        jobs=jobs, notion_token='t', file_token='', notion_namespace='',
        timezone='UTC', max_in_flight=kw.pop('max_in_flight', len(jobs)),
        stagger=0, **kw,
    )
    worker.client = client
    worker._sleep = lambda seconds: None
    worker.processed = []
    worker._process_export = lambda job, url: worker.processed.append(
        (job.page_id, url)
    )
    return worker


def jobs(*ids, recursive=False):
    return [PageJob(page_id=i, deck=f'deck-{i}', recursive=recursive)
            for i in ids]


def test_all_tasks_polled_in_one_request():
    client = FakeClient({'a': 1, 'b': 2, 'c': 3})
    worker = make_worker(client, jobs('a', 'b', 'c'))
    worker._run()
    assert sorted(worker.processed) == [
        ('a', 'url-a'), ('b', 'url-b'), ('c', 'url-c')]
    # first poll covers all three tasks, later polls only the unfinished
    assert len(client.polls) == 3
    assert sorted(client.polls[0]) == ['task-a', 'task-b', 'task-c']
    assert client.polls[-1] == ['task-c']


def test_failed_task_reported_others_continue():
    client = FakeClient({'a': 1, 'b': 1}, fail={'b'})
    worker = make_worker(client, jobs('a', 'b'))
    worker._run()
    assert worker.processed == [('a', 'url-a')]
    assert len(worker.signals.error.calls) == 1
    assert 'b' in worker.signals.error.calls[0][0]


def test_rate_limited_poll_is_retried():
    client = FakeClient({'a': 1}, rate_limit_polls=2)
    worker = make_worker(client, jobs('a'))
    worker._run()
    assert worker.processed == [('a', 'url-a')]
    assert not worker.signals.error.calls


def test_sequential_mode_limits_in_flight():
    client = FakeClient({'a': 2, 'b': 2})
    worker = make_worker(client, jobs('a', 'b'), max_in_flight=1)
    worker._run()
    assert all(len(p) == 1 for p in client.polls)
    assert [p for p, _ in worker.processed] == ['a', 'b']


def test_unchanged_pages_are_skipped_but_recursive_never():
    client = FakeClient({'a': 1, 'b': 1, 'r': 1},
                        edited={'a': 100, 'b': 200, 'r': 300})
    pages = jobs('a', 'b') + jobs('r', recursive=True)
    worker = make_worker(client, pages,
                         known_last_edited={'a': 100, 'b': 1, 'r': 300})
    worker._run()
    assert worker.signals.page_skipped.calls == [('a', 'deck-a')]
    assert sorted(client.enqueued) == ['b', 'r']
    assert pages[1].last_edited == 200  # recorded for saving after success


def test_no_skipping_without_known_state():
    client = FakeClient({'a': 1}, edited={'a': 100})
    worker = make_worker(client, jobs('a'), known_last_edited=None)
    worker._run()
    assert client.enqueued == ['a']
    assert not worker.signals.page_skipped.calls


def test_enqueue_gives_up_after_repeated_rate_limits():
    class AlwaysLimited(FakeClient):
        def enqueue_export_task(self, *a):
            raise RateLimited(None)

    worker = make_worker(AlwaysLimited({}), jobs('a', 'b'))
    worker._run()
    assert len(worker.signals.error.calls) == 2
    assert worker.processed == []


def test_export_url_from_task_raises_on_error():
    with pytest.raises(NotionClientError):
        NotionClient.export_url_from_task({'error': 'boom'})
    assert NotionClient.export_url_from_task({'status': {'type': 'x'}}) is None


# -- subdecks -----------------------------------------------------------------

import zipfile  # noqa: E402
from pathlib import Path  # noqa: E402

from n2a.notion_client import AuthError  # noqa: E402
from n2a.sync_worker import subdeck_for  # noqa: E402

ID1, ID2, ID3 = 'a' * 32, 'b' * 32, 'c' * 32


@pytest.mark.parametrize('rel, expected', [
    (f'Root {ID1}.html', 'Deck'),
    (f'Root {ID1}/IAM {ID2}.html', 'Deck::IAM'),
    (f'Root {ID1}/IAM {ID2}/Policies {ID3}.html', 'Deck::IAM::Policies'),
    (f'Root {ID1}/A::B "q" {ID2}.html', 'Deck::A:B q'),
    (f'Root {ID1}/{ID2}.html', 'Deck::Untitled'),
])
def test_subdeck_for(rel, expected):
    root = Path('/tmp/export')
    assert subdeck_for(root / rel, root, 'Deck') == expected


def _toggle_page(question: str) -> str:
    return (
        '<html><body><article id="11111111-2222-3333-4444-555555555555">'
        f'<details class="toggle"><summary>{question}</summary><p>a</p>'
        '</details></article></body></html>'
    )


class ExportClient(FakeClient):
    """Serves a zip with a root page and nested subpages."""

    def __init__(self, files):
        super().__init__({'p': 1})
        self.files = files

    def download(self, url, destination):
        with zipfile.ZipFile(destination, 'w') as zf:
            for name, body in self.files.items():
                zf.writestr(name, body)


EXPORT = {
    # Notion wraps exports in a folder; the root page is the shallowest file
    f'Export/Root {ID1}.html': _toggle_page('root q'),
    f'Export/Root {ID1}/IAM {ID2}.html': _toggle_page('iam q'),
    f'Export/Root {ID1}/IAM {ID2}/Policies {ID3}.html': _toggle_page('pol q'),
}


def _export_worker(subdecks):
    worker = make_worker(ExportClient(EXPORT), [
        PageJob(page_id='p', deck='AWS', recursive=True, subdecks=subdecks)])
    del worker._process_export  # use the real one
    worker._run()
    return {
        (deck, root): [n.front for n in notes]
        for deck, root, notes in worker.signals.result.calls
    }


def test_export_grouped_into_subdecks():
    assert _export_worker(subdecks=True) == {
        ('AWS', 'AWS'): ['root q'],
        ('AWS::IAM', 'AWS'): ['iam q'],
        ('AWS::IAM::Policies', 'AWS'): ['pol q'],
    }


def test_export_without_subdecks_uses_one_deck():
    result = _export_worker(subdecks=False)
    assert list(result) == [('AWS', 'AWS')]
    assert sorted(result[('AWS', 'AWS')]) == ['iam q', 'pol q', 'root q']


# -- expired tokens -----------------------------------------------------------


def test_expired_token_stops_whole_sync():
    class Expired(FakeClient):
        def enqueue_export_task(self, *a):
            self.enqueued.append(a[0])
            raise AuthError('notion_token')

    client = Expired({})
    worker = make_worker(client, jobs('a', 'b', 'c'))
    worker.client = client
    worker.run = SyncWorker.run.__get__(worker)
    # run() builds a real client; give it ours instead
    import n2a.sync_worker as sw
    original = sw.NotionClient
    sw.NotionClient = lambda *a, **k: client
    try:
        worker.run()
    finally:
        sw.NotionClient = original
    assert client.enqueued == ['a']                     # stopped at once
    assert worker.signals.auth_failed.calls == [('notion_token',)]
    assert worker.signals.finished.calls == [()]
    assert not worker.signals.error.calls               # no per-page noise


# -- Notion routing and split exports ------------------------------------------


def test_tasks_polled_per_workspace():
    class TwoSpaces(FakeClient):
        def get_space_id(self, page_id):
            return 'space-b' if page_id == 'b' else 'space-a'

        def get_tasks(self, task_ids, space_id=None):
            self.polled_spaces = getattr(self, 'polled_spaces', []) + [space_id]
            return super().get_tasks(task_ids, space_id)

    client = TwoSpaces({'a': 1, 'b': 1, 'c': 1})
    worker = make_worker(client, jobs('a', 'b', 'c'))
    worker._run()
    assert sorted(client.polled_spaces) == ['space-a', 'space-b']
    assert len(worker.processed) == 3


def test_split_export_with_inner_zips():
    import io

    def zipped(files):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            for name, body in files.items():
                zf.writestr(name, body)
        return buf.getvalue()

    parts = {
        'Export-Part-1.zip': zipped({f'Root {ID1}.html': _toggle_page('root q')}),
        'Export-Part-2.zip': zipped({f'Root {ID1}/Sub {ID2}.html': _toggle_page('sub q')}),
    }

    class SplitClient(ExportClient):
        def download(self, url, destination):
            with zipfile.ZipFile(destination, 'w') as zf:
                for name, body in parts.items():
                    zf.writestr(name, body)

    worker = make_worker(SplitClient({}), [
        PageJob(page_id='p', deck='AWS', recursive=True, subdecks=True)])
    del worker._process_export
    worker._run()
    got = {deck: [n.front for n in notes] for deck, _, notes in worker.signals.result.calls}
    assert got == {'AWS': ['root q'], 'AWS::Sub': ['sub q']}
