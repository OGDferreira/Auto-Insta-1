from datetime import date, datetime, timezone
import secrets

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
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
    collaborator_rate_per_account: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    collaborator_daily_target: Mapped[int] = mapped_column(Integer, default=0)
    collaborator_daily_bonus: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    sharkbot_webhook_token: Mapped[str] = mapped_column(String(120), unique=True, index=True, default=lambda: secrets.token_urlsafe(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    instagram_accounts: Mapped[list["InstagramAccount"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )
    scheduled_posts: Mapped[list["ScheduledPost"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )
    posting_batches: Mapped[list["PostingBatch"]] = relationship(
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
    ice_breakers: Mapped[str] = mapped_column(Text, default="[]")
    connection_status: Mapped[str] = mapped_column(String(20), default="pending")
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    followers_count: Mapped[int] = mapped_column(Integer, default=0)
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
    automation_rules: Mapped[list["AutomationRule"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class CollaboratorConnection(Base):
    __tablename__ = "collaborator_connections"
    __table_args__ = (
        UniqueConstraint("owner_id", "instagram_user_id", name="uq_collaborator_connection_owner_instagram"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    collaborator_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    instagram_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("instagram_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    instagram_user_id: Mapped[str] = mapped_column(String(120))
    instagram_username: Mapped[str] = mapped_column(String(120), default="")
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    rate_per_account: Mapped[float] = mapped_column(Numeric(10, 2))
    daily_target: Mapped[int] = mapped_column(Integer)
    daily_bonus: Mapped[float] = mapped_column(Numeric(10, 2))


class CollaboratorDailyBonus(Base):
    __tablename__ = "collaborator_daily_bonuses"
    __table_args__ = (
        UniqueConstraint("collaborator_id", "local_date", name="uq_collaborator_daily_bonus"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    collaborator_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    local_date: Mapped[date] = mapped_column(Date, index=True)
    target: Mapped[int] = mapped_column(Integer)
    amount: Mapped[float] = mapped_column(Numeric(10, 2))
    awarded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AutomationRule(Base):
    __tablename__ = "automation_rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=True, index=True
    )
    target_account_ids: Mapped[str] = mapped_column(Text, default="[]")
    rule_type: Mapped[str] = mapped_column(String(20), index=True)
    trigger_keywords: Mapped[str] = mapped_column(Text, default="")
    message_text: Mapped[str] = mapped_column(Text, default="")
    dm_followup_text: Mapped[str] = mapped_column(Text, default="")
    media_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    drive_media_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    drive_account_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    drive_credentials_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    owner: Mapped[User] = relationship()
    account: Mapped[InstagramAccount | None] = relationship(back_populates="automation_rules")


class PostingBatch(Base):
    __tablename__ = "posting_batches"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160), default="Lote de publicações")
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    account_ids: Mapped[str] = mapped_column(Text, default="[]")
    is_loop: Mapped[bool] = mapped_column(Boolean, default=False)
    loop_interval_minutes: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    owner: Mapped[User] = relationship(back_populates="posting_batches")
    scheduled_posts: Mapped[list["ScheduledPost"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )


class ScheduledPost(Base):
    __tablename__ = "scheduled_posts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("instagram_accounts.id", ondelete="CASCADE"), index=True
    )
    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("posting_batches.id", ondelete="SET NULL"), nullable=True, index=True
    )
    loop_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    media_url: Mapped[str] = mapped_column(Text)
    original_media_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    drive_media_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    drive_account_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    drive_credentials_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_type: Mapped[str] = mapped_column(String(20), default="IMAGE")
    caption: Mapped[str] = mapped_column(Text, default="")
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(20), default="scheduled")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    owner: Mapped[User] = relationship(back_populates="scheduled_posts")
    account: Mapped[InstagramAccount] = relationship(back_populates="scheduled_posts")
    batch: Mapped[PostingBatch | None] = relationship(back_populates="scheduled_posts")


class NotificationSubscription(Base):
    __tablename__ = "notification_subscriptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    p256dh: Mapped[str] = mapped_column(Text)
    auth: Mapped[str] = mapped_column(Text)
    pwa_installed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    user: Mapped[User] = relationship(back_populates="notification_subscriptions")


class AppNotification(Base):
    __tablename__ = "app_notifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(180))
    body: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(String(500), default="/dashboard#overview")
    category: Mapped[str] = mapped_column(String(40), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class CollaboratorConnectionBatch(Base):
    __tablename__ = "collaborator_connection_batches"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    collaborator_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    collaborator_name: Mapped[str] = mapped_column(String(80))
    account_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    notify_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class BotEvent(Base):
    __tablename__ = "bot_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    account: Mapped[InstagramAccount | None] = relationship(back_populates="bot_events")


class DirectContact(Base):
    __tablename__ = "direct_contacts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("instagram_accounts.id", ondelete="CASCADE"), index=True)
    sender_id: Mapped[str] = mapped_column(String(120), index=True)
    last_inbound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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
