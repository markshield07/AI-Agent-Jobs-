"""Dashboard API. Phase 1 covers resume intake and the fact base.

Every handler is sync, so FastAPI runs it in the threadpool, and every handler
resolves its own connection from `Database` in its body. Both matter: a resolved
connection cannot be passed between threads, and resolving it in a dependency
does not guarantee the same thread runs the handler.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from jobagent.answers import list_answers
from jobagent.api.models import (
    ActiveIn,
    FactIn,
    FactOut,
    KeywordsIn,
    NeverClaimIn,
    UploadOut,
)
from jobagent.config import Settings
from jobagent.db.database import Database
from jobagent.resume import facts as fact_store
from jobagent.resume.extract import EmptyResume, UnsupportedResume
from jobagent.resume.intake import ingest_resume, latest_base

router = APIRouter(prefix="/api")

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


async def get_db(request: Request) -> Database:
    return request.app.state.db


async def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


# Annotated dependencies rather than `= Depends(...)` defaults: same wiring, and it
# keeps mutable-default linting honest instead of blanket-ignored.
DbDep = Annotated[Database, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


def _as_out(fact: fact_store.Fact) -> FactOut:
    return FactOut(
        id=fact.id,
        kind=fact.kind,
        text=fact.text,
        detail=fact.detail,
        tags=fact.tags,
        source=fact.source,
        active=fact.active,
    )


def _fact_by_id(conn, fact_id: int) -> FactOut:
    found = next(f for f in fact_store.list_facts(conn, include_inactive=True) if f.id == fact_id)
    return _as_out(found)


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ------------------------------------------------------------------- resume --


@router.post("/resume", response_model=UploadOut)
def upload_resume(
    file: Annotated[UploadFile, File()],
    db: DbDep,
    settings: SettingsDep,
) -> UploadOut:
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        raise HTTPException(400, "The uploaded file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Resume is larger than 10MB.")

    try:
        result = ingest_resume(db.connection(), data, file.filename or "resume", settings)
    except (UnsupportedResume, EmptyResume) as exc:
        raise HTTPException(400, str(exc)) from exc

    return UploadOut(
        base_id=result.base_id,
        filename=result.filename,
        facts_created=len(result.fact_ids),
        already_uploaded=result.already_uploaded,
    )


@router.get("/resume")
def current_resume(db: DbDep) -> dict[str, object]:
    base = latest_base(db.connection())
    if base is None:
        raise HTTPException(404, "No resume uploaded yet.")
    return base


# ---------------------------------------------------------------- fact base --


@router.get("/facts", response_model=list[FactOut])
def get_facts(
    db: DbDep,
    kind: str | None = None,
    source: str | None = None,
    include_inactive: bool = False,
) -> list[FactOut]:
    found = fact_store.list_facts(
        db.connection(), kind=kind, source=source, include_inactive=include_inactive
    )
    return [_as_out(f) for f in found]


@router.post("/facts", response_model=FactOut, status_code=201)
def create_fact(body: FactIn, db: DbDep) -> FactOut:
    conn = db.connection()
    blocked = fact_store.violates_never_claim(conn, body.text)
    if blocked:
        raise HTTPException(
            409,
            f"{', '.join(blocked)} is on your never-claim list. "
            "Remove it from that list first if you do want to claim it.",
        )
    try:
        fact_id = fact_store.add_fact(
            conn,
            fact_store.Fact(
                kind=body.kind,
                text=body.text,
                detail=body.detail,
                tags=body.tags,
                source="user_added",
            ),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    return _fact_by_id(conn, fact_id)


@router.post("/facts/keywords", response_model=list[FactOut], status_code=201)
def add_keywords(body: KeywordsIn, db: DbDep) -> list[FactOut]:
    """Bulk-add skills or keywords. Already-present skills are skipped."""
    conn = db.connection()
    blocked = sorted(
        {
            term
            for keyword in body.keywords
            for term in fact_store.violates_never_claim(conn, keyword)
        }
    )
    if blocked:
        raise HTTPException(409, f"On your never-claim list: {', '.join(blocked)}.")

    created_ids = fact_store.add_keywords(conn, body.keywords)
    return [_fact_by_id(conn, fact_id) for fact_id in created_ids]


@router.patch("/facts/{fact_id}", response_model=FactOut)
def update_fact(fact_id: int, body: ActiveIn, db: DbDep) -> FactOut:
    """Deactivate or restore a fact. Facts are never deleted — variants cite them."""
    conn = db.connection()
    if not fact_store.set_active(conn, fact_id, body.active):
        raise HTTPException(404, f"No fact {fact_id}.")
    return _fact_by_id(conn, fact_id)


# ---------------------------------------------------------- never-claim list --


@router.get("/never-claim")
def get_never_claim(db: DbDep) -> list[dict[str, object]]:
    return fact_store.list_never_claim(db.connection())


@router.post("/never-claim", status_code=201)
def create_never_claim(body: NeverClaimIn, db: DbDep) -> dict[str, str]:
    try:
        fact_store.add_never_claim(db.connection(), body.term, body.note)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"term": body.term.strip()}


@router.delete("/never-claim/{term}", status_code=204)
def delete_never_claim(term: str, db: DbDep) -> None:
    if not fact_store.remove_never_claim(db.connection(), term):
        raise HTTPException(404, f"{term} is not on the never-claim list.")


# -------------------------------------------------------------- answer bank --


@router.get("/answers")
def get_answers(db: DbDep) -> list[dict[str, object]]:
    return list_answers(db.connection())
