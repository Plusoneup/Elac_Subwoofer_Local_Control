"""GUI regression tests: script loads without errors, cards keep a sane width (no overlap, no squeezed
columns), no sideways scrolling in any GUI preset, and every label is translated in both languages.

Needs Playwright with Chromium (``pip install playwright && playwright install chromium``);
skipped when it is not available. Loads the page from disk, so no server is needed.
"""
import itertools
import unittest
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

PAGE = Path(__file__).resolve().parents[1] / "static" / "index.html"
ALL_SECTIONS = '["preset-card","control-card","connection-card","page-eq","page-autoeq"]'
STATES = {"expanded": "[]", "collapsed": ALL_SECTIONS}


def open_page(browser, width, height, state):
    """Load the page with every section expanded or collapsed (the state the GUI remembers per browser)."""
    page = browser.new_page(viewport={"width": width, "height": height})
    page.add_init_script(f"try{{localStorage.setItem('elac_gui_collapsed_v1', '{STATES[state]}')}}catch(e){{}}")
    page.goto(PAGE.as_uri())
    return page


CARDS = ["preset-card", "control-card", "connection-card", "page-eq", "page-autoeq"]
CONTRAST_JS = """(()=>{const lum=h=>{const n=parseInt(h.slice(1),16);return [n>>16&255,n>>8&255,n&255].map(c=>{c/=255;return c<=0.03928?c/12.92:Math.pow((c+0.055)/1.055,2.4)}).reduce((a,c,i)=>a+c*[0.2126,0.7152,0.0722][i],0)};
 const a=lum(getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()),b=lum(document.body.classList.contains('theme-light')?'#f2f4f6':'#0d1014');
 return (Math.max(a,b)+0.05)/(Math.min(a,b)+0.05)})()"""
SIZES = [(1440, 900), (1000, 800), (820, 900), (390, 844)]


def overlaps(a, b):
    w = min(a["x"] + a["width"], b["x"] + b["width"]) - max(a["x"], b["x"])
    h = min(a["y"] + a["height"], b["y"] + b["height"]) - max(a["y"], b["y"])
    return w > 1 and h > 1


