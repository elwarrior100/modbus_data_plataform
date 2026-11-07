from datetime import datetime, timezone

import pyarrow as pa
import pytest

from helpers import NOW, SEC, sample
from ingest import bronze, g1
from ingest.bronze import load_nodes
from ingest.envelope import Event, to_us

NODES = load_nodes()
BAD = 0x80310000        # BadNoCommunication


def rules(events, *, expected=None, table=None):
    table = table if table is not None else bronze.to_table(events, "b1")
    found = g1.validate(table, len(events) if expected is None else expected, NODES, NOW)
    return {v.rule for v in found}


def test_a_clean_batch_passes():
    good = [sample(i) for i in range(5)] + [sample(7, status=BAD), sample(8, node="ns=2;s=PUMP-01.state", value="2"),
                                          sample(9, node="ns=2;s=PUMP-01.alarm", value="true")]
    assert rules(good) == set()


def test_g1_01_wrong_schema():
    table = bronze.to_table([sample(1)], "b1").drop_columns(["server_ts"])
    assert rules([sample(1)], table=table) == {"G1-01"}


def test_g1_02_duplicate_or_tampered_event_id():
    e = sample(1)
    assert rules([e, e]) == {"G1-02"}
    table = bronze.to_table([e], "b1")
    tampered = table.set_column(table.schema.get_field_index("raw_value"), "raw_value", pa.array(["9.999"]))
    assert rules([e], table=tampered) == {"G1-02"}           # o valor mudou mas o id já não bate


def test_g1_03_unknown_node():
    assert rules([sample(1, node="ns=2;s=NOPE.x")]) == {"G1-03"}


def test_g1_04_type_and_json():
    assert rules([sample(1, value='"abc"')]) == {"G1-04"}               # texto num nó numérico
    assert rules([sample(1, value="not json")]) == {"G1-04"}
    assert rules([sample(1, node="ns=2;s=PUMP-01.alarm", value="1")]) == {"G1-04"}      # 1 não é booleano
    assert rules([sample(1, node="ns=2;s=PUMP-01.state", value="70000")]) == {"G1-04"}  # fora de uint16
    assert rules([sample(1, node="ns=2;s=PUMP-01.state", value="true")]) == {"G1-04"}   # bool não é inteiro


def test_g1_05_status_and_value_must_agree():
    assert rules([Event.build("ot-gateway", "ns=2;s=PUMP-01.outlet_pressure", "5.0", BAD, NOW, NOW, NOW)]) == {"G1-05"}
    assert rules([Event.build("ot-gateway", "ns=2;s=PUMP-01.outlet_pressure", None, 0, NOW, NOW, NOW)]) == {"G1-05"}


def test_g1_06_source_ts_only_rejects_garbage_not_clock_skew():
    year_2000 = to_us(datetime(2000, 1, 1, tzinfo=timezone.utc))
    assert rules([sample(1, source_ts=year_2000)]) == {"G1-06"}
    assert rules([sample(1, source_ts=None)]) == {"G1-06"}                      # ausente num valor Good
    assert rules([sample(1, source_ts=NOW + 600 * SEC)]) == set()               # clock_skew de 10 min passa (é do G2)
    assert rules([sample(1, source_ts=NOW + 3 * 86400 * SEC)]) == {"G1-06"}     # 3 dias no futuro é lixo


def test_g1_07_ingest_ts_in_the_future():
    assert rules([sample(1, ingest_offset=7200 * SEC)]) == {"G1-07"}


def test_g1_08_row_count_must_match_the_spool():
    assert rules([sample(1), sample(2)], expected=3) == {"G1-08"}
