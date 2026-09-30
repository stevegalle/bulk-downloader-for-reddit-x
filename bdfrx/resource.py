import hashlib
import logging
import re
import time
import urllib.parse
from collections.abc import Callable
from typing import TYPE_CHECKING, Optional

import requests
from praw.models import Submission

from bdfrx.exceptions import BulkDownloaderException, TemporaryDownloadError

if TYPE_CHECKING:
    import _hashlib

logger = logging.getLogger(__name__)

FIRST_RETRY_WAIT = 10
# Responses meaning "not now" rather than "not there": timeouts, rate limiting, and server trouble
TEMPORARY_STATUS_CODES = frozenset((408, 425, 429, 500, 502, 503, 504))


class Resource:
    def __init__(
        self,
        source_submission: Submission,
        url: str,
        download_function: Callable,
        extension: Optional[str] = None,
    ) -> None:
        self.source_submission = source_submission
        self.content: Optional[bytes] = None
        self.url = url
        self.hash: Optional[_hashlib.HASH] = None
        self.extension = extension
        self.download_function = download_function
        if not self.extension:
            self.extension = self._determine_extension()

    @staticmethod
    def retry_download(url: str) -> Callable:
        return lambda global_params: Resource.http_download(url, global_params)

    def download(self, download_parameters: Optional[dict] = None) -> None:
        if download_parameters is None:
            download_parameters = {}
        if not self.content:
            try:
                content = self.download_function(download_parameters)
            except requests.exceptions.RequestException as e:
                # Any network failure, not just ConnectionError (a dropped transfer is a ChunkedEncodingError)
                raise TemporaryDownloadError(f"Could not download resource: {e}")
            except BulkDownloaderException:
                raise
            if content:
                self.content = content
        if not self.hash and self.content:
            self.create_hash()

    def create_hash(self) -> None:
        self.hash = hashlib.md5(self.content, usedforsecurity=False)

    def _determine_extension(self) -> Optional[str]:
        extension_pattern = re.compile(r".*(\..{3,5})$")
        stripped_url = urllib.parse.urlsplit(self.url).path
        match = extension_pattern.search(stripped_url)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def http_download(url: str, download_parameters: dict) -> Optional[bytes]:
        """Download a URL, retrying network failures, rate limiting and server errors.

        Waits 10, 20, 40... seconds between attempts (or what a Retry-After header asks for) while the total stays
        within max_wait_time, then raises TemporaryDownloadError so the post is tried again on a later run.
        Other HTTP errors (e.g. 404) raise BulkDownloaderException at once.
        """
        headers = download_parameters.get("headers")
        max_wait_time = download_parameters.get("max_wait_time", 300)
        waited = 0
        next_wait = FIRST_RETRY_WAIT
        attempt = 0
        while True:
            attempt += 1
            retry_after = None
            try:
                response = requests.get(url, headers=headers, timeout=16)
            except requests.exceptions.RequestException as e:
                problem = str(e)
            else:
                if re.match(r"^2\d{2}", str(response.status_code)) and response.content:
                    return response.content
                if response.status_code not in TEMPORARY_STATUS_CODES:
                    raise BulkDownloaderException(
                        f"Unrecoverable error requesting resource: HTTP Code {response.status_code}",
                    )
                problem = f"HTTP {response.status_code}"
                retry_after = Resource._retry_after(response)
            wait = retry_after if retry_after is not None else next_wait
            if waited + wait > max_wait_time:
                raise TemporaryDownloadError(
                    f"Gave up on {url} after {attempt} attempt(s) and {waited}s of waiting: {problem}",
                )
            logger.warning(f"Error downloading {url} (attempt {attempt}), retrying in {wait} seconds: {problem}")
            time.sleep(wait)
            waited += wait
            next_wait *= 2

    @staticmethod
    def _retry_after(response: requests.Response) -> Optional[int]:
        """Seconds a Retry-After header asks for, if it gives a number"""
        value = response.headers.get("Retry-After", "")
        return int(value) if value.isdigit() else None
