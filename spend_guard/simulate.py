"""Dry-run a proposed spend.

"Can I run this batch of 400 renders before the end of the month" is a question
an operator should be able to ask without finding out by running it. Simulation
evaluates a sequence against real current state and writes nothing: no ledger
entry, no reservation, no side effect of any kind.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Sequence, Tuple

from .budget import UNCATEGORISED
from .guard import Decision, Overlay, SpendGuard, SpendRequest
from .money import Money

__all__ = ["SimulationStep", "SimulationResult", "simulate"]


@dataclass(frozen=True)
class SimulationStep:
    index: int
    request: SpendRequest
    decision: Decision

    @property
    def allowed(self) -> bool:
        return self.decision.allowed

    @property
    def amount(self) -> Optional[Money]:
        return self.decision.amount


@dataclass(frozen=True)
class SimulationResult:
    """What would happen, step by step, if this sequence were charged."""

    steps: Tuple[SimulationStep, ...] = ()
    total: Money = field(default_factory=Money.zero)

    @property
    def all_allowed(self) -> bool:
        return all(step.allowed for step in self.steps)

    @property
    def first_denied(self) -> Optional[SimulationStep]:
        for step in self.steps:
            if not step.allowed:
                return step
        return None

    @property
    def allowed_count(self) -> int:
        return sum(1 for step in self.steps if step.allowed)

    def to_mapping(self) -> dict:
        return {
            "all_allowed": self.all_allowed,
            "allowed_count": self.allowed_count,
            "denied_count": len(self.steps) - self.allowed_count,
            "total_if_allowed": self.total.format(),
            "steps": [step.decision.to_mapping() for step in self.steps],
        }


def simulate(
    guard: SpendGuard,
    requests: Sequence,
    *,
    now: Optional[datetime] = None,
    stop_on_denial: bool = False,
) -> SimulationResult:
    """Evaluate ``requests`` in order against current state, writing nothing.

    Each allowed step is layered onto an overlay so the next step sees the
    budget the previous one would have consumed. A denied step does not consume
    anything, so a cheap call after an expensive refusal can still be reported
    as allowed, which is the truth an operator wants when planning a batch.
    """
    state = guard.state()
    overlay = Overlay()
    steps: List[SimulationStep] = []
    total = Money.zero()

    for index, item in enumerate(requests):
        request = _coerce(item)
        decision = guard.evaluate(request, now=now, state=state, overlay=overlay)
        steps.append(SimulationStep(index=index, request=request, decision=decision))
        if decision.allowed and decision.amount is not None:
            overlay = overlay.plus(request.category, decision.amount)
            total = total + decision.amount
        elif stop_on_denial:
            break

    return SimulationResult(steps=tuple(steps), total=total)


def _coerce(item) -> SpendRequest:
    if isinstance(item, SpendRequest):
        return item
    if isinstance(item, str):
        return SpendRequest(key=item)
    if isinstance(item, dict):
        return SpendRequest(
            key=item["key"],
            units=item.get("units", 1),
            category=item.get("category", UNCATEGORISED),
            amount=item.get("amount"),
            note=item.get("note", ""),
        )
    if isinstance(item, (tuple, list)):
        key = item[0]
        units = item[1] if len(item) > 1 else 1
        category = item[2] if len(item) > 2 else UNCATEGORISED
        return SpendRequest(key=key, units=units, category=category)
    raise TypeError(f"cannot read a spend request from {type(item).__name__}")
