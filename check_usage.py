#!/usr/bin/env python3
"""Check Claude.ai usage stats across multiple accounts via internal API."""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from playwright.async_api import async_playwright, Error as PlaywrightError

PROJECT_DIR = Path(__file__).parent
PROFILES_DIR = PROJECT_DIR / "profiles"
CONFIG_PATH = PROJECT_DIR / "accounts.json"

BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
]

API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
    "anthropic-client-platform": "web_claude_ai",
    "anthropic-client-version": "1.0.0",
    "Content-Type": "application/json",
}


def create_accounts_interactive() -> list[dict]:
    import re
    print("No accounts.json found. Let's set up your accounts.\n")
    accounts = []
    while True:
        name = input("  Account name (e.g. 'Personal', 'Work') [empty to finish]: ").strip()
        if not name:
            break
        email = input("  Email address: ").strip()
        if not email:
            break
        account_id = re.sub(r"[^a-z0-9-]", "", name.lower().replace(" ", "-"))
        accounts.append({"name": name, "id": account_id, "email": email})
        print(f"  Added '{name}' ({email})\n")

    if not accounts:
        print("No accounts added. Exiting.")
        sys.exit(1)

    CONFIG_PATH.write_text(json.dumps(accounts, indent=2))
    print(f"Saved {len(accounts)} account(s) to {CONFIG_PATH}\n")
    return accounts


def load_accounts() -> list[dict]:
    if not CONFIG_PATH.exists():
        return create_accounts_interactive()
    return json.loads(CONFIG_PATH.read_text())


def format_reset(resets_at: str | None) -> str:
    if not resets_at:
        return "not started"
    reset_dt = datetime.fromisoformat(resets_at)
    now = datetime.now(timezone.utc)
    diff = reset_dt - now
    total_seconds = int(diff.total_seconds())
    if total_seconds <= 0:
        return "resetting now"
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    if hours > 24:
        return f"resets {reset_dt.strftime('%a %H:%M')}"
    if hours > 0:
        return f"resets in {hours}h {minutes}m"
    return f"resets in {minutes}m"


def state_path(account: dict) -> Path:
    return PROFILES_DIR / account["id"] / "state.json"


def load_cookies(account: dict) -> httpx.Cookies:
    sp = state_path(account)
    state = json.loads(sp.read_text())
    cookies = httpx.Cookies()
    for c in state["cookies"]:
        if "claude.ai" in c["domain"]:
            cookies.set(c["name"], c["value"], domain="claude.ai", path=c["path"])
    return cookies


