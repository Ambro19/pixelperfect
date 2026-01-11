# backend/tests/test_screenshot.py
import pytest
from services.screenshot_service import screenshot_service

@pytest.mark.asyncio
async def test_basic_screenshot():
    """Test basic screenshot capture"""
    await screenshot_service.initialize()
    
    screenshot = await screenshot_service.capture_screenshot(
        url="https://example.com",
        width=1920,
        height=1080
    )
    
    assert len(screenshot) > 0
    assert isinstance(screenshot, bytes)
    
    await screenshot_service.cleanup()

@pytest.mark.asyncio
async def test_full_page_screenshot():
    """Test full page screenshot"""
    await screenshot_service.initialize()
    
    screenshot = await screenshot_service.capture_screenshot(
        url="https://example.com",
        full_page=True
    )
    
    assert len(screenshot) > 100000  # Should be larger
    
    await screenshot_service.cleanup()

@pytest.mark.asyncio
async def test_dark_mode():
    """Test dark mode screenshot"""
    await screenshot_service.initialize()
    
    screenshot = await screenshot_service.capture_screenshot(
        url="https://example.com",
        dark_mode=True
    )
    
    assert len(screenshot) > 0
    
    await screenshot_service.cleanup()