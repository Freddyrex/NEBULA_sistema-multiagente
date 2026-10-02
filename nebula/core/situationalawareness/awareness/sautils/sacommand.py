import asyncio
from abc import abstractmethod
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nebula.core.situationalawareness.awareness.sautils.samoduleagent import SAModuleAgent


class SACommandType(Enum):
    CONNECTIVITY = "Connectivity"
    AGGREGATION = "Aggregation"
    TRAINING = "Training"
    COGNITIVE = "Cognitive"


# TODO make differents parts
class SACommandAction(Enum):
    IDLE = "idle"
    DISCONNECT = "disconnect"
    RECONNECT = "reconnect"
    SEARCH_CONNECTIONS = "search_connections"
    MAINTAIN_CONNECTIONS = "maintain_connections"
    ADJUST_WEIGHT = "adjust_weight"
    DISCARD_UPDATE = "discard_update"
    # --- SMAC (cognitive multi-agent system) actions ---
    ADJUST_TRAINING_POLICY = "adjust_training_policy"
    REQUEST_TOPOLOGY_CHANGE = "request_topology_change"
    SELECT_AGGREGATOR = "select_aggregator"
    FLAG_FOR_REVIEW = "flag_for_human_review"
    PAUSE_LOCAL_TRAINING = "pause_local_training"
    REDUCE_BATCH_SIZE = "reduce_batch_size"
    EXCLUDE_NODE = "exclude_node"


class SACommandPRIO(Enum):
    CRITICAL = 20
    HIGH = 10
    MEDIUM = 5
    LOW = 3
    MAINTENANCE = 1


class SACommandState(Enum):
    PENDING = "pending"
    DISCARDED = "discarded"
    EXECUTED = "executed"

    """                                             ###############################
                                                    #      SA COMMAND CLASS       #
                                                    ###############################
    """


