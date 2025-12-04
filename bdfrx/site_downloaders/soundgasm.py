import logging
from typing import Optional, List
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from bdfrx.site_downloaders.base_downloader import BaseDownloader
from bdfrx.resource import Resource
from bdfrx.site_authenticator import SiteAuthenticator  # If auth is needed later

logger = logging.getLogger(__name__)

class Soundgasm(BaseDownloader):
    def __init__(self, post):
        super().__init__(post, typical_extension=".m4a")  # Default to .m4a based on the page source

    def find_resources(self, authenticator: Optional[SiteAuthenticator] = None) -> List[Resource]:
        """Extract all .m4a audio resources from the Soundgasm page, handling multiples."""
        resources = []
        logger.info(f"*******Finding .m4a resources for {self.post.url}")
        # Fetch the page content
        try:
            response = self.retrieve_url(self.post.url)
            soup = BeautifulSoup(response.text, 'html.parser')
        except Exception as e:
            logger.error(f"Failed to fetch page for {self.post.url}: {e}")
            raise ValueError(f"Could not retrieve content from {self.post.url}")

        # Find all <script> tags and extract .m4a URLs from jPlayer setMedia calls
        script_tags = soup.find_all('script')
        potential_urls = set()  # Use set to avoid duplicates
        for script in script_tags:
            if script.string:  # Check if script has content
                # Regex to match m4a: "url" patterns (handles multiples)
                matches = re.findall(r'm4a:\s*"([^"]+\.m4a)"', script.string, re.IGNORECASE)
                for match in matches:
                    audio_url = urljoin(self.post.url, match)  # Handle relative URLs
                    potential_urls.add(audio_url)

        # Create Resources for each unique .m4a URL
        for audio_url in potential_urls:
            # Optional: Validate URL with HEAD request
            try:
                head_response = self.head_url(audio_url)
                if head_response.status_code != 200:
                    logger.warning(f"Skipping invalid URL: {audio_url} (status {head_response.status_code})")
                    continue
            except Exception as e:
                logger.warning(f"Could not verify {audio_url}: {e}")
                continue

            # Create Resource
            resource = Resource(
                source=self.post,
                url=audio_url,
                download_function=Resource.http_download,  # Built-in HTTP downloader with retries
                extension=".m4a",
                hash_hex=""  # Hash computed post-download
            )
            resources.append(resource)

        if not resources:
            raise ValueError(f"No .m4a audio resources found for {self.post.url}")

        logger.debug(f"Found {len(resources)} .m4a resources for {self.post.url}")
        return resources
