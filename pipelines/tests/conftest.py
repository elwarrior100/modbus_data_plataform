import pytest

from fixture_bronze import bronze_files, generate


@pytest.fixture(scope="session")
def week():
    """7 dias de bronze: (eventos, {caminho: bytes}). Gera-se uma só vez por sessão."""
    events = generate()
    return events, bronze_files(events)


@pytest.fixture(scope="session")
def gold_lake():
    """O dia sintético do gold (tests/fixture_gold.py): (lago com a silver no main, eventos, horas recusadas pelo G2)."""
    from fixture_gold import make_silver_lake
    return make_silver_lake()
