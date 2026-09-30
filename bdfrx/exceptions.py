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
    so a later run tries it again"""
