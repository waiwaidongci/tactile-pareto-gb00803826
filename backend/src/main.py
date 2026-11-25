import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from sqlalchemy import text

from scripts.seed_demo import seed
from src.config.settings import SEED
from src.middlewares.http import install
from src.models.database import cache, engine
from src.models.tables import metadata
from src.routes.api import router
from src.routes.shift_api import router as shift_router

# 即便测试以不进入 lifespan 的方式实例化 TestClient，也保证表结构就绪（create_all 幂等）。
metadata.create_all(engine)


@asynccontextmanager
async def lifespan(app):
    metadata.create_all(engine)
    if SEED:
        try:
            logging.info('Demo seed: %s', seed())
        except Exception:
            logging.exception('Demo seed failed. Run python -m scripts.seed_demo to retry.')
    yield


app = FastAPI(title='业务分析工作台', lifespan=lifespan)
install(app)
app.include_router(router)
app.include_router(shift_router)
# 同时暴露 /api 前缀，兼容经网关或直连后端时带 /api 的调用习惯（nginx 会剥离 /api）。
app.include_router(router, prefix='/api')
app.include_router(shift_router, prefix='/api')


@app.get('/health')
def health():
    with engine.connect() as connection:
        connection.execute(text('SELECT 1'))
    cache.ping()
    return {'status': 'ok'}


if __name__ == '__main__':
    uvicorn.run(app, host='0.0.0.0', port=8000)
