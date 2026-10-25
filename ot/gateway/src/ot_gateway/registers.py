"""Descodificação de registos Modbus (independente do simulador: o contrato é o mapa de registos)."""
import struct

SIZES = {"uint16": 1, "int16": 1, "uint32": 2, "float32": 2}


def decode(dtype: str, regs: list[int], word_order: str):
    if dtype in ("uint16", "int16"):
        value = regs[0]
        return value - 0x10000 if dtype == "int16" and value >= 0x8000 else value
    high, low = (regs[0], regs[1]) if word_order == "ABCD" else (regs[1], regs[0])
    word = (high << 16) | low
    if dtype == "uint32":
        return word
    if dtype == "float32":
        return struct.unpack(">f", struct.pack(">I", word))[0]
    raise ValueError(dtype)
