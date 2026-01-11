# backend/services/screenshot_service.py
from playwright.async_api import async_playwright
import asyncio
from typing import Optional, Dict, Any
import io
import base64

class ScreenshotService:
    """Core screenshot service using Playwright"""
    
    def __init__(self):
        self.browser = None
        self.playwright = None
    
    async def initialize(self):
        """Initialize Playwright browser"""
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox']
        )
    
    async def cleanup(self):
        """Cleanup browser resources"""
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
    
    async def capture_screenshot(
        self,
        url: str,
        width: int = 1920,
        height: int = 1080,
        full_page: bool = False,
        format: str = "png",
        quality: Optional[int] = None,
        delay: int = 0,
        dark_mode: bool = False,
        remove_elements: Optional[list] = None
    ) -> bytes:
        """
        Capture a screenshot of a URL
        
        Args:
            url: Website URL to screenshot
            width: Viewport width
            height: Viewport height
            full_page: Capture full page or viewport only
            format: Image format (png, jpeg, webp)
            quality: Image quality for jpeg (0-100)
            delay: Delay before screenshot (seconds)
            dark_mode: Enable dark mode
            remove_elements: CSS selectors to remove
        
        Returns:
            Screenshot as bytes
        """
        if not self.browser:
            await self.initialize()
        
        context = await self.browser.new_context(
            viewport={'width': width, 'height': height},
            color_scheme='dark' if dark_mode else 'light'
        )
        
        page = await context.new_page()
        
        try:
            # Navigate to URL
            await page.goto(url, wait_until='networkidle', timeout=30000)
            
            # Remove unwanted elements (cookie banners, popups, etc.)
            if remove_elements:
                for selector in remove_elements:
                    await page.evaluate(f'''
                        document.querySelectorAll('{selector}').forEach(el => el.remove());
                    ''')
            
            # Wait for custom delay
            if delay > 0:
                await asyncio.sleep(delay)
            
            # Capture screenshot
            screenshot_options = {
                'full_page': full_page,
                'type': format
            }
            
            if format == 'jpeg' and quality:
                screenshot_options['quality'] = quality
            
            screenshot_bytes = await page.screenshot(**screenshot_options)
            
            return screenshot_bytes
            
        finally:
            await page.close()
            await context.close()

# Global instance
screenshot_service = ScreenshotService()