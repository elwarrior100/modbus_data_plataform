import pytest

from fixture_bronze import bronze_files, generate


@pytest.fixture(scope="session")
def week():
    """7 dias de bronze: (eventos, {caminho: bytes}). Gera-se uma só vez por sessão."""
    events = generate()
    return events, bronze_files(events)
