import pytest

from diematic.registers.codecs import (
    BitsCodec,
    CodecError,
    EnumCodec,
    TempCodec,
    TimeProgramCodec,
    UintCodec,
)


class TestTemp:
    codec = TempCodec()

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (0x0001, 0.1),
            (0x8001, -0.1),
            (223, 22.3),
            (0x8000 | 125, -12.5),
            (0, 0.0),
            (0x8000, 0.0),
        ],
    )
    def test_decode(self, raw, expected):
        assert self.codec.decode([raw]) == expected

    def test_unavailable_marker_decodes_to_none(self):
        assert self.codec.decode([0xFFFF]) is None

    @pytest.mark.parametrize("value", [0.1, -0.1, 22.3, -12.5, 0.0, 90.0, 1500 / 10])
    def test_round_trip(self, value):
        assert self.codec.decode(self.codec.encode(value)) == value

    def test_encode_negative_sets_sign_bit(self):
        assert self.codec.encode(-0.1) == [0x8001]

    def test_encode_out_of_range(self):
        with pytest.raises(CodecError):
            self.codec.encode(4000.0)

    @pytest.mark.parametrize("value", ["22", None, True, float("nan")])
    def test_encode_rejects_non_numbers(self, value):
        with pytest.raises(CodecError):
            self.codec.encode(value)


class TestUint:
    def test_plain(self):
        codec = UintCodec()
        assert codec.decode([42]) == 42
        assert codec.encode(42) == [42]

    def test_scale_and_invalid(self):
        codec = UintCodec(scale=0.1, invalid=frozenset({0xFFFF}))
        assert codec.decode([123]) == 12.3
        assert codec.decode([0xFFFF]) is None
        assert codec.encode(12.3) == [123]

    @pytest.mark.parametrize("value", [-1, 70000])
    def test_out_of_range(self, value):
        with pytest.raises(CodecError):
            UintCodec().encode(value)


class TestEnum:
    codec = EnumCodec({0: "off", 2: "night", 4: "day"})

    def test_decode_known_and_unknown(self):
        assert self.codec.decode([2]) == "night"
        assert self.codec.decode([3]) is None

    def test_encode(self):
        assert self.codec.encode("day") == [4]

    def test_encode_unknown_label(self):
        with pytest.raises(CodecError):
            self.codec.encode("sunrise")


class TestBits:
    codec = BitsCodec({"antifreeze": 0, "night": 1, "day": 2, "auto": 3})

    def test_decode(self):
        assert self.codec.decode([0b1010]) == {
            "antifreeze": False,
            "night": True,
            "day": False,
            "auto": True,
        }

    def test_apply_changes_only_named_bits(self):
        assert self.codec.apply(0b1010_0000, {"auto": True, "night": True}) == 0b1010_1010
        assert self.codec.apply(0b1111, {"day": False}) == 0b1011

    def test_unknown_flag(self):
        with pytest.raises(CodecError):
            self.codec.apply(0, {"turbo": True})


class TestTimeProgram:
    codec = TimeProgramCodec()

    def test_documented_example(self):
        # Manual example: 0x0003 0xFFFF 0xFF00 -> day from 07:00 to 20:00
        assert self.codec.decode([0x0003, 0xFFFF, 0xFF00]) == "07:00-20:00"
        assert self.codec.encode("07:00-20:00") == [0x0003, 0xFFFF, 0xFF00]

    def test_all_night_and_all_day(self):
        assert self.codec.decode([0, 0, 0]) == ""
        assert self.codec.encode("") == [0, 0, 0]
        assert self.codec.decode([0xFFFF] * 3) == "00:00-24:00"
        assert self.codec.encode("00:00-24:00") == [0xFFFF] * 3

    def test_multiple_periods_round_trip(self):
        text = "05:30-08:00,16:00-22:30"
        assert self.codec.decode(self.codec.encode(text)) == text

    @pytest.mark.parametrize(
        "text", ["07:10-08:00", "08:00-07:00", "7-8", "25:00-26:00", "07:00-07:00"]
    )
    def test_invalid_periods(self, text):
        with pytest.raises(CodecError):
            self.codec.encode(text)


class TestMask:
    def test_enum_ignores_the_masked_out_bits(self):
        codec = EnumCodec({0: "P1", 1: "P2", 2: "P3", 3: "P4"}, mask=3)
        assert codec.decode([8192]) == "P1"
        assert codec.decode([8193]) == "P2"
        assert codec.decode([0x2003]) == "P4"

    def test_without_a_mask_the_whole_word_counts(self):
        codec = EnumCodec({0: "P1"})
        assert codec.decode([8192]) is None

    def test_uint_applies_the_mask_after_the_unavailable_marker(self):
        codec = UintCodec(mask=0x00FF, invalid=frozenset({0xFFFF}))
        assert codec.decode([0x1234]) == 0x34
        assert codec.decode([0xFFFF]) is None

    def test_encoding_is_not_affected_by_the_mask(self):
        assert EnumCodec({2: "P3"}, mask=3).encode("P3") == [2]
