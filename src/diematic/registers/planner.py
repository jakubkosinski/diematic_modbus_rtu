"""Groups registers into contiguous read blocks."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from diematic.registers.model import RegisterDef


@dataclass(frozen=True, slots=True)
class Block:
    start: int
    count: int
    registers: tuple[RegisterDef, ...]

    @property
    def end(self) -> int:
        return self.start + self.count

    def split(self) -> list[Block]:
        """Break into blocks that contain no unmapped gap registers."""
        return plan_blocks(self.registers, max_block=self.count, max_gap=0)


def plan_blocks(
    registers: Iterable[RegisterDef], max_block: int = 40, max_gap: int = 0
) -> list[Block]:
    """Merge registers into blocks of at most `max_block` words.

    Neighbouring registers are joined when fewer than or exactly `max_gap` unmapped words lie
    between them. Reading unmapped words can fail on some gateways, hence the 0 default; callers
    fall back to `Block.split()` when a merged block is rejected.
    """
    ordered = sorted(registers, key=lambda r: r.address)
    blocks: list[Block] = []
    current: list[RegisterDef] = []
    for register in ordered:
        if current:
            gap = register.address - (current[-1].address + current[-1].width)
            span = register.address + register.width - current[0].address
            if gap <= max_gap and span <= max_block:
                current.append(register)
                continue
            blocks.append(_close(current))
            current = []
        current.append(register)
    if current:
        blocks.append(_close(current))
    return blocks


def _close(registers: list[RegisterDef]) -> Block:
    start = registers[0].address
    return Block(start, registers[-1].address + registers[-1].width - start, tuple(registers))
