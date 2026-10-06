import asyncio
import re

from urllib.parse import urlparse
from playwright.async_api import async_playwright, Page

from src.config.ahq_services import settings
from src.tools.browser_setup import ensure_chromium, is_missing_browser_error
from src.tools.url_guard import validate_public_http_url

MAX_PAGES = 20
# Ceiling on what a caller may ask for. A hosted crawl holds a headless Chromium for its whole run,
# so the page count is bounded even when the caller wants a whole app.
MAX_PAGES_LIMIT = 50
# Collapsed menu groups and flyouts opened per page before capture. An app's whole navigation is
# rarely more than a few dozen groups; the bound stops a page full of accordions running forever.
MAX_MENU_EXPANSIONS = 40
# Visible text longer than this is content, not a label - not a usable locator.
MAX_LOCATOR_TEXT = 60
# Collapsed navigation that hides its items until opened: antd/rc-menu submenus, ARIA menus and
# disclosure buttons, inside the page's navigation only - never a form's dropdown.
COLLAPSED_MENU_SELECTOR = ", ".join(
    f"{scope} [aria-expanded=\"false\"]:not([data-crawl-expanded])"
    for scope in ("nav", "aside", "[role=\"navigation\"]", "[role=\"menu\"]", "[role=\"menubar\"]", ".ant-menu")
)
NETWORK_IDLE_TIMEOUT = 10_000  # ms
OVERLAY_WAIT_STEPS = 6  # x 500 ms: how long a page is watched for a panel that opens after load
LOGIN_FORM_RENDER_TIMEOUT = 10_000  # ms: how long a credentialed crawl waits for the sign-in form to render

# Marks the sign-in form for the locators below. A password field counts only when an email or
# username field sits with it - in its <form>, or within a few enclosing elements when there is no
# form. A lone password-type box is something else: an API page's "Bearer token" field was taken
# for the login password, so the crawl typed the credentials into it and never signed in.
_MARK_LOGIN_FORM_JS = """() => {
  const visible = el => !!el && el.offsetParent !== null;
  const userish = el => visible(el) && el.type !== 'password' && (el.type === 'email' ||
    /user|email|e-mail|login|account|mobile|phone/i.test([el.name, el.id, el.placeholder,
      el.autocomplete, el.getAttribute('aria-label')].join(' ')));
  document.querySelectorAll('[data-crawl-login]').forEach(el => el.removeAttribute('data-crawl-login'));
  for (const pw of document.querySelectorAll('input[type=password]')) {
    if (!visible(pw)) continue;
    let scope = pw.closest('form');
    if (!scope || ![...scope.querySelectorAll('input')].some(userish)) {
      scope = null;
      let node = pw.parentElement;
      for (let depth = 0; node && depth < 6; depth++, node = node.parentElement) {
        if ([...node.querySelectorAll('input')].some(userish)) { scope = node; break; }
      }
    }
    if (!scope) continue;
    pw.setAttribute('data-crawl-login', 'password');
    scope.setAttribute('data-crawl-login', 'form');
    const user = [...scope.querySelectorAll('input')].find(userish);
    if (user) user.setAttribute('data-crawl-login', 'username');
    return true;
  }
  return false;
}"""

# The control that opens a sign-in form kept behind it - a button that opens a dialog, or a link to
# a sign-in page, often in a new tab. Exact wording only, so "Sign up" or "Log in with Google" is
# never followed.
_LOGIN_TRIGGER = r"^\s*(log\s?in|sign\s?in)\s*$"


class _NoLoginForm(Exception):
    """The page has no sign-in form, so there is nothing to submit the credentials to."""


async def _settle(page) -> None:
    """Best-effort wait for a page to finish loading; never fails."""
    try:
        await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT)
    except Exception:
        pass


async def _find_login_form(page) -> bool:
    """Marks the page's sign-in form; waits for one to render first. False when there is none."""
    try:
        await page.locator('input[type="password"]').first.wait_for(
            state="visible", timeout=LOGIN_FORM_RENDER_TIMEOUT)
    except Exception:
        pass
    try:
        return bool(await page.evaluate(_MARK_LOGIN_FORM_JS))
    except Exception:
        return False


