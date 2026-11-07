"""O spool vive no PostgreSQL, por isso os testes precisam de um. Cada teste recebe um esquema novo e
isolado (criado antes e apagado depois), para nunca se tocarem. Aponta INGEST_TEST_DSN a um Postgres de teste."""
import os
import uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

BASE = os.environ.get("INGEST_TEST_DSN")


@pytest.fixture
def new_dsn():
    """Uma fábrica: cada chamada devolve o endereço de um esquema novo e vazio."""
    if not BASE:
        pytest.skip("sem Postgres de teste (INGEST_TEST_DSN)")
    created = []

    def make() -> str:
        schema = "t_" + uuid.uuid4().hex[:12]
        with psycopg.connect(BASE, autocommit=True) as admin:
            admin.execute(f"CREATE SCHEMA {schema}")
        created.append(schema)
        return make_conninfo(BASE, options=f"-c search_path={schema}")

    yield make
    with psycopg.connect(BASE, autocommit=True) as admin:
        for schema in created:
            admin.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture
def dsn(new_dsn):
    return new_dsn()
