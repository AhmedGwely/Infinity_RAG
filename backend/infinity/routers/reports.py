from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit, providers, rag
from ..comparison import run_comparison
from ..deps import current_user, engineer_up, get_db
from ..models import Report, User
from ..ratelimit import limit
from ..services import embedder, qdrant

router = APIRouter(tags=["reports"])

REPORT_TASK = (
    "Write a structured engineering report as Markdown with these sections where the evidence supports them: "
    "Summary, Key findings, Technical details (include values with units), Standards and compliance, Conflicts or gaps. "
    "Every factual sentence must carry [S#] markers; label anything beyond the evidence as inference."
)


class CompareBody(BaseModel):
    subjects: list[str] = Field(min_length=2, max_length=4)
    aspects: list[str] = Field(min_length=1, max_length=12)
    title: str | None = None


class ReportBody(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    instruction: str = Field(min_length=5, max_length=3000)


def _dict(r: Report, full: bool = True) -> dict:
    out = {"id": r.id, "kind": r.kind, "title": r.title, "created_at": r.created_at}
    if full:
        out.update({"content": r.content, "data": r.data, "citations": r.citations})
    return out


@router.post("/compare")
def compare(body: CompareBody, request: Request, user: User = Depends(engineer_up), db: Session = Depends(get_db)):
    limit(f"compare:{user.id}", 10, 60)
    subjects = [s.strip() for s in body.subjects if s.strip()]
    aspects = [a.strip() for a in body.aspects if a.strip()]
    if len(set(subjects)) < 2:
        raise HTTPException(422, "Provide at least two different subjects.")
    llm, _p, _m = providers.build_provider(db)
    try:
        data, citations = run_comparison(llm, db, user.role, subjects, aspects,
                                         lambda q: rag.retrieve(db, q, user.role, qdrant(), embedder(), limit=8))
    except ValueError as error:
        raise HTTPException(502, f"The model returned an unusable comparison ({error}). Try again or use a different model.") from None
    report = Report(user_id=user.id, kind="comparison", title=body.title or f"Compare: {' vs '.join(subjects)}"[:300],
                    content="", data=data, citations=citations)
    db.add(report)
    db.commit()
    audit.record(db, "comparison_generated", request=request, user=user, target=report.title)
    return _dict(report)


@router.post("/reports/generate")
def generate(body: ReportBody, request: Request, user: User = Depends(engineer_up), db: Session = Depends(get_db)):
    limit(f"report:{user.id}", 10, 60)
    evidence = rag.retrieve(db, body.instruction, user.role, qdrant(), embedder(), limit=12)
    llm, _p, _m = providers.build_provider(db)
    if not evidence:
        raise HTTPException(404, "No accessible sources contain relevant evidence for this report.")
    text = rag.complete(llm, rag.build_prompt(body.instruction, evidence, REPORT_TASK)).strip()
    from app.generation.prompt_templates import validated_citation_ids
    ids = validated_citation_ids(text, len(evidence))
    report = Report(user_id=user.id, kind="report", title=body.title.strip(), content=text, data={"instruction": body.instruction},
                    citations=[evidence[i - 1].citation(f"S{i}") for i in ids])
    db.add(report)
    db.commit()
    audit.record(db, "report_generated", request=request, user=user, target=report.title)
    return _dict(report)


@router.get("/reports")
def list_reports(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return [_dict(r, full=False) for r in db.scalars(select(Report).where(Report.user_id == user.id).order_by(Report.created_at.desc()).limit(200))]


def _own(db: Session, user: User, rid: str) -> Report:
    r = db.get(Report, rid)
    if not r or r.user_id != user.id:
        raise HTTPException(404, "Report not found.")
    return r


@router.get("/reports/{rid}")
def get_report(rid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _dict(_own(db, user, rid))


@router.delete("/reports/{rid}")
def delete_report(rid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.delete(_own(db, user, rid))
    db.commit()
    return {"ok": True}


@router.get("/reports/{rid}/export")
def export(rid: str, format: str = "md", user: User = Depends(current_user), db: Session = Depends(get_db)):
    r = _own(db, user, rid)
    refs = "\n".join(f"- [{c['id']}] {c['document_title']} \u2014 {c['location']}" + (f" ({c['uri']})" if c.get("uri") else "") for c in r.citations)
    if r.kind == "comparison" and format == "csv":
        out = io.StringIO()
        w = csv.writer(out)
        subjects = r.data.get("subjects", [])
        w.writerow(["Aspect", *subjects, "Sources"])
        for row in r.data.get("rows", []):
            cells = [row["values"][s]["value"] or "NOT FOUND IN SOURCES" for s in subjects]
            marks = sorted({m for s in subjects for m in row["values"][s]["citations"]})
            w.writerow([row["aspect"], *cells, " ".join(marks)])
        return PlainTextResponse(out.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="comparison-{r.id[:8]}.csv"'})
    if r.kind == "comparison":
        subjects = r.data.get("subjects", [])
        lines = [f"# {r.title}", "", "| Aspect | " + " | ".join(subjects) + " |", "|---|" + "---|" * len(subjects)]
        for row in r.data.get("rows", []):
            cells = [(row["values"][s]["value"] + " " + "".join(f"[{c}]" for c in row["values"][s]["citations"])) if row["values"][s]["status"] == "verified" else "_not found in sources_" for s in subjects]
            lines.append(f"| {row['aspect']} | " + " | ".join(cells) + " |")
        body = "\n".join(lines) + ("\n\n## Conflicts\n" + "\n".join(f"- {c}" for c in r.data.get("conflicts", [])) if r.data.get("conflicts") else "")
    else:
        body = f"# {r.title}\n\n{r.content}"
    text = f"{body}\n\n## Sources\n{refs}\n\n_Generated {r.created_at:%Y-%m-%d %H:%M} UTC by INFINITY AI. Verify against the cited sources before use._\n"
    return PlainTextResponse(text, media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="report-{r.id[:8]}.md"'})
