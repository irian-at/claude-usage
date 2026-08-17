#!/usr/bin/env python3
"""Check Claude.ai usage stats across multiple accounts via internal API."""

import setproctitle
setproctitle.setproctitle("claude-usage-checker")

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright, Error as PlaywrightError

PROJECT_DIR = Path(__file__).parent
PROFILES_DIR = PROJECT_DIR / "profiles"
CONFIG_PATH = PROJECT_DIR / "accounts.json"

BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
]


def create_accounts_interactive() -> list[dict]:
    import re
    print("No accounts.json found. Let's set up your accounts.\n")
    accounts = []
    while True:
        email = input("  Email address [empty to finish]: ").strip()
        if not email:
            break
        account_id = re.sub(r"[^a-z0-9-]", "", email.split("@")[0].lower().replace(".", "-"))
        accounts.append({"id": account_id, "email": email})
        print(f"  Added {email}\n")

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
            try:
                await page.goto("https://claude.ai/login", wait_until="domcontentloaded", timeout=60_000)
            except PlaywrightError as e:
                print(f"  Login page is loading slowly ({e}); continuing. Use the browser window once it appears.")

            print(f"\n  Setting up {email}")
            print("  In the browser window, either:")
            print("    A) click 'Continue with Google' and finish the Google sign-in, or")
            print(f"    B) click 'Continue with email' ({email} is pre-filled),")
            print(f"       then check the {email} inbox (also spam).")
            print("       If the mail contains a sign-in LINK instead of a code:")
            print("       right-click it, copy the link address, and paste it below.")

            if email:
                try:
                    email_input = page.locator("input[type='email'], input[name='email']").first
                    await email_input.wait_for(timeout=15_000)
                    await email_input.fill(email)
                except PlaywrightError:
                    print(f"  Could not pre-fill the email. Enter {email} manually in the browser.")

            code = input("  Code or sign-in link from the email (leave empty if you used Google sign-in): ").strip()
            if code.startswith("http"):
                try:
                    await page.goto(code, wait_until="domcontentloaded", timeout=60_000)
                except PlaywrightError:
                    print("  Could not open the link automatically. Paste it into the browser's address bar manually.")
            elif code:
                try:
                    code_input = page.locator("input[inputmode='numeric'], input[type='number'], input[type='text']").first
                    await code_input.wait_for(timeout=10_000)
                    await code_input.fill(code)
                    await code_input.press("Enter")
                except PlaywrightError:
                    print("  Could not auto-fill the code. Please enter it manually in the browser.")

            print("  Waiting for login to complete (up to 5 minutes)...")

            deadline = asyncio.get_event_loop().time() + 300
            while asyncio.get_event_loop().time() < deadline:
                try:
                    current_url = page.url
                    if "claude.ai" in current_url and "/login" not in current_url:
                        await ctx.storage_state(path=str(state_path(account)))
                        print(f"  {email} logged in successfully.")
                        await ctx.close()
                        return True
                except PlaywrightError:
                    print(f"  Browser closed for {email}. Skipping.")
                    return False
                await asyncio.sleep(2)

            print(f"  Timeout waiting for login on {email}.")
            await ctx.close()
            return False

        except PlaywrightError as e:
            print(f"  Error for {email}: {e}")
            return False


async def fetch_json(page, url: str) -> tuple[int, dict | list | None]:
    """Fetch a JSON API endpoint using the browser context (bypasses Cloudflare)."""
    resp = await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    status = resp.status if resp else 0
    if status != 200:
        return status, None
    text = await page.evaluate("() => document.body.innerText")
    return status, json.loads(text)


async def check_account(account: dict, playwright) -> dict:
    profile_path = PROFILES_DIR / account["id"]
    email = account["email"]
    if not profile_path.exists():
        return {"email": email, "error": "Not set up yet. Run: python check_usage.py setup"}

    try:
        ctx = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=BROWSER_ARGS,
            ignore_default_args=["--enable-automation"],
        )
    except PlaywrightError as e:
        return {"email": email, "error": f"Browser launch failed: {e}"}

    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # First navigate to claude.ai to pass any Cloudflare challenge
        await page.goto("https://claude.ai", wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2000)

        if "login" in page.url:
            await ctx.close()
            return {"email": email, "error": "Session expired. Run: python check_usage.py setup"}

        status, orgs = await fetch_json(page, "https://claude.ai/api/organizations")
        if status == 403 or orgs is None:
            await ctx.close()
            return {"email": email, "error": "Session expired. Run: python check_usage.py setup"}

        chat_orgs = [o for o in orgs if "chat" in o.get("capabilities", [])]
        paid_orgs = [o for o in chat_orgs if o.get("raven_type") or o.get("billing_type") not in (None, "none")]
        if not paid_orgs:
            paid_orgs = chat_orgs

        # Only personal orgs count; team orgs are ignored
        paid_orgs = [o for o in paid_orgs if o.get("raven_type") != "team"]

        org_results = []
        for org in paid_orgs:
            usage_status, usage_data = await fetch_json(page, f"https://claude.ai/api/organizations/{org['uuid']}/usage")
            if usage_status == 200 and usage_data:
                org_results.append({
                    "org_name": org["name"],
                    "billing_type": org.get("billing_type"),
                    "raven_type": org.get("raven_type"),
                    "usage": usage_data,
                })

        await ctx.close()
        return {"email": email, "orgs": org_results}

    except PlaywrightError as e:
        try:
            await ctx.close()
        except PlaywrightError:
            pass
        return {"email": email, "error": f"Browser error: {e}"}


def print_result(result: dict) -> None:
    email = result["email"]
    if "error" in result:
        print(f"\n  {email}: {result['error']}")
        return

    for org in result.get("orgs", []):
        usage = org["usage"]
        plan = org.get("raven_type") or org.get("billing_type") or "free"

        print(f"\n  {email} ({plan}):")

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
        emails = ", ".join(a["email"] for a in needs_setup)
        print(f"Accounts need login: {emails}\n")
        for account in needs_setup:
            success = await setup_account(account)
            if not success:
                print(f"  Skipped {account['email']}. Re-run setup to retry.")
        print("\nSetup complete.")
        if len(sys.argv) > 1 and sys.argv[1] == "setup":
            return

    print("Checking Claude.ai usage...\n")
    print("=" * 60)

    results = []
    async with async_playwright() as p:
        for account in accounts:
            result = await check_account(account, p)
            results.append(result)
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