class SACommand:
    """
    Base class for Situational Awareness (SA) module commands.

    This class defines the core structure and behavior of commands that can be
    issued by SA agents. Each command has an associated type, action, target,
    priority, and execution state. Commands may also declare whether they can be
    executed in parallel. Subclasses must implement the actual logic for execution
    and conflict detection.

    Attributes:
        command_type (SACommandType): Type of the command (e.g., parameter update, structural change).
        action (SACommandAction): Specific action the command performs.
        owner (SAModuleAgent): Reference to the module or agent that issued the command.
        target (Any): Target of the command (e.g., node, parameter name).
        priority (SACommandPRIO): Priority level of the command.
        parallelizable (bool): Indicates whether the command can be run concurrently.
        confidence (float): Confidence of the issuer in [0, 1]. Advisory commands
            (e.g., from the LLM cognitive layer) use values < 1; deterministic
            agents default to 1.0.
        explanation (str): Short human-readable rationale (used for auditability
            in the dashboard and decision log).
        ttl_rounds (int | None): Number of FL rounds after being issued during
            which the command stays valid. None means it does not expire.
        _state (SACommandState): Internal state of the command (e.g., PENDING, DISCARDED).
        _state_future (asyncio.Future): Future that resolves when the command changes state.
    """

    def __init__(
        self,
        command_type: SACommandType,
        action: SACommandAction,
        owner: "SAModuleAgent",
        target,
        priority: SACommandPRIO = SACommandPRIO.MEDIUM,
        parallelizable=False,
        confidence: float = 1.0,
        explanation: str = "",
        ttl_rounds: int | None = None,
    ):
        self._command_type = command_type
        self._action = action
        self._owner = owner
        self._target = target  # Could be a node, parameter, etc.
        self._priority = priority
        self._parallelizable = parallelizable
        self._confidence = max(0.0, min(1.0, float(confidence)))
        self._explanation = explanation or ""
        self._ttl_rounds = ttl_rounds
        self._issued_round: int | None = None
        self._state = SACommandState.PENDING
        self._state_future = asyncio.get_event_loop().create_future()

    @abstractmethod
    async def execute(self):
        """
        Execute the command's action on the specified target.

        This method must be implemented by subclasses to define the actual logic
        of how the command affects the system. It may involve sending messages,
        modifying local or global state, or interacting with external components.
        """
        raise NotImplementedError

    @abstractmethod
    async def conflicts_with(self, other: "SACommand") -> bool:
        """
        Determine whether this command conflicts with another command.

        This method must be implemented by subclasses to define conflict logic,
        e.g., whether two commands target the same resource in incompatible ways.
        Used during arbitration to resolve simultaneous command suggestions.

        Parameters:
            other (SACommand): Another command instance to check for conflicts.

        Returns:
            bool: True if there is a conflict, False otherwise.
        """
        raise NotImplementedError

    async def discard_command(self):
        """
        Mark this command as discarded, updating its internal state accordingly.
        """
        await self._update_command_state(SACommandState.DISCARDED)

    def got_higher_priority_than(self, other_prio: SACommandPRIO):
        """
        Compare this command's priority against another.

        Args:
            other_prio (SACommandPRIO): The priority of the other command.

        Returns:
            bool: True if this command has higher priority, False otherwise.
        """
        return self._priority.value > other_prio.value

    def get_prio(self):
        """
        Retrieve the priority level of this command.

        Returns:
            SACommandPRIO: The command's priority enum.
        """
        return self._priority

    async def get_owner(self):
        """
        Asynchronously obtain the owner agent of this command.

        Returns:
            Agent: The agent instance that owns this command.
        """
        return await self._owner.get_agent()

    def get_action(self) -> SACommandAction:
        """
        Get the action associated with this command.

        Returns:
            SACommandAction: The command's action enum.
        """
        return self._action

    async def _update_command_state(self, sacs: SACommandState):
        """
        Update the command's state and resolve its completion future if pending.

        Args:
            sacs (SACommandState): The new state to assign to the command.
        """
        self._state = sacs
        if not self._state_future.done():
            self._state_future.set_result(sacs)

    def get_state_future(self) -> asyncio.Future:
        """
        Get the Future that will be completed when the command's state changes.

        Returns:
            asyncio.Future: Future that resolves to the command's final state.
        """
        return self._state_future

    def is_parallelizable(self):
        """
        Indicates whether this command can be executed in parallel with others.

        Returns:
            bool: True if parallel execution is allowed, False otherwise.
        """
        return self._parallelizable

    def get_confidence(self) -> float:
        """
        Confidence of the issuer in this command, in [0, 1].

        Returns:
            float: Confidence value (1.0 for deterministic agents).
        """
        return self._confidence

    def get_explanation(self) -> str:
        """
        Short human-readable rationale for this command (auditability).

        Returns:
            str: Explanation text (may be empty for heuristic commands).
        """
        return self._explanation

    def get_ttl_rounds(self) -> int | None:
        """
        Number of FL rounds this command stays valid after being issued.

        Returns:
            int | None: TTL in rounds, or None if it does not expire.
        """
        return self._ttl_rounds

    def set_issued_round(self, round_number: int):
        """Record the FL round in which this command was issued."""
        self._issued_round = round_number

    def is_expired(self, current_round: int | None) -> bool:
        """Check whether the command TTL has elapsed.

        Args:
            current_round (int | None): Current FL round.

        Returns:
            bool: True if the command expired and must not be executed.
        """
        if self._ttl_rounds is None or current_round is None or self._issued_round is None:
            return False
        return (current_round - self._issued_round) > self._ttl_rounds

    def get_command_type(self) -> SACommandType:
        """
        Get the type of this command.

        Returns:
            SACommandType: The command's type.
        """
        return self._command_type

    def to_dict(self) -> dict:
        """Serialize the command metadata (for prompts, logs and dashboard)."""
        return {
            "type": self._command_type.value,
            "action": self._action.value,
            "target": str(self._target),
            "priority": self._priority.value,
            "confidence": self._confidence,
            "explanation": self._explanation,
        }

    @classmethod
    def from_llm_decision(cls, decision: dict, owner: "SAModuleAgent") -> "SACommand | None":
        """Build an SACommand from a structured LLM decision (SMAC cognitive layer).

        The decision must follow the SMAC schema with keys ``decision`` (one of
        ``no_action``, ``adjust_training_policy``, ``request_topology_change``,
        ``select_aggregator``, ``flag_for_human_review``), ``confidence``,
        ``explanation`` and optional ``target_agent``, ``parameters`` and
        ``priority``.

        Args:
            decision (dict): Structured decision produced by the LLM.
            owner (SAModuleAgent): Agent wrapper owning the resulting command.

        Returns:
            SACommand | None: The command, or None for ``no_action``.
        """
        action_name = str(decision.get("decision", "no_action"))
        if action_name == "no_action":
            return None
        mapping = {
            "adjust_training_policy": (SACommandType.TRAINING, SACommandAction.ADJUST_TRAINING_POLICY),
            "request_topology_change": (SACommandType.CONNECTIVITY, SACommandAction.REQUEST_TOPOLOGY_CHANGE),
            "select_aggregator": (SACommandType.AGGREGATION, SACommandAction.SELECT_AGGREGATOR),
            "flag_for_human_review": (SACommandType.COGNITIVE, SACommandAction.FLAG_FOR_REVIEW),
        }
        if action_name not in mapping:
            raise ValueError(f"Unknown LLM decision: {action_name}")
        command_type, action = mapping[action_name]
        priority_name = str(decision.get("priority", "LOW")).upper()
        priority = SACommandPRIO.__members__.get(priority_name, SACommandPRIO.LOW)
        target = decision.get("parameters", {}) or {}
        if decision.get("target_agent"):
            target = {"target_agent": decision["target_agent"], **target} if isinstance(target, dict) else target
        return CognitiveCommand(
            action=action,
            owner=owner,
            target=target,
            priority=priority,
            parallelizable=True,
            confidence=decision.get("confidence", 0.5),
            explanation=decision.get("explanation", "")[:500],
        )

    def __repr__(self):
        return (
            f"{self.__class__.__name__}(Type={self._command_type.value}, "
            f"Action={self._action.value}, Target={self._target}, Priority={self._priority.value})"
        )

    """                                             ###############################
                                                    #     SA COMMAND SUBCLASS     #
                                                    ###############################
    """


