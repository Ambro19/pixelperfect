# backend/tests/load_test.py
import asyncio
import aiohttp
import time

async def make_request(session, url, token):
    """Make screenshot request"""
    async with session.post(
        url,
        json={"url": "https://example.com", "width": 1920, "height": 1080},
        headers={"Authorization": f"Bearer {token}"}
    ) as response:
        return await response.json()

async def load_test(num_requests=100):
    """Test with concurrent requests"""
    start_time = time.time()
    
    async with aiohttp.ClientSession() as session:
        tasks = [
            make_request(session, "http://localhost:8000/api/v1/screenshot", "YOUR_TOKEN")
            for _ in range(num_requests)
        ]
        results = await asyncio.gather(*tasks)
    
    end_time = time.time()
    duration = end_time - start_time
    
    print(f"Completed {num_requests} requests in {duration:.2f} seconds")
    print(f"Average: {duration/num_requests:.2f} seconds per request")
    print(f"Throughput: {num_requests/duration:.2f} requests/second")

# Run test
asyncio.run(load_test(100))