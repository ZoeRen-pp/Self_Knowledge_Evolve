"""POST /api/v1/query — declarative query engine endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.app_factory import get_app
from src.query.engine import QueryEngine
from src.query.validator import ValidationError

router = APIRouter(prefix="/api/v1", tags=["query"])


class QueryRequest(BaseModel):
    name: str | None = None
    intent: str | None = None
    steps: list[dict[str, Any]] = Field(..., min_length=1, max_length=20)


@router.post("/query")
def run_query(body: QueryRequest, _app=Depends(get_app)):
    plan = body.model_dump(exclude_none=True)
    engine = QueryEngine(_app)
    try:
        return engine.execute(plan)
    except ValidationError as exc:
        return JSONResponse(status_code=400, content={"error": "validation_error", "details": exc.errors})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})