"""Playwright browser automation for capturing page resources."""

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Optional

from playwright.async_api import Page, Response, async_playwright
from playwright_stealth import stealth_async

from .utils import should_skip_url


@dataclass
class CapturedResource:
    """A captured network resource."""

    url: str
    content_type: str
    body: bytes


async def solve_turnstile(page: Page, on_status: Optional[callable] = None) -> bool:
    """Attempt to solve Cloudflare Turnstile if present."""
    def _status(msg: str) -> None:
        if on_status:
            on_status(msg)

    # Check for token already present
    token = await page.evaluate("""() => {
        const el = document.querySelector('[name=cf-turnstile-response]');
        if (el && el.value && el.value.length > 10) return el.value;
        if (typeof turnstile !== 'undefined') {
            try { return turnstile.getResponse(); } catch (e) {}
        }
        return '';
    }""")
    if token:
        _status("Turnstile token already present.")
        return True

    # Look for Turnstile iframe
    for _ in range(15): # Try for 30 seconds
        iframe_info = await page.evaluate("""() => {
            const iframes = document.querySelectorAll('iframe');
            for (const f of iframes) {
                const src = f.src || '';
                if (src.includes('challenges.cloudflare.com')) {
                    const r = f.getBoundingClientRect();
                    return { x: r.left, y: r.top, w: r.width, h: r.height };
                }
            }
            return null;
        }""")

        if iframe_info:
            _status("Turnstile iframe found, clicking...")
            # Calculate click point like in source.cpp
            cx = iframe_info['x'] + 28 + random.randint(-4, 4)
            cy = iframe_info['y'] + (iframe_info['h'] / 2) + random.randint(-3, 3)
            
            await page.mouse.move(cx, cy, steps=random.randint(10, 20))
            await asyncio.sleep(random.uniform(0.1, 0.3))
            await page.mouse.click(cx, cy)
            
            # Wait for response
            for _ in range(10):
                await asyncio.sleep(1)
                token = await page.evaluate("""() => {
                    const el = document.querySelector('[name=cf-turnstile-response]');
                    if (el && el.value && el.value.length > 10) return el.value;
                    return '';
                }""")
                if token:
                    _status("Turnstile solved!")
                    return True
        
        await asyncio.sleep(2)
    
    return False


async def capture_page_resources(
    url: str,
    wait_time: int = 0,
    on_status: callable = None,
) -> list[CapturedResource]:
    """Load a page and capture all network resources.

    Args:
        url: URL of the page to load.
        wait_time: Additional seconds to wait after page load for JS content.
        on_status: Optional callback for status updates (receives string message).

    Returns:
        List of captured resources with their content.

    Raises:
        Exception: If browser launch or page load fails.
    """
    captured: list[CapturedResource] = []
    pending_responses: list[Response] = []

    def _status(msg: str) -> None:
        if on_status:
            on_status(msg)

    async def handle_response(response: Response) -> None:
        """Collect successful responses for later processing."""
        # Skip non-successful responses
        if not response.ok:
            return

        # Skip URLs we can't/shouldn't download
        if should_skip_url(response.url):
            return

        pending_responses.append(response)

    async with async_playwright() as p:
        _status("Launching stealthy browser...")
        browser = await p.chromium.launch(headless=False) # Headful often helps with bypass
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080},
            accept_downloads=True,
            bypass_csp=True,
        )
        
        # Apply stealth
        await stealth_async(context)
        
        page = await context.new_page()

        # Register response handler BEFORE navigation
        page.on("response", handle_response)

        _status(f"Navigating to {url}...")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
            # Try to solve Turnstile
            await solve_turnstile(page, on_status)
            
            # Wait for network idle after potential solving
            try:
                await page.wait_for_load_state("networkidle", timeout=30000)
            except:
                pass

        except Exception as e:
            await browser.close()
            raise RuntimeError(f"Failed to load page: {e}") from e

        # Additional wait time for SPAs/lazy-loaded content
        if wait_time > 0:
            _status(f"Waiting {wait_time}s for additional content...")
            await asyncio.sleep(wait_time)

        # Process all collected responses
        _status(f"Processing {len(pending_responses)} responses...")
        for response in pending_responses:
            try:
                body = await response.body()
                content_type = response.headers.get("content-type", "")
                captured.append(CapturedResource(
                    url=response.url,
                    content_type=content_type,
                    body=body,
                ))
            except Exception:
                # Response body may no longer be available (e.g., redirects)
                # Just skip it
                pass

        await browser.close()

    return captured
