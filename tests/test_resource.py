from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
import requests

from bdfrx.exceptions import BulkDownloaderException, TemporaryDownloadError
from bdfrx.resource import Resource


@pytest.mark.parametrize(
    ("test_url", "expected"),
    (
        ("test.png", ".png"),
        ("another.mp4", ".mp4"),
        ("test.jpeg", ".jpeg"),
        ("http://www.random.com/resource.png", ".png"),
        ("https://www.resource.com/test/example.jpg", ".jpg"),
        ("hard.png.mp4", ".mp4"),
        ("https://preview.redd.it/7zkmr1wqqih61.png?width=237&format=png&auto=webp&s=19de214e634cbcad99", ".png"),
        ("test.jpg#test", ".jpg"),
        ("test.jpg?width=247#test", ".jpg"),
        ("https://www.test.com/test/test2/example.png?random=test#thing", ".png"),
    ),
)
def test_resource_get_extension(test_url: str, expected: str):
    test_resource = Resource(MagicMock(), test_url, lambda: None)
    result = test_resource._determine_extension()
    assert result == expected


@pytest.mark.online
@pytest.mark.parametrize(
    ("test_url", "expected_hash"),
    (("https://www.iana.org/_img/2013.1/iana-logo-header.svg", "426b3ac01d3584c820f3b7f5985d6623"),),
)
def test_download_online_resource(test_url: str, expected_hash: str):
    test_resource = Resource(MagicMock(), test_url, Resource.retry_download(test_url))
    test_resource.download()
    assert test_resource.hash.hexdigest() == expected_hash


def _response(status: int, content: bytes = b"", headers: Optional[dict] = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.content = content
    response.headers = headers or {}
    return response


@patch("bdfrx.resource.time.sleep")
@patch("bdfrx.resource.requests.get")
def test_http_download_retries_a_dropped_transfer(mock_get: MagicMock, mock_sleep: MagicMock):
    mock_get.side_effect = [requests.exceptions.ChunkedEncodingError("Connection broken"), _response(200, b"video")]
    assert Resource.http_download("https://example.com/a.mp4", {"max_wait_time": 30}) == b"video"
    mock_sleep.assert_called_once_with(10)


@pytest.mark.parametrize(
    ("max_wait_time", "expected_waits"),
    ((30, [10, 20]), (120, [10, 20, 40]), (5, [])),
)
@patch("bdfrx.resource.time.sleep")
@patch("bdfrx.resource.requests.get")
def test_http_download_gives_up_within_max_wait_time(
    mock_get: MagicMock,
    mock_sleep: MagicMock,
    max_wait_time: int,
    expected_waits: list[int],
):
    mock_get.side_effect = requests.exceptions.ReadTimeout("timed out")
    with pytest.raises(TemporaryDownloadError, match=f"{len(expected_waits) + 1} attempt"):
        Resource.http_download("https://example.com/a.mp4", {"max_wait_time": max_wait_time})
    assert [c.args[0] for c in mock_sleep.call_args_list] == expected_waits


@patch("bdfrx.resource.time.sleep")
@patch("bdfrx.resource.requests.get")
def test_http_download_honours_retry_after(mock_get: MagicMock, mock_sleep: MagicMock):
    mock_get.side_effect = [_response(429, headers={"Retry-After": "25"}), _response(200, b"ok")]
    assert Resource.http_download("https://example.com/a.jpg", {"max_wait_time": 30}) == b"ok"
    mock_sleep.assert_called_once_with(25)
    mock_get.side_effect = [_response(429, headers={"Retry-After": "600"})]
    with pytest.raises(TemporaryDownloadError, match="HTTP 429"):
        Resource.http_download("https://example.com/a.jpg", {"max_wait_time": 30})


@pytest.mark.parametrize("status", (500, 502, 503))
@patch("bdfrx.resource.time.sleep")
@patch("bdfrx.resource.requests.get")
def test_http_download_retries_server_errors(mock_get: MagicMock, mock_sleep: MagicMock, status: int):
    mock_get.side_effect = [_response(status), _response(200, b"ok")]
    assert Resource.http_download("https://example.com/a.jpg", {"max_wait_time": 30}) == b"ok"


@patch("bdfrx.resource.time.sleep")
@patch("bdfrx.resource.requests.get")
def test_http_download_does_not_retry_not_found(mock_get: MagicMock, mock_sleep: MagicMock):
    mock_get.return_value = _response(404)
    with pytest.raises(BulkDownloaderException) as info:
        Resource.http_download("https://example.com/a.jpg", {"max_wait_time": 30})
    assert not isinstance(info.value, TemporaryDownloadError)
    mock_sleep.assert_not_called()


def test_download_turns_any_network_error_into_a_temporary_error():
    def broken(_params: dict) -> bytes:
        raise requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead")

    resource = Resource(MagicMock(), "https://example.com/a.mp4", broken)
    with pytest.raises(TemporaryDownloadError):
        resource.download()


@patch("bdfrx.resource.time.sleep")
@patch("bdfrx.resource.requests.get")
def test_http_download_never_retries_without_waiting(mock_get: MagicMock, mock_sleep: MagicMock):
    # Some hosts answer 429 with "Retry-After: 0" for as long as they're blocking you
    mock_get.return_value = _response(429, headers={"Retry-After": "0"})
    with pytest.raises(TemporaryDownloadError) as info:
        Resource.http_download("https://i.imgur.com/abc.mp4", {"max_wait_time": 300})
    assert [c.args[0] for c in mock_sleep.call_args_list] == [10, 20, 40]
    assert mock_get.call_count == 4
    assert info.value.rate_limited_host == "i.imgur.com"


@patch("bdfrx.resource.time.sleep")
@patch("bdfrx.resource.requests.get")
def test_only_429_marks_a_host_rate_limited(mock_get: MagicMock, mock_sleep: MagicMock):
    mock_get.return_value = _response(503)
    with pytest.raises(TemporaryDownloadError) as info:
        Resource.http_download("https://example.com/a.jpg", {"max_wait_time": 30})
    assert info.value.rate_limited_host is None
