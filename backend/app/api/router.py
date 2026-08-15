from fastapi import APIRouter

from app.api import data_sync, health

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(data_sync.router)