async def _reveal_login_form(page, hosted: bool) -> bool:
    """
    Opens a sign-in form that the page keeps behind a "Log in" / "Sign in" control.

    A site's front page is often a landing page or a usable signed-out app rather than a login
    screen: a marketing page whose "Sign In" opens the app's sign-in page in a new tab, or an app
    whose "Log in" opens a dialog while a help panel that opens on first visit covers the button.
    A link is followed in this same page so a new-tab target is not lost; a button is clicked once
    any open panel is closed.
    """
    trigger = page.locator("a:visible, button:visible").filter(
        has_text=re.compile(_LOGIN_TRIGGER, re.IGNORECASE)).first
    try:
        if not await trigger.count():
            return False
        href = await trigger.evaluate("el => el.tagName === 'A' ? el.href : null")
        if href and not href.startswith("javascript:"):
            # The hosted server opens only public addresses, the same check every crawled page gets.
            if hosted and await validate_public_http_url(href):
                return False
            await page.goto(href, wait_until="domcontentloaded", timeout=30_000)
            await _settle(page)
        else:
            await page.keyboard.press("Escape")
            try:
                await trigger.click(timeout=5_000)
            except Exception:
                await trigger.click(timeout=5_000, force=True)
    except Exception:
        return False
    return await _find_login_form(page)


def _dedup_key(url: str) -> str:
    """Key for the visited set. A bare origin and its rooted form are one page, and a fragment
    never identifies a distinct server-rendered page, so both must collapse — otherwise a
    redirect target or a self-link burns a second slot out of max_pages on the same page."""
    parsed = urlparse(url)
    return parsed._replace(path=parsed.path or "/", fragment="").geturl()

# Hosted pods serve many tenants: cap simultaneous headless-Chromium instances so N users
# crawling at once can't OOM the pod (memory is sized for ~this many browsers, see the Helm
# values). Waiting callers just queue on the semaphore; the MCP client's own request timeout
# is the backstop.
_CRAWL_SEMAPHORE = asyncio.Semaphore(max(1, settings.ahq_mcp_crawl_concurrency))


