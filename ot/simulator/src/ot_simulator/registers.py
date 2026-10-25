"""Codificação de valores em registos Modbus de 16 bits."""
import struct
from enum import Enum


class WordOrder(str, Enum):
    ABCD = "ABCD"  # palavra alta no registo de menor endereço
    CDAB = "CDAB"  # palavras trocadas: palavra baixa primeiro


SIZES = {"uint16": 1, "int16": 1, "uint32": 2, "float32": 2}
LIMITS = {"uint16": (0, 0xFFFF), "int16": (-0x8000, 0x7FFF), "uint32": (0, 0xFFFFFFFF)}


def n_registers(dtype: str) -> int:
    return SIZES[dtype]


def to_raw(dtype: str, value: float, scale: float):
    """Valor de engenharia -> valor bruto (inteiro, ou float para float32)."""
    if dtype == "float32":
        return float(value)
    lo, hi = LIMITS[dtype]
    return max(lo, min(hi, round(value / scale)))


def from_raw(dtype: str, raw, scale: float) -> float:
    return float(raw) if dtype == "float32" else raw * scale


def encode(dtype: str, raw, order: WordOrder) -> list[int]:
    if dtype in ("uint16", "int16"):
        return [raw & 0xFFFF]
    if dtype == "uint32":
        word = raw & 0xFFFFFFFF
    elif dtype == "float32":
        word = struct.unpack(">I", struct.pack(">f", raw))[0]
    else:
        raise ValueError(dtype)
    high, low = word >> 16, word & 0xFFFF
    return [high, low] if order is WordOrder.ABCD else [low, high]


def decode(dtype: str, regs: list[int], order: WordOrder):
    if dtype in ("uint16", "int16"):
        value = regs[0]
        return value - 0x10000 if dtype == "int16" and value >= 0x8000 else value
    high, low = (regs[0], regs[1]) if order is WordOrder.ABCD else (regs[1], regs[0])
    word = (high << 16) | low
    if dtype == "uint32":
        return word
    return struct.unpack(">f", struct.pack(">I", word))[0]
