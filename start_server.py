import os
import sys
import asyncio
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Force the Proactor Event Loop policy at the process level before Uvicorn is imported/run
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import uvicorn


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "0.0.0.0")
    print("===================================================")
    print(f"       Starting JobFlow AI on {host}:{port}        ")
    print("===================================================")
    uvicorn.run("main:app", host=host, port=port, reload=False)