async def _extract_locators(page: Page) -> list[dict]:
    return await page.evaluate("""(MAX_TEXT) => {
        const elements = [];
        const interactable = document.querySelectorAll(
            'a, button, input, select, textarea, [role="button"], [role="link"], [role="menuitem"], [onclick]'
        );

        const getXPath = (el) => {
            if (el.id) return `//*[@id="${el.id}"]`;
            if (el === document.body) return '/html/body';
            const siblings = el.parentNode.childNodes;
            let pos = 0;
            for (let i = 0; i < siblings.length; i++) {
                if (siblings[i] === el) break;
                if (siblings[i].nodeType === 1 && siblings[i].tagName === el.tagName) pos++;
            }
            return `${getXPath(el.parentNode)}/${el.tagName.toLowerCase()}[${pos + 1}]`;
        };

        interactable.forEach((el) => {
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) return;

            // Set only below, when nothing on the element is meant to identify it - the case where
            // its visible text is the most stable handle it has.
            let stable = true;
            let css = null;
            if (el.id) {
                css = `#${el.id}`;
            } else if (el.getAttribute('data-testid')) {
                css = `[data-testid="${el.getAttribute('data-testid')}"]`;
            } else if (el.getAttribute('aria-label')) {
                css = `[aria-label="${el.getAttribute('aria-label')}"]`;
            } else if (el.getAttribute('name')) {
                css = el.tagName.toLowerCase() + '[name="' + el.getAttribute('name') + '"]';
            } else if (el.getAttribute('placeholder')) {
                css = el.tagName.toLowerCase() + '[placeholder="' + el.getAttribute('placeholder') + '"]';
            } else {
                stable = false;
                // Every class goes through CSS.escape. A Tailwind arbitrary value such as
                // mt-[0.5px] is not merely unmatched when left raw - the bracket opens an
                // attribute selector, so the whole query throws InvalidSelectorException and
                // the step fails with "The element locator is invalid". className is also an
                // SVGAnimatedString on SVG elements, which has no trim().
                const classes = (typeof el.className === 'string')
                    ? el.className.trim().split(/\\s+/).filter(Boolean).map(function (c) { return CSS.escape(c); })
                    : [];
                css = el.tagName.toLowerCase() + (classes.length ? '.' + classes.join('.') : '');
            }

            // A sidebar item has no id, test id or label - only its text. A class chain is shared
            // by every item in the menu and a positional XPath breaks when one is added, so the
            // label is the locator: //a[normalize-space(.)='Test Scripts'].
            let textXpath = null;
            const label = (el.textContent || '').replace(/\\s+/g, ' ').trim();
            if (!stable && label && label.length <= MAX_TEXT) {
                const literal = label.indexOf("'") === -1 ? "'" + label + "'"
                    : label.indexOf('"') === -1 ? '"' + label + '"'
                    : "concat('" + label.split("'").join(`', "'", '`) + "')";
                textXpath = '//' + el.tagName.toLowerCase() + '[normalize-space(.)=' + literal + ']';
            }

            elements.push({
                tag: el.tagName.toLowerCase(),
                textXpath: textXpath,
                type: el.getAttribute('type') || null,
                // innerText is what the user SEES, after CSS. textContent is what a text-based
                // locator has to match. They differ whenever text-transform is in play — an
                // antd/Tailwind sidebar reading "Test Management" in the DOM renders as
                // "TEST MANAGEMENT", and a locator written against the rendered string matches
                // nothing. Both are returned so the caller can pick, and textTransform names
                // the reason when they disagree.
                text: (el.innerText || el.value || '').trim().slice(0, 100),
                textContent: (el.textContent || '').trim().slice(0, 100),
                textTransform: (function () {
                    var t = getComputedStyle(el).textTransform;
                    return (t && t !== 'none') ? t : null;
                })(),
                ariaLabel: el.getAttribute('aria-label') || null,
                placeholder: el.getAttribute('placeholder') || null,
                id: el.id || null,
                name: el.getAttribute('name') || null,
                css: css,
                xpath: getXPath(el),
            });
        });
        return elements;
    }""", MAX_LOCATOR_TEXT)


async def _count_matches(page: Page, selector: str, *, is_xpath: bool = False) -> int:
    try:
        return await page.locator(f"xpath={selector}" if is_xpath else selector).count()
    except Exception:
        return 0


async def _validate_locators(page: Page, locators: list[dict]) -> list[dict]:
    """Keep locators that resolve to EXACTLY ONE element, recording which strategy achieved it.

    Matching at all is not the bar. The extractor falls back to a bare tag selector whenever an
    element has no id and no usable class — every footer link on saucedemo.com comes back as
    css "a" — and a `count() > 0` check accepts that happily. A step built from it acts on the
    first link on the page rather than the intended element, while the crawl reports a 100%
    resolution rate, so the failure is silent in both directions.
    """
    valid = []
    for loc in locators:
        css_hits = await _count_matches(page, loc["css"]) if loc.get("css") else 0
        xpath_hits = (
            await _count_matches(page, loc["xpath"], is_xpath=True) if loc.get("xpath") else 0
        )

        text_hits = (
            await _count_matches(page, loc["textXpath"], is_xpath=True) if loc.get("textXpath") else 0
        )
        text_xpath = loc.pop("textXpath", None)
        if text_hits == 1:
            # The label beats both a class chain and a position: it survives the menu being
            # reordered or grown, and it reads as what the step clicks.
            loc["positionalXpath"] = loc.get("xpath")
            loc["xpath"] = text_xpath
            xpath_hits = 1
            loc["preferred"] = "xpath"
            if css_hits == 1:
                css_hits = 0  # a unique class chain is still a class chain; keep it as a fallback
        elif css_hits == 1:
            loc["preferred"] = "css"
        elif xpath_hits == 1:
            loc["preferred"] = "xpath"
        else:
            # Neither strategy identifies one element; reporting it as a captured locator
            # would hand the caller a selector that silently acts on the wrong thing.
            continue

        if css_hits != 1:
            # Explicit so a caller assembling locationStrategies never promotes it to primary.
            loc["cssAmbiguous"] = True
        valid.append(loc)
    return valid


