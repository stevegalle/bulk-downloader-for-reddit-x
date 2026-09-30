class BulkDownloaderException(Exception):
    pass


class RedditUserError(BulkDownloaderException):
    pass


class RedditAuthenticationError(RedditUserError):
    pass


class SiteDownloaderError(BulkDownloaderException):
    pass


class NotADownloadableLinkError(SiteDownloaderError):
    pass


class ResourceNotFound(SiteDownloaderError):
    pass


class TemporaryDownloadError(SiteDownloaderError):
    """Network trouble or rate limiting that may clear up: the post is skipped this run but not recorded as done,
    so a later run tries it again. rate_limited_host is set when the host refused with HTTP 429."""

    def __init__(self, message: str, rate_limited_host: "str | None" = None) -> None:
        super().__init__(message)
        self.rate_limited_host = rate_limited_host


class RateLimitedError(BulkDownloaderException):
    """A host keeps refusing with HTTP 429: the run stops (exit code RATE_LIMITED_EXIT_CODE) so it can be retried
    later, rather than working through the rest of its posts against the same wall"""

    def __init__(self, host: str) -> None:
        super().__init__(f"Stopped: rate limited by {host}")
        self.host = host


RATE_LIMITED_EXIT_CODE = 75  # EX_TEMPFAIL