class ConnectivityCommand(SACommand):
    """Commands related to connectivity."""

    def __init__(
        self,
        action: SACommandAction,
        owner: "SAModuleAgent",
        target: str,
        priority: SACommandPRIO = SACommandPRIO.MEDIUM,
        parallelizable=False,
        action_function=None,
        *args,
        confidence: float = 1.0,
        explanation: str = "",
        ttl_rounds: int | None = None,
    ):
        super().__init__(
            SACommandType.CONNECTIVITY, action, owner, target, priority, parallelizable,
            confidence, explanation, ttl_rounds,
        )
        self._action_function = action_function
        self._args = args

    async def execute(self):
        """Executes the assigned action function with the given parameters."""
        await self._update_command_state(SACommandState.EXECUTED)
        if self._action_function:
            if asyncio.iscoroutinefunction(self._action_function):
                await self._action_function(*self._args)
            else:
                self._action_function(*self._args)

    async def conflicts_with(self, other: "ConnectivityCommand") -> bool:
        """Determines if two commands conflict with each other."""
        if await self._owner.get_agent() == await other._owner.get_agent():
            return False

        if self._target == other._target:
            conflict_pairs = [
                {SACommandAction.DISCONNECT, SACommandAction.DISCONNECT},
            ]
            return {self._action, other._action} in conflict_pairs
        else:
            conflict_pairs = [
                {SACommandAction.DISCONNECT, SACommandAction.RECONNECT},
                {SACommandAction.DISCONNECT, SACommandAction.MAINTAIN_CONNECTIONS},
                {SACommandAction.DISCONNECT, SACommandAction.SEARCH_CONNECTIONS},
            ]
            return {self._action, other._action} in conflict_pairs