async def crawl_url(url: str, credentials: dict = None, max_pages: int = MAX_PAGES,
                    hosted: bool = False, follow_links: bool = True) -> dict:
    max_pages = max(1, min(int(max_pages or MAX_PAGES), MAX_PAGES_LIMIT))
    if hosted:
        # SSRF guard (Slice 9j): a hosted crawl runs INSIDE the cluster, so a crafted URL
        # (or a same-domain link found while crawling) must never reach private/metadata
        # addresses. Checked here for the entry URL and again on every dequeued URL below —
        # per-navigation DNS re-resolution is the guard; the rebinding TOCTOU window between
        # check and goto is a documented accepted residual risk (Playwright can't pin IPs).
        blocked = await validate_public_http_url(url)
        if blocked:
            return {"error": blocked}

    async with _CRAWL_SEMAPHORE:
        return await _crawl(url, credentials, max_pages, hosted, follow_links)


# Dialogs and panels open over the page on arrival - a welcome tour, a help panel, a cookie banner.
# Each is reported with its close control's label, if it has one.
_OPEN_OVERLAYS_JS = r"""() => {
  const shown = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const closer = /^(\u00d7|x|close|dismiss|got it|skip|skip tour|no thanks|maybe later|ok|accept|accept all)$/i;
  return [...document.querySelectorAll('[role=dialog],[role=alertdialog],[aria-modal="true"],dialog[open]')]
    .filter(shown).map(e => {
      const label = e.getAttribute('aria-labelledby');
      const title = ((label && document.getElementById(label)?.innerText) ||
                     e.querySelector('h1,h2,h3,h4')?.innerText || '').trim().split('\n')[0].slice(0, 80);
      const close = [...e.querySelectorAll('button,[role=button],a')].filter(shown).find(b =>
        /close|dismiss/i.test(b.getAttribute('aria-label') || '') || closer.test((b.innerText || '').trim()));
      return { title, close: close ? ((close.innerText || '').trim() || close.getAttribute('aria-label')) : null };
    });
}"""


async def _dismiss_overlays(page) -> list:
    """
    Closes whatever is open over the page on arrival and reports how, so a test of the page can do
    the same first. A help panel that opens on a first visit covered the sidebar of a signed-in
    app: every click a generated script made landed on the panel, and the run failed on a step that
    looked correct. Escape is tried first, then the overlay's own close control.
    """
    # These typically open a moment after the page has loaded (one measured at 2.3 s), later than
    # the capture would otherwise start, so watch for one briefly before concluding there is none.
    overlays = []
    try:
        for _ in range(OVERLAY_WAIT_STEPS):
            overlays = await page.evaluate(_OPEN_OVERLAYS_JS)
            if overlays:
                break
            await page.wait_for_timeout(500)
    except Exception:
        return []
    found = []
    for overlay in overlays[:3]:
        closed_with = None
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(500)
            if len(await page.evaluate(_OPEN_OVERLAYS_JS)) < len(overlays):
                closed_with = "Press key ESCAPE"
            elif overlay.get("close"):
                await page.get_by_role("button", name=overlay["close"]).first.click(timeout=3_000)
                await page.wait_for_timeout(500)
                if len(await page.evaluate(_OPEN_OVERLAYS_JS)) < len(overlays):
                    closed_with = f'Click "{overlay["close"]}"'
        except Exception:
            pass
        found.append({"title": overlay.get("title") or "untitled dialog",
                      "closes_with": closed_with or "could not be closed automatically"})
        overlays = await page.evaluate(_OPEN_OVERLAYS_JS)
        if not overlays:
            break
    return found


