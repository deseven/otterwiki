#!/usr/bin/env python
# vim: set et ts=8 sts=4 sw=4 ai:

"""
otterwiki.page_titles

Page title management system that extracts titles from markdown headers
and maintains an in-memory cache for quick access.
"""

import os
import threading
from typing import Dict, Optional, Tuple
from timeit import default_timer as timer

from otterwiki.util import get_header
from otterwiki.helper import get_filename


class PageTitleManager:
    """
    Manages page titles extracted from markdown headers.

    When USE_PAGE_HEADER_AS_TITLE is enabled, this class:
    1. Builds an in-memory cache of page paths -> titles on startup
    2. Extracts titles from the first line if it starts with '#'
    3. Falls back to filename-based titles if no header is found
    4. Updates the cache when pages are saved or loaded
    """

    def __init__(self, storage, app_config):
        self.storage = storage
        self.app_config = app_config
        self.enabled = app_config.get('USE_PAGE_HEADER_AS_TITLE', False)

        # Thread-safe cache: pagepath -> (title, mtime)
        self._cache: Dict[str, Tuple[Optional[str], float]] = {}
        self._lock = threading.RLock()

        if self.enabled:
            self._build_initial_cache()

    def _build_initial_cache(self):
        """Build the initial cache of all page titles on startup."""
        if not self.enabled:
            return

        t_start = timer()
        app = self.app_config.get(
            '_app_instance'
        )  # We'll pass this from server.py
        if app:
            app.logger.info(
                "PageTitleManager: Building initial page title cache..."
            )

        try:
            # Get all markdown files
            files, _ = self.storage.list()
            md_files = [f for f in files if f.endswith('.md')]

            for filename in md_files:
                try:
                    # Convert filename to pagepath (remove .md extension)
                    pagepath = (
                        filename[:-3] if filename.endswith('.md') else filename
                    )

                    # Extract title from file content
                    title = self._extract_title_from_file(filename)

                    # Get file modification time for cache invalidation
                    try:
                        mtime = self.storage.mtime(filename)
                    except (FileNotFoundError, AttributeError):
                        mtime = 0.0

                    # Store in cache
                    with self._lock:
                        self._cache[pagepath] = (title, mtime)

                except Exception as e:
                    if app:
                        app.logger.warning(
                            f"PageTitleManager: Error processing {filename}: {e}"
                        )
                    continue

            if app:
                app.logger.info(
                    f"PageTitleManager: Built cache for {len(self._cache)} pages "
                    f"in {timer() - t_start:.3f} seconds"
                )

        except Exception as e:
            if app:
                app.logger.error(
                    f"PageTitleManager: Error building initial cache: {e}"
                )

    def _extract_title_from_file(self, filename: str) -> Optional[str]:
        """
        Extract title from a markdown file.

        Returns the first line if it starts with '#', otherwise None.
        """
        try:
            content = self.storage.load(filename)
            return self._extract_title_from_content(content)
        except Exception:
            return None

    def _extract_title_from_content(self, content: str) -> Optional[str]:
        """
        Extract title from markdown content.

        Returns the first line if it starts with '#', otherwise None.
        """
        if not content:
            return None

        # Get the first line
        first_line = content.split('\n')[0].strip()

        # Check if it starts with '#'
        if first_line.startswith('#'):
            # Remove the '#' characters and any leading/trailing whitespace
            title = first_line.lstrip('#').strip()
            return title if title else None

        return None

    def get_page_title(
        self, pagepath: str, fallback_to_filename: bool = True
    ) -> Optional[str]:
        """
        Get the title for a page.

        Args:
            pagepath: The page path (without .md extension)
            fallback_to_filename: Whether to fall back to filename-based title

        Returns:
            The page title if found, None if not found and fallback is disabled
        """
        if not self.enabled:
            return None

        # Normalize pagepath
        pagepath = pagepath.rstrip('/')
        if pagepath.endswith('.md'):
            pagepath = pagepath[:-3]

        # Check cache first
        with self._lock:
            if pagepath in self._cache:
                title, cached_mtime = self._cache[pagepath]

                # Check if cache is still valid
                filename = get_filename(pagepath)
                try:
                    current_mtime = self.storage.mtime(filename)
                    if current_mtime <= cached_mtime:
                        return title
                except (FileNotFoundError, AttributeError):
                    # File doesn't exist or no mtime available
                    pass

        # Cache miss or invalid - refresh from file
        return self._refresh_page_title(pagepath, fallback_to_filename)

    def _refresh_page_title(
        self, pagepath: str, fallback_to_filename: bool = True
    ) -> Optional[str]:
        """Refresh the title for a specific page from disk."""
        filename = get_filename(pagepath)

        try:
            # Extract title from file
            title = self._extract_title_from_file(filename)

            # Get current mtime
            try:
                mtime = self.storage.mtime(filename)
            except (FileNotFoundError, AttributeError):
                mtime = 0.0

            # Update cache
            with self._lock:
                self._cache[pagepath] = (title, mtime)

            return title

        except Exception:
            # File doesn't exist or can't be read
            with self._lock:
                # Remove from cache if it was there
                self._cache.pop(pagepath, None)
            return None

    def update_page_title(self, pagepath: str, content: str = None):
        """
        Update the cached title for a page.

        Args:
            pagepath: The page path (without .md extension)
            content: Optional content to extract title from (if not provided, reads from file)
        """
        if not self.enabled:
            return

        # Normalize pagepath
        pagepath = pagepath.rstrip('/')
        if pagepath.endswith('.md'):
            pagepath = pagepath[:-3]

        if content is not None:
            # Extract title from provided content
            title = self._extract_title_from_content(content)
        else:
            # Read from file
            title = self._extract_title_from_file(get_filename(pagepath))

        # Get current mtime
        filename = get_filename(pagepath)
        try:
            mtime = self.storage.mtime(filename)
        except (FileNotFoundError, AttributeError):
            mtime = 0.0

        # Update cache
        with self._lock:
            self._cache[pagepath] = (title, mtime)

    def remove_page_title(self, pagepath: str):
        """Remove a page from the title cache."""
        if not self.enabled:
            return

        # Normalize pagepath
        pagepath = pagepath.rstrip('/')
        if pagepath.endswith('.md'):
            pagepath = pagepath[:-3]

        with self._lock:
            self._cache.pop(pagepath, None)

    def get_display_title(
        self, pagepath: str, full: bool = False, header: str = None
    ) -> str:
        """
        Get the display title for a page, with fallback to filename-based title.

        This method returns display titles but should NOT be used for URL generation.
        URLs should always be based on the original filename structure.

        Args:
            pagepath: The page path
            full: Whether to return the full path or just the page name
            header: Optional header hint (for compatibility with existing code)

        Returns:
            The display title (either from header or filename)
        """
        if not self.enabled:
            # Fall back to original behavior - import here to avoid circular import
            from otterwiki.helper import get_pagename

            return get_pagename(pagepath, full=full, header=header)

        # Try to get title from cache for the current page
        cached_title = self.get_page_title(
            pagepath, fallback_to_filename=False
        )

        if not full:
            # For short titles, just return the cached title or fall back
            if cached_title:
                return cached_title
            else:
                # Import here to avoid circular import
                from otterwiki.helper import get_pagename

                return get_pagename(pagepath, full=False, header=header)
        else:
            # For full paths, we need to be careful not to break URL structure
            # Only use cached title for the final part, keep directory structure intact
            parts = pagepath.rstrip('/').split('/')
            if len(parts) > 1:
                # Get the directory parts using original logic
                dir_parts = parts[:-1]
                dir_path = '/'.join(dir_parts)
                # Import here to avoid circular import
                from otterwiki.helper import get_pagename

                dir_display = get_pagename(dir_path, full=True, header=None)

                # Use cached title for the final part if available
                if cached_title:
                    return f"{dir_display}/{cached_title}"
                else:
                    return get_pagename(pagepath, full=True, header=header)
            else:
                # Single level - use cached title or fall back
                if cached_title:
                    return cached_title
                else:
                    # Import here to avoid circular import
                    from otterwiki.helper import get_pagename

                    return get_pagename(pagepath, full=True, header=header)

    def clear_cache(self):
        """Clear the entire title cache."""
        with self._lock:
            self._cache.clear()

    def get_cache_stats(self) -> Dict[str, int]:
        """Get cache statistics for debugging."""
        with self._lock:
            return {
                'total_entries': len(self._cache),
                'entries_with_titles': len(
                    [
                        1
                        for title, _ in self._cache.values()
                        if title is not None
                    ]
                ),
                'entries_without_titles': len(
                    [1 for title, _ in self._cache.values() if title is None]
                ),
            }


# Global instance - will be initialized in server.py
page_title_manager: Optional[PageTitleManager] = None


def get_page_title_manager() -> Optional[PageTitleManager]:
    """Get the global page title manager instance."""
    return page_title_manager


def init_page_title_manager(storage, app_config):
    """Initialize the global page title manager instance."""
    global page_title_manager
    page_title_manager = PageTitleManager(storage, app_config)
    return page_title_manager
