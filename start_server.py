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
    print("===================================================")
    print("       Starting JobFlow AI with Proactor Loop      ")
    print("===================================================")
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
