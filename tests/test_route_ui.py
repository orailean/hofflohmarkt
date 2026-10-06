from html.parser import HTMLParser
import json
from pathlib import Path
import unittest

import webapp


class IdParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if "id" in attributes:
            self.ids.add(attributes["id"])


class RouteOverviewUiTests(unittest.TestCase):
    def test_page_exposes_overview_cached_match_and_admin_action(self):
        response = webapp.index()
        parser = IdParser()
        parser.feed(Path(response.path).read_text(encoding="utf-8"))
        self.assertTrue({"routeOverview", "cachedMatch", "cachedRouteList",
                         "adminArea", "adminRebuildBtn", "overviewList",
                         "overviewPrev", "overviewNext", "overviewClearAll",
                         "overviewDeleteError"}.issubset(parser.ids))

    def test_route_controls_have_labels_in_every_supported_language(self):
        required = {"cache.title", "cache.empty", "cache.open", "cache.match",
                    "cache.rebuild", "cache.adminIntro", "cache.set",
                    "cache.previous", "cache.next", "cache.routeCountMany",
                    "cache.routeTypes", "cache.savedAt", "cache.option",
                    "cache.updated", "cache.unnamedFlyer", "cache.chooseSet",
                    "cache.deleteMap", "cache.deleteMapConfirm", "cache.deleteError",
                    "cache.clearAll", "cache.clearAllConfirm", "cache.clearAllError"}
        for lang in ("en", "de", "ro"):
            with self.subTest(lang=lang):
                values = json.loads((Path("static/i18n") / f"{lang}.json").read_text())
                self.assertTrue(required.issubset(values))
                self.assertTrue(all(values[key].strip() for key in required))


if __name__ == "__main__":
    unittest.main()
