"""The result scan classifies a disabled 更晚車次 control as the end of the list."""

import pytest
from playwright.sync_api import sync_playwright

from thsr_watcher.booking_browser import (
    FEEDBACK, RESULT_TABLE, TRAIN_RADIO, PlaywrightBookingBrowser, _SCAN_JS,
)

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
    '<div><button disabled>更晚車次</button></div>',
    '<div><button disabled><span>更晚車次</span></button></div>',
    '<div><button aria-disabled="true"><span>更晚車次</span></button></div>',
    '<div><input type="button" disabled value="更晚車次"></div>',
    '<fieldset disabled><div><button>更晚車次</button></div></fieldset>',
])
def test_disabled_later_train_control_has_no_next_page(scan_page, html):
    assert scan(scan_page, RADIO + html)["kind"] == "later"
    assert scan(scan_page, RADIO + html)["hasNext"] is False
    assert scan_page.locator('[data-thsr-watcher-later]').count() == 0


@pytest.mark.parametrize("word,attribute", [
    ("更晚車次", "later"), ("更早車次", "earlier"),
])
def test_nested_control_uses_button_state_and_clears_stale_marker(scan_page, word, attribute):
    pager = scan(scan_page, RADIO + f'<div><button><span>{word}</span></button></div>')
    flag = "hasNext" if attribute == "later" else "hasEarlier"
    assert pager[flag] is True
    marked = scan_page.locator(f'[data-thsr-watcher-{attribute}]')
    assert marked.evaluate('(el) => el.tagName') == "BUTTON"
    scan_page.locator('button').evaluate('(el) => el.disabled = true')
    found = scan_page.evaluate(_SCAN_JS, {
        "table": RESULT_TABLE, "radio": TRAIN_RADIO, "feedback": FEEDBACK,
        "patterns": "nomatch", "staleOrigin": None, "staleCodes": None,
    })
    assert found["pager"][flag] is False
    assert marked.count() == 0


def test_enabled_later_train_control_has_a_next_page(scan_page):
    pager = scan(scan_page, RADIO + "<button>更晚車次</button>")
    assert pager["kind"] == "later"
    assert pager["hasNext"] is True


def test_unrecognized_later_train_text_stays_unknown(scan_page):
    pager = scan(scan_page, RADIO + "<p>請選擇更晚車次</p>")
    assert pager["kind"] == "unknown"
    assert pager["hasNext"] is False


@pytest.mark.parametrize("latest_codes", [
    ["825", "829", "833", "837", "841"],
    ["825"],
])
def test_result_refresh_uses_latest_trains_and_pager_together(scan_page, latest_codes):
    def radios(codes):
        return ''.join(
            f'<input type="radio" name="TrainQueryDataViewPanel:TrainGroup" '
            f'querycode="{code}" querydeparture="14:19" queryarrival="15:39">'
            for code in codes
        )

    scan_page.set_content(
        '<body><div id="results">' + radios(["833", "837", "841"])
        + '</div><button disabled>更晚車次</button></body>'
    )
    browser = PlaywrightBookingBrowser()
    browser._page = scan_page
    initial = browser._wait_for_scan()
    # The DOM finishes updating between the first scan and the pager refresh.
    scan_page.locator('#results').evaluate('(el, html) => el.innerHTML = html', radios(latest_codes))
    scan_page.locator('body').evaluate(
        '(el) => el.insertAdjacentHTML("beforeend", "<button>更早車次</button>")'
    )
    trains = browser._collect_result_pages(initial, empty_result=None, follow=False)
    assert [train.number for train in trains] == latest_codes
    assert [train.number for train in browser._listed] == latest_codes
    assert set(browser._train_page) == set(latest_codes)
    assert browser.has_earlier_results() is True
    assert browser.has_later_results() is False
