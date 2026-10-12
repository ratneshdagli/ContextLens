"""Interest profile, onboarding, topics."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from backend import db
from backend.config import TOPICS
from backend.services import interests

router = APIRouter(prefix="/api", tags=["profile"])


@router.get("/topics")
async def topics():
    return [{"id": k, "label": v["label"], "desc": v["desc"]} for k, v in TOPICS.items()]


@router.get("/profile")
async def get_profile():
    p = await interests.get_profile()
    _, n = await interests.get_taste()
    return {**p, "taste_signals": n}


class ProfilePatch(BaseModel):
    interests: Optional[list[dict]] = None
    topics: Optional[dict[str, int]] = None
    muted_keywords: Optional[list[str]] = None
    muted_outlets: Optional[list[str]] = None
    followed: Optional[list[str]] = None
    onboarded: Optional[bool] = None


@router.patch("/profile")
async def patch_profile(body: ProfilePatch):
    return await interests.save_profile(body.model_dump(exclude_none=True))


class Onboard(BaseModel):
    topics: list[str] = []
    about: str = ""                 # free text: "AI chips, Indian economy, F1"
    muted: list[str] = []


@router.post("/onboarding")
async def onboarding(body: Onboard):
    chosen = set(body.topics)
    # chosen topics get 'More'; everything else stays at 'Some' so major news outside your topics still reaches the editors
    weights = {k: (2 if k in chosen else 1) for k in TOPICS}
    p = await interests.save_profile({
        "interests": interests.split_interest_text(body.about),
        "topics": weights, "muted_keywords": body.muted, "onboarded": True})
    return p


@router.get("/onboarding/status")
async def onboarding_status():
    p = await interests.get_profile()
    return {"complete": bool(p["onboarded"])}


@router.post("/profile/reset-taste")
async def reset_taste():
    await interests.reset_taste()
    await db.execute("DELETE FROM feedback")
    return {"ok": True}
