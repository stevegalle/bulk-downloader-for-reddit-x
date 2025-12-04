import logging
from typing import Optional, List
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from bdfrx.exceptions import SiteDownloaderError
from bdfrx.resource import Resource
from bdfrx.site_authenticator import SiteAuthenticator
from bdfrx.site_downloaders.base_downloader import BaseDownloader

logger = logging.getLogger(__name__)

class Soundgasm(BaseDownloader):
    def __init__(self, post):
        super().__init__(post, typical_extension=".m4a")

    def find_resources(self, authenticator: Optional[SiteAuthenticator] = None) -> List[Resource]:
        resources = []
        logger.info(f"*******Finding .m4a resources for {self.post.url}")
        try:
            response = self.retrieve_url(self.post.url)
            soup = BeautifulSoup(response.text, 'html.parser')
        except Exception as e:
            logger.error(f"Failed to fetch page for {self.post.url}: {e}")
            raise SiteDownloaderError(f"Could not retrieve content from {self.post.url}")

        script_tags = soup.find_all('script')
        potential_urls = set()
        for script in script_tags:
            if script.string:
                matches = re.findall(r'm4a:\s*"([^"]+\.m4a)"', script.string, re.IGNORECASE)
                for match in matches:
                    audio_url = urljoin(self.post.url, match)
                    potential_urls.add(audio_url)

        for audio_url in potential_urls:
            try:
                head_response = self.head_url(audio_url)
                if head_response.status_code != 200:
                    logger.warning(f"Skipping invalid URL: {audio_url} (status {head_response.status_code})")
                    continue
            except Exception as e:
                logger.warning(f"Could not verify {audio_url}: {e}")
                continue

            # Use retry_download to wrap http_download with the URL baked in
            resource = Resource(
                source_submission=self.post,  # Matches 'source_submission'
                url=audio_url,
                download_function=Resource.retry_download(audio_url),  # Wrapped for single-arg call
                extension=".m4a"
            )
            resources.append(resource)
            logger.debug(f"Created resource for {audio_url}")

        if not resources:
            raise SiteDownloaderError(f"No .m4a audio resources found for {self.post.url}")

        logger.debug(f"Found {len(resources)} .m4a resources for {self.post.url}")
        return resources
