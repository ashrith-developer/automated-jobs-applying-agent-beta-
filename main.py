import asyncio
import sys
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Force Proactor Event Loop on Windows to support Playwright browser automation
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import os
import json
import shutil
import base64
import secrets
from typing import List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, BackgroundTasks, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from pydantic import BaseModel
import database
from agent import JobFlowAgent

app = FastAPI(title="JobFlow AI API")

# Optional HTTP Basic Auth for cloud deployments (e.g. Render)
class OptionalBasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        expected_user = os.environ.get("DASHBOARD_USERNAME")
        expected_pass = os.environ.get("DASHBOARD_PASSWORD")
        
        # If no credentials configured in environment, allow open access
        if not expected_user or not expected_pass:
            return await call_next(request)
            
        auth = request.headers.get("Authorization")
        if auth and auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                user, pwd = decoded.split(":", 1)
                if secrets.compare_digest(user, expected_user) and secrets.compare_digest(pwd, expected_pass):
                    return await call_next(request)
            except Exception:
                pass
                
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="JobFlow AI Dashboard"'},
            content="Authentication Required\n"
        )

app.add_middleware(OptionalBasicAuthMiddleware)

# File locations
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_PATH = os.path.join(BASE_DIR, "profile.json")
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)

# Active WebSockets clients
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                # Connection might have died
                pass

manager = ConnectionManager()

# Global Agent Runner State
active_agent = None
active_task = None

def load_profile_data() -> dict:
    if not os.path.exists(PROFILE_PATH):
        raise HTTPException(status_code=404, detail="Profile configuration not found.")
    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    if "settings" not in data:
        data["settings"] = {}
    if not data["settings"].get("gemini_api_key"):
        data["settings"]["gemini_api_key"] = os.environ.get("GEMINI_API_KEY", "")
    return data

def save_profile_data(data: dict):
    with open(PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# API Endpoints
@app.get("/api/profile")
def get_profile():
    return load_profile_data()

@app.post("/api/profile")
def update_profile(profile_data: dict):
    try:
        # Validate that the settings structure is kept
        save_profile_data(profile_data)
        return {"status": "success", "message": "Profile saved successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/history")
def get_history():
    return database.get_all_applications()

@app.get("/api/stats")
def get_stats():
    return database.get_stats()

@app.post("/api/resume")
async def upload_resume(file: UploadFile = File(...)):
    try:
        # Save as "resume.<ext>" inside project folder
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in [".pdf", ".docx", ".txt"]:
            raise HTTPException(status_code=400, detail="Invalid file type. Only PDF, DOCX, and TXT are supported.")
        
        # Clean any old resumes in workspace to avoid confusion
        for filename in os.listdir(BASE_DIR):
            if filename.lower().startswith("resume") and (filename.endswith(".pdf") or filename.endswith(".docx") or filename.endswith(".txt")):
                try:
                    os.remove(os.path.join(BASE_DIR, filename))
                except Exception:
                    pass
                    
        target_path = os.path.join(BASE_DIR, f"resume{ext}")
        with open(target_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
        # Try to save the uploaded filename to profile settings for visual reference
        try:
            profile = load_profile_data()
            if "settings" not in profile:
                profile["settings"] = {}
            profile["settings"]["resume_name"] = file.filename
            save_profile_data(profile)
        except Exception:
            pass
            
        return {"status": "success", "filename": file.filename}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Agent Execution Operations
async def agent_runner_task():
    global active_agent, active_task
    try:
        profile = load_profile_data()
        
        # Setup log/screenshot callbacks to broadcast via WebSockets
        async def on_log(message: str):
            await manager.broadcast({"type": "log", "data": message})
            
        async def on_screenshot(path: str):
            await manager.broadcast({"type": "screenshot", "data": path})
            
        active_agent = JobFlowAgent(
            profile_data=profile,
            log_callback=on_log,
            screenshot_callback=on_screenshot
        )
        
        await manager.broadcast({"type": "status", "data": "running"})
        await active_agent.start()
        
    except asyncio.CancelledError:
        if active_agent:
            active_agent.is_running = False
        await manager.broadcast({"type": "log", "data": "⚠️ Automation stopped by user."})
    except Exception as e:
        import traceback
        traceback.print_exc()
        err_msg = str(e) if str(e).strip() else repr(e)
        await manager.broadcast({"type": "log", "data": f"🚨 Runner Error: {err_msg}"})
    finally:
        active_task = None
        active_agent = None
        await manager.broadcast({"type": "status", "data": "idle"})

@app.post("/api/agent/start")
async def start_agent():
    global active_task
    if active_task is not None:
        return {"status": "error", "message": "Agent is already running."}
        
    active_task = asyncio.create_task(agent_runner_task())
    return {"status": "success", "message": "Agent started successfully."}

@app.post("/api/agent/stop")
async def stop_agent():
    global active_task, active_agent
    if active_task is None:
        return {"status": "error", "message": "Agent is not running."}
        
    # Cancel the asyncio task
    active_task.cancel()
    if active_agent:
        active_agent.is_running = False
        
    # Wait for task shutdown
    await asyncio.sleep(0.5)
    return {"status": "success", "message": "Agent stop signal sent."}

@app.get("/api/agent/status")
def get_agent_status():
    is_running = active_task is not None
    return {"status": "success", "running": is_running}

# WebSocket Endpoint
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        # Send current status on connect
        is_running = active_task is not None
        await websocket.send_json({"type": "status", "data": "running" if is_running else "idle"})
        await websocket.send_json({"type": "log", "data": "[SYSTEM] Connected to JobFlow AI Real-time Logs."})
        
        while True:
            # Just keep connection open, we primarily broadcast down
            data = await websocket.receive_text()
            # Echo or process client requests if any
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# Serve Frontend static assets
# Note: we serve static directory at "/"
@app.get("/")
def read_root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

# Mount the static directory for CSS/JS/Screenshots
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
