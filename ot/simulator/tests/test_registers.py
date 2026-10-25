from ot_simulator.registers import WordOrder, decode, encode, to_raw


def test_uint32_word_orders_from_the_register_map():
    assert encode("uint32", 100000, WordOrder.ABCD) == [0x0001, 0x86A0]
    assert encode("uint32", 100000, WordOrder.CDAB) == [0x86A0, 0x0001]


def test_reading_cdab_as_abcd_gives_the_absurd_value_from_the_doc():
    regs = encode("uint32", 100000, WordOrder.CDAB)
    assert decode("uint32", regs, WordOrder.ABCD) == 2_258_632_705


def test_float32_example_from_the_register_map():
    assert encode("float32", 120.5, WordOrder.ABCD) == [0x42F1, 0x0000]
    assert decode("float32", [0x42F1, 0x0000], WordOrder.ABCD) == 120.5


def test_int16_negative_is_twos_complement():
    regs = encode("int16", to_raw("int16", -5.5, 0.1), WordOrder.ABCD)
    assert regs == [0xFFC9]
    assert decode("int16", regs, WordOrder.ABCD) == -55


def test_scaling_and_clipping():
    assert to_raw("uint16", 5.23, 0.01) == 523
    assert to_raw("uint16", 9999, 0.01) == 0xFFFF
    assert to_raw("uint16", -3, 0.01) == 0


def test_round_trip_all_types():
    for dtype, raw in (("uint16", 4321), ("int16", -4321), ("uint32", 3_000_000_000), ("float32", 0.5)):
        for order in WordOrder:
            assert decode(dtype, encode(dtype, raw, order), order) == raw
