import uvicorn
from app.core.config import settings

if __name__ == "__main__":
    print(f"Starting Remote Desktop Server on {settings.HOST}:{settings.PORT}...")
    print(f"Debug mode: {settings.DEBUG}")
    print(f"Encoder: {settings.ENCODER}")
    
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG
    )
