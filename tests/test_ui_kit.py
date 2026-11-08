"""The migrated UI must load anonymously with all of its local dependencies."""
from html.parser import HTMLParser
import re
from urllib.parse import urljoin, urlsplit

import pytest

from app import app


class PageResources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("src"):
            self.urls.append(attrs["src"])
        if tag == "link" and attrs.get("rel") in ("stylesheet", "icon"):
            self.urls.append(attrs["href"])


@pytest.mark.parametrize("path", ["/login", "/static/ui/example.html", "/static/ui/workspace.html"])
def test_ui_pages_load_all_styles_scripts_and_icons_without_login(ctx, path):
    client = app.test_client()
    response = client.get(path)
    assert response.status_code == 200
    resources = PageResources()
    resources.feed(response.text)
    assert resources.urls
    pending = [urljoin(path, url) for url in resources.urls]
    visited = set()
    while pending:
        url = pending.pop()
        if url in visited:
            continue
        visited.add(url)
        assert not urlsplit(url).netloc
        assert url.startswith("/static/ui/")
        asset = client.get(url)
        assert asset.status_code == 200, url
        assert asset.mimetype != "text/html", url
        if url.endswith(".css"):
            assert asset.mimetype == "text/css"
            pending.extend(urljoin(url, dependency) for dependency in re.findall(r'@import url\("([^"]+)"\)', asset.text))
