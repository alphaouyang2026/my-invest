from fastapi import FastAPI

from app.api.router import api_router
from app.core.logging import configure_logging

configure_logging()

app = FastAPI(title="个人日本股票研究与模拟交易系统 API")
app.include_router(api_router)
