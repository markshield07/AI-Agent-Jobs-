"""Dashboard API for tailoring: make a variant for a job, read it, fetch its PDF."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse

from jobagent.api.routes import DbDep, SettingsDep
from jobagent.llm.backend import LLMError, LLMUnavailable
from jobagent.tailor import store
from jobagent.tailor.models import Variant
from jobagent.tailor.pipeline import TailorError, tailor_job

router = APIRouter(prefix="/api")


def _full(variant: Variant) -> dict[str, Any]:
    body = store.variant_summary(variant)
    body["content"] = variant.content.model_dump()
    body["cover_letter"] = variant.cover_letter.paragraphs if variant.cover_letter else None
    return body


@router.post("/jobs/{job_id}/tailor", status_code=201)
def tailor(job_id: str, db: DbDep, settings: SettingsDep, cover_letter: bool = True) -> Any:
    """Tailor the resume to this job now. Returns the variant, ready or rejected."""
    try:
        variant = tailor_job(db.connection(), job_id, settings, with_cover_letter=cover_letter)
    except TailorError as exc:
        raise HTTPException(404 if "No job" in str(exc) else 409, str(exc)) from exc
    except LLMUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(502, f"The model call failed: {exc}") from exc
    return _full(variant)


@router.get("/jobs/{job_id}/variants")
def job_variants(job_id: str, db: DbDep) -> list[dict[str, Any]]:
    return [store.variant_summary(v) for v in store.list_variants(db.connection(), job_id=job_id)]


@router.get("/variants")
def recent_variants(db: DbDep, limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
    return [store.variant_summary(v) for v in store.list_variants(db.connection(), limit=limit)]


def _get(db, variant_id: int) -> Variant:
    variant = store.get_variant(db.connection(), variant_id)
    if variant is None:
        raise HTTPException(404, f"No variant {variant_id}.")
    return variant


@router.get("/variants/{variant_id}")
def variant_detail(variant_id: int, db: DbDep) -> dict[str, Any]:
    return _full(_get(db, variant_id))


@router.get("/variants/{variant_id}/pdf")
def variant_pdf(variant_id: int, db: DbDep) -> FileResponse:
    variant = _get(db, variant_id)
    if not variant.pdf_path or not Path(variant.pdf_path).is_file():
        raise HTTPException(404, "This variant has no PDF.")
    return FileResponse(
        variant.pdf_path,
        media_type="application/pdf",
        filename=Path(variant.pdf_path).name,
    )


@router.get("/variants/{variant_id}/cover-letter", response_class=PlainTextResponse)
def variant_cover_letter(variant_id: int, db: DbDep) -> str:
    variant = _get(db, variant_id)
    if variant.cover_letter is None:
        raise HTTPException(404, "This variant has no cover letter.")
    return "\n\n".join(variant.cover_letter.paragraphs)
