import asyncio
from app.workers.scanner import run_scanner

if __name__ == "__main__":
    asyncio.run(run_scanner())
