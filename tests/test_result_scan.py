"""The result scan classifies a disabled 更晚車次 control as the end of the list."""

import pytest
from playwright.sync_api import sync_playwright

from thsr_watcher.booking_browser import FEEDBACK, RESULT_TABLE, TRAIN_RADIO, _SCAN_JS

RADIO = (
    '<input type="radio" name="TrainQueryDataViewPanel:TrainGroup" '
    'querycode="0149" querydeparture="18:00" queryarrival="19:00">'
)


def scan(page, html):
    page.set_content(f"<body>{html}</body>")
    found = page.evaluate(_SCAN_JS, {
        "table": RESULT_TABLE, "radio": TRAIN_RADIO, "feedback": FEEDBACK,
        "patterns": "nomatch", "staleOrigin": None, "staleCodes": None,
    })
    return found["pager"]


@pytest.fixture(scope="module")
def scan_page():
    try:
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=True)
    except Exception as exc:
        pytest.skip(f"Chromium is not available: {exc}")
    page = browser.new_page()
    yield page
    browser.close()
    playwright.stop()


@pytest.mark.parametrize("html", [
    "<button disabled>更晚車次</button>",
    '<button aria-disabled="true">更晚車次</button>',
    '<div class="uk-disabled"><button>更晚車次</button></div>',
    '<input type="button" disabled value="更晚車次">',
])
def test_disabled_later_train_control_has_no_next_page(scan_page, html):
    assert scan(scan_page, RADIO + html)["kind"] == "later"
    assert scan(scan_page, RADIO + html)["hasNext"] is False


def test_enabled_later_train_control_has_a_next_page(scan_page):
    pager = scan(scan_page, RADIO + "<button>更晚車次</button>")
    assert pager["kind"] == "later"
    assert pager["hasNext"] is True


def test_unrecognized_later_train_text_stays_unknown(scan_page):
    pager = scan(scan_page, RADIO + "<p>請選擇更晚車次</p>")
    assert pager["kind"] == "unknown"
    assert pager["hasNext"] is False
