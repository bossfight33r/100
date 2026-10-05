"""Стратегии расчёта дохода. Итог всегда считается из сохранённой статистики, не хранится сам по себе."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol, runtime_checkable

from clipfactory.schemas import Campaign, Publication, StatsSnapshot

CENT = Decimal("0.01")


@dataclass(frozen=True)
class Earning:
    publication_id: str
    views: int
    amount: Decimal


@runtime_checkable
class EarningsStrategy(Protocol):
    name: str

    def compute(
        self, campaign: Campaign, publication: Publication, latest: StatsSnapshot | None
    ) -> Earning: ...


class FlatRatePerK:
    """earnings = views / 1000 × rate_per_1k_views."""

    name = "flat_per_1k"

    def compute(
        self, campaign: Campaign, publication: Publication, latest: StatsSnapshot | None
    ) -> Earning:
        views = latest.views if latest else 0
        rate = Decimal(str(campaign.rate_per_1k_views))
        amount = (Decimal(views) / Decimal(1000) * rate).quantize(CENT, rounding=ROUND_HALF_UP)
        return Earning(publication_id=publication.id, views=views, amount=amount)


STRATEGIES: dict[str, EarningsStrategy] = {FlatRatePerK.name: FlatRatePerK()}


def strategy_for(campaign: Campaign) -> EarningsStrategy:
    """Точка расширения: пороги, бонусы, лимиты, ставки по платформам, даты кампании."""
    return STRATEGIES[FlatRatePerK.name]
