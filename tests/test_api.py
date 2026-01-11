# backend/tests/test_api.py
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

def test_screenshot_api():
    """Test screenshot API endpoint"""
    # Register user
    response = client.post("/register", json={
        "username": "testuser",
        "email": "test@example.com",
        "password": "password123"
    })
    assert response.status_code == 200
    
    # Login
    response = client.post("/token_json", data={
        "username": "testuser",
        "password": "password123"
    })
    assert response.status_code == 200
    token = response.json()["access_token"]
    
    # Create screenshot
    response = client.post(
        "/api/v1/screenshot",
        json={
            "url": "https://example.com",
            "width": 1920,
            "height": 1080
        },
        headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    data = response.json()
    assert "screenshot_url" in data
    assert "screenshot_id" in data