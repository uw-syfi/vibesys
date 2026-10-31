"""What a row's tokens would cost: the estimate order, over the rates the host knows.

``RecordedPricing`` is the estimate when the host holds no price list: a row's cost is the one
the provider reported or the journal recorded, and a row that has neither is unpriced. It never
guesses a price. The list-priced estimate is added beside it.

Light: ``vs_proto`` and ``server.usage_summary``.

Public surface: ``RecordedPricing``.
"""

from __future__ import annotations

from server.usage_summary import Priced
from vs_proto.api import UsageCostBasis, UsageSpeed, usage_state


class RecordedPricing:
    """Prices by the cost already known: reported, else recorded for an older turn, else none."""

    def price(self, provider: str, row: usage_state.UsageRollupRow) -> Priced:
        """The estimate of ``row``: its reported or recorded cost, or unpriced."""
        del provider
        model = row.model.strip().lower() or "unknown"
        if row.recorded_usd is not None:
            if row.basis == UsageCostBasis.PROVIDER_REPORTED:
                return Priced(model, usage_state.UsageEstimateSource.REPORTED, row.recorded_usd)
            if row.basis == UsageCostBasis.LEGACY_RECORDED:
                return Priced(model, usage_state.UsageEstimateSource.RECORDED, row.recorded_usd)
        return Priced(model, usage_state.UsageEstimateSource.UNPRICED)

    def resolved(
        self, provider: str, model: str, speed: UsageSpeed
    ) -> tuple[usage_state.UsageRate | None, usage_state.UsageEstimateSource]:
        """No model has a listed rate."""
        del provider, model, speed
        return None, usage_state.UsageEstimateSource.UNPRICED

    @property
    def pricing(self) -> usage_state.UsagePricing:
        """No price list: bundled, with no model known."""
        return usage_state.UsagePricing(status=usage_state.UsagePricingStatus.BUNDLED)

    @property
    def book(self) -> usage_state.UsagePriceBook:
        """Nothing a person set."""
        return usage_state.UsagePriceBook()
