from sqlalchemy import BigInteger, DateTime, JSON, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from datetime import datetime
from typing import Annotated

from database import Base

UTC_NOW = Annotated[datetime, mapped_column(DateTime(timezone=True), server_default=text("now()"))]

class User(Base):
    __tablename__ = "users"                                      # pyright: ignore[reportUnannotatedClassAttribute]

    id: Mapped[int] = mapped_column(primary_key=True)
    short_uuid: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[UTC_NOW]  # pyright: ignore[reportUninitializedInstanceVariable]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # pyright: ignore[reportUninitializedInstanceVariable]
    traffic_limit_bytes: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    traffic_used_bytes: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    # RW access ledger (rw/policy.py): persisted grace window so a restart
    # cannot extend a Remnawave-outage grace. Nullable JSON blob.
    access_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class TrafficCursor(Base):
    """Durable per-container accounting cursor for traffic quotas.

    One row per (node, container_id): the last observed ABSOLUTE byte total
    of that container's stats counter. Keyed by node + full 64-hex container
    id (short ids and names can collide across daemons). The user row's
    traffic_used_bytes stays the ONLY aggregate - these rows are baselines,
    never per-node cumulative copies. Persisted so a panel restart or a
    missed tick can neither lose nor double-count traffic.
    """
    __tablename__ = "traffic_cursors"                            # pyright: ignore[reportUnannotatedClassAttribute]
    __table_args__ = (
        UniqueConstraint("node", "container_id", name="uq_traffic_cursor_node_container"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    node: Mapped[str] = mapped_column(String(64))
    container_id: Mapped[str] = mapped_column(String(64))
    owner: Mapped[str] = mapped_column(String(255), index=True)
    last_total: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[UTC_NOW]  # pyright: ignore[reportUninitializedInstanceVariable]