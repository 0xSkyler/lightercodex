"""Synchronous transitions are atomic within the single asyncio event loop."""

from enum import StrEnum


class State(StrEnum):
    STARTING = "STARTING"
    SYNCING = "SYNCING"
    FLAT = "FLAT"
    ENTRY_PENDING = "ENTRY_PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    OPEN_LONG = "OPEN_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    EXIT_PENDING = "EXIT_PENDING"
    PARTIAL_EXIT = "PARTIAL_EXIT"
    RECOVERY = "RECOVERY"
    HALTED = "HALTED"


class StateMachine:
    def __init__(self) -> None:
        self.state = State.STARTING
        self.entries_disabled = False

    def transition(self, target: State) -> None:
        common = {State.RECOVERY, State.HALTED}
        allowed = {
            State.STARTING: {State.SYNCING},
            State.SYNCING: {State.FLAT, State.OPEN_LONG, State.OPEN_SHORT},
            State.FLAT: {State.ENTRY_PENDING, State.SYNCING},
            State.ENTRY_PENDING: {
                State.PARTIALLY_FILLED,
                State.OPEN_LONG,
                State.OPEN_SHORT,
                State.FLAT,
            },
            State.PARTIALLY_FILLED: {
                State.OPEN_LONG,
                State.OPEN_SHORT,
                State.EXIT_PENDING,
                State.FLAT,
            },
            State.OPEN_LONG: {State.EXIT_PENDING, State.PARTIAL_EXIT, State.FLAT},
            State.OPEN_SHORT: {State.EXIT_PENDING, State.PARTIAL_EXIT, State.FLAT},
            State.EXIT_PENDING: {State.PARTIAL_EXIT, State.OPEN_LONG, State.OPEN_SHORT, State.FLAT},
            State.PARTIAL_EXIT: {State.EXIT_PENDING, State.OPEN_LONG, State.OPEN_SHORT, State.FLAT},
            State.RECOVERY: {
                State.SYNCING,
                State.FLAT,
                State.OPEN_LONG,
                State.OPEN_SHORT,
                State.EXIT_PENDING,
            },
            State.HALTED: {State.RECOVERY},
        }
        if target == self.state:
            return
        if target not in allowed[self.state] | common:
            raise RuntimeError(f"Invalid transition {self.state} -> {target}")
        self.state = target
        if target == State.HALTED:
            self.entries_disabled = True

    def begin_entry(self) -> bool:
        if self.state != State.FLAT or self.entries_disabled:
            return False
        self.transition(State.ENTRY_PENDING)
        return True
