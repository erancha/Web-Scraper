"""Ynet News provider.

Scrapes https://www.ynet.co.il/news and reports newly published articles.

This provider tracks two sets of IDs in state.<provider-key>.json:
- evaluated_ids: article URLs we already evaluated to avoid repeated LLM work
- notified_ids: articles that were already notified
"""

from __future__ import annotations

from .ynet_ai_html_base import YnetAiHtmlProviderBase


class YnetNews(YnetAiHtmlProviderBase):
    """Ynet News provider.

    Scrapes Ynet's news listing page and reports newly published articles.
    """
    @property
    def name(self) -> str:
        """Human-readable provider name."""
        return "Ynet News"

    @property
    def state_key(self) -> str:
        """Unique key used to namespace this provider's data: state.<provider-key>.json."""
        return "ynet_news"

    @property
    def url(self) -> str:
        """Listing page URL fetched by the base HTML provider."""
        # This is the *listing page* that the base class fetches.
        # The base parser also uses this value as the base URL for url-joining relative <a href="..."> links.
        return "https://www.ynet.co.il/news/"

    @property
    def allowed_path_prefixes(self) -> tuple[str, ...]:
        """URL path prefixes considered candidate article links."""
        # These prefixes are matched against the URL path of *links found inside* the fetched listing page.
        # The listing page is /news/247, but it can contain <a href="/article/..."> links; those are the ones
        # we want to keep as candidate items.
        return ("/news/article", "/news/blog/article")
