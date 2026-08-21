from fastapi import APIRouter

from app.api import data_sync, health, quality, snapshots, stock_pool

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(data_sync.router)
api_router.include_router(snapshots.router)
api_router.include_router(quality.router)
api_router.include_router(stock_pool.router)
