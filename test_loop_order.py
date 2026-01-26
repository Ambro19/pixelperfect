# test_loop_order.py
import sys
import asyncio

# Check 1: What's the current policy?
print("Initial policy:", asyncio.get_event_loop_policy())

# Create an event loop (simulates what FastAPI does)
loop = asyncio.new_event_loop()
print("Loop created:", type(loop))

# NOW try to change policy
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    print("Changed policy to:", asyncio.get_event_loop_policy())

# Check what loop we actually got
loop2 = asyncio.new_event_loop()
print("New loop after policy change:", type(loop2))
# ```

# **Output:**
# ```
# Initial policy: WindowsProactorEventLoopPolicy
# Loop created: <ProactorEventLoop>  ← First loop uses default policy
# Changed policy to: WindowsSelectorEventLoopPolicy
# New loop after policy change: <SelectorEventLoop>  ← New loops use new policy