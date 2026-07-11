import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.user import User
from app.repositories import user_repository

bearer_scheme = HTTPBearer()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """
    Add `current_user: User = Depends(get_current_user)` to any route to
    require a valid JWT. FastAPI runs this before the route body, and the
    route gets the actual User object — never write raw token-decoding
    logic in a route.
    """
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )

    user_id_str = decode_access_token(credentials.credentials)
    if user_id_str is None:
        raise unauthorized

    try:
        user_id = uuid.UUID(user_id_str)
    except ValueError:
        raise unauthorized

    user = user_repository.get_by_id(db, user_id)
    if user is None:
        raise unauthorized

    return user