def _launch(test, p):
    try:
        return p.chromium.launch()
    except Exception as exc:  # browser binary missing
        test.skipTest(f"chromium unavailable: {exc}")


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class GuiLayoutTests(unittest.TestCase):
    def test_page_script_runs_without_errors_and_initialises_layout(self):
        # A script error part-way through startup leaves the grid un-initialised (cards squeezed into slivers).
        with sync_playwright() as p:
            browser = _launch(self, p)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(PAGE.as_uri())
            page.wait_for_timeout(300)
            self.assertEqual(errors, [])
            self.assertIn("gui-layout-1", page.evaluate("document.getElementById('page-control').className"))
            browser.close()

    def test_cards_are_not_squeezed(self):
        with sync_playwright() as p:
            browser = _launch(self, p)
            for state in STATES:
                page = open_page(browser, 1440, 900, state)
                widths = page.evaluate("Object.fromEntries([...document.querySelectorAll('#page-control .card')].map(c=>[c.id,Math.round(c.getBoundingClientRect().width)]))")
                self.assertGreaterEqual(min(widths.values()), 250, f"{state}: {widths}")
                page.close()
            browser.close()

    def test_every_label_is_translated(self):
        with sync_playwright() as p:
            browser = _launch(self, p)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(PAGE.as_uri())
            page.click("#appearanceBtn")  # the language picker lives in the Appearance panel
            for lang in ("pl", "en"):
                page.select_option("#langPicker", lang)
                raw = page.evaluate("[...document.querySelectorAll('[data-i18n]')].filter(e=>e.textContent===e.dataset.i18n&&e.dataset.i18n.length>2).map(e=>e.dataset.i18n)")
                self.assertEqual(raw, [], f"untranslated keys in {lang}")
            browser.close()

    def test_no_overlap_expanded_or_collapsed_at_any_size(self):
        with sync_playwright() as p:
            browser = _launch(self, p)
            for width, height in SIZES:
                for state in STATES:
                    page = open_page(browser, width, height, state)
                    page.click("#appearanceBtn")  # the open panel must not break the layout either
                    boxes = {i: page.eval_on_selector("#" + i, "e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y+scrollY,width:r.width,height:r.height}}") for i in CARDS}
                    hits = [(a, b) for a, b in itertools.combinations(CARDS, 2) if overlaps(boxes[a], boxes[b])]
                    self.assertEqual(hits, [], f"{width}x{height} {state}")
                    self.assertEqual(page.evaluate("document.documentElement.scrollWidth-innerWidth"), 0, f"sideways scroll at {width}x{height} {state}")
                    if width <= 760:
                        narrow = [i for i in CARDS if boxes[i]["width"] < boxes["page-eq"]["width"] * 0.95]
                        self.assertEqual(narrow, [], f"cards not full width at {width}px")
                    page.close()
            browser.close()

    def test_header_status_fits_and_device_sheet_toggles(self):
        # The status badges live in the sticky header; on a phone they must fit one row, without sideways scroll.
        with sync_playwright() as p:
            browser = _launch(self, p)
            for width, height in SIZES:
                page = open_page(browser, width, height, "expanded")
                hdr = page.eval_on_selector("header.top", "e=>{const r=e.getBoundingClientRect();return {h:r.height,pos:getComputedStyle(e).position}}")
                self.assertEqual(hdr["pos"], "sticky")
                self.assertLess(hdr["h"], 130, f"header too tall at {width}px: {hdr}")
                rows = page.evaluate("new Set([...document.querySelectorAll('#sthead .badge2')].map(b=>Math.round(b.getBoundingClientRect().top))).size")
                self.assertEqual(rows, 1, f"status badges wrap at {width}px")
                self.assertEqual(page.evaluate("document.documentElement.scrollWidth-innerWidth"), 0)
                page.mouse.wheel(0, 1500)
                page.wait_for_timeout(100)
                self.assertLessEqual(abs(page.eval_on_selector("header.top", "e=>e.getBoundingClientRect().top")), 1, "header must stay on top")
                page.click("#deviceSheetBtn")
                self.assertTrue(page.is_visible("#deviceSheet"))
                box = page.eval_on_selector("#deviceSheet", "e=>{const r=e.getBoundingClientRect();return {l:r.left,r:r.right}}")
                self.assertGreaterEqual(box["l"], 0)
                self.assertLessEqual(box["r"], width)
                page.keyboard.press("Escape")
                self.assertFalse(page.is_visible("#deviceSheet"))
                page.close()
            browser.close()

    def test_removed_status_cards_and_auto_on_extras_are_gone(self):
        with sync_playwright() as p:
            browser = _launch(self, p)
            page = open_page(browser, 1440, 900, "expanded")
            for sel in ("#quick-card", "#device-card", "#page-device", "#powerModeRaw", "#autoon-card", "#writeDelayBtn", "#writeThresholdBtn", "#writeControlsBtn", "#writePresetBtn", "#control-card .range-meta", "[data-i18n=unit_unconfirmed]", "nav.nav"):
                self.assertEqual(page.locator(sel).count(), 0, sel)
            browser.close()

    def test_collapsed_sections_are_evenly_spaced(self):
        with sync_playwright() as p:
            browser = _launch(self, p)
            page = open_page(browser, 390, 844, "collapsed")
            tops = page.evaluate("[...document.querySelectorAll('.card[id]')].map(e=>{const r=e.getBoundingClientRect();return [r.top,r.bottom]}).sort((a,b)=>a[0]-b[0])")
            gaps = {round(b[0] - a[1]) for a, b in zip(tops, tops[1:])}
            self.assertEqual(len(gaps), 1, f"uneven gaps between sections: {gaps}")
            browser.close()

    def test_accent_and_swatches_stay_visible_in_both_themes(self):
        # White accent on the light theme (and black on the dark one) must not disappear.
        with sync_playwright() as p:
            browser = _launch(self, p)
            for theme, accent in (("light", "#FFFFFF"), ("dark", "#000000"), ("light", "#F4C2C2")):
                page = browser.new_page(viewport={"width": 390, "height": 844})
                page.add_init_script(f"try{{localStorage.setItem('elac_gui_theme','{theme}');localStorage.setItem('elac_gui_accent','{accent}')}}catch(e){{}}")
                page.goto(PAGE.as_uri())
                ratio = page.evaluate(CONTRAST_JS)
                self.assertGreaterEqual(ratio, 3, f"{theme} + {accent}")
                colours = page.evaluate("[...document.querySelectorAll('.accent-swatches button')].map(b=>getComputedStyle(b).backgroundColor)")
                self.assertEqual(len(set(colours)), len(colours), f"swatches lost their colours in {theme}: {colours}")
                page.close()
            browser.close()

    def test_every_theme_loads_cleanly_and_fits_a_phone(self):
        with sync_playwright() as p:
            browser = _launch(self, p)
            for theme in ("dark", "light", "amp", "pixel", "ash", "aramid", "debut", "gunmetal", "no-such-theme"):
                page = browser.new_page(viewport={"width": 390, "height": 844})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.add_init_script(f"try{{localStorage.setItem('elac_gui_theme','{theme}')}}catch(e){{}}")
                page.goto(PAGE.as_uri())
                page.wait_for_timeout(200)
                self.assertEqual(errors, [], theme)
                expected = theme if theme != "no-such-theme" else "dark"
                self.assertEqual(page.input_value("#themePicker"), expected)
                classes = page.evaluate("[...document.body.classList].filter(c=>c.startsWith('theme-'))")
                self.assertEqual(classes, [] if expected == "dark" else ["theme-" + expected])
                self.assertEqual(page.evaluate("document.documentElement.scrollWidth-innerWidth"), 0, theme)
                page.close()
            browser.close()

    def test_header_badge_opens_section_and_flashes_the_setting(self):
        with sync_playwright() as p:
            browser = _launch(self, p)
            page = open_page(browser, 390, 844, "collapsed")
            page.click("#sthead [data-goto='delayEntry']")
            page.wait_for_timeout(150)
            self.assertNotIn("collapsed", page.get_attribute("#control-card", "class"))
            self.assertIn("goto-flash", page.eval_on_selector("#delayEntry", "e=>e.closest('.knob').className"))
            page.wait_for_timeout(700)
            box = page.eval_on_selector("#delayEntry", "e=>{const r=e.getBoundingClientRect();return {top:r.top,bottom:r.bottom}}")
            self.assertGreater(box["top"], 0)
            self.assertLess(box["bottom"], 844)
            page.click("#sthead [data-goto='@aeq']")
            page.wait_for_timeout(150)
            self.assertNotIn("collapsed", page.get_attribute("#page-autoeq", "class"))
            self.assertIn("goto-flash", page.get_attribute("#aeqOnRow", "class"))
            browser.close()

    def test_write_switch_shows_one_floppy_per_theme(self):
        with sync_playwright() as p:
            browser = _launch(self, p)
            for theme in ("dark", "light", "amp", "pixel"):
                page = browser.new_page(viewport={"width": 390, "height": 844})
                page.add_init_script(f"try{{localStorage.setItem('elac_gui_theme','{theme}')}}catch(e){{}}")
                page.goto(PAGE.as_uri())
                shown = page.evaluate("[...document.querySelectorAll('#writeModeBtn svg')].filter(e=>getComputedStyle(e).display!=='none').map(e=>e.getAttribute('class'))")
                self.assertEqual(shown, ["gl-px" if theme == "pixel" else "gl-sm"], theme)
                self.assertTrue(page.evaluate("getComputedStyle(document.querySelector('#writeModeBtn .fl-slash')).display!=='none'"), "crossed out while writes are off")
                self.assertEqual(page.get_attribute("#writeModeBtn", "aria-label") or page.text_content("#writeModeLabel"), page.text_content("#writeModeLabel"))
                page.close()
            browser.close()

    def test_header_buttons_share_one_height(self):
        with sync_playwright() as p:
            browser = _launch(self, p)
            for theme in ("dark", "light", "amp", "pixel", "ash", "aramid", "debut", "gunmetal"):
                for width in (390, 1280):
                    page = browser.new_page(viewport={"width": width, "height": 800})
                    page.add_init_script(f"try{{localStorage.setItem('elac_gui_theme','{theme}')}}catch(e){{}}")
                    page.goto(PAGE.as_uri())
                    boxes = page.evaluate("['collapseAllBtn','appearanceBtn','connStatus','writeModeBtn'].map(id=>{const r=document.getElementById(id).getBoundingClientRect();return [Math.round(r.height),Math.round(r.top)]})")
                    self.assertEqual(len(set(map(tuple, boxes))), 1, f"{theme} {width}: {boxes}")
                    page.close()
            browser.close()

    def test_text_stays_readable_in_every_theme(self):
        # The main button and the control labels once rendered nearly invisible (black on black, pale grey on white).
        ratio = """(sel)=>{const rgb=c=>c.match(/[\\d.]+/g).slice(0,3).map(Number);
          const lum=a=>a.map(c=>{c/=255;return c<=0.03928?c/12.92:Math.pow((c+0.055)/1.055,2.4)}).reduce((s,c,i)=>s+c*[0.2126,0.7152,0.0722][i],0);
          let e=document.querySelector(sel),bg=null;const fg=rgb(getComputedStyle(e).color);
          for(let n=e;n&&!bg;n=n.parentElement){const s=getComputedStyle(n);
            for(const c of [s.backgroundColor,(s.backgroundImage.match(/rgba?\\([^)]*\\)/)||[''])[0]]){
              const v=(c.match(/[\\d.]+/g)||[]);if(!bg&&v.length>=3&&(v.length<4||+v[3]>0.5))bg=v.slice(0,3).map(Number)}}
          const a=lum(fg),b=lum(bg||[0,0,0]);return (Math.max(a,b)+0.05)/(Math.min(a,b)+0.05)}"""
        with sync_playwright() as p:
            browser = _launch(self, p)
            for theme in ["dark", "light", "amp", "pixel", "ash", "aramid", "debut", "gunmetal"]:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.add_init_script(f"try{{localStorage.setItem('elac_gui_theme','{theme}');localStorage.setItem('elac_gui_collapsed_v1','[]')}}catch(e){{}}")
                page.goto(PAGE.as_uri())
                page.wait_for_timeout(200)
                for sel, need in (("#connectRead", 4.5), ("#control-card .knob label", 4.5)):
                    self.assertGreaterEqual(page.evaluate(ratio, sel), need, f"{theme}: {sel}")
                page.close()
            browser.close()


if __name__ == "__main__":
    unittest.main()
