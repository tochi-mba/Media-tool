"""The provider that makes a browser download a file without anyone clicking.

The flow is always the same shape, and the recipe fills in the specifics: open the start
URL, perform the recipe's steps, find the result that matches the query, then trigger the
download inside ``expect_download`` and hand the captured file to the artifact store.

Nothing here is site-specific. Supporting a new site is a recipe file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from media_tool.core.logging import get_logger
from media_tool.providers.base import (
    ProviderError,
    ProviderNotFoundError,
    ProviderTimeoutError,
)
from media_tool.providers.browser.recipes import (
    StepAction,
    placeholders_for,
    render,
)

if TYPE_CHECKING:
    from media_tool.domain.artifacts import DownloadArtifact
    from media_tool.domain.media import MediaQuery
    from media_tool.providers.browser.page import BrowserRuntime, DownloadLike, PageLike
    from media_tool.providers.browser.recipes import Placeholders, SiteRecipe, Step
    from media_tool.storage.base import ArtifactSink

logger = get_logger(__name__)

DEFAULT_CONTENT_TYPE = "application/octet-stream"


class BrowserDownloadProvider:
    """Drives a headless browser through a site recipe to capture one file."""

    def __init__(self, *, runtime: BrowserRuntime, recipe: SiteRecipe) -> None:
        self._runtime = runtime
        self._recipe = recipe

    @property
    def name(self) -> str:
        return f"browser:{self._recipe.name}"

    async def healthy(self) -> bool:
        return await self._runtime.healthy()

    async def aclose(self) -> None:
        await self._runtime.aclose()

    async def download(self, *, query: MediaQuery, sink: ArtifactSink) -> DownloadArtifact:
        """Fetch one query, capturing whatever the site downloads.

        Raises:
            ProviderNotFoundError: if no result on the page matches the query.
            ProviderTimeoutError: if the site never starts a download.
            ProviderError: if navigation or any step fails.
        """
        placeholders = placeholders_for(query)
        recipe = self._recipe

        async with self._runtime.acquire_page() as page:
            await self._navigate(page, render(recipe.start_url, placeholders, url_encode=True))

            for step in recipe.steps:
                await self._perform(page, step, placeholders)

            await self._require_match(page, placeholders)

            download = await self._trigger(page, recipe.download_trigger, placeholders)
            await self._save(download, sink)

            logger.info(
                "download_captured",
                recipe=recipe.name,
                key=query.key,
                suggested_filename=download.suggested_filename,
            )

            return sink.commit(
                suggested_filename=download.suggested_filename,
                content_type=DEFAULT_CONTENT_TYPE,
                source_url=download.url,
            )

    async def _save(self, download: DownloadLike, sink: ArtifactSink) -> None:
        """Move the captured bytes into the staging slot.

        Wrapped so a disk or driver failure here is reported as a download failure with
        context, rather than escaping unclassified and being logged as an internal bug.
        """
        try:
            await download.save_as(sink.staging_path)
        except Exception as error:
            msg = f"could not save {download.suggested_filename!r}: {error}"
            raise ProviderError(msg) from error

    async def _navigate(self, page: PageLike, url: str) -> None:
        try:
            await page.goto(url)
        except Exception as error:
            msg = f"could not open {url!r}: {error}"
            raise ProviderError(msg) from error

    async def _perform(self, page: PageLike, step: Step, placeholders: Placeholders) -> None:
        selector = render(step.selector, placeholders)
        try:
            if step.action is StepAction.CLICK:
                await page.click(selector)
            elif step.action is StepAction.FILL:
                await page.fill(selector, render(step.value or "", placeholders))
            else:
                await page.wait_for_selector(selector)
        except Exception as error:
            msg = f"step {step.action.value} on {selector!r} failed: {error}"
            raise ProviderError(msg) from error

    async def _require_match(self, page: PageLike, placeholders: Placeholders) -> None:
        """Fail early when the page holds nothing matching the query.

        Without this the trigger would click whatever happens to be first, and the job
        would report success for the wrong file -- a far worse outcome than a clean miss.
        """
        recipe = self._recipe
        if recipe.result_selector is None:
            return

        selector = render(recipe.result_selector, placeholders)
        if await page.query_count(selector) == 0:
            msg = f"no results on the page for {placeholders['query']!r}"
            raise ProviderNotFoundError(msg)

        required = [
            rendered
            for term in recipe.match.text_contains
            # A term that resolves to nothing does not constrain anything:
            # '{episode_tag}' should simply not apply to a film.
            if (rendered := render(term, placeholders).strip())
        ]
        if not required:
            return

        texts = await page.text_contents(selector)
        if not any(all(term.casefold() in text.casefold() for term in required) for text in texts):
            msg = f"no result matched {' + '.join(required)!r}"
            raise ProviderNotFoundError(msg)

    async def _trigger(
        self, page: PageLike, step: Step, placeholders: Placeholders
    ) -> DownloadLike:
        """Perform the triggering action and capture the download it starts."""
        selector = render(step.selector, placeholders)
        try:
            async with page.expect_download() as handle:
                await self._perform(page, step, placeholders)
        except ProviderError:
            raise
        except TimeoutError as error:
            msg = f"clicking {selector!r} started no download"
            raise ProviderTimeoutError(msg) from error
        except Exception as error:
            msg = f"capturing the download from {selector!r} failed: {error}"
            raise ProviderError(msg) from error
        else:
            download: DownloadLike = await handle.value
            return download
