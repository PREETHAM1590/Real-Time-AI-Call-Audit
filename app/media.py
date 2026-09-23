"""Provider-independent helpers for live transcript finalisation."""


class FinalBuffer:
    """Keep replaceable partials and immutable finals in caller-supplied order.

    Callers apply segments in canonical transcript order; timestamp sorting
    belongs at the persistence boundary where those timestamps are available.
    """

    def __init__(self) -> None:
        self.partials: dict[str, str] = {}
        self.finals: dict[str, str] = {}

    def apply(self, segment_id: str, text: str, final: bool) -> None:
        if segment_id in self.finals:
            return
        if final:
            self.finals[segment_id] = text
            self.partials.pop(segment_id, None)
        else:
            self.partials[segment_id] = text

    def final_text(self) -> str:
        """Join stable finals in the order they were first finalized."""
        return " ".join(self.finals.values())
