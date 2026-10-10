from diematic.registers.model import RegisterDef
from diematic.registers.planner import plan_blocks


def reg(address, type_="uint"):
    return RegisterDef(key=f"r{address}", address=address, type=type_, kind="measure", name="x")


def spans(blocks):
    return [(b.start, b.count) for b in blocks]


def test_contiguous_registers_merge():
    assert spans(plan_blocks([reg(10), reg(11), reg(12)])) == [(10, 3)]


def test_gap_splits_by_default():
    assert spans(plan_blocks([reg(10), reg(12)])) == [(10, 1), (12, 1)]


def test_gap_is_bridged_up_to_max_gap():
    assert spans(plan_blocks([reg(10), reg(13), reg(20)], max_gap=2)) == [(10, 4), (20, 1)]


def test_block_size_is_capped():
    blocks = plan_blocks([reg(a) for a in range(100, 110)], max_block=4)
    assert spans(blocks) == [(100, 4), (104, 4), (108, 2)]


def test_input_order_does_not_matter():
    assert spans(plan_blocks([reg(12), reg(10), reg(11)])) == [(10, 3)]


def test_multi_word_registers_count_their_width():
    program = RegisterDef(key="p", address=126, type="time_program", kind="parameter", name="p")
    blocks = plan_blocks([program, reg(129)])
    assert spans(blocks) == [(126, 4)]


def test_split_removes_gaps():
    block = plan_blocks([reg(10), reg(13), reg(14)], max_gap=5)[0]
    assert spans(block.split()) == [(10, 1), (13, 2)]


def test_empty_input():
    assert plan_blocks([]) == []