async def setup_account(account: dict) -> bool:
    profile_path = PROFILES_DIR / account["id"]
    profile_path.mkdir(parents=True, exist_ok=True)
    email = account.get("email", "")

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=BROWSER_ARGS,
            ignore_default_args=["--enable-automation"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        try:
            await page.goto("https://claude.ai/login")
            await page.wait_for_load_state("networkidle")

            print(f"\n  Setting up '{account['name']}' ({email})")
            print("  Use EMAIL login (not Google) — enter the verification code from your email.")

            if email:
                try:
                    email_input = page.locator("input[type='email'], input[name='email'], input[type='text']").first
                    await email_input.wait_for(timeout=5000)
                    await email_input.fill(email)
                    submit_btn = page.locator("button:has-text('E-Mail'), button:has-text('email'), button:has-text('Email')").first
                    await submit_btn.click(timeout=5000)
                    print(f"  Email filled in. Check {email} for the verification code.")
                except PlaywrightError:
                    print(f"  Could not auto-fill email. Please enter {email} manually.")

            print("  Waiting for login to complete (5 min timeout)...")

            deadline = asyncio.get_event_loop().time() + 300
            while asyncio.get_event_loop().time() < deadline:
                try:
                    current_url = page.url
                    if "claude.ai" in current_url and "/login" not in current_url:
                        await ctx.storage_state(path=str(state_path(account)))
                        print(f"  '{account['name']}' logged in successfully.")
                        await ctx.close()
                        return True
                except PlaywrightError:
                    print(f"  Browser closed for '{account['name']}'. Skipping.")
                    return False
                await asyncio.sleep(2)

            print(f"  Timeout waiting for login on '{account['name']}'.")
            await ctx.close()
            return False

        except PlaywrightError as e:
            print(f"  Error for '{account['name']}': {e}")
            return False


async def refresh_state(account: dict) -> bool:
    """Re-export storage state from persistent profile (no login needed)."""
    profile_path = PROFILES_DIR / account["id"]
    if not profile_path.exists():
        return False

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=BROWSER_ARGS,
            ignore_default_args=["--enable-automation"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://claude.ai", timeout=30_000)
        await page.wait_for_load_state("domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2000)

        if "login" in page.url:
            await ctx.close()
            return False

        await ctx.storage_state(path=str(state_path(account)))
        await ctx.close()
        return True


async def check_account(account: dict) -> dict:
    sp = state_path(account)
    if not sp.exists():
        return {"name": account["name"], "error": "Not set up yet. Run: python check_usage.py setup"}

    try:
        cookies = load_cookies(account)
    except (json.JSONDecodeError, KeyError) as e:
        return {"name": account["name"], "error": f"Bad state file: {e}"}

    async with httpx.AsyncClient(cookies=cookies, headers=API_HEADERS, base_url="https://claude.ai") as client:
        resp = await client.get("/api/organizations")
        if resp.status_code == 403:
            # Try refreshing the state from persistent profile
            if await refresh_state(account):
                cookies = load_cookies(account)
                async with httpx.AsyncClient(cookies=cookies, headers=API_HEADERS, base_url="https://claude.ai") as client2:
                    resp = await client2.get("/api/organizations")
                    if resp.status_code != 200:
                        return {"name": account["name"], "error": "Session expired. Run: python check_usage.py setup"}
                    client = client2
                    # Fall through to process orgs
            else:
                return {"name": account["name"], "error": "Session expired. Run: python check_usage.py setup"}

        if resp.status_code != 200:
            return {"name": account["name"], "error": f"API error: {resp.status_code}"}

        orgs = resp.json()
        chat_orgs = [o for o in orgs if "chat" in o.get("capabilities", [])]
        paid_orgs = [o for o in chat_orgs if o.get("raven_type") or o.get("billing_type") not in (None, "none")]
        if not paid_orgs:
            paid_orgs = chat_orgs

        org_results = []
        for org in paid_orgs:
            usage_resp = await client.get(f"/api/organizations/{org['uuid']}/usage")
            if usage_resp.status_code == 200:
                org_results.append({
                    "org_name": org["name"],
                    "billing_type": org.get("billing_type"),
                    "raven_type": org.get("raven_type"),
                    "usage": usage_resp.json(),
                })

    return {"name": account["name"], "email": account.get("email", ""), "orgs": org_results}


def print_result(result: dict) -> None:
    name = result["name"]
    if "error" in result:
        print(f"\n  {name}: {result['error']}")
        return

    for org in result.get("orgs", []):
        usage = org["usage"]
        plan = org.get("raven_type") or org.get("billing_type") or "free"

        print(f"\n  {name} ({plan}):")

        session = usage.get("five_hour")
        if session:
            print(f"    Session:      {session['utilization']:5.0f}%   {format_reset(session.get('resets_at'))}")

        weekly = usage.get("seven_day")
        if weekly:
            print(f"    All models:   {weekly['utilization']:5.0f}%   {format_reset(weekly.get('resets_at'))}")

        sonnet = usage.get("seven_day_sonnet")
        if sonnet:
            print(f"    Sonnet:       {sonnet['utilization']:5.0f}%   {format_reset(sonnet.get('resets_at'))}")

        opus = usage.get("seven_day_opus")
        if opus:
            print(f"    Opus:         {opus['utilization']:5.0f}%   {format_reset(opus.get('resets_at'))}")

        cowork = usage.get("seven_day_cowork")
        if cowork:
            print(f"    Cowork:       {cowork['utilization']:5.0f}%   {format_reset(cowork.get('resets_at'))}")

        extra = usage.get("extra_usage")
        if extra and extra.get("is_enabled"):
            used = extra.get("used_credits") or 0
            limit = extra.get("monthly_limit") or 0
            util = extra.get("utilization") or 0
            print(f"    Extra usage:  {util:5.0f}%   ({used:.0f}/{limit} credits)")


async def main() -> None:
    accounts = load_accounts()

    needs_setup = [a for a in accounts if not state_path(a).exists()]
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        needs_setup = accounts

    if needs_setup:
        names = ", ".join(a["name"] for a in needs_setup)
        print(f"Accounts need login: {names}\n")
        for account in needs_setup:
            success = await setup_account(account)
            if not success:
                print(f"  Skipped '{account['name']}'. Re-run setup to retry.")
        print("\nSetup complete.")
        if len(sys.argv) > 1 and sys.argv[1] == "setup":
            return

    print("Checking Claude.ai usage...\n")
    print("=" * 60)

    results = await asyncio.gather(*[check_account(account) for account in accounts])

    for result in results:
        print_result(result)

    print()

    # Write results to JSON for other scripts to consume
    output = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "accounts": results,
    }
    output_path = PROJECT_DIR / "usage_latest.json"
    output_path.write_text(json.dumps(output, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
