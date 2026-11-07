"""O writer: transforma lotes do spool em bronze no lakeFS, com um passo de cada vez e todos repetíveis."""
import json
import logging
import time

import pyarrow as pa

from . import bronze, g1
from .spool import TERMINAL, Batch, Spool

log = logging.getLogger("ingest.writer")


class Writer:
    def __init__(self, spool: Spool, lake, nodes: dict[str, str], *, prefix: str = "bronze", target: str = "main",
                 max_rows: int = 5000, max_age_s: float = 60.0, now=None, hook=None):
        self.spool, self.lake, self.nodes = spool, lake, nodes
        self.prefix, self.target = prefix, target
        self.max_rows, self.max_age_s = max_rows, max_age_s
        self.now = now or (lambda: int(time.time() * 1_000_000))
        self.hook = hook or (lambda step: None)      # os testes usam-no para simular um crash em qualquer passo

    def process(self, batch: Batch) -> str:
        """Percorre o ciclo do lote do princípio. Cada passo é idempotente, por isso retomar um lote
        a meio é igual a fazê-lo de novo: o que já estava feito conta como feito."""
        current = self.spool.batch(batch.batch_id)
        if current.state in TERMINAL:
            return current.state
        events, branch = self.spool.rows(batch.batch_id), f"etl-{batch.batch_id}"
        parts = bronze.partitions(events, batch.batch_id, self.prefix)

        self.lake.ensure_branch(branch, self.target)
        self.hook("branch")
        for path, rows in parts.items():
            self.lake.upload(branch, path, bronze.to_parquet(bronze.to_table(rows, batch.batch_id)))
            self.hook("upload")

        self.lake.commit(branch, f"bronze: lote {batch.batch_id}", {
            "batch_id": batch.batch_id, "rows": str(len(events)),
            "first_ingest_ts": str(min(e.ingest_ts for e in events)), "last_ingest_ts": str(max(e.ingest_ts for e in events))})
        self.hook("commit")

        table = pa.concat_tables([bronze.from_parquet(self.lake.read(branch, p)) for p in parts])
        violations = g1.validate(table, len(events), self.nodes, self.now())
        self.hook("validate")
        if violations:
            return self._reject(batch, branch, violations)

        self.lake.merge(branch, self.target, f"bronze: lote {batch.batch_id} validado pelo G1")
        self.hook("merge")
        self.spool.set_state(batch.batch_id, "exported")
        log.info("lote %s exportado: %d linhas em %d partições", batch.batch_id, len(events), len(parts))
        return "exported"

    def _reject(self, batch: Batch, branch: str, violations: list[g1.Violation]) -> str:
        """Gate falhado: o branch fica por juntar, com o relatório, e o main não é tocado."""
        report = g1.report(violations)
        self.lake.upload(branch, f"{self.prefix}/_rejected/{batch.batch_id}/g1_report.json",
                         json.dumps(report, indent=2).encode())
        self.lake.commit(branch, f"G1 rejeitou o lote {batch.batch_id}", {"batch_id": batch.batch_id, "g1": "rejected"})
        self.spool.set_state(batch.batch_id, "rejected", json.dumps(report["violations"][:3]))
        log.error("lote %s REJEITADO pelo G1 (%d violações); branch %s ficou por juntar",
                  batch.batch_id, len(violations), branch)
        return "rejected"

    def run_once(self) -> list[tuple[str, str]]:
        """Retoma os lotes por acabar e forma e escreve os novos que já estejam prontos."""
        done = [(b.batch_id, self.process(b)) for b in self.spool.unfinished()]
        while batch := self.spool.assign_batch(self.max_rows, self.max_age_s, self.now()):
            done.append((batch.batch_id, self.process(batch)))
        return done

    def run_forever(self, interval_s: float = 10.0) -> None:
        while True:
            try:
                self.run_once()
            except Exception:                       # um erro de rede não pode matar o writer
                log.exception("falha a processar lotes; volto a tentar em %ss", interval_s)
            time.sleep(interval_s)
