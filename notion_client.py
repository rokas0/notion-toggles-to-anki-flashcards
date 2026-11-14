"""Minimal client for Notion's private v3 API (export + metadata)."""

from __future__ import annotations

import time
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import requests

from .helpers import get_logger

#: Connection exceptions
CONNECTION_EXCEPTIONS = (
    requests.exceptions.RequestException,
    ConnectionResetError,
    ConnectionAbortedError,
)


class NotionClientError(Exception):
    """Notion client exception."""


class AuthError(NotionClientError):
    """A login cookie was rejected (expired, revoked or mistyped)."""

    def __init__(self, token_key: str) -> None:
        super().__init__(f'Notion rejected your {token_key}')
        #: Config key of the rejected token: notion_token or file_token
        self.token_key = token_key


class RateLimited(NotionClientError):
    """Notion asked us to slow down."""

    def __init__(self, wait: Optional[float] = None) -> None:
        super().__init__('Rate limited by Notion')
        #: Seconds suggested by the server (Retry-After), if any
        self.wait = wait


class NotionClient:
    """Notion client."""

    API = 'https://www.notion.so/api/v3'
    #: (connect, read) timeout for every HTTP request, sec
    REQUEST_TIMEOUT = (10, 60)
    #: Retries for 5xx responses
    SERVER_ERROR_RETRIES = 5
    #: Pause between 5xx retries, sec
    SERVER_ERROR_WAIT = 2
    #: Headers for downloading the export archive from file.notion.com
    DOWNLOAD_HEADERS = {
        'User-Agent': (
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; '
            'rv:152.0) Gecko/20100101 Firefox/152.0'
        ),
        'Accept': 'text/html,application/xhtml+xml,'
                  'application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }

    def __init__(
        self, token: str, debug: bool = False, file_token: str = ''
    ) -> None:
        """Init the client.

        :param token: Notion `token_v2` cookie
        :param debug: debug mode
        :param file_token: `file_token` cookie for file.notion.com downloads
        """
        self.logger = get_logger(self.__class__.__name__, debug)
        self.file_token = file_token
        self.session = requests.Session()
        self.session.cookies.update({'token_v2': token})

    @staticmethod
    def _retry_after(resp: requests.Response) -> Optional[float]:
        try:
            return max(float(resp.headers.get('Retry-After', '')), 1.0)
        except ValueError:
            return None

    @staticmethod
    def _is_rate_limited_body(data: Any) -> bool:
        if not isinstance(data, dict) or not data.get('isNotionError'):
            return False
        return (
            data.get('name') == 'UserRateLimitResponse'
            or (data.get('clientData') or {}).get('type') == 'rate_limited'
        )

    def _post(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST to the v3 API and return decoded JSON.

        :raises RateLimited: on HTTP 429 or a rate-limit error body
        :raises NotionClientError: on auth, network or decoding errors
        """
        for attempt in range(1, self.SERVER_ERROR_RETRIES + 1):
            try:
                resp = self.session.post(
                    f'{self.API}/{endpoint}',
                    json=payload,
                    timeout=self.REQUEST_TIMEOUT,
                )
            except CONNECTION_EXCEPTIONS as exc:
                raise NotionClientError(f'Request error: {exc}') from exc
            if resp.status_code in (401, 403):
                raise AuthError('notion_token')
            if resp.status_code == 429:
                raise RateLimited(self._retry_after(resp))
            if resp.status_code >= 500:
                self.logger.warning(
                    'Notion server error %s on %s (%s/%s)',
                    resp.status_code, endpoint, attempt,
                    self.SERVER_ERROR_RETRIES,
                )
                time.sleep(self.SERVER_ERROR_WAIT)
                continue
            try:
                data = resp.json()
            except JSONDecodeError as exc:
                raise NotionClientError(
                    f'Cannot decode {endpoint} response'
                ) from exc
            if self._is_rate_limited_body(data):
                raise RateLimited(self._retry_after(resp))
            return data
        raise NotionClientError(f'Notion server errors on {endpoint}')

    def enqueue_export_task(
        self, page_id: str, recursive: bool, timezone: str
    ) -> str:
        """Start an HTML export of a page.

        :returns: task id
        """
        data = self._post('enqueueTask', {
            'task': {
                'eventName': 'exportBlock',
                'request': {
                    'block': {'id': page_id},
                    'recursive': recursive,
                    'exportOptions': {
                        'exportType': 'html',
                        'timeZone': timezone,
                        'locale': 'en',
                    },
                },
            }
        })
        if 'taskId' not in data:
            raise NotionClientError(f'No task id in response: {data}')
        self.logger.info(
            'Export task posted: page_id=%s recursive=%s task_id=%s',
            page_id, recursive, data['taskId'],
        )
        return data['taskId']

    def get_tasks(self, task_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        """Fetch the status of several tasks in a single request.

        :returns: task id -> raw task result
        """
        data = self._post('getTasks', {'taskIds': list(task_ids)})
        self.logger.debug('getTasks response: %s', data)
        return {r['id']: r for r in data.get('results', []) if 'id' in r}

    @staticmethod
    def export_url_from_task(task: Dict[str, Any]) -> Optional[str]:
        """Export URL of a finished task, None if still running.

        :raises NotionClientError: if the task failed
        """
        if task.get('error'):
            raise NotionClientError(str(task['error']))
        if task.get('state') == 'failure':
            raise NotionClientError(f'Export failed: {task}')
        status = task.get('status') or {}
        if status.get('type') == 'complete' and status.get('exportURL'):
            return status['exportURL']
        return None

    def download(self, url: str, destination: Path) -> None:
        """Download an export archive."""
        cookies = {'file_token': self.file_token} if self.file_token else {}
        try:
            with requests.get(
                url,
                stream=True,
                headers=self.DOWNLOAD_HEADERS,
                cookies=cookies,
                timeout=self.REQUEST_TIMEOUT,
            ) as r:
                if r.status_code in (401, 403):
                    raise AuthError('file_token')
                r.raise_for_status()
                with open(destination, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        f.write(chunk)
        except CONNECTION_EXCEPTIONS as exc:
            raise NotionClientError(f'Download failed: {exc}') from exc

    def get_last_edited(self, page_id: str) -> Optional[int]:
        """Page `last_edited_time` (ms since epoch), None if unavailable."""
        data = self._post('syncRecordValues', {
            'requests': [{
                'pointer': {'table': 'block', 'id': page_id},
                'version': -1,
            }]
        })
        record = (
            data.get('recordMap', {}).get('block', {}).get(page_id) or {}
        )
        value = record.get('value') or {}
        # Newer responses nest the record one level deeper
        if isinstance(value.get('value'), dict):
            value = value['value']
        edited = value.get('last_edited_time')
        return int(edited) if edited is not None else None
