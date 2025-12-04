import logging
import re
from typing import Optional, List
from urllib.parse import urlparse

from praw.models import Submission

from bdfrx.exceptions import SiteDownloaderError
from bdfrx.resource import Resource
from bdfrx.site_authenticator import SiteAuthenticator
from bdfrx.site_downloaders.base_downloader import BaseDownloader
from bdfrx.site_downloaders.soundgasm import Soundgasm  # Import for delegation

logger = logging.getLogger(__name__)


class SelfPost(BaseDownloader):
    def __init__(self, post: Submission) -> None:
        super().__init__(post)

    def find_resources(self, authenticator: Optional[SiteAuthenticator] = None) -> List[Resource]:
        resources: List[Resource] = []

        # Original text export as .txt
        text_content = self.export_to_string().encode("utf-8")
        text_resource = Resource(self.post, self.post.url, lambda: None, ".txt")
        text_resource.content = text_content
        text_resource.create_hash()
        resources.append(text_resource)
        logger.debug(f"Added text resource for submission {self.post.id}")

        # Extract soundgasm.net links from selftext
        links = re.findall(r'(https?://(?:www\.)?soundgasm\.net/[^\s\)"\']+)', self.post.selftext)
        logger.debug(f"Found {len(links)} potential soundgasm links in selftext for {self.post.id}")

        for link in links:
            parsed = urlparse(link)
            if 'soundgasm.net' not in parsed.netloc:
                logger.debug(f"Skipping non-soundgasm link: {link}")
                continue
            try:
                # Create a mock post to delegate to Soundgasm
                mock_post = type('MockPost', (), {
                    'url': link,
                    'id': self.post.id,
                    'title': self.post.title,
                    'subreddit': self.post.subreddit,
                    'author': self.post.author,
                    'created_utc': self.post.created_utc,
                    'fullname': self.post.fullname  # Added for better compatibility
                })()
                soundgasm_downloader = Soundgasm(mock_post)
                audio_resources = soundgasm_downloader.find_resources(authenticator)

                # Override source_submission to use the real post for naming
                for res in audio_resources:
                    res.source_submission = self.post  # Use original submission for formatting

                resources.extend(audio_resources)
                logger.debug(f"Extracted {len(audio_resources)} audio resources from {link}")
            except Exception as e:
                logger.warning(f"Failed to extract audio from {link}: {e}")
                # Continue without raising to allow .txt to save

        if len(resources) == 1:  # Only text, no audio found
            logger.debug(f"No audio resources found for self-post {self.post.id}")

        return resources

    def export_to_string(self) -> str:
        """Self posts are formatted here"""
        return (
            "## ["
            + self.post.fullname
            + "]("
            + self.post.url
            + ")\n"
            + self.post.selftext
            + "\n\n---\n\n"
            + "submitted to [r/"
            + self.post.subreddit.display_name
            + "](https://www.reddit.com/r/"
            + self.post.subreddit.display_name
            + ") by [u/"
            + (self.post.author.name if self.post.author else "DELETED")
            + "](https://www.reddit.com/user/"
            + (self.post.author.name if self.post.author else "DELETED")
            + ")"
        )
