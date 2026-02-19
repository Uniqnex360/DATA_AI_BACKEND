#!/usr/bin/env python3
import asyncio
import sys
import os

# Add the project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.core.auto_migrate import auto_migration

async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "safe"
    print(f"🔄 Running migration in '{mode}' mode...")
    
    if mode == "reset":
        confirm = input("  This will DELETE ALL DATA! Type 'YES' to confirm: ")
        if confirm != "YES":
            return
            
    await auto_migration.run(mode=mode)

if __name__ == "__main__":
    asyncio.run(main())