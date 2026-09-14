from fastapi import APIRouter, Depends
from app.services.screen_track import ScreenTrack
from app.routers.auth import get_current_user
from app.models.user import User

router = APIRouter()

@router.get("")
@router.get("/")
async def list_monitors(current_user: User = Depends(get_current_user)):
    return ScreenTrack.list_monitors()
