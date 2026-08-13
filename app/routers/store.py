from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models import Category, User
from app.schemas import CategoryOut

router = APIRouter(prefix="/api/store", tags=["store"])


@router.get("/categories", response_model=list[CategoryOut])
def list_categories(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[CategoryOut]:
    categories = db.scalars(
        select(Category)
        .where(Category.active.is_(True), Category.deleted_at.is_(None))
        .order_by(Category.sort_order.asc(), Category.id.asc())
        .offset(offset)
        .limit(limit)
    ).all()
    return [CategoryOut.model_validate(category) for category in categories]
