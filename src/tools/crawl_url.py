import asyncio

from urllib.parse import urlparse
from playwright.async_api import async_playwright, Page

from src.config.ahq_services import settings
from src.tools.browser_setup import ensure_chromium, is_missing_browser_error
from src.tools.url_guard import validate_public_http_url

MAX_PAGES = 20
NETWORK_IDLE_TIMEOUT = 10_000  # ms


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
    return await page.evaluate("""() => {
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

            elements.push({
                tag: el.tagName.toLowerCase(),
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
    }""")


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

        if css_hits == 1:
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
                    hosted: bool = False) -> dict:
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
        return await _crawl(url, credentials, max_pages, hosted)


async def _capture_page(page, final_url: str = None) -> dict:
    """One page's locator harvest, in the shape the tool returns per page.

    `final_url` reports where we actually landed rather than where we asked to go: add_locators
    upserts by page URL, so a redirect (http->https, "/" -> "/home", auth bounce) would otherwise
    file the captured locators under a URL that no longer serves them.
    """
    locators = await _extract_locators(page)
    valid_locators = await _validate_locators(page, locators)
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
    }


async def _crawl(url: str, credentials: dict, max_pages: int, hosted: bool) -> dict:
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
            await login_page.goto(url, wait_until="networkidle", timeout=30_000)
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
                password_field = login_page.locator('input[type="password"]').first
                form = password_field.locator("xpath=ancestor::form[1]")
                await form.locator(
                    'input[type="email"], input[name*="user"], input[name*="email"]'
                ).first.fill(credentials.get("username", ""))
                await password_field.fill(credentials.get("password", ""))
                start_url = login_page.url
                await form.locator('button[type="submit"], input[type="submit"]').first.click()
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

                links = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
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
