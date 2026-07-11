from sqlalchemy.orm import Session

from app.core.security import create_access_token, hash_password, verify_password
from app.models.user import User
from app.repositories import user_repository


class EmailAlreadyRegisteredError(Exception):
    pass


class InvalidCredentialsError(Exception):
    pass


def register(db: Session, email: str, password: str) -> User:
    existing = user_repository.get_by_email(db, email)
    if existing:
        raise EmailAlreadyRegisteredError()

    hashed = hash_password(password)
    return user_repository.create(db, email=email, hashed_password=hashed)


def login(db: Session, email: str, password: str) -> str:
    """Returns a JWT access token if credentials are valid."""
    user = user_repository.get_by_email(db, email)

    # Deliberately the same error for "no such user" and "wrong password" —
    # revealing which one it was lets an attacker enumerate valid emails.
    if not user or not verify_password(password, user.hashed_password):
        raise InvalidCredentialsError()

    return create_access_token(user_id=str(user.id))
