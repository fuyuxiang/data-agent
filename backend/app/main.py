from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.semantic import router as semantic_router

app = FastAPI(title="Data Agent", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(semantic_router)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}