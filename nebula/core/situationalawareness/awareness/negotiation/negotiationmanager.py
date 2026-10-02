from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from nebula.core.eventmanager import EventManager
from nebula.core.nebulaevents import RoundEndEvent

if TYPE_CHECKING:
    from nebula.core.situationalawareness.awareness.sareasoner import SAReasoner


class NegotiationState(Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    COUNTERED = "countered"
    EXPIRED = "expired"


@dataclass
class NegotiationProposal:
    """A single contract-net topology negotiation with a peer node."""

    proposal_id: str
    peer: str
    edge_a: str
    edge_b: str
    change: str  # "connect" | "disconnect"
    reason: str
    issued_round: int | None
    ttl_rounds: int = 2
    is_initiator: bool = True
    state: NegotiationState = NegotiationState.PENDING
    response: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def is_expired(self, current_round: int | None) -> bool:
        if current_round is None or self.issued_round is None:
            return False
        return (current_round - self.issued_round) > self.ttl_rounds


class NegotiationManager:
    """Inter-node coordination for topology changes (SMAC, Fase 5).

    Lightweight contract-net protocol over the existing
    ``CommunicationsManager`` transport using the ``negotiation`` protobuf
    message:

    1. Node A proposes an edge change to B (``PROPOSE``).
    2. B evaluates with its local ``SAReasoner`` and replies
       ``ACCEPT`` / ``REJECT`` (``COUNTER_PROPOSAL`` is evaluated as a new
       proposal with the same id).
    3. Only on mutual ``ACCEPT`` is the restructuring ``SACommand`` executed
       on both sides, avoiding inconsistent graph states.
    4. Proposals expire after ``timeout_rounds`` (default 2) and may be
       retried by the initiator.

    The manager is opt-in (``sa_reasoner.negotiation.enabled``). When
    disabled — or when the peer never answers (e.g. mixed-version
    federations) — the node keeps the legacy unilateral behavior.
    """

    def __init__(self, config: dict):
        self._sar: SAReasoner | None = config.get("sar")
        self._addr: str = config.get("addr", "unknown")
        self._enabled: bool = bool(config.get("enabled", False))
        self._timeout_rounds: int = int(config.get("timeout_rounds", 2))
        self._wait_timeout_s: float = float(config.get("wait_timeout_s", 5.0))
        self._verbose: bool = bool(config.get("verbose", False))
        try:
            from nebula.core.network.communications import CommunicationsManager

            self._cm = CommunicationsManager.get_instance()
        except Exception:
            self._cm = None
        self._pending: dict[str, NegotiationProposal] = {}
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled and self._cm is not None

    async def init(self):
        """Subscribe to negotiation message events and round-end expiry."""
        if not self._enabled:
            return
        from functools import partial

        em = EventManager.get_instance()
        await em.subscribe(("negotiation", "propose"), self._on_propose)
        await em.subscribe(("negotiation", "accept"), partial(self._on_response, action="accept"))
        await em.subscribe(("negotiation", "reject"), partial(self._on_response, action="reject"))
        await em.subscribe(("negotiation", "counter_proposal"), partial(self._on_response, action="counter_proposal"))
        await em.subscribe_node_event(RoundEndEvent, self._on_round_end)
        logging.info("🤝 NegotiationManager initialized (inter-node coordination enabled)")

    def _current_round(self) -> int | None:
        try:
            return self._sar.current_round if self._sar is not None else None
        except Exception:
            return None

    async def propose_topology_change(
        self, peer: str, edge_a: str, edge_b: str, change: str, reason: str = ""
    ) -> str | None:
        """Propose an edge change to a peer (fire-and-forget).

        Returns:
            str | None: Proposal id, or None when negotiation is disabled
            or the message cannot be sent.
        """
        if not self.enabled:
            return None
        if change not in ("connect", "disconnect"):
            msg = f"Unknown topology change: {change}"
            raise ValueError(msg)
        proposal = NegotiationProposal(
            proposal_id=uuid.uuid4().hex,
            peer=peer,
            edge_a=edge_a,
            edge_b=edge_b,
            change=change,
            reason=reason,
            issued_round=self._current_round(),
            ttl_rounds=self._timeout_rounds,
            is_initiator=True,
        )
        try:
            msg = self._cm.create_message(
                "negotiation",
                "propose",
                proposal_id=proposal.proposal_id,
                edge_a=edge_a,
                edge_b=edge_b,
                change=change,
                reason=reason,
                ttl_rounds=self._timeout_rounds,
                round=self._current_round() or 0,
            )
            await self._cm.send_message(peer, msg)
        except Exception as e:
            logging.warning(f"NegotiationManager | cannot send proposal to {peer}: {e}")
            return None
        async with self._lock:
            self._pending[proposal.proposal_id] = proposal
        if self._verbose:
            logging.info(f"NegotiationManager | proposed {change} {edge_a}<->{edge_b} to {peer}")
        return proposal.proposal_id

    async def wait_for_response(self, proposal_id: str, timeout_s: float | None = None) -> NegotiationState:
        """Wait (bounded) for the peer response to a proposal."""
        timeout = self._wait_timeout_s if timeout_s is None else timeout_s
        async with self._lock:
            proposal = self._pending.get(proposal_id)
        if proposal is None:
            return NegotiationState.EXPIRED
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proposal.response.wait(), timeout=timeout)
        return proposal.state

    def is_accepted(self, proposal_id: str | None) -> bool:
        """Check whether a proposal was accepted (non-blocking)."""
        if not proposal_id:
            return False
        proposal = self._pending.get(proposal_id)
        return proposal is not None and proposal.state == NegotiationState.ACCEPTED

    async def pending_proposals(self) -> list[dict[str, Any]]:
        """Serializable snapshot of pending proposals (observability)."""
        async with self._lock:
            return [
                {
                    "proposal_id": p.proposal_id,
                    "peer": p.peer,
                    "edge": [p.edge_a, p.edge_b],
                    "change": p.change,
                    "state": p.state.value,
                    "is_initiator": p.is_initiator,
                }
                for p in self._pending.values()
                if p.state == NegotiationState.PENDING
            ]

    async def _reply(self, peer: str, action: str, proposal: NegotiationProposal):
        """Send ACCEPT/REJECT/COUNTER_PROPOSAL for a proposal."""
        try:
            msg = self._cm.create_message(
                "negotiation",
                action,
                proposal_id=proposal.proposal_id,
                edge_a=proposal.edge_a,
                edge_b=proposal.edge_b,
                change=proposal.change,
                reason=proposal.reason,
                ttl_rounds=proposal.ttl_rounds,
                round=self._current_round() or 0,
            )
            await self._cm.send_message(peer, msg)
        except Exception as e:
            logging.warning(f"NegotiationManager | cannot reply {action} to {peer}: {e}")

    async def _evaluate_proposal(self, edge_a: str, edge_b: str, change: str) -> bool:
        """Evaluate an incoming proposal with the local reasoner state.

        Accept when no restructure is already running and no conflicting
        pending proposal exists; reject otherwise. Deterministic and local —
        the node's own GroqReasoningAgent may be consulted for ambiguous
        cases in future work.
        """
        try:
            sar = self._sar
            if sar is None:
                return False
            lock = sar.get_restructure_process_lock()
            if lock.locked():
                return False
        except Exception:
            return False
        async with self._lock:
            for p in self._pending.values():
                if p.state != NegotiationState.PENDING:
                    continue
                if {p.edge_a, p.edge_b} == {edge_a, edge_b} and p.change != change:
                    return False
        return True

    async def _on_propose(self, source: str, message):
        """Handle an incoming PROPOSE (or COUNTER_PROPOSAL terms)."""
        try:
            proposal = NegotiationProposal(
                proposal_id=message.proposal_id,
                peer=source,
                edge_a=message.edge_a,
                edge_b=message.edge_b,
                change=message.change,
                reason=message.reason,
                issued_round=message.round,
                ttl_rounds=message.ttl_rounds or self._timeout_rounds,
                is_initiator=False,
            )
        except Exception as e:
            logging.warning(f"NegotiationManager | malformed proposal: {e}")
            return
        accepted = await self._evaluate_proposal(proposal.edge_a, proposal.edge_b, proposal.change)
        proposal.state = NegotiationState.ACCEPTED if accepted else NegotiationState.REJECTED
        async with self._lock:
            self._pending[proposal.proposal_id] = proposal
        if self._cm is not None:
            await self._reply(source, "accept" if accepted else "reject", proposal)
        if self._verbose:
            logging.info(
                f"NegotiationManager | {'accepted' if accepted else 'rejected'} "
                f"{proposal.change} {proposal.edge_a}<->{proposal.edge_b} from {source}"
            )

    async def _on_response(self, source: str, message, action: str):
        """Handle ACCEPT/REJECT/COUNTER_PROPOSAL for our proposals."""
        async with self._lock:
            proposal = self._pending.get(message.proposal_id)
        if proposal is None or not proposal.is_initiator:
            return
        if action == "accept":
            proposal.state = NegotiationState.ACCEPTED
        elif action == "reject":
            proposal.state = NegotiationState.REJECTED
        elif action == "counter_proposal":
            proposal.state = NegotiationState.COUNTERED
            proposal.edge_a, proposal.edge_b, proposal.change = message.edge_a, message.edge_b, message.change
        proposal.response.set()
        if self._verbose:
            logging.info(f"NegotiationManager | proposal {proposal.proposal_id} {action} by {source}")

    async def _on_round_end(self, event):
        """Expire proposals older than the negotiation timeout."""
        current = self._current_round()
        if current is None:
            return
        async with self._lock:
            for proposal in self._pending.values():
                if proposal.state == NegotiationState.PENDING and proposal.is_expired(current):
                    proposal.state = NegotiationState.EXPIRED
                    proposal.response.set()

    async def stop(self):
        async with self._lock:
            for proposal in self._pending.values():
                proposal.response.set()
            self._pending.clear()
