"""SQLAlchemy ORM models — 1:1 port of the Prisma schema.

Physical naming is PRESERVED (table names `Provider`, `Model`, … and camelCase
columns `baseUrl`, `apiKey`, …) so an existing Postgres deployment keeps every
row across the Python conversion. New SQLite/Postgres databases are created
with the same layout.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Provider(Base):
    __tablename__ = "Provider"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String, unique=True)
    name: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String, default="openai")  # openai|anthropic|gemini|builtin|exchange
    baseUrl: Mapped[str] = mapped_column(String, default="")
    prefix: Mapped[str] = mapped_column(String, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    color: Mapped[str] = mapped_column(String, default="#10b981")
    docsUrl: Mapped[str | None] = mapped_column(String, nullable=True)
    authUrl: Mapped[str | None] = mapped_column(String, nullable=True)
    requiresAuth: Mapped[bool] = mapped_column(Boolean, default=False)
    freeTier: Mapped[str | None] = mapped_column(String, nullable=True)
    createdAt: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    keys: Mapped[list["ProviderKey"]] = relationship(
        back_populates="provider", cascade="all, delete-orphan"
    )
    models: Mapped[list["Model"]] = relationship(
        back_populates="provider", cascade="all, delete-orphan"
    )


class ProviderKey(Base):
    __tablename__ = "ProviderKey"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    providerId: Mapped[int] = mapped_column(ForeignKey("Provider.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String, default="key")
    apiKey: Mapped[str] = mapped_column(Text)
    weight: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    cooldownUntil: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lastError: Mapped[str | None] = mapped_column(String, nullable=True)
    reqCount: Mapped[int] = mapped_column(Integer, default=0)
    errCount: Mapped[int] = mapped_column(Integer, default=0)
    lastUsedAt: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    createdAt: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    provider: Mapped[Provider] = relationship(back_populates="keys")


class Model(Base):
    __tablename__ = "Model"
    __table_args__ = ({"sqlite_autoincrement": True},)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    providerId: Mapped[int] = mapped_column(ForeignKey("Provider.id", ondelete="CASCADE"))
    modelId: Mapped[str] = mapped_column(String)
    exposedId: Mapped[str] = mapped_column(String)
    displayName: Mapped[str] = mapped_column(String, default="")
    isFree: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String, default="unknown")  # healthy|cooling|dead|unknown
    httpStatus: Mapped[int] = mapped_column(Integer, default=0)
    latencyMs: Mapped[int] = mapped_column(Integer, default=0)
    checkedAt: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    contextLength: Mapped[int] = mapped_column(Integer, default=0)
    maxOutput: Mapped[int] = mapped_column(Integer, default=0)
    capabilities: Mapped[str] = mapped_column(Text, default="{}")
    priceIn: Mapped[float] = mapped_column(Float, default=0)
    priceOut: Mapped[float] = mapped_column(Float, default=0)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    provider: Mapped[Provider] = relationship(back_populates="models")


class ModelRoute(Base):
    __tablename__ = "ModelRoute"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    publicId: Mapped[str] = mapped_column(String, unique=True)
    fallbacks: Mapped[str] = mapped_column(Text, default="[]")
    auto: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(String, default="")
    createdAt: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ClientKey(Base):
    __tablename__ = "ClientKey"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String)
    token: Mapped[str] = mapped_column(String, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    allowedModels: Mapped[str] = mapped_column(String, default="*")
    # Comma/space list of trading scopes; empty means NO trading access.
    tradingScopes: Mapped[str] = mapped_column(String, default="")
    rpmLimit: Mapped[int] = mapped_column(Integer, default=60)
    tpdLimit: Mapped[int] = mapped_column(Integer, default=0)
    tokensIn: Mapped[int] = mapped_column(Integer, default=0)
    tokensOut: Mapped[int] = mapped_column(Integer, default=0)
    reqCount: Mapped[int] = mapped_column(Integer, default=0)
    lastUsedAt: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    createdAt: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RequestLog(Base):
    __tablename__ = "RequestLog"
    __table_args__ = (Index("RequestLog_ts_idx", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    clientKeyId: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clientName: Mapped[str] = mapped_column(String, default="")
    providerId: Mapped[int | None] = mapped_column(Integer, nullable=True)
    providerName: Mapped[str] = mapped_column(String, default="")
    model: Mapped[str] = mapped_column(String)
    upstreamModel: Mapped[str] = mapped_column(String, default="")
    endpoint: Mapped[str] = mapped_column(String, default="/v1/chat/completions")
    status: Mapped[int] = mapped_column(Integer)
    latencyMs: Mapped[int] = mapped_column(Integer, default=0)
    tokensIn: Mapped[int] = mapped_column(Integer, default=0)
    tokensOut: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(String, default="")
    via: Mapped[str] = mapped_column(String, default="")
    spoofed: Mapped[bool] = mapped_column(Boolean, default=False)
    # Time to first token (ms) for streaming paths; 0 when not observed.
    ttftMs: Mapped[int] = mapped_column(Integer, default=0)


class TerminalCommand(Base):
    __tablename__ = "TerminalCommand"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    command: Mapped[str] = mapped_column(Text)
    output: Mapped[str] = mapped_column(Text)
    exitCode: Mapped[int] = mapped_column(Integer, default=0)
    durationMs: Mapped[int] = mapped_column(Integer, default=0)
    cwd: Mapped[str] = mapped_column(String, default="/workspace")
    createdAt: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SystemConfig(Base):
    __tablename__ = "SystemConfig"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class StorageProviderRow(Base):
    __tablename__ = "StorageProvider"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    type: Mapped[str] = mapped_column(String)  # local|builtin_cloud|firebase|supabase|s3|github|webdav
    config: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String, default="disconnected")  # connected|disconnected|pending|error
    isBuiltin: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    freeTier: Mapped[str | None] = mapped_column(String, nullable=True)
    usageMb: Mapped[float] = mapped_column(Float, default=0)
    quotaMb: Mapped[float] = mapped_column(Float, default=0)
    region: Mapped[str | None] = mapped_column(String, nullable=True)
    docsUrl: Mapped[str | None] = mapped_column(String, nullable=True)
    authUrl: Mapped[str | None] = mapped_column(String, nullable=True)
    lastTestAt: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lastError: Mapped[str | None] = mapped_column(String, nullable=True)
    createdAt: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class StorageFile(Base):
    __tablename__ = "StorageFile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String)
    path: Mapped[str] = mapped_column(String, default="/")
    size: Mapped[int] = mapped_column(Integer, default=0)
    mime: Mapped[str] = mapped_column(String, default="application/octet-stream")
    providerKey: Mapped[str] = mapped_column(String, default="local_disk")
    data: Mapped[str | None] = mapped_column(Text, nullable=True)  # base64 payload (local disk)
    createdAt: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AgentTask(Base):
    __tablename__ = "AgentTask"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # cuid-like
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default="queued")  # queued|running|completed|failed|cancelled
    model: Mapped[str] = mapped_column(String, default="auto")
    maxSteps: Mapped[int] = mapped_column(Integer, default=8)
    summary: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    createdAt: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    startedAt: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finishedAt: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    steps: Mapped[list["AgentStep"]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="AgentStep.stepNumber"
    )


class AgentStep(Base):
    __tablename__ = "AgentStep"
    __table_args__ = (Index("AgentStep_taskId_idx", "taskId"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    taskId: Mapped[str] = mapped_column(ForeignKey("AgentTask.id", ondelete="CASCADE"))
    stepNumber: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, default="ok")  # ok|error|info
    latencyMs: Mapped[int] = mapped_column(Integer, default=0)
    createdAt: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    task: Mapped[AgentTask] = relationship(back_populates="steps")


class McpServer(Base):
    """A user-registered MCP server ("plugin") for the console + agent.

    Additive table: created at boot by `Base.metadata.create_all`, never
    touched by bootstrap beyond existing rows. `headers` holds the user's
    credentials as JSON and is always masked on the way out (see
    nova.mcp_client.mask_headers) — same rule as the storage targets.
    """

    __tablename__ = "McpServer"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # slug, e.g. "deepwiki"
    name: Mapped[str] = mapped_column(String)
    transport: Mapped[str] = mapped_column(String, default="http")  # http|stdio
    url: Mapped[str] = mapped_column(String, default="")
    command: Mapped[str] = mapped_column(Text, default="")   # stdio: "npx -y @scope/server"
    headers: Mapped[str] = mapped_column(Text, default="{}")  # JSON object, secrets included
    allow: Mapped[str] = mapped_column(Text, default="[]")     # JSON list of tool names ([] = all)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String, default="unknown")  # unknown|connected|error
    lastError: Mapped[str | None] = mapped_column(Text, nullable=True)
    tools: Mapped[str] = mapped_column(Text, default="[]")    # cached tools/list snapshot
    lastCheckedAt: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    createdAt: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updatedAt: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ProviderSession(Base):
    __tablename__ = "ProviderSession"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    providerKey: Mapped[str] = mapped_column(String)
    username: Mapped[str] = mapped_column(String)
    displayName: Mapped[str | None] = mapped_column(String, nullable=True)
    plan: Mapped[str] = mapped_column(String, default="free")
    status: Mapped[str] = mapped_column(String, default="connected")
    tokenMask: Mapped[str | None] = mapped_column(String, nullable=True)
    connectedAt: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expiresAt: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class UsageWindow(Base):
    """Per-(provider, model, key) request/token counters for rate limiting.

    Additive telemetry table: one row per open window, `windowKind` being
    "minute" or "day" and `bucket` the epoch minute / epoch day it covers. The
    unique tuple makes `nova.ratelimit.record_usage` an idempotent upsert, so
    concurrent requests cannot double-create a bucket. Old rows are pruned by
    `nova.ratelimit.prune_usage` (2 days kept). `keyId` is 0 for aggregate
    counters that are not tied to one upstream key.
    """

    __tablename__ = "UsageWindow"
    __table_args__ = (
        Index("UsageWindow_key_idx", "providerId", "modelId", "keyId", "windowKind", "bucket", unique=True),
        Index("UsageWindow_ts_idx", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    providerId: Mapped[int] = mapped_column(Integer, default=0)
    modelId: Mapped[str] = mapped_column(String, default="")
    keyId: Mapped[int] = mapped_column(Integer, default=0)
    windowKind: Mapped[str] = mapped_column(String, default="minute")  # minute|day
    bucket: Mapped[int] = mapped_column(Integer, default=0)
    requests: Mapped[int] = mapped_column(Integer, default=0)
    tokensIn: Mapped[int] = mapped_column(Integer, default=0)
    tokensOut: Mapped[int] = mapped_column(Integer, default=0)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TradeOrder(Base):
    """One order intent and its lifecycle state.

    `providerId` / `clientKeyId` are plain ints on purpose: deleting a provider
    or a client key must never cascade into trading history.
    """

    __tablename__ = "TradeOrder"
    __table_args__ = (
        Index("TradeOrder_clientOrderId_idx", "clientOrderId"),
        Index("TradeOrder_createdAt_idx", "createdAt"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    publicId: Mapped[str] = mapped_column(String, unique=True)
    clientOrderId: Mapped[str] = mapped_column(String, default="")
    exchange: Mapped[str] = mapped_column(String, default="")
    providerId: Mapped[int | None] = mapped_column(Integer, nullable=True)
    symbol: Mapped[str] = mapped_column(String)
    side: Mapped[str] = mapped_column(String)  # buy|sell
    orderType: Mapped[str] = mapped_column(String, default="market")
    quantity: Mapped[float] = mapped_column(Float, default=0)
    notional: Mapped[float] = mapped_column(Float, default=0)
    limitPrice: Mapped[float | None] = mapped_column(Float, nullable=True)
    # intent|submitted|partial|filled|rejected|cancelled|failed
    status: Mapped[str] = mapped_column(String, default="intent")
    mode: Mapped[str] = mapped_column(String, default="sandbox")  # sandbox|live
    reason: Mapped[str] = mapped_column(String, default="")
    clientKeyId: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The declarative API reserves `metadata`; the DB column keeps the name.
    metadata_: Mapped[str] = mapped_column("metadata", Text, default="{}")
    createdAt: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updatedAt: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class TradeFill(Base):
    """An execution (full or partial) reported by an exchange."""

    __tablename__ = "TradeFill"
    __table_args__ = (
        Index("TradeFill_orderPublicId_idx", "orderPublicId"),
        Index("TradeFill_createdAt_idx", "createdAt"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    orderPublicId: Mapped[str] = mapped_column(String, default="")
    exchange: Mapped[str] = mapped_column(String, default="")
    symbol: Mapped[str] = mapped_column(String)
    side: Mapped[str] = mapped_column(String, default="")
    quantity: Mapped[float] = mapped_column(Float, default=0)
    price: Mapped[float] = mapped_column(Float, default=0)
    fee: Mapped[float] = mapped_column(Float, default=0)
    feeAsset: Mapped[str] = mapped_column(String, default="")
    upstreamFillId: Mapped[str] = mapped_column(String, default="")
    mode: Mapped[str] = mapped_column(String, default="")
    createdAt: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RiskPolicy(Base):
    """Operator-defined guardrail evaluated before an order can leave."""

    __tablename__ = "RiskPolicy"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # slug
    name: Mapped[str] = mapped_column(String)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    maxOrderNotional: Mapped[float] = mapped_column(Float, default=0)
    dailyLossCap: Mapped[float] = mapped_column(Float, default=0)
    allowedSymbols: Mapped[str] = mapped_column(Text, default="[]")
    maxOpenOrders: Mapped[int] = mapped_column(Integer, default=0)
    # The declarative API reserves `metadata`; the DB column keeps the name.
    metadata_: Mapped[str] = mapped_column("metadata", Text, default="{}")
    createdAt: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updatedAt: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class TradeAuditLog(Base):
    """Append-only trading audit trail. ORM update/delete is blocked in
    nova/trading_audit.py; rows are written by every trading decision path."""

    __tablename__ = "TradeAuditLog"
    __table_args__ = (
        Index("TradeAuditLog_ts_idx", "ts"),
        Index("TradeAuditLog_event_idx", "event"),
        Index("TradeAuditLog_orderPublicId_idx", "orderPublicId"),
        Index("TradeAuditLog_clientKeyId_idx", "clientKeyId"),
        Index("TradeAuditLog_exchange_idx", "exchange"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # intent|refusal|submission|fill|read|kill_switch|cancel
    event: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="")
    code: Mapped[str] = mapped_column(String, default="")
    actor: Mapped[str] = mapped_column(String, default="")  # gateway|agent|admin|system
    clientKeyId: Mapped[int | None] = mapped_column(Integer, nullable=True)
    providerId: Mapped[int | None] = mapped_column(Integer, nullable=True)
    providerKey: Mapped[str] = mapped_column(String, default="")
    exchange: Mapped[str] = mapped_column(String, default="")
    symbol: Mapped[str] = mapped_column(String, default="")
    orderPublicId: Mapped[str] = mapped_column(String, default="")
    message: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[str] = mapped_column(Text, default="{}")
    mode: Mapped[str] = mapped_column(String, default="")


ALL_TABLES = [
    Provider, ProviderKey, Model, ModelRoute, ClientKey, RequestLog,
    TerminalCommand, SystemConfig, StorageProviderRow, StorageFile,
    AgentTask, AgentStep, ProviderSession, McpServer, UsageWindow,
    TradeOrder, TradeFill, RiskPolicy, TradeAuditLog,
]