async def _capture_page(page, final_url: str = None) -> dict:
    """One page's locator harvest, in the shape the tool returns per page.

    `final_url` reports where we actually landed rather than where we asked to go: add_locators
    upserts by page URL, so a redirect (http->https, "/" -> "/home", auth bounce) would otherwise
    file the captured locators under a URL that no longer serves them.
    """
    overlays = await _dismiss_overlays(page)
    locators = await _extract_locators(page)
    valid_locators = await _validate_locators(page, locators)
    seen = {(loc.get("xpath"), loc.get("text")) for loc in locators}
    revealed, opened = await _expand_navigation(page, seen)
    locators += revealed["all"]
    valid_locators += revealed["valid"]
    total, valid = len(locators), len(valid_locators)
    resolution_rate = round(valid / total, 2) if total > 0 else 0.0
    return {
        "url": final_url or page.url,
        "title": await page.title(),
        "locators": valid_locators,
        "total_found": total,
        "total_valid": valid,
        "resolution_rate": resolution_rate,
        "passes_threshold": resolution_rate >= 0.80,
        "menus_expanded": opened,
        **({"overlays_on_arrival": overlays,
            "overlay_note": ("These were open over the page when it loaded and would block every click "
                             "behind them. A test of this page must close them first with the "
                             "closes_with step, right after the page loads.")} if overlays else {}),
    }


async def _expand_navigation(page, seen: set) -> tuple[dict, int]:
    """
    Open every collapsed menu group and flyout in the page's navigation, one at a time,
    capturing what each reveals.

    A collapsed antd submenu renders its items at zero size and a flyout mounts its items only
    once opened, so a capture of the page as loaded misses most of a sidebar - every module under
    a closed group. Each batch is validated straight after its click: a flyout closes when the
    next one opens, so validating at the end would find its items gone. A click that navigates
    instead of expanding is undone, so the capture stays on the page it describes.
    """
    revealed = {"all": [], "valid": []}
    opened = 0
    start_url = page.url
    for _ in range(MAX_MENU_EXPANSIONS):
        # A handle, not a locator: a locator re-resolves on every use, so once this element is
        # marked it would quietly point at the NEXT collapsed menu and click that one instead.
        try:
            trigger = await page.query_selector(COLLAPSED_MENU_SELECTOR)
        except Exception:
            # A page that cannot be queried still gets the capture it already has.
            break
        if trigger is None:
            break
        try:
            await trigger.evaluate("el => el.setAttribute('data-crawl-expanded', 'true')")
            if not await trigger.is_visible():
                continue
            await trigger.click(timeout=3_000)
            await page.wait_for_timeout(400)
        except Exception:
            continue
        if page.url != start_url:
            try:
                await page.go_back(wait_until="domcontentloaded")
                await page.wait_for_timeout(500)
            except Exception:
                break
            continue
        opened += 1
        fresh = [loc for loc in await _extract_locators(page)
                 if (loc.get("xpath"), loc.get("text")) not in seen]
        for loc in fresh:
            seen.add((loc.get("xpath"), loc.get("text")))
        revealed["all"] += fresh
        revealed["valid"] += await _validate_locators(page, fresh)
    return revealed, opened


