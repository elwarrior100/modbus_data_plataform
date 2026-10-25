"""Relógio injetável: o simulador usa o real, os testes usam um controlado à mão."""
import time


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def time(self) -> float:
        return time.time()


class FakeClock:
    def __init__(self, start_time: float = 1_800_000_000.0):
        self._mono = 0.0
        self._start = start_time

    def monotonic(self) -> float:
        return self._mono

    def time(self) -> float:
        return self._start + self._mono

    def advance(self, seconds: float) -> None:
        self._mono += seconds
