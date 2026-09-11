from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    instagram_accounts: Mapped[list["InstagramAccount"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )
    scheduled_posts: Mapped[list["ScheduledPost"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )


class InstagramAccount(Base):
    __tablename__ = "instagram_accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    instagram_user_id: Mapped[str] = mapped_column(String(120), index=True)
    username: Mapped[str] = mapped_column(String(120))
    profile_picture_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_encrypted: Mapped[str] = mapped_column(Text)
    auto_reply_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_reply_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    owner: Mapped[User] = relationship(back_populates="instagram_accounts")
    scheduled_posts: Mapped[list["ScheduledPost"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class ScheduledPost(Base):
    __tablename__ = "scheduled_posts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("instagram_accounts.id", ondelete="CASCADE"), index=True
    )
    media_url: Mapped[str] = mapped_column(Text)
    media_type: Mapped[str] = mapped_column(String(20), default="IMAGE")
    caption: Mapped[str] = mapped_column(Text, default="")
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(20), default="scheduled")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    owner: Mapped[User] = relationship(back_populates="scheduled_posts")
    account: Mapped[InstagramAccount] = relationship(back_populates="scheduled_posts")
