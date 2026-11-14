from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit, providers, rag
from ..deps import current_user, get_db
from ..models import Conversation, Message, User
from ..ratelimit import limit
from ..services import embedder, qdrant

router = APIRouter(tags=["chat"])


class ChatBody(BaseModel):
    question: str = Field(min_length=2, max_length=4000)
    conversation_id: str | None = None


class SearchBody(BaseModel):
    query: str = Field(min_length=2, max_length=1000)
    limit: int = Field(default=12, ge=1, le=30)


def _msg(m: Message) -> dict:
    return {"id": m.id, "role": m.role, "content": m.content, "citations": m.citations, "meta": m.meta, "created_at": m.created_at}


@router.post("/search")
def search(body: SearchBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Retrieval only (no LLM): shows what each channel found and the fused ranking."""
    limit(f"search:{user.id}", 60, 60)
    evidence = rag.retrieve(db, body.query, user.role, qdrant(), embedder(), limit=body.limit)
    return {"results": [e.citation(f"R{i}") for i, e in enumerate(evidence, start=1)]}


@router.post("/chat")
def chat(body: ChatBody, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    limit(f"chat:{user.id}", 30, 60)
    conversation = None
    if body.conversation_id:
        conversation = db.get(Conversation, body.conversation_id)
        if not conversation or conversation.user_id != user.id:
            raise HTTPException(404, "Conversation not found.")
    if conversation is None:
        conversation = Conversation(user_id=user.id, title=body.question.strip()[:80])
        db.add(conversation)
        db.flush()
    previous = db.scalars(select(Message).where(Message.conversation_id == conversation.id, Message.role == "user").order_by(Message.created_at.desc()).limit(1)).first()
    query = body.question.strip()
    if previous and len(query.split()) < 6:
        query = f"{previous.content} {query}"  # short follow-ups inherit the previous question's context for retrieval only
    db.add(Message(conversation_id=conversation.id, role="user", content=body.question.strip()))
    db.commit()
    evidence = rag.retrieve(db, query, user.role, qdrant(), embedder())
    llm, provider, model = providers.build_provider(db)
    answer = rag.answer_question(llm, body.question.strip(), evidence)
    now = datetime.now(timezone.utc)  # column defaults are applied at flush time, so reply.created_at would still be None here
    reply = Message(conversation_id=conversation.id, role="assistant", content=answer.text, citations=answer.citations,
                    meta={"provider": provider, "model": model, "evidence_retrieved": answer.evidence_count, "found": answer.found},
                    created_at=now)
    db.add(reply)
    conversation.updated_at = now
    db.commit()
    audit.record(db, "chat_query", request=request, user=user, target=conversation.id, detail={"question": body.question[:200], "found": answer.found})
    return {"conversation_id": conversation.id, "message": _msg(reply)}


@router.get("/conversations")
def conversations(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Conversation).where(Conversation.user_id == user.id).order_by(Conversation.updated_at.desc()).limit(100))
    return [{"id": c.id, "title": c.title, "updated_at": c.updated_at} for c in rows]


@router.get("/conversations/{cid}")
def conversation(cid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = db.get(Conversation, cid)
    if not c or c.user_id != user.id:
        raise HTTPException(404, "Conversation not found.")
    msgs = db.scalars(select(Message).where(Message.conversation_id == cid).order_by(Message.created_at)).all()
    return {"id": c.id, "title": c.title, "messages": [_msg(m) for m in msgs]}


@router.delete("/conversations/{cid}")
def delete_conversation(cid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = db.get(Conversation, cid)
    if not c or c.user_id != user.id:
        raise HTTPException(404, "Conversation not found.")
    db.delete(c)
    db.commit()
    return {"ok": True}
