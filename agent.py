import asyncio
import os
import random
import uuid
import sys
from datetime import datetime
from typing import Dict, Any, List, Annotated
import operator
from playwright.async_api import async_playwright
from langgraph.graph import StateGraph, END

import database
import llm

# Directories for session data and screenshots
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USER_DATA_DIR = os.path.join(BASE_DIR, "user_data")
SCREENSHOTS_DIR = os.path.join(BASE_DIR, "static", "screenshots")
os.makedirs(USER_DATA_DIR, exist_ok=True)
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

class JobFlowAgent:
    def __init__(self, profile_data: dict, log_callback=None, screenshot_callback=None):
        self.profile = profile_data
        self.log_callback = log_callback
        self.screenshot_callback = screenshot_callback
        self.api_key = profile_data.get("settings", {}).get("gemini_api_key", "") or os.environ.get("GEMINI_API_KEY", "")

        self.auto_submit = profile_data.get("settings", {}).get("auto_submit", True)
        self.is_running = True
        
        # State Machine memory
        self.page = None
        self.job_cards = []
        self.current_card_index = 0

    def get_custom_qa_answer(self, question: str) -> str:
        """
        Reads from custom_qa.json and returns user-defined answer if it exists.
        """
        custom_qa_file = os.path.join(BASE_DIR, "custom_qa.json")
        if not os.path.exists(custom_qa_file):
            return None
        try:
            import json
            with open(custom_qa_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get(question)
        except Exception:
            return None

    def register_custom_qa(self, question: str, answer: str):
        """
        Registers a newly resolved question and its answer in custom_qa.json.
        """
        custom_qa_file = os.path.join(BASE_DIR, "custom_qa.json")
        import json
        
        data = {
            "__instructions__": "This is your custom Q&A dictionary. The agent registers new questions here with its generated answers. You can edit any answer here, and the agent will use your edited answer next time!"
        }
        
        if os.path.exists(custom_qa_file):
            try:
                with open(custom_qa_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                pass
                
        # If the question is not already registered, save it!
        if question not in data:
            data[question] = answer
            try:
                with open(custom_qa_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
            except Exception:
                pass

    async def log(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted = f"[{timestamp}] {message}"
        try:
            import sys
            print(formatted)
        except UnicodeEncodeError:
            # Safely handle Windows CP1252 consoles that crash on emojis
            try:
                enc = sys.stdout.encoding or 'utf-8'
                print(formatted.encode(enc, errors='replace').decode(enc))
            except Exception:
                pass
        
        # Ensure WebSocket callback always receives logs even if terminal print fails
        if self.log_callback:
            try:
                await self.log_callback(formatted)
            except Exception:
                pass

    async def sleep_human_like(self, min_sec=1.5, max_sec=3.5):
        delay = random.uniform(min_sec, max_sec)
        await asyncio.sleep(delay)

    # ================= LANGGRAPH NODES =================
    async def search_node(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        LangGraph Node: Scrapes job cards from the search pane based on current keyword/location.
        """
        if not self.is_running or state["applied_count"] >= state["max_applications"]:
            return state

        kw = state["keywords"][state["keyword_index"]]
        loc = state["locations"][state["location_index"]]
        
        await self.log(f"🤖 [LangGraph Node: Search] Crawling '{kw}' in '{loc}'...")
        
        # Navigate to search (f_AL=true is Easy Apply)
        search_url = f"https://www.linkedin.com/jobs/search/?keywords={kw}&location={loc}&f_AL=true"
        try:
            await self.page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            await self.log(f"⚠️ Search navigation timeout or slow load: {e}. Proceeding anyway...")
        await self.sleep_human_like(3, 5)
        
        # Scroll search pane to load more jobs
        try:
            await self.page.evaluate("document.querySelector('.jobs-search-results-list').scrollTop = 500")
            await self.sleep_human_like(1, 2)
            await self.page.evaluate("document.querySelector('.jobs-search-results-list').scrollTop = 1000")
            await self.sleep_human_like(1, 2)
        except Exception:
            pass
            
        # Wait for the job cards container or list items to appear to avoid 0-card race conditions
        try:
            await self.page.wait_for_selector("li.jobs-search-results__list-item", timeout=12000)
        except Exception:
            try:
                await self.page.wait_for_selector("[data-occludable-job-id]", timeout=5000)
            except Exception:
                pass
            
        # Extract job cards on the active pane
        self.job_cards = await self.page.locator("li.jobs-search-results__list-item").all()
        if len(self.job_cards) == 0:
            # Fallback selector
            self.job_cards = await self.page.locator("[data-occludable-job-id]").all()
            
        await self.log(f"Found {len(self.job_cards)} job cards in search panel.")
        self.current_card_index = 0
        
        # State machine transition fix: if 0 cards found, advance search indices immediately in the returned state
        if len(self.job_cards) == 0:
            next_state = dict(state)
            next_loc_idx = state["location_index"] + 1
            if next_loc_idx < len(state["locations"]):
                next_state["location_index"] = next_loc_idx
            else:
                next_kw_idx = state["keyword_index"] + 1
                if next_kw_idx < len(state["keywords"]):
                    next_state["keyword_index"] = next_kw_idx
                    next_state["location_index"] = 0
                else:
                    # Mark indices out of bounds to trigger end transition
                    next_state["keyword_index"] = len(state["keywords"])
            return next_state
            
        return state

    async def parse_and_fill_node(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        LangGraph Node: Parses the active job detail pane and fills easy apply form using Pydantic structured output.
        """
        if not self.is_running or self.current_card_index >= len(self.job_cards):
            return state
            
        card = self.job_cards[self.current_card_index]
        self.current_card_index += 1
        
        try:
            # Resilient Job Title Selector
            title_selectors = [
                ".job-card-list__title",
                "[class*='job-card-list__title']",
                "[class*='job-card-container__link']",
                ".job-card-title",
                "a[href*='/jobs/view/']"
            ]
            title = "Unknown Title"
            for selector in title_selectors:
                loc = card.locator(selector).first
                if await loc.is_visible():
                    title = (await loc.inner_text()).strip()
                    break
            
            # Resilient Company Name Selector
            company_selectors = [
                ".job-card-container__primary-description",
                "[class*='job-card-container__primary-description']",
                "[class*='company-name']",
                ".job-card-container__company-name",
                "span.job-card-container__primary-description"
            ]
            company = "Unknown Company"
            for selector in company_selectors:
                loc = card.locator(selector).first
                if await loc.is_visible():
                    company = (await loc.inner_text()).strip()
                    break
            self.current_company = company
            
            # Click card
            await card.click()
            await self.sleep_human_like(2, 3)
            
            job_url = self.page.url
            await self.log(f"👉 🤖 [LangGraph Node: Parser] Analyzing: '{title}' at '{company}'...")
            
            # Check DB Cache
            existing = database.get_cached_answer(f"applied:{job_url}")
            if existing:
                await self.log("⏭️ Already processed or applied. Skipping.")
                return state
                
            easy_apply_btn = self.page.locator("button.jobs-apply-button:has-text('Easy Apply')").first
            if not await easy_apply_btn.is_visible():
                easy_apply_btn = self.page.locator("button:has-text('Easy Apply')").first
                
            if not await easy_apply_btn.is_visible():
                await self.log("⏭️ No 'Easy Apply' button found on detail page. Skipping.")
                return state
                
            # Add to SQLite
            app_id = database.add_application(
                job_title=title,
                company=company,
                location=state["locations"][state["location_index"]],
                link=job_url,
                status="Applying"
            )
            
            # Click apply
            await easy_apply_btn.click()
            await self.sleep_human_like(2, 3)
            
            success = await self.fill_easy_apply_form(self.page, title, company, app_id)
            
            next_state = dict(state)
            if success:
                next_state["applied_count"] = state["applied_count"] + 1
                await self.log(f"✅ Successfully submitted to '{title}' at '{company}'! ({next_state['applied_count']}/{state['max_applications']})")
                database.save_qa_pair(f"applied:{job_url}", "true")
            else:
                await self.log(f"❌ Failed to complete application to '{title}' at '{company}'.")
                
            # State machine transition fix: if last card, advance search indices in the returned state
            if self.current_card_index >= len(self.job_cards):
                next_loc_idx = state["location_index"] + 1
                if next_loc_idx < len(state["locations"]):
                    next_state["location_index"] = next_loc_idx
                else:
                    next_kw_idx = state["keyword_index"] + 1
                    if next_kw_idx < len(state["keywords"]):
                        next_state["keyword_index"] = next_kw_idx
                        next_state["location_index"] = 0
                    else:
                        next_state["keyword_index"] = len(state["keywords"])
            return next_state
                
        except Exception as card_err:
            await self.log(f"⚠️ Error handling card: {card_err}")
            
        # Ensure indices are advanced if an error occurs on the last card
        next_state = dict(state)
        if self.current_card_index >= len(self.job_cards):
            next_loc_idx = state["location_index"] + 1
            if next_loc_idx < len(state["locations"]):
                next_state["location_index"] = next_loc_idx
            else:
                next_kw_idx = state["keyword_index"] + 1
                if next_kw_idx < len(state["keywords"]):
                    next_state["keyword_index"] = next_kw_idx
                    next_state["location_index"] = 0
                else:
                    next_state["keyword_index"] = len(state["keywords"])
        return next_state

    def should_continue_crawler(self, state: Dict[str, Any]) -> str:
        """
        LangGraph Transition Edge: Determines the next transition in the state machine.
        """
        if not self.is_running or state["applied_count"] >= state["max_applications"]:
            return "end"
            
        # Check if we have more cards in current list
        if self.current_card_index < len(self.job_cards):
            return "parse_and_fill"
            
        # If we have reached the end of the cards (or found 0), the state indices have already been advanced by the nodes!
        if state["keyword_index"] < len(state["keywords"]):
            return "search"
            
        return "end"

    # ================= CORE AGENT WORKFLOW =================
    async def start(self):
        self.is_running = True
        
        # 1. Initialize Playwright
        async with async_playwright() as p:
            await self.log("Starting JobFlow Browser Session...")
            
            is_linux = sys.platform.startswith("linux")
            headless = True if is_linux else self.profile.get("settings", {}).get("headless", False)
            
            # Determine User Agent: Only override if running headless to prevent version spoofing discrepancies in headful mode
            ua_param = None
            if headless:
                ua_param = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
            
            browser_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-service-autorun",
                "--password-store=basic",
                "--use-mock-keychain",
                "--disable-features=EdgeSidebar,EdgeWallet,EdgeSearchCustomization,EdgeHubArea,EdgeInfluencerCard,EdgeSmartScreen"
            ]
            if is_linux:
                browser_args.extend(["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])

            # On Windows try Edge first, on Linux/Docker use standard Chromium
            try:
                if not is_linux:
                    browser = await p.chromium.launch(
                        headless=headless,
                        channel="msedge",
                        args=browser_args
                    )
                else:
                    browser = await p.chromium.launch(
                        headless=headless,
                        args=browser_args
                    )
            except Exception as launch_err:
                await self.log(f"⚠️ Falling back to default Chromium browser: {launch_err}")
                browser = await p.chromium.launch(
                    headless=headless,
                    args=browser_args
                )
            
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=ua_param
            )
            
            # Load standard cookies.json if it exists to retrieve active login session
            cookies_file = os.path.join(BASE_DIR, "cookies.json")
            if os.path.exists(cookies_file):
                await self.log("🔄 Loading saved session cookies from cookies.json...")
                import json
                try:
                    with open(cookies_file, "r") as f:
                        cookies = json.load(f)
                    await context.add_cookies(cookies)
                except Exception as ce:
                    await self.log(f"⚠️ Failed to load saved cookies: {ce}")
            
            # Ultimate Stealth Injection: Emulates genuine Microsoft Edge on prototype, permissions, plugins level
            await context.add_init_script("""
                try {
                    // 1. Delete and override webdriver on prototype
                    const newProto = Object.getPrototypeOf(navigator);
                    delete newProto.webdriver;
                    Object.defineProperty(newProto, 'webdriver', { get: () => undefined });
                } catch(e) {}
                
                try {
                    // 2. Mock chrome runtime object
                    window.chrome = {
                        runtime: {},
                        loadTimes: function() {},
                        csi: function() {},
                        app: {}
                    };
                } catch(e) {}
                
                try {
                    // 3. Mock plugins
                    Object.defineProperty(navigator, 'plugins', {
                        get: () => [
                            { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                            { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' }
                        ]
                    });
                } catch(e) {}
                
                try {
                    // 4. Mock languages
                    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                } catch(e) {}
                
                try {
                    // 5. Mock permissions query
                    const originalQuery = window.navigator.permissions.query;
                    window.navigator.permissions.query = (parameters) => (
                        parameters.name === 'notifications' ?
                            Promise.resolve({ state: Notification.permission }) :
                            originalQuery(parameters)
                    );
                } catch(e) {}
            """)
            
            self.page = await context.new_page()
            
            try:
                # 2. Check LinkedIn Authentication State
                # Check if we have a saved login session (cookies.json exists and contains active cookies)
                if not os.path.exists(cookies_file):
                    await self.log("🧹 No active session found. Preparing a clean, stable login environment...")
                    # Go directly to login page cleanly and once
                    try:
                        await self.page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=60000)
                    except Exception as e:
                        await self.log(f"⚠️ Login navigation warning/slow load: {e}. Proceeding anyway...")
                else:
                    await self.log("🔄 Active session cookies loaded. Accessing feed...")
                    try:
                        await self.page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=60000)
                    except Exception as e:
                        await self.log(f"⚠️ Feed navigation warning/slow load: {e}. Proceeding anyway...")
                
                await self.sleep_human_like(2, 4)
                
                # Check if we are logged in (if we are authenticated, we will be on the feed page)
                if "feed" not in self.page.url:
                    await self.log("🔐 USER ACTION REQUIRED: Please log into LinkedIn in the browser window.")
                    await self.log("Waiting for active session...")
                    
                    # Wait up to 5 minutes for user to manually log in
                    logged_in = False
                    for i in range(150): # 150 * 2 seconds = 300 seconds
                        if not self.is_running:
                            break
                        if "feed" in self.page.url:
                            logged_in = True
                            break
                        await asyncio.sleep(2)
                        
                    if not logged_in:
                        await self.log("❌ Login timeout or agent stopped. Exiting.")
                        # Delete cookies file if login was unsuccessful
                        try:
                            if os.path.exists(cookies_file):
                                os.remove(cookies_file)
                        except Exception:
                            pass
                        await browser.close()
                        return
                    
                    await self.log("✅ Successfully authenticated!")
                    # Save successful login session cookies
                    try:
                        import json
                        cookies = await context.cookies()
                        with open(cookies_file, "w") as f:
                            json.dump(cookies, f, indent=2)
                        await self.log("💾 Active session cookies saved locally to cookies.json!")
                    except Exception as se:
                        await self.log(f"⚠️ Failed to save session cookies: {se}")
                    
                else:
                    # Refresh saved cookies just in case
                    try:
                        import json
                        cookies = await context.cookies()
                        with open(cookies_file, "w") as f:
                            json.dump(cookies, f, indent=2)
                    except Exception:
                        pass
                    
                await self.log("LinkedIn session active. Preparing job crawler...")
                
                # 3. Initialize LangGraph Workflow Orchestration Engine
                keywords = self.profile.get("settings", {}).get("search_keywords", ["Software Engineering Intern"])
                locations = self.profile.get("settings", {}).get("search_locations", ["Remote"])
                applied_today = 0
                max_apps = self.profile.get("settings", {}).get("max_applications", 10)
                
                # Define Graph Structure
                workflow = StateGraph(dict)
                
                workflow.add_node("search", self.search_node)
                workflow.add_node("parse_and_fill", self.parse_and_fill_node)
                
                workflow.set_entry_point("search")
                
                workflow.add_conditional_edges(
                    "search",
                    self.should_continue_crawler,
                    {
                        "search": "search",
                        "parse_and_fill": "parse_and_fill",
                        "end": END
                    }
                )
                
                workflow.add_conditional_edges(
                    "parse_and_fill",
                    self.should_continue_crawler,
                    {
                        "search": "search",
                        "parse_and_fill": "parse_and_fill",
                        "end": END
                    }
                )
                
                app = workflow.compile()
                
                initial_state = {
                    "keywords": keywords,
                    "locations": locations,
                    "keyword_index": 0,
                    "location_index": 0,
                    "applied_count": applied_today,
                    "max_applications": max_apps,
                    "logs": []
                }
                
                await self.log("🚀 [LangGraph] Starting workflow engine state machine...")
                await app.ainvoke(initial_state)
                
            except Exception as e:
                await self.log(f"🚨 Fatal Agent Error: {e}")
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass
                self.is_running = False
                await self.log("Browser session closed.")

    async def fill_easy_apply_form(self, page, title: str, company: str, app_id: int) -> bool:
        """
        Walks through the multi-step LinkedIn Easy Apply modal form.
        """
        logs = []
        async def record_log(msg: str):
            logs.append(msg)
            await self.log(msg)
            
        await record_log("Entering Application form wizard...")
        
        # We will loop through form pages (Next -> Next -> Review -> Submit)
        max_steps = 15
        step = 0
        
        while step < max_steps:
            if not self.is_running:
                return False
                
            step += 1
            # Wait for slide transitions and new elements to render in the DOM
            await self.sleep_human_like(1.8, 3.0)
            await record_log(f"Form Step #{step}: Scanning elements...")
            
            # Check if there is an error banner
            error_selector = ".artdeco-inline-feedback--error"
            if await page.locator(error_selector).first.is_visible():
                error_msg = await page.locator(error_selector).first.inner_text()
                await record_log(f"⚠️ Form validation error on page: {error_msg.strip()}")
                
            # Take screenshot of the current state for logs/dashboard
            screenshot_name = f"app_{app_id}_step_{step}.png"
            screenshot_path = os.path.join(SCREENSHOTS_DIR, screenshot_name)
            await page.screenshot(path=screenshot_path)
            database.update_application(app_id, "Applying", "\n".join(logs), f"/static/screenshots/{screenshot_name}")
            if self.screenshot_callback:
                await self.screenshot_callback(f"/static/screenshots/{screenshot_name}")
                
            # 1. Look for form elements
            # A. File inputs (Upload resume)
            file_inputs = await page.locator("input[type='file']").all()
            for file_input in file_inputs:
                try:
                    # Look if resume needs to be uploaded
                    label = await page.locator(f"label[for='{await file_input.get_attribute('id')}']").inner_text()
                    if "resume" in label.lower() or "cv" in label.lower():
                        # Find resume file in current folder
                        resume_file = None
                        for file in os.listdir(BASE_DIR):
                            if file.lower().startswith("resume") and (file.endswith(".pdf") or file.endswith(".docx") or file.endswith(".txt")):
                                resume_file = os.path.join(BASE_DIR, file)
                                break
                        
                        if resume_file:
                            await record_log(f"📄 Uploading resume file: {os.path.basename(resume_file)}")
                            await file_input.set_input_files(resume_file)
                            await self.sleep_human_like(1.5, 2.5)
                        else:
                            await record_log("⚠️ Resume requested, but no 'resume.pdf/docx/txt' found in project folder!")
                except Exception as file_err:
                    await record_log(f"⚠️ Error uploading file: {file_err}")

            # Diagnostic & Broadened Form Scanning
            try:
                raw_inputs = await page.locator("input, textarea, select, fieldset").all()
                await record_log(f"🔎 Found {len(raw_inputs)} form-related elements on active page DOM.")
            except Exception as diag_err:
                await record_log(f"⚠️ Diagnostic scanning error: {diag_err}")

            # B. Text/Numeric Inputs (Broadened to include email, tel, number, no-type, and textareas)
            inputs = await page.locator("input[type='text'], input[type='tel'], input[type='email'], input[type='number'], input:not([type]), textarea").all()
            for inp in inputs:
                try:
                    inp_type = await inp.get_attribute("type") or "text"
                    if inp_type in ["submit", "button", "image", "radio", "checkbox", "file", "hidden"]:
                        continue
                        
                    inp_id = await inp.get_attribute("id") or ""
                    inp_name = await inp.get_attribute("name") or ""
                    
                    # Log element detection
                    is_visible = await inp.is_visible()
                    is_disabled = await inp.is_disabled()
                    val = await inp.input_value()
                    
                    if not is_visible or is_disabled:
                        continue
                        
                    # Try finding label in multiple ways
                    label_text = ""
                    if inp_id:
                        # 1. label with explicit 'for' attribute
                        label_loc = page.locator(f"label[for='{inp_id}']")
                        if await label_loc.is_visible():
                            label_text = await label_loc.inner_text()
                        
                        # 2. enclosing label element (ancestor)
                        if not label_text:
                            ancestor_label = page.locator(f"label:has(#{inp_id})")
                            if await ancestor_label.count() > 0 and await ancestor_label.first.is_visible():
                                label_text = await ancestor_label.first.inner_text()
                                
                    if not label_text and inp_id:
                        # 3. label/span in parent container
                        parent = page.locator(f"div:has(> #{inp_id}), .jobs-easy-apply-form-element:has(#{inp_id})").first
                        if await parent.count() > 0:
                            lbl_loc = parent.locator(".artdeco-text-input__label, .jobs-easy-apply-form-element__label, label").first
                            if await lbl_loc.is_visible():
                                label_text = await lbl_loc.inner_text()
                                
                    if not label_text:
                        # 4. Fallback placeholder or name
                        label_text = await inp.get_attribute("placeholder") or inp_name or "Question"
                        
                    # Scrape formatting instructions/subtext
                    instructions_text = ""
                    described_by = await inp.get_attribute("aria-describedby")
                    if described_by:
                        for desc_id in described_by.split():
                            desc_loc = page.locator(f"#{desc_id}")
                            if await desc_loc.is_visible():
                                instructions_text += " " + await desc_loc.inner_text()
                    if not instructions_text and inp_id:
                        parent = page.locator(f"div:has(> #{inp_id}), .jobs-easy-apply-form-element:has(#{inp_id})").first
                        if await parent.count() > 0:
                            inst_loc = parent.locator(".artdeco-text-input__instructions, .jobs-easy-apply-form-element__instructions, [class*='instructions']").first
                            if await inst_loc.is_visible():
                                instructions_text = await inst_loc.inner_text()
                                
                    label_text = label_text.strip().replace("*", "").strip()
                    if instructions_text:
                        label_text += f" (Instructions: {instructions_text.strip()})"
                    
                    # Fill if blank
                    if not val:
                        # Determine data type hint
                        data_hint = "text"
                        if inp_type == "number" or "year" in label_text.lower() or "count" in label_text.lower():
                            data_hint = "number"
                        
                        # Check Q&A Database or Standard Q&As
                        answer = self.resolve_qa_value(label_text, data_hint)
                        await record_log(f"✍️ Filling field '{label_text}' -> '{answer}'")
                        await inp.fill(answer)
                        await self.sleep_human_like(0.5, 1.2)
                    else:
                        await record_log(f"ℹ️ Field '{label_text}' already pre-filled with '{val}'. Skipping.")
                except Exception as inp_err:
                    await record_log(f"⚠️ Error filling text field: {inp_err}")

            # C. Select Dropdowns (Resilient label matching)
            selects = await page.locator("select").all()
            for sel in selects:
                try:
                    if not await sel.is_visible() or await sel.is_disabled():
                        continue
                        
                    sel_id = await sel.get_attribute("id")
                    label_text = ""
                    if sel_id:
                        # 1. label with explicit 'for'
                        label_loc = page.locator(f"label[for='{sel_id}']")
                        if await label_loc.is_visible():
                            label_text = await label_loc.inner_text()
                        
                        # 2. enclosing label element (ancestor)
                        if not label_text:
                            ancestor_label = page.locator(f"label:has(#{sel_id})")
                            if await ancestor_label.count() > 0 and await ancestor_label.first.is_visible():
                                label_text = await ancestor_label.first.inner_text()
                                
                        # 3. label in parent container
                        if not label_text:
                            parent = page.locator(f"div:has(> #{sel_id}), .jobs-easy-apply-form-element:has(#{sel_id})").first
                            if await parent.count() > 0:
                                lbl_loc = parent.locator(".artdeco-text-input__label, .jobs-easy-apply-form-element__label, label").first
                                if await lbl_loc.is_visible():
                                    label_text = await lbl_loc.inner_text()
                                    
                    if not label_text:
                        label_text = await sel.get_attribute("name") or "Question"
                            
                    # Scrape formatting instructions/subtext
                    instructions_text = ""
                    described_by = await sel.get_attribute("aria-describedby")
                    if described_by:
                        for desc_id in described_by.split():
                            desc_loc = page.locator(f"#{desc_id}")
                            if await desc_loc.is_visible():
                                instructions_text += " " + await desc_loc.inner_text()
                    if not instructions_text and sel_id:
                        parent = page.locator(f"div:has(> #{sel_id}), .jobs-easy-apply-form-element:has(#{sel_id})").first
                        if await parent.count() > 0:
                            inst_loc = parent.locator("[class*='instructions']").first
                            if await inst_loc.is_visible():
                                instructions_text = await inst_loc.inner_text()
                                
                    label_text = label_text.strip().replace("*", "").strip()
                    if instructions_text:
                        label_text += f" (Instructions: {instructions_text.strip()})"
                    
                    # Resolve answer
                    answer = self.resolve_qa_value(label_text, "select")
                    
                    # Find matching option in select
                    options = await sel.locator("option").all()
                    best_option_val = None
                    for opt in options:
                        opt_text = await opt.inner_text()
                        opt_val = await opt.get_attribute("value")
                        if answer.lower() in opt_text.lower() or opt_text.lower() in answer.lower():
                            best_option_val = opt_val
                            break
                            
                    if best_option_val:
                        await record_log(f"🔽 Selecting option '{label_text}' -> '{answer}'")
                        await sel.select_option(value=best_option_val)
                    else:
                        # Select the first non-placeholder option
                        if len(options) > 1:
                            opt_val = await options[1].get_attribute("value")
                            await record_log(f"🔽 Selecting first option for '{label_text}' -> {await options[1].inner_text()}")
                            await sel.select_option(value=opt_val)
                    await self.sleep_human_like(0.5, 1.2)
                except Exception as sel_err:
                    await record_log(f"⚠️ Dropdown error: {sel_err}")

            # D. Radio Buttons (Yes/No fieldsets - Resilient Legend extraction)
            fieldsets = await page.locator("fieldset").all()
            for fs in fieldsets:
                try:
                    if not await fs.is_visible():
                        continue
                        
                    # Retrieve the question text from legend (resiliently, even if hidden using text_content)
                    legend_loc = fs.locator("legend")
                    legend_text = ""
                    if await legend_loc.count() > 0:
                        legend_text = (await legend_loc.text_content()).strip()
                    else:
                        # Sibling/Parent/Child labels or spans inside the fieldset
                        label_loc = fs.locator("span, label").first
                        if await label_loc.count() > 0:
                            legend_text = (await label_loc.text_content()).strip()
                            
                    if not legend_text:
                        legend_text = "Question"
                        
                    # Scrape formatting instructions/subtext
                    inst_loc = fs.locator("[class*='instructions']").first
                    instructions_text = ""
                    if await inst_loc.is_visible():
                        instructions_text = await inst_loc.inner_text()
                        
                    legend_text = legend_text.strip().replace("*", "").strip()
                    if instructions_text:
                        legend_text += f" (Instructions: {instructions_text.strip()})"
                    
                    # Check if already answered in this fieldset
                    checked = await fs.locator("input[type='radio']:checked").count()
                    if checked > 0:
                        await record_log(f"ℹ️ Radio '{legend_text}' already answered. Skipping.")
                        continue
                        
                    answer = self.resolve_qa_value(legend_text, "boolean")
                    
                    # Locate Yes/No radio options
                    radios = await fs.locator("input[type='radio']").all()
                    radio_clicked = False
                    for r in radios:
                        r_id = await r.get_attribute("id")
                        r_label = await fs.locator(f"label[for='{r_id}']").inner_text()
                        if answer.lower() == r_label.strip().lower() or (answer.lower() == "yes" and "yes" in r_label.lower()) or (answer.lower() == "no" and "no" in r_label.lower()):
                            await record_log(f"🔘 Selecting radio '{legend_text}' -> '{r_label.strip()}'")
                            # We click the label as clicking raw radio inputs sometimes fails
                            await fs.locator(f"label[for='{r_id}']").click()
                            radio_clicked = True
                            break
                            
                    if not radio_clicked and len(radios) > 0:
                        # Click the first option by default
                        r_id = await radios[0].get_attribute("id")
                        await record_log(f"🔘 Fallback selecting radio '{legend_text}' -> '{await fs.locator(f'label[for=\"{r_id}\"]').inner_text()}'")
                        await fs.locator(f"label[for='{r_id}']").click()
                    await self.sleep_human_like(0.5, 1.2)
                except Exception as fs_err:
                    await record_log(f"⚠️ Radio fieldset error: {fs_err}")

            # 2. Look for navigation buttons (Next, Review, Submit) scoped within the Easy Apply container
            submit_btn = page.locator(".jobs-easy-apply-content button:has-text('Submit application'), .artdeco-modal button:has-text('Submit application'), button:has-text('Submit application')").first
            review_btn = page.locator(".jobs-easy-apply-content button:has-text('Review'), .artdeco-modal button:has-text('Review'), button:has-text('Review')").first
            next_btn = page.locator(".jobs-easy-apply-content button:has-text('Next'), .artdeco-modal button:has-text('Next'), button:has-text('Next')").first
            
            if await submit_btn.is_visible():
                if self.auto_submit:
                    await record_log("🚀 Clicking 'Submit application'...")
                    await submit_btn.click()
                    await self.sleep_human_like(4, 6)
                    
                    # Capture confirmation
                    screenshot_name = f"app_{app_id}_success.png"
                    await page.screenshot(path=os.path.join(SCREENSHOTS_DIR, screenshot_name))
                    database.update_application(app_id, "Applied", "\n".join(logs), f"/static/screenshots/{screenshot_name}")
                    
                    # Click close if there is a close button
                    close_btn = page.locator("button[aria-label='Dismiss']").first
                    if await close_btn.is_visible():
                        await close_btn.click()
                    return True
                else:
                    await record_log("⏸️ 'Auto-Submit' is disabled. Leaving form filled as Draft.")
                    # Save as draft
                    database.update_application(app_id, "Draft", "\n".join(logs), f"/static/screenshots/{screenshot_name}")
                    # Click cancel/dismiss to close
                    close_btn = page.locator("button[aria-label='Dismiss']").first
                    if await close_btn.is_visible():
                        await close_btn.click()
                        await self.sleep_human_like(1, 2)
                        confirm_unapplied = page.locator("button:has-text('Discard')").first
                        if await confirm_unapplied.is_visible():
                            await confirm_unapplied.click()
                    return True
                    
            elif await review_btn.is_visible():
                await record_log("Clicking 'Review' button...")
                await review_btn.click()
                await self.sleep_human_like(2, 3)
                
            elif await next_btn.is_visible():
                await record_log("Clicking 'Next' button...")
                await next_btn.click()
                await self.sleep_human_like(2, 3)
                
            else:
                # If we are stuck and none of these buttons are visible, try generic continue selectors
                generic_next = page.locator("button[data-easy-apply-next-button]").first
                if await generic_next.is_visible():
                    await record_log("Clicking generic next button...")
                    await generic_next.click()
                    await self.sleep_human_like(2, 3)
                else:
                    await record_log("❌ Stuck: Could not locate Next, Review, Submit, or close buttons on page.")
                    # Close the application modal
                    close_btn = page.locator("button[aria-label='Dismiss']").first
                    if await close_btn.is_visible():
                        await close_btn.click()
                        await self.sleep_human_like(1, 2)
                        discard_btn = page.locator("button:has-text('Discard')").first
                        if await discard_btn.is_visible():
                            await discard_btn.click()
                            
                    database.update_application(app_id, "Failed", "\n".join(logs), f"/static/screenshots/{screenshot_name}")
                    return False
                    
        database.update_application(app_id, "Failed", "\n".join(logs) + "\nForm step limit reached.", f"/static/screenshots/{screenshot_name}")
        return False

    def resolve_qa_value(self, question: str, field_type: str) -> str:
        """
        Attempts to resolve an screening question using Pydantic validated structured outputs.
        """
        clean_q = question.strip()
        
        # 1. Check custom_qa.json first (user-defined modifications take absolute priority!)
        custom_ans = self.get_custom_qa_answer(clean_q)
        if custom_ans is not None:
            return custom_ans
            
        # 2. Get answer from the resolver
        ans = self._resolve_qa_value_internal(clean_q, field_type)
        
        # 3. Register in custom_qa.json for future user modifications
        self.register_custom_qa(clean_q, ans)
        return ans

    def _resolve_qa_value_internal(self, clean_q: str, field_type: str) -> str:
        # 1. Check SQLite Cache
        cached = database.get_cached_answer(clean_q)
        if cached:
            # Self-healing: if cached answer is Yes/No, but field is a text/select/number input, ignore the stale cache!
            if cached in ["Yes", "No"] and field_type not in ["boolean", "radio", "fieldset"]:
                pass
            else:
                return cached
            
        # 2. Prioritize dynamic LLM answering for all questions if the Gemini key is available!
        if self.api_key:
            ans = llm.answer_question(
                clean_q, 
                field_type, 
                self.profile, 
                self.api_key, 
                getattr(self, "current_company", None)
            )
            if ans:
                database.save_qa_pair(clean_q, ans)
                return ans
                
        # 3. Check Profile standard fields (Fallback local heuristics if LLM is offline or no key loaded)
        profile_qa = self.profile.get("standard_qa", {})
        q_lower = clean_q.lower()
        
        if "salary" in q_lower or "compensation" in q_lower or "ctc" in q_lower:
            # If the question asks about CURRENT salary/CTC, return "0" since the candidate is a student
            if "current" in q_lower or "currently" in q_lower:
                database.save_qa_pair(clean_q, "0")
                return "0"
            ans = profile_qa.get("desired_salary", "9,00,000")
            if "lakh" in q_lower:
                cleaned_nums = "".join([c for c in ans if c.isdigit()])
                if cleaned_nums:
                    try:
                        val = int(cleaned_nums)
                        if val >= 100000:
                            ans = str(val // 100000)
                        elif val < 100:
                            ans = str(val)
                    except ValueError:
                        ans = "9"
                else:
                    ans = "9"
            database.save_qa_pair(clean_q, ans)
            return ans
        elif "sponsorship" in q_lower or "sponsoring" in q_lower or "require visa" in q_lower:
            ans = self.profile.get("personal", {}).get("requires_sponsorship", "No")
            database.save_qa_pair(clean_q, ans)
            return ans
        elif "work authorization" in q_lower or "authorized" in q_lower:
            ans = "Yes" if "authorize" in self.profile.get("personal", {}).get("work_authorization", "").lower() or "us citizen" in self.profile.get("personal", {}).get("citizenship", "").lower() else "No"
            database.save_qa_pair(clean_q, ans)
            return ans
        elif "notice period" in q_lower or "start date" in q_lower or "how soon" in q_lower:
            ans = profile_qa.get("notice_period", "Immediate")
            if "day" in q_lower or "how many days" in q_lower:
                ans = "0" if ans.lower() in ["none", "immediate", "0"] else "30"
            database.save_qa_pair(clean_q, ans)
            return ans
        elif "gender" in q_lower or "sex" in q_lower:
            ans = profile_qa.get("gender", "Decline to Self-Identify")
            database.save_qa_pair(clean_q, ans)
            return ans
        elif "race" in q_lower or "ethnicity" in q_lower:
            ans = profile_qa.get("race", "Decline to Self-Identify")
            database.save_qa_pair(clean_q, ans)
            return ans
        elif "disability" in q_lower:
            ans = profile_qa.get("disability", "No, I do not have a disability")
            database.save_qa_pair(clean_q, ans)
            return ans
        elif "veteran" in q_lower:
            ans = profile_qa.get("veteran", "No, I am not a veteran")
            database.save_qa_pair(clean_q, ans)
            return ans
        elif "relocate" in q_lower:
            ans = profile_qa.get("will_relocate", "Yes")
            database.save_qa_pair(clean_q, ans)
            return ans
        elif "email" in q_lower:
            ans = self.profile.get("personal", {}).get("email", "")
            database.save_qa_pair(clean_q, ans)
            return ans
        elif "phone" in q_lower or "mobile" in q_lower or "contact" in q_lower:
            ans = self.profile.get("personal", {}).get("phone", "")
            database.save_qa_pair(clean_q, ans)
            return ans
        elif "linkedin" in q_lower:
            ans = self.profile.get("personal", {}).get("linkedin", "")
            database.save_qa_pair(clean_q, ans)
            return ans
        elif "github" in q_lower:
            ans = self.profile.get("personal", {}).get("github", "")
            database.save_qa_pair(clean_q, ans)
            return ans
        elif "city" in q_lower or "location" in q_lower or "address" in q_lower:
            ans = self.profile.get("personal", {}).get("location", "India")
            database.save_qa_pair(clean_q, ans)
            return ans
        elif "title" in q_lower or "position" in q_lower:
            ans = "AI & Deep Learning Developer"
            exp = self.profile.get("experience", [])
            if exp:
                ans = exp[0].get("title", ans)
            database.save_qa_pair(clean_q, ans)
            return ans
        elif "company" in q_lower or "employer" in q_lower:
            ans = "Academic Projects"
            exp = self.profile.get("experience", [])
            if exp:
                ans = exp[0].get("company", ans)
            database.save_qa_pair(clean_q, ans)
            return ans
        elif "description" in q_lower:
            ans = "Developed AI and Deep Learning frameworks."
            exp = self.profile.get("experience", [])
            if exp:
                ans = exp[0].get("description", ans)
            database.save_qa_pair(clean_q, ans)
            return ans
        elif "years of" in q_lower or "experience with" in q_lower or "how many years" in q_lower:
            # Enforce LangChain structured outputs
            if self.api_key:
                ans = llm.answer_question(clean_q, field_type, self.profile, self.api_key, getattr(self, "current_company", None))
                if ans:
                    database.save_qa_pair(clean_q, ans)
                    return ans
            ans = profile_qa.get("years_experience", "2")
            database.save_qa_pair(clean_q, ans)
            return ans
            
        # 3. Call structured guardrailed resolver
        if self.api_key:
            ans = llm.answer_question(clean_q, field_type, self.profile, self.api_key, getattr(self, "current_company", None))
            if ans:
                database.save_qa_pair(clean_q, ans)
                return ans
                
        # 4. Fallbacks
        if field_type == "boolean":
            return "Yes"
        elif field_type == "number":
            return "2"
        return "Yes"
