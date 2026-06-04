"""
Protocol — the unit injected into a voice agent.

Generalizes the legacy `Capability` framework in three ways the design doc
spells out:

  1. Multi-tool. A protocol can own a *set* of related VAPI tools, not just
     one. Single-tool ports stay single-tool — `tools()` returns a 1-element
     list. Multi-tool protocols (e.g. the upcoming `BookAppointmentProtocol`)
     return several.
  2. Typed per-clinic config (`config_model`). Empty by default
     (`EmptyConfig`); protocols opt in by overriding the ClassVar with a
     Pydantic model. Persisted config is validated against this model on
     write and on agent build.
  3. PMS-adapter dependency (`requires`). A typed slot for the adapter class
     the protocol talks to. In step 2 the slot is reserved for forward
     compat — the runtime gate stays `supported_pms`. The switch to
     adapter-typing happens when we have a second adapter to validate the
     contract against.

The `__init__` signature is back-compatible with the legacy `Capability`
constructor used by the hypervisor factory — `clinic_id`, `clinic_name`,
`pms_type`, `credential_id` — plus an optional `config` for protocols that
declare one. Legacy single-tool protocols implement `to_vapi_tool()` (a
shim that returns the single dict) for code paths that still expect the
old method; `tools()` is the new canonical accessor.
"""
from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

# Forward-ref-only import to avoid a circular dep at module load:
# api.voice_agent.pms imports nothing from protocols, but the adapter
# module pulls in httpx and BQ stacks we don't want at protocol-import time.
from api.voice_agent.pms import PMSAdapter


class EmptyConfig(BaseModel):
    """Default config model for protocols with no per-clinic knobs.

    Pydantic-strict: extra fields rejected. A protocol that stores no
    config still gets a validated `config` attribute (an empty model
    instance) so downstream code can rely on `proto.config` being defined.
    """

    model_config = {"extra": "forbid"}


class Protocol:
    """Base class for voice-agent protocols.

    Subclasses set the ClassVars and implement `tools()` + `prompt_fragment`.
    A protocol with one tool returns a 1-element list from `tools()`; the
    framework flattens across protocols when assembling the agent payload.

    PMS compatibility: `supported_pms` is the runtime gate (a tuple of
    `pms_type` strings, or `None` for PMS-agnostic). `requires` names the
    adapter the protocol expects; it's informational in step 2 and will
    become the gate in a later step once a second adapter exists.
    """

    id: ClassVar[str]
    display_name: ClassVar[str]
    description: ClassVar[str]
    # Single-tool legacy protocols set this; multi-tool protocols leave it
    # blank (their `tools()` decides the names). Useful for the toggle UI.
    agent_tool_name: ClassVar[str] = ""

    # Tuple of pms_type values this protocol supports, or None for
    # PMS-agnostic. Empty tuple = unusable. Runtime compatibility gate.
    supported_pms: ClassVar[tuple[str, ...] | None] = None

    # PMS adapter the protocol's HTTP-backed tools expect. None = no PMS
    # dependency. Reserved for the future type-based gate; ignored at
    # runtime in step 2.
    requires: ClassVar[type[PMSAdapter] | None] = None

    # Other protocol ids this protocol's prompt + tools depend on. The
    # framework treats a protocol as effective (included in the sync) only
    # when its row is enabled AND every dependency is also enabled.
    # Example: Cancel Appointment depends on Verify Caller Identification
    # and Locate Appointment — its prompt instructs the agent to look up
    # the patient + the appointment before calling cancel_appointment, so
    # enabling Cancel without those is a guaranteed runtime failure.
    depends_on: ClassVar[tuple[str, ...]] = ()

    # True = instantiated on every sync regardless of toggle state.
    # Used for foundational protocols (e.g. submit_ticket).
    always_on: ClassVar[bool] = False

    # Typed per-clinic config. Default is `EmptyConfig` (no fields).
    config_model: ClassVar[type[BaseModel]] = EmptyConfig

    def __init__(
        self,
        clinic_id: str,
        clinic_name: str,
        pms_type: str,
        credential_id: str,
        config: BaseModel | None = None,
    ):
        self.clinic_id = clinic_id
        self.clinic_name = clinic_name
        self.pms_type = pms_type or "none"
        self.credential_id = credential_id
        self.config = config if config is not None else self.config_model()

        if self.supported_pms is not None and self.pms_type not in self.supported_pms:
            raise ValueError(
                f"{type(self).__name__} does not support pms_type={self.pms_type!r} "
                f"(supported: {self.supported_pms})"
            )

    # ── Subclass surface ──────────────────────────────────────────────────────

    def tools(self) -> list[dict]:
        """VAPI tool definitions this protocol contributes. Override."""
        raise NotImplementedError

    @property
    def prompt_fragment(self) -> str:
        """Markdown injected into the assembled system prompt. Override."""
        raise NotImplementedError

    # ── Back-compat: legacy single-tool accessor ──────────────────────────────

    def to_vapi_tool(self) -> dict:
        """Return the single tool dict.

        Back-compat shim for legacy callers that expected one tool per
        Capability. Errors loudly if a multi-tool protocol gets called this
        way — that's a bug in the caller, not the protocol.
        """
        tools = self.tools()
        if len(tools) != 1:
            raise RuntimeError(
                f"{type(self).__name__}.to_vapi_tool() called on a multi-tool "
                f"protocol (got {len(tools)} tools). Use tools() instead."
            )
        return tools[0]
