from typing import Sequence

from fastapi import HTTPException, status

from sqlalchemy import delete, select, update, exists as sa_exists
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from users.models import TrafficCursor, User
from users.schemas import UserSchema



class TrafficCursorDB:
    """Durable (node, container_id) -> absolute-byte baseline rows.

    Cursors and the user's traffic_used_bytes are updated in ONE
    transaction (add_delta_with_cursors) so a crash between them can never
    lose or double-count a tick's traffic. A cursor row is an upsert target
    only: it never echoes cumulative bytes back into the user aggregate.
    """

    @staticmethod
    async def get_cursor(
        db: AsyncSession, node: str, container_id: str
    ) -> TrafficCursor | None:
        result = await db.execute(
            select(TrafficCursor).where(
                TrafficCursor.node == node,
                TrafficCursor.container_id == container_id,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def delete_owner_cursors(db: AsyncSession, owner: str) -> None:
        await db.execute(
            delete(TrafficCursor).where(TrafficCursor.owner == owner)
        )
        await db.commit()

    @staticmethod
    async def add_delta_with_cursors(
        db: AsyncSession,
        short_uuid: str,
        delta: int,
        cursors: list[tuple[str, str, int]],
    ) -> None:
        """Atomically apply user delta and persist new cursor baselines.

        cursors: (node, container_id, last_total) triples for every
        container of this owner observed in the current tick - including
        unchanged ones - so all baselines advance together. No-op commits
        are skipped when there is nothing to change.
        """
        if delta < 0:
            raise ValueError(f"negative traffic delta for {short_uuid}: {delta}")
        if delta == 0 and not cursors:
            return

        if delta > 0:
            await db.execute(
                update(User)
                .where(User.short_uuid == short_uuid)
                .values(traffic_used_bytes=User.traffic_used_bytes + delta)
            )

        for node, container_id, last_total in cursors:
            stmt = pg_insert(TrafficCursor).values(
                node=node,
                container_id=container_id,
                owner=short_uuid,
                last_total=last_total,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[TrafficCursor.node, TrafficCursor.container_id],
                set_={"last_total": last_total, "owner": short_uuid},
            )
            await db.execute(stmt)

        await db.commit()

    @staticmethod
    async def reset_traffic_with_baselines(
        db: AsyncSession,
        short_uuid: str,
        observed: list[tuple[str, str, int]],
    ) -> None:
        """Reset the aggregate and rebase baselines in ONE transaction.

        traffic_used_bytes = 0; every OBSERVED (running, stats-readable)
        container's cursor is set to its current absolute total - counting
        restarts from now, pre-reset history is never re-billed. Cursors of
        unobservable containers are dropped: a container that reappears is
        a first sample again (deterministic, its full total counts).
        """
        await db.execute(
            update(User).where(User.short_uuid == short_uuid).values(traffic_used_bytes=0)
        )
        await db.execute(
            delete(TrafficCursor).where(TrafficCursor.owner == short_uuid)
        )
        for node, container_id, total in observed:
            stmt = pg_insert(TrafficCursor).values(
                node=node,
                container_id=container_id,
                owner=short_uuid,
                last_total=total,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[TrafficCursor.node, TrafficCursor.container_id],
                set_={"last_total": total, "owner": short_uuid},
            )
            await db.execute(stmt)
        await db.commit()

class UserDB:
    @staticmethod
    async def add(db: AsyncSession, data: UserSchema) -> User:
        user = User(**data.model_dump())

        db.add(user)
        await db.commit()
        await db.refresh(user)

        return user

    @staticmethod
    async def get(db: AsyncSession, short_uuid: str) -> UserSchema:
        result = await db.execute(select(User).where(User.short_uuid == short_uuid))
        user = result.scalar_one_or_none()
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )
        
        return UserSchema.model_validate(user)

    @staticmethod
    async def update(db: AsyncSession, user: UserSchema) -> bool:
        await db.execute(
            update(User)
            .where(User.short_uuid == user.short_uuid)
            .values(**user.model_dump(exclude={"short_uuid"}))
        )
        await db.commit()

        return True
        
    @staticmethod
    async def delete(db: AsyncSession, short_uuid: str) -> bool:
        _= await db.execute(delete(User).where(User.short_uuid == short_uuid))
        await db.commit()

        return True

    @staticmethod
    async def get_all(db: AsyncSession) -> list[UserSchema]:
        result = await db.execute(select(User))
        users: Sequence[User] = result.scalars().all()
        if users is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Users not found",
            )

        return [UserSchema.model_validate(user) for user in users]

    @staticmethod
    async def exists(db: AsyncSession, short_uuid: str) -> bool:
        result = await db.execute(
            sa_exists(select(User).where(User.short_uuid == short_uuid)).select()
        )
        return result.scalar_one()

    @staticmethod
    async def update_traffic_used(db: AsyncSession, short_uuid: str, delta: int) -> None:
        await db.execute(
            update(User)
            .where(User.short_uuid == short_uuid)
            .values(traffic_used_bytes=User.traffic_used_bytes + delta)
        )
        await db.commit()

    @staticmethod
    async def set_traffic_limit(db: AsyncSession, short_uuid: str, limit: int) -> None:
        await db.execute(
            update(User).where(User.short_uuid == short_uuid).values(traffic_limit_bytes=limit)
        )
        await db.commit()

    @staticmethod
    async def reset_traffic(db: AsyncSession, short_uuid: str) -> None:
        """Zero the aggregate AND the baseline cursors in one transaction.

        The user-facing rebase path is TrafficManager.reset_user_accounting
        (per-user lock + observed-total baselines); this statement shape is
        the DB-level fallback used with a plain session. Resetting
        traffic_used_bytes alone would leave durable (node, container_id)
        baselines high: the next tick would replay the whole old counter as
        a delta and double-count history. Both sides are cleared together.
        """
        await db.execute(
            update(User).where(User.short_uuid == short_uuid).values(traffic_used_bytes=0)
        )
        await db.execute(
            delete(TrafficCursor).where(TrafficCursor.owner == short_uuid)
        )
        await db.commit()