async def _crawl(url: str, credentials: dict, max_pages: int, hosted: bool, follow_links: bool = True) -> dict:
    base_domain = urlparse(url).netloc
    visited: set[str] = set()
    pages_data = []

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except Exception as e:
            # The classic playwright pip-vs-browser mismatch: the installed playwright package
            # expects a specific chromium build (e.g. chromium-1228) that `playwright install`
            # hasn't downloaded yet — true on any fresh install, and again after every playwright
            # version bump. Fetch it and retry rather than making the caller run a command and
            # start the whole request over.
            if not is_missing_browser_error(e):
                raise
            failure = await ensure_chromium(hosted)
            if failure:
                return {"error": failure}
            browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()

        post_login_url = None
        pre_auth_page = None
        auth = None
        if credentials:
            # Every failure below is caught and ignored so the crawl still returns the pre-auth
            # pages, which are worth having. That silence is the problem: bad credentials came
            # back as pages_crawled: 1 with no error, which is indistinguishable from an app that
            # genuinely has one page — and sends the caller off debugging timing and headless
            # theories instead of the password. Record what happened as we go and report it.
            auth = {"attempted": True, "succeeded": False, "detail": None, "final_url": None}
            login_page = await context.new_page()
            # domcontentloaded, then a best-effort settle: a page that keeps a connection open
            # never goes network-idle, and requiring it timed out a sign-in page that a browser
            # renders in three seconds.
            await login_page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await _settle(login_page)
            # networkidle is not "the form has rendered": a client-rendered sign-in page (Next.js,
            # React) mounts its form a beat after the network goes quiet, and on the slower hosted
            # pod the capture below used to run in that gap — pages_crawled: 1, 0 locators, ok:true
            # (live 2026-10-02 against dev.automationhq.ai/login, while a local run of the same
            # call found all 9). _find_login_form waits for the password field, and the form then
            # gets the settle time the normal crawl loop already gives a page.
            login_form = await _find_login_form(login_page) or await _reveal_login_form(login_page, hosted)
            await login_page.wait_for_timeout(1_000)
            # Capture the sign-in form BEFORE submitting it. This is the only moment it exists in
            # this crawl: once the context holds a session, every later visit to the same URL
            # renders the authenticated app instead, so a credentialed crawl used to come back
            # with the whole product and none of the email/password/submit locators a login test
            # actually needs — costing a second, credential-less crawl to get them.
            try:
                pre_auth_page = await _capture_page(login_page)
            except Exception:
                pre_auth_page = None
            try:
                # Scope username/submit lookups to the <form> containing the password field,
                # not the whole page. A login page commonly also has a nearby "Sign Up" button
                # (confirmed live against app.automationhq.ai: both "SIGN IN" and "Sign Up" are
                # button[type="submit"]) — an unscoped selector matches both, Playwright's
                # strict-mode click throws on the ambiguity, and the bare except below swallowed
                # it silently, so the form was never actually submitted despite no visible error.
                if not login_form:
                    raise _NoLoginForm()
                form = login_page.locator('[data-crawl-login="form"]')
                password_field = login_page.locator('[data-crawl-login="password"]')
                await login_page.locator('[data-crawl-login="username"]').first.fill(
                    credentials.get("username", ""))
                await password_field.fill(credentials.get("password", ""))
                start_url = login_page.url
                submit = form.locator('button[type="submit"], input[type="submit"]')
                if not await submit.count():
                    # A form without a real submit button signs in from a plain button.
                    submit = form.locator("button").filter(has_text=re.compile(
                        r"^\s*(log\s?in|sign\s?in|continue|submit|next)\s*$", re.IGNORECASE))
                if await submit.count():
                    await submit.first.click()
                else:
                    await password_field.press("Enter")
                # networkidle alone races the SPA's post-login redirect: there's commonly a brief
                # network lull right after the login API call resolves and before the client-side
                # navigation to the authenticated area actually starts, so wait_for_load_state can
                # return while still sitting on the login URL (confirmed live against
                # app.automationhq.ai: login → /checking → /{org}/{project}/dashboard). Wait for the
                # URL to actually change first, then let it settle. A prior version of this fix
                # checked `"login" not in u.lower()` instead of comparing against `start_url` — that
                # is trivially true from the very first instant whenever the caller's own crawl
                # target (the page we log in FROM) doesn't itself contain the substring "login" (e.g.
                # this app's bare root "/" also renders the login form), making the wait a same-tick
                # no-op and leaving the exact race it was meant to close (confirmed live: crawl_url
                # against "https://app.automationhq.ai/" still only ever discovered the 4 pre-auth
                # pages). Comparing against the URL actually seen before the click, whatever it was,
                # correctly catches the first real navigation (to the transitional "/checking" page)
                # regardless of what the starting URL happens to be.
                try:
                    await login_page.wait_for_url(lambda u: u != start_url, timeout=15_000)
                except Exception:
                    # Sounds the same for bad credentials and for a submit that never fired,
                    # so report what was observed rather than guessing which.
                    auth["detail"] = ("still on the sign-in URL 15s after submitting - the "
                                      "credentials were most likely rejected")
                await login_page.wait_for_load_state("networkidle", timeout=15_000)
                # A changed URL alone is not proof: a form that submits as GET, or posts back to
                # itself, moves the URL while leaving you on the sign-in screen. A completed
                # sign-in stops rendering a password field; a rejected one re-renders it with an
                # error. That is the signal that distinguishes them.
                if await login_page.locator('input[type="password"]').count():
                    auth["succeeded"] = False
                    auth["detail"] = auth["detail"] or (
                        "a password field is still on the page after submitting — the "
                        "credentials were most likely rejected")
                elif auth["detail"] is None:
                    auth["succeeded"] = True
            except _NoLoginForm:
                auth["detail"] = (
                    "no sign-in form was found - not on this page and not behind a 'Log in' / "
                    "'Sign in' button. Crawl the sign-in page's own URL (e.g. .../login or .../auth) "
                    "with the credentials. The pages below are what a signed-out visitor sees.")
            except Exception as exc:
                # Keep the type: str() on a Playwright timeout can be empty, which would render
                # as "could not complete the sign-in form: " and say nothing at all.
                auth["detail"] = (f"could not complete the sign-in form: "
                                  f"{type(exc).__name__}: {exc}".rstrip(": "))
            # Where the login landed is the authenticated entry point, and it has to be crawled
            # explicitly. Starting the crawl from the caller's original url instead never leaves
            # the login screen on any app that keeps serving the login form at its pre-auth URL
            # for an already-signed-in session — confirmed live against saucedemo.com, where
            # login redirects to /inventory.html but "/" still renders the form, so a
            # credentialed crawl returned nothing but the three login-page fields.
            post_login_url = login_page.url
            auth["final_url"] = post_login_url
            await login_page.close()

        queue = [url]
        if post_login_url and _dedup_key(post_login_url) != _dedup_key(url):
            queue.insert(0, post_login_url)
        if pre_auth_page:
            # Already captured, and re-visiting it now would overwrite the sign-in form with
            # whatever the authenticated session renders at that URL — mark it done.
            pages_data.append(pre_auth_page)
            visited.add(_dedup_key(pre_auth_page["url"]))

        while queue and len(visited) < max_pages:
            current_url = queue.pop(0)
            if _dedup_key(current_url) in visited:
                continue
            visited.add(_dedup_key(current_url))

            if hosted:
                blocked = await validate_public_http_url(current_url)
                if blocked:
                    pages_data.append({"url": current_url, "error": blocked})
                    continue

            try:
                page = await context.new_page()
                # domcontentloaded is the hard requirement — networkidle is best-effort only.
                # A page that keeps a persistent connection open (websocket/long-polling) never
                # goes network-idle, which would otherwise block the crawl on that page forever.
                await page.goto(current_url, wait_until="domcontentloaded", timeout=NETWORK_IDLE_TIMEOUT + 20_000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT)
                except Exception:
                    pass
                await page.wait_for_timeout(1_000)

                # Report where we actually landed, not where we asked to go: add_locators upserts
                # by page URL, so a redirect (http->https, "/" -> "/home", auth bounce) would
                # otherwise file the captured locators under a URL that no longer serves them.
                final_url = page.url
                visited.add(_dedup_key(final_url))

                pages_data.append(await _capture_page(page, final_url))

                # follow_links=False captures the landing page only - enough for a test of its own
                # menus, without spending a browser on every page they lead to.
                links = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)") if follow_links else []
                for link in links:
                    parsed = urlparse(link)
                    if (
                        parsed.scheme in ("http", "https")
                        and parsed.netloc == base_domain
                        and _dedup_key(link) not in visited
                        and link not in queue
                    ):
                        queue.append(link)

                await page.close()

            except Exception as e:
                pages_data.append({"url": current_url, "error": str(e)})

        await browser.close()

    total_locators = sum(p.get("total_valid", 0) for p in pages_data)
    result = {
        "pages_crawled": len([p for p in pages_data if "error" not in p]),
        "total_locators": total_locators,
        "pages": pages_data,
    }
    if auth is not None:
        if auth["succeeded"] and auth["detail"] is None:
            auth["detail"] = "signed in and reached the authenticated area"
        result["auth"] = auth
    return result