class AggregationCommand(SACommand):
    """Commands related to data aggregation."""

    def __init__(
        self,
        action: SACommandAction,
        owner: "SAModuleAgent",
        target: dict,
        priority: SACommandPRIO = SACommandPRIO.MEDIUM,
        parallelizable=False,
        confidence: float = 1.0,
        explanation: str = "",
        ttl_rounds: int | None = None,
    ):
        super().__init__(
            SACommandType.CONNECTIVITY, action, owner, target, priority, parallelizable,
            confidence, explanation, ttl_rounds,
        )

    async def execute(self):
        await self._update_command_state(SACommandState.EXECUTED)
        return self._target

    async def conflicts_with(self, other: "AggregationCommand") -> bool:
        """Determines if two commands conflict with each other."""
        topologic_conflict = False
        weight_conflict = False

        if set(self._target.keys()) != set(other._target.keys()):
            topologic_conflict = True

        weight_conflict = any(
            abs(self._target[node][1] - other._target[node][1]) > 0
            for node in self._target.keys()
            if node in other._target.keys()
        )

        return weight_conflict and topologic_conflict

    """                                             ###############################
                                                    #     SA COMMAND FACTORY      #
                                                    ###############################
    """


class TrainingCommand(SACommand):
    """Commands that adjust the local training strategy (SMAC)."""

    def __init__(
        self,
        action: SACommandAction,
        owner: "SAModuleAgent",
        target: dict,
        priority: SACommandPRIO = SACommandPRIO.MEDIUM,
        parallelizable=True,
        action_function=None,
        confidence: float = 1.0,
        explanation: str = "",
        ttl_rounds: int | None = None,
        *args,
    ):
        super().__init__(
            SACommandType.TRAINING,
            action,
            owner,
            target,
            priority,
            parallelizable,
            confidence,
            explanation,
            ttl_rounds,
        )
        self._action_function = action_function
        self._args = args

    async def execute(self):
        """Executes the assigned action function with the given parameters."""
        await self._update_command_state(SACommandState.EXECUTED)
        if self._action_function:
            if asyncio.iscoroutinefunction(self._action_function):
                await self._action_function(*self._args)
            else:
                self._action_function(*self._args)

    async def conflicts_with(self, other: "TrainingCommand") -> bool:
        """Two training commands conflict when they target the same knob differently."""
        if await self._owner.get_agent() == await other._owner.get_agent():
            return False
        if self._action == other._action:
            return self._target != other._target
        conflicting = {
            SACommandAction.PAUSE_LOCAL_TRAINING,
            SACommandAction.ADJUST_TRAINING_POLICY,
            SACommandAction.REDUCE_BATCH_SIZE,
        }
        return self._action in conflicting and other._action in conflicting


class CognitiveCommand(SACommand):
    """Advisory commands emitted by the SMAC cognitive layer (LLM or learners)."""

    def __init__(
        self,
        action: SACommandAction,
        owner: "SAModuleAgent",
        target: dict,
        priority: SACommandPRIO = SACommandPRIO.LOW,
        parallelizable=True,
        action_function=None,
        confidence: float = 0.5,
        explanation: str = "",
        ttl_rounds: int | None = 3,
        *args,
    ):
        super().__init__(
            SACommandType.COGNITIVE,
            action,
            owner,
            target,
            priority,
            parallelizable,
            confidence,
            explanation,
            ttl_rounds,
        )
        self._action_function = action_function
        self._args = args

    async def execute(self):
        """Executes the advisory action (no-op when no function is attached)."""
        await self._update_command_state(SACommandState.EXECUTED)
        if self._action_function:
            if asyncio.iscoroutinefunction(self._action_function):
                return await self._action_function(*self._args)
            else:
                return self._action_function(*self._args)
        return self._target

    async def conflicts_with(self, other: "CognitiveCommand") -> bool:
        """Cognitive advisories conflict only on the same action with different targets."""
        if await self._owner.get_agent() == await other._owner.get_agent():
            return False
        return self._action == other._action and self._target != other._target


def factory_sa_command(sacommand_type, *config) -> SACommand:
    """
    Factory function to create situational awareness command instances.

    Args:
        sacommand_type (str): Identifier of the command type (e.g., "connectivity", "aggregation").
        *config: Positional arguments to initialize the specific command class.

    Returns:
        SACommand: An instance of the requested situational awareness command.

    Raises:
        ValueError: If the provided command type is not recognized.
    """
    options = {
        "connectivity": ConnectivityCommand,
        "aggregation": AggregationCommand,
        "training": TrainingCommand,
        "cognitive": CognitiveCommand,
    }

    cs = options.get(sacommand_type)
    if cs is None:
        raise ValueError(f"Unknown SACommand type: {sacommand_type}")
    return cs(*config)
