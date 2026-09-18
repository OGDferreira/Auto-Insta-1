from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str | None] = mapped_column(String(320), unique=True, index=True, nullable=True)
    username: Mapped[str] = mapped_column(String(80), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="admin", index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    instagram_accounts: Mapped[list["InstagramAccount"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )
    scheduled_posts: Mapped[list["ScheduledPost"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )
    parent: Mapped["User | None"] = relationship(
        remote_side="User.id", back_populates="collaborators"
    )
    collaborators: Mapped[list["User"]] = relationship(
        back_populates="parent", cascade="all, delete-orphan"
    )
    notification_subscriptions: Mapped[list["NotificationSubscription"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class InstagramAccount(Base):
    __tablename__ = "instagram_accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    instagram_user_id: Mapped[str] = mapped_column(String(120), index=True)
    facebook_page_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    username: Mapped[str] = mapped_column(String(120))
    profile_picture_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_encrypted: Mapped[str] = mapped_column(Text, default="")
    auto_reply_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_reply_text: Mapped[str] = mapped_column(Text, default="")
    direct_reply_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    direct_reply_text: Mapped[str] = mapped_column(Text, default="")
    comment_reply_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    comment_reply_text: Mapped[str] = mapped_column(Text, default="")
    connection_status: Mapped[str] = mapped_column(String(20), default="connected")
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    owner: Mapped[User] = relationship(back_populates="instagram_accounts")
    scheduled_posts: Mapped[list["ScheduledPost"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    bot_events: Mapped[list["BotEvent"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    instagram_metrics: Mapped[list["InstagramMetric"]] = relationship(
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
    original_media_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    thumbnail_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    thumbnail_storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_type: Mapped[str] = mapped_column(String(20), default="IMAGE")
    caption: Mapped[str] = mapped_column(Text, default="")
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(20), default="scheduled")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    owner: Mapped[User] = relationship(back_populates="scheduled_posts")
    account: Mapped[InstagramAccount] = relationship(back_populates="scheduled_posts")


class NotificationSubscription(Base):
    __tablename__ = "notification_subscriptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    p256dh: Mapped[str] = mapped_column(Text)
    auth: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    user: Mapped[User] = relationship(back_populates="notification_subscriptions")


class BotEvent(Base):
    __tablename__ = "bot_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(30), index=True)
    value: Mapped[float] = mapped_column(Float, default=0.0)
    webhook_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    customer_name: Mapped[str | None] = mapped_column(String(180), nullable=True)
    customer_username: Mapped[str | None] = mapped_column(String(120), nullable=True)
    bot_name: Mapped[str | None] = mapped_column(String(180), nullable=True)
    transaction_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    plan_name: Mapped[str | None] = mapped_column(String(180), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    account: Mapped[InstagramAccount | None] = relationship(back_populates="bot_events")


class InstagramMetric(Base):
    __tablename__ = "instagram_metrics"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("instagram_accounts.id", ondelete="CASCADE"), index=True
    )
    metric_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    reach: Mapped[int] = mapped_column(Integer, default=0)
    account: Mapped[InstagramAccount] = relationship(back_populates="instagram_metrics")
