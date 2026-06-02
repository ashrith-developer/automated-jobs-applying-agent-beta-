// JobFlow AI Dashboard Logic
document.addEventListener("DOMContentLoaded", () => {
    let ws = null;
    let currentProfile = {};
    let isAgentRunning = false;

    // --- DOM Elements ---
    const navItems = document.querySelectorAll(".nav-item");
    const tabPanes = document.querySelectorAll(".tab-pane");
    const sideStatusIndicator = document.getElementById("side-status-indicator");
    const btnSideToggle = document.getElementById("btn-side-toggle");
    const livePulse = document.getElementById("live-pulse");
    const userDisplayName = document.getElementById("user-display-name");
    
    // Stats
    const statApplied = document.getElementById("stat-applied");
    const statRate = document.getElementById("stat-rate");
    const statFailed = document.getElementById("stat-failed");
    const statDrafts = document.getElementById("stat-drafts");
    
    // Console logs
    const consoleLogs = document.getElementById("console-logs");
    const clearConsoleBtn = document.getElementById("clear-console");
    
    // Viewport
    const viewportPlaceholder = document.getElementById("viewport-placeholder");
    const viewportImage = document.getElementById("viewport-image");
    const viewportStatus = document.getElementById("viewport-status");
    
    // Forms & Settings
    const profileForm = document.getElementById("profile-form");
    const profileStatusMsg = document.getElementById("profile-status-msg");
    const apiSettingsSaveBtn = document.getElementById("btn-save-ai-settings");
    const apiKeyInput = document.getElementById("gemini-api-key-input");
    const toggleKeyVisible = document.getElementById("toggle-key-visible");
    const toggleAutoSubmit = document.getElementById("toggle-auto-submit");
    const toggleHeadless = document.getElementById("toggle-headless");
    
    // Resume Dropzone
    const dropzone = document.getElementById("resume-dropzone");
    const fileInput = document.getElementById("resume-file-input");
    const triggerUploadBtn = document.getElementById("btn-trigger-upload");
    const activeFileDisplay = document.getElementById("active-file-display");
    const uploadedFileName = document.getElementById("uploaded-file-name");
    
    // Search Settings
    const newKeywordInput = document.getElementById("new-keyword-input");
    const btnAddKeyword = document.getElementById("btn-add-keyword");
    const keywordList = document.getElementById("keyword-list");
    const newLocationInput = document.getElementById("new-location-input");
    const btnAddLocation = document.getElementById("btn-add-location");
    const locationList = document.getElementById("location-list");
    const maxApplicationsRange = document.getElementById("max-applications-range");
    const maxAppsVal = document.getElementById("max-apps-val");
    const btnSaveSearchConfig = document.getElementById("btn-save-search-config");
    const searchConfigStatusMsg = document.getElementById("search-config-status-msg");
    
    // History
    const historyTableBody = document.getElementById("history-table-body");
    const refreshHistoryBtn = document.getElementById("btn-refresh-history");
    
    // Modal
    const logModal = document.getElementById("log-modal");
    const btnCloseModal = document.getElementById("btn-close-modal");
    const closeModalOverlay = document.getElementById("close-modal-overlay");
    const modalLogContent = document.getElementById("modal-log-content");
    const modalTitle = document.getElementById("modal-title");

    // ================= 1. NAVIGATION TAB ROUTING =================
    navItems.forEach(item => {
        item.addEventListener("click", (e) => {
            e.preventDefault();
            const targetTab = item.getAttribute("data-tab");
            
            navItems.forEach(nav => nav.classList.remove("active"));
            tabPanes.forEach(pane => pane.classList.remove("active"));
            
            item.classList.add("active");
            document.getElementById(`${targetTab}-tab`).classList.add("active");
            
            // Trigger specific loads if needed
            if (targetTab === "history") {
                loadHistoryTable();
            }
        });
    });

    // ================= 2. WEBSOCKET LOGGING ENGINE =================
    function initWebSocket() {
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const wsUrl = `${protocol}//${window.location.host}/ws`;
        
        ws = new WebSocket(wsUrl);
        
        ws.onopen = () => {
            appendLogLine("[SYSTEM] WebSocket connected to JobFlow control bus.");
        };
        
        ws.onmessage = (event) => {
            const msg = JSON.parse(event.data);
            
            if (msg.type === "log") {
                appendLogLine(msg.data);
            } else if (msg.type === "screenshot") {
                updateViewport(msg.data);
            } else if (msg.type === "status") {
                updateAgentStatusUI(msg.data);
            }
        };
        
        ws.onclose = () => {
            appendLogLine("[SYSTEM] WebSocket disconnected. Retrying connection in 5s...");
            setTimeout(initWebSocket, 5000);
        };
    }

    function appendLogLine(text) {
        const line = document.createElement("div");
        line.className = "log-line";
        
        if (text.includes("[SYSTEM]")) {
            line.classList.add("system");
        } else if (text.includes("✅") || text.includes("Successfully")) {
            line.classList.add("success");
        } else if (text.includes("❌") || text.includes("🚨") || text.includes("Fatal")) {
            line.classList.add("error");
        } else if (text.includes("⚠️") || text.includes("validation error") || text.includes("Stuck")) {
            line.classList.add("warning");
        }
        
        line.innerText = text;
        consoleLogs.appendChild(line);
        consoleLogs.scrollTop = consoleLogs.scrollHeight;
    }

    clearConsoleBtn.addEventListener("click", () => {
        consoleLogs.innerHTML = "";
        appendLogLine("[SYSTEM] Console log history cleared.");
    });

    function updateViewport(imagePath) {
        if (!imagePath) return;
        viewportPlaceholder.classList.add("hidden");
        viewportImage.src = `${imagePath}?t=${new Date().getTime()}`; // Avoid caching
        viewportImage.classList.remove("hidden");
        viewportStatus.textContent = "Live Stream";
        viewportStatus.className = "badge badge-ai";
    }

    // ================= 3. PROFILE DATA BINDING =================
    async function loadProfile() {
        try {
            const res = await fetch("/api/profile");
            currentProfile = await res.json();
            
            // Populate display name
            const first = currentProfile.personal?.first_name || "";
            const last = currentProfile.personal?.last_name || "";
            userDisplayName.textContent = `${first} ${last}`.trim() || "Candidate";
            
            // Populate form fields
            populateForm(profileForm, currentProfile);
            
            // Populate settings tab
            apiKeyInput.value = currentProfile.settings?.gemini_api_key || "";
            toggleAutoSubmit.checked = currentProfile.settings?.auto_submit ?? true;
            toggleHeadless.checked = !(currentProfile.settings?.headless ?? false);
            
            // Render active resume if present
            if (currentProfile.settings?.resume_name) {
                renderResumeCard(currentProfile.settings.resume_name);
            }
            
            // Render search settings
            renderSearchSettings();
            
        } catch (err) {
            console.error("Failed to load candidate profile details", err);
        }
    }

    function populateForm(form, data, parentKey = "") {
        for (const key in data) {
            if (data.hasOwnProperty(key)) {
                const fullKey = parentKey ? `${parentKey}.${key}` : key;
                
                if (typeof data[key] === "object" && data[key] !== null && !Array.isArray(data[key])) {
                    populateForm(form, data[key], fullKey);
                } else {
                    const input = form.querySelector(`[name="${fullKey}"]`);
                    if (input) {
                        if (input.tagName === "SELECT") {
                            input.value = data[key];
                        } else {
                            input.value = data[key] || "";
                        }
                    }
                }
            }
        }
    }

    profileForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        
        // Serialize inputs
        const updated = JSON.parse(JSON.stringify(currentProfile)); // deep clone
        const inputs = profileForm.querySelectorAll("input, select");
        
        inputs.forEach(inp => {
            const name = inp.name;
            const val = inp.value;
            if (!name) return;
            
            const parts = name.split(".");
            let current = updated;
            
            for (let i = 0; i < parts.length; i++) {
                const part = parts[i];
                if (i === parts.length - 1) {
                    current[part] = val;
                } else {
                    if (!current[part]) current[part] = {};
                    current = current[part];
                }
            }
        });
        
        try {
            const res = await fetch("/api/profile", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(updated)
            });
            const data = await res.json();
            
            if (data.status === "success") {
                currentProfile = updated;
                const first = currentProfile.personal?.first_name || "";
                const last = currentProfile.personal?.last_name || "";
                userDisplayName.textContent = `${first} ${last}`.trim() || "Candidate";
                
                showStatusMsg(profileStatusMsg, "✅ Candidate profile saved successfully!", "success");
            } else {
                showStatusMsg(profileStatusMsg, `❌ Error: ${data.detail}`, "error");
            }
        } catch (err) {
            showStatusMsg(profileStatusMsg, "❌ Failed to save candidate details.", "error");
        }
    });

    function showStatusMsg(element, text, type) {
        element.textContent = text;
        element.className = `status-msg ${type}`;
        setTimeout(() => {
            element.textContent = "";
        }, 5000);
    }

    // ================= 4. RESUME UPLOAD HANDLERS =================
    triggerUploadBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        fileInput.click();
    });
    
    dropzone.addEventListener("click", (e) => {
        if (e.target === fileInput) return;
        fileInput.click();
    });
    
    fileInput.addEventListener("click", (e) => {
        e.stopPropagation();
    });
    
    // Prevent defaults on drag events
    ["dragenter", "dragover", "dragleave", "drop"].forEach(eventName => {
        dropzone.addEventListener(eventName, preventDefaults, false);
    });
    
    function preventDefaults(e) {
        e.preventDefault();
        e.stopPropagation();
    }
    
    dropzone.addEventListener("drop", (e) => {
        const dt = e.dataTransfer;
        const files = dt.files;
        if (files.length) uploadResumeFile(files[0]);
    });
    
    fileInput.addEventListener("change", (e) => {
        if (e.target.files.length) uploadResumeFile(e.target.files[0]);
    });

    async function uploadResumeFile(file) {
        const formData = new FormData();
        formData.append("file", file);
        
        dropzone.classList.add("hidden");
        activeFileDisplay.classList.remove("hidden");
        uploadedFileName.textContent = "Uploading file, please wait...";
        
        try {
            const res = await fetch("/api/resume", {
                method: "POST",
                body: formData
            });
            const data = await res.json();
            
            if (data.status === "success") {
                currentProfile.settings = currentProfile.settings || {};
                currentProfile.settings.resume_name = data.filename;
                renderResumeCard(data.filename);
                appendLogLine(`[SYSTEM] Resume successfully uploaded and cached: ${data.filename}`);
            } else {
                dropzone.classList.remove("hidden");
                activeFileDisplay.classList.add("hidden");
                alert(`Upload failed: ${data.detail}`);
            }
        } catch (err) {
            dropzone.classList.remove("hidden");
            activeFileDisplay.classList.add("hidden");
            alert("Error uploading resume file.");
        }
    }

    function renderResumeCard(filename) {
        dropzone.classList.add("hidden");
        activeFileDisplay.classList.remove("hidden");
        uploadedFileName.textContent = filename;
        
        const ext = filename.split(".").pop().toLowerCase();
        const icon = activeFileDisplay.querySelector(".file-icon i");
        if (ext === "pdf") {
            icon.className = "fa-solid fa-file-pdf";
        } else if (ext === "docx") {
            icon.className = "fa-solid fa-file-word";
        } else {
            icon.className = "fa-solid fa-file-lines";
        }
    }

    // ================= 5. GEMINI & AUTOMATION TOGGLES =================
    apiSettingsSaveBtn.addEventListener("click", async () => {
        currentProfile.settings = currentProfile.settings || {};
        currentProfile.settings.gemini_api_key = apiKeyInput.value.trim();
        currentProfile.settings.auto_submit = toggleAutoSubmit.checked;
        currentProfile.settings.headless = !toggleHeadless.checked;
        
        try {
            const res = await fetch("/api/profile", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(currentProfile)
            });
            const data = await res.json();
            if (data.status === "success") {
                appendLogLine("[SYSTEM] Gemini AI and automation settings saved.");
                alert("AI parameters configured!");
            }
        } catch (err) {
            alert("Error saving API keys.");
        }
    });

    toggleKeyVisible.addEventListener("click", () => {
        const type = apiKeyInput.getAttribute("type") === "password" ? "text" : "password";
        apiKeyInput.setAttribute("type", type);
        toggleKeyVisible.querySelector("i").className = type === "password" ? "fa-solid fa-eye" : "fa-solid fa-eye-slash";
    });

    // ================= 6. SEARCH PARAMETERS CHIPS MANAGER =================
    maxApplicationsRange.addEventListener("input", (e) => {
        maxAppsVal.textContent = e.target.value;
    });

    function renderSearchSettings() {
        const keywords = currentProfile.settings?.search_keywords || [];
        const locations = currentProfile.settings?.search_locations || [];
        const cap = currentProfile.settings?.max_applications || 20;
        
        maxApplicationsRange.value = cap;
        maxAppsVal.textContent = cap;
        
        // Keywords
        keywordList.innerHTML = "";
        keywords.forEach((kw, idx) => {
            const chip = document.createElement("li");
            chip.className = "tag-item";
            chip.innerHTML = `${kw} <i class="fa-solid fa-circle-xmark" data-type="kw" data-idx="${idx}"></i>`;
            keywordList.appendChild(chip);
        });
        
        // Locations
        locationList.innerHTML = "";
        locations.forEach((loc, idx) => {
            const chip = document.createElement("li");
            chip.className = "tag-item";
            chip.innerHTML = `${loc} <i class="fa-solid fa-circle-xmark" data-type="loc" data-idx="${idx}"></i>`;
            locationList.appendChild(chip);
        });
        
        // Bind delete listeners on chips
        document.querySelectorAll(".tag-item i").forEach(btn => {
            btn.addEventListener("click", (e) => {
                const type = e.target.getAttribute("data-type");
                const idx = parseInt(e.target.getAttribute("data-idx"));
                
                if (type === "kw") {
                    currentProfile.settings.search_keywords.splice(idx, 1);
                } else {
                    currentProfile.settings.search_locations.splice(idx, 1);
                }
                renderSearchSettings();
            });
        });
    }

    btnAddKeyword.addEventListener("click", () => {
        const val = newKeywordInput.value.trim();
        if (!val) return;
        currentProfile.settings = currentProfile.settings || {};
        currentProfile.settings.search_keywords = currentProfile.settings.search_keywords || [];
        currentProfile.settings.search_keywords.push(val);
        newKeywordInput.value = "";
        renderSearchSettings();
    });
    
    btnAddLocation.addEventListener("click", () => {
        const val = newLocationInput.value.trim();
        if (!val) return;
        currentProfile.settings = currentProfile.settings || {};
        currentProfile.settings.search_locations = currentProfile.settings.search_locations || [];
        currentProfile.settings.search_locations.push(val);
        newLocationInput.value = "";
        renderSearchSettings();
    });

    btnSaveSearchConfig.addEventListener("click", async () => {
        currentProfile.settings = currentProfile.settings || {};
        currentProfile.settings.max_applications = parseInt(maxApplicationsRange.value);
        
        try {
            const res = await fetch("/api/profile", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(currentProfile)
            });
            const data = await res.json();
            if (data.status === "success") {
                showStatusMsg(searchConfigStatusMsg, "✅ Search settings successfully saved!", "success");
                appendLogLine("[SYSTEM] Search parameters updated.");
            }
        } catch (err) {
            showStatusMsg(searchConfigStatusMsg, "❌ Failed to save search details.", "error");
        }
    });

    // ================= 7. AGENT EXECUTION CONTROL FLOW =================
    async function toggleAgentState() {
        if (isAgentRunning) {
            // STOP Agent
            btnSideToggle.innerHTML = "<i class='fa-solid fa-spinner fa-spin'></i> Stopping...";
            btnSideToggle.disabled = true;
            try {
                const res = await fetch("/api/agent/stop", { method: "POST" });
                const data = await res.json();
                if (data.status === "error") {
                    alert(data.message);
                    btnSideToggle.disabled = false;
                }
            } catch (err) {
                alert("Network error stopping agent.");
                btnSideToggle.disabled = false;
            }
        } else {
            // Check resume and api key first
            if (!currentProfile.settings?.resume_name) {
                alert("Please upload your Resume first on the 'Resume & AI' tab.");
                return;
            }
            if (!currentProfile.settings?.gemini_api_key) {
                if (!confirm("No Gemini API Key entered. The agent will run with basic heuristics only. Continue?")) {
                    return;
                }
            }
            
            // START Agent
            btnSideToggle.innerHTML = "<i class='fa-solid fa-spinner fa-spin'></i> Launching...";
            btnSideToggle.disabled = true;
            try {
                const res = await fetch("/api/agent/start", { method: "POST" });
                const data = await res.json();
                if (data.status === "success") {
                    appendLogLine("[SYSTEM] Application crawler thread starting up...");
                } else {
                    alert(data.message);
                    btnSideToggle.disabled = false;
                }
            } catch (err) {
                alert("Network error launching agent.");
                btnSideToggle.disabled = false;
            }
        }
    }

    btnSideToggle.addEventListener("click", toggleAgentState);

    function updateAgentStatusUI(status) {
        if (status === "running") {
            isAgentRunning = true;
            
            // Side status panel
            sideStatusIndicator.className = "status-indicator running";
            sideStatusIndicator.querySelector(".status-text").textContent = "Agent Active";
            
            // Button toggle
            btnSideToggle.className = "btn btn-danger";
            btnSideToggle.innerHTML = "<i class='fa-solid fa-stop'></i> Stop Agent";
            btnSideToggle.disabled = false;
            
            // Top indicator
            livePulse.classList.remove("hidden");
            
            // Viewport status
            viewportStatus.textContent = "Live Screen";
            viewportStatus.className = "badge badge-ai";
        } else {
            isAgentRunning = false;
            
            // Side status panel
            sideStatusIndicator.className = "status-indicator idle";
            sideStatusIndicator.querySelector(".status-text").textContent = "Agent Idle";
            
            // Button toggle
            btnSideToggle.className = "btn btn-primary";
            btnSideToggle.innerHTML = "<i class='fa-solid fa-play'></i> Start Agent";
            btnSideToggle.disabled = false;
            
            // Top indicator
            livePulse.classList.add("hidden");
            
            // Viewport
            viewportStatus.textContent = "Inactive";
            viewportStatus.className = "badge";
        }
    }

    // ================= 8. APPLIED HISTORY RENDERER =================
    async function loadHistoryTable() {
        try {
            // Load stats first
            const statsRes = await fetch("/api/stats");
            const stats = await statsRes.json();
            
            statApplied.textContent = stats.applied;
            statRate.textContent = `${stats.success_rate}%`;
            statFailed.textContent = stats.failed;
            statDrafts.textContent = stats.draft;
            
            // Load table
            const res = await fetch("/api/history");
            const history = await res.json();
            
            historyTableBody.innerHTML = "";
            if (history.length === 0) {
                historyTableBody.innerHTML = `<tr><td colspan="7" class="loading-placeholder">No jobs applied to yet. Set your keyword search list and click Start Agent!</td></tr>`;
                return;
            }
            
            history.forEach(item => {
                const tr = document.createElement("tr");
                
                // Formatted date
                const date = item.date_applied ? item.date_applied.split(" ")[0] : "-";
                
                // Status badge
                const statusBadge = `<span class="history-badge ${item.status.toLowerCase()}">${item.status}</span>`;
                
                // Outreach text preview
                let outreachHTML = `<span class="help-text">N/A</span>`;
                if (item.cover_letter) {
                    outreachHTML = `<button class="btn btn-secondary btn-sm btn-view-cl" data-id="${item.id}"><i class="fa-solid fa-file-alt"></i> View Outreach</button>`;
                }
                
                // Actions
                const actionsHTML = `
                    <button class="btn btn-icon btn-sm btn-diag" data-id="${item.id}" title="Diagnostic logs"><i class="fa-solid fa-code-branch"></i></button>
                    ${item.screenshot_path ? `<a href="${item.screenshot_path}" target="_blank" class="btn btn-icon btn-sm" title="View screenshot"><i class="fa-solid fa-image"></i></a>` : ""}
                `;
                
                tr.innerHTML = `
                    <td>${date}</td>
                    <td><strong>${item.job_title}</strong></td>
                    <td>${item.company}</td>
                    <td><i class="fa-solid fa-location-dot text-muted"></i> ${item.location}</td>
                    <td>${statusBadge}</td>
                    <td>${outreachHTML}</td>
                    <td>${actionsHTML}</td>
                `;
                
                historyTableBody.appendChild(tr);
            });
            
            // Bind action click listeners
            document.querySelectorAll(".btn-view-cl").forEach(btn => {
                btn.addEventListener("click", (e) => {
                    const id = parseInt(e.currentTarget.getAttribute("data-id"));
                    const record = history.find(r => r.id === id);
                    if (record) openCLModal(record);
                });
            });
            
            document.querySelectorAll(".btn-diag").forEach(btn => {
                btn.addEventListener("click", (e) => {
                    const id = parseInt(e.currentTarget.getAttribute("data-id"));
                    const record = history.find(r => r.id === id);
                    if (record) openDiagModal(record);
                });
            });
            
        } catch (err) {
            historyTableBody.innerHTML = `<tr><td colspan="7" class="loading-placeholder error">Failed to query applications database history.</td></tr>`;
        }
    }

    refreshHistoryBtn.addEventListener("click", loadHistoryTable);

    // ================= 9. DIAGNOSTIC MODAL DIALOGS =================
    function openDiagModal(record) {
        modalTitle.innerHTML = `<i class="fa-solid fa-bug"></i> Terminal Diagnostic: ${record.job_title} at ${record.company}`;
        modalLogContent.innerHTML = "";
        
        const logs = record.log_output || "No detailed logs saved for this application step.";
        const lines = logs.split("\n");
        
        lines.forEach(text => {
            const line = document.createElement("div");
            line.className = "log-line";
            if (text.includes("⚠️") || text.includes("error")) line.classList.add("warning");
            if (text.includes("❌") || text.includes("Failed")) line.classList.add("error");
            if (text.includes("✅") || text.includes("Submitted")) line.classList.add("success");
            line.innerText = text;
            modalLogContent.appendChild(line);
        });
        
        logModal.classList.remove("hidden");
    }

    function openCLModal(record) {
        modalTitle.innerHTML = `<i class="fa-solid fa-file-signature"></i> Tailwind Cover Letter: ${record.job_title} at ${record.company}`;
        modalLogContent.innerHTML = `<pre style="font-family: inherit; white-space: pre-wrap; color: var(--text-primary); line-height: 1.6;">${record.cover_letter}</pre>`;
        logModal.classList.remove("hidden");
    }

    function closeModal() {
        logModal.classList.add("hidden");
    }

    btnCloseModal.addEventListener("click", closeModal);
    closeModalOverlay.addEventListener("click", closeModal);

    // ================= 10. INITIALIZATION RUNNERS =================
    initWebSocket();
    loadProfile();
    // Periodically sync stats & history when active
    setInterval(() => {
        if (isAgentRunning) {
            loadHistoryTable();
        }
    }, 4000);
});
