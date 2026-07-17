from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import auth_routes, chat_routes, collection_routes, document_routes, health_routes
from app.core.config import settings

app = FastAPI(title="InCite API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_routes.router)
app.include_router(auth_routes.router)
app.include_router(collection_routes.router)
app.include_router(document_routes.router)
app.include_router(chat_routes.router)
