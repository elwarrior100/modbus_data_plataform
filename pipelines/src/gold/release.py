"""Releases do gold: tag imutável + manifest + ponteiro `LATEST_STABLE` + rollback (ADR-019, D7 e D9).

Em simples:
- uma **release** é uma tag do lakeFS (`gold-v1.0.0_AAAAMMDDHHmm`) sobre um commit do `main` que leva consigo o seu
  **manifest** (`_releases/<tag>/manifest.json`): o que está no gold desse dia, de que silver e de que bronze veio, que
  código o fez e o que os gates disseram. A tag não se move nem se apaga (hooks release_pre_create_tag/_delete_tag);
- o **`LATEST_STABLE`** (`_releases/LATEST_STABLE`) é um ficheiro pequeno no `main` com o nome de UMA release: é o que
  os consumidores (Fase 7) leem para saber qual usar. Muda por merge, e o hook release_pointer_pre_merge só aceita uma
  release que existe e cujo G3 não falhou. Só avança sozinho com G3 `pass`; com `warn` a release existe, mas o ponteiro
  só avança se alguém o decidir (`point`);
- um **rollback** é escrever no ponteiro o nome da release anterior: um merge de um ficheiro, sem copiar dados.

Ordem de uma release (porque o manifest não pode conter o ID do commit onde está, e a tag tem de apontar para um commit
que já tem o manifest):  branch → manifest → commit → merge (= commit C) → tag em C → (G3 pass) ponteiro → merge.

Versões (PLAN §7): MAJOR.MINOR vêm de SCHEMA_VERSION (mudam com o esquema do gold); PATCH conta as releases anteriores
do MESMO dia (um reprocessamento é um PATCH). O carimbo `_AAAAMMDDHHmm` (UTC) torna cada nome único. Uma release
diária de dados novos não muda a versão, só o carimbo (`gold-v1.0.0_202611240240`, `gold-v1.0.0_202611250240`).
**`_` e não `+`** (o PLAN dizia `+`, como o SemVer): o lakeFS aceita `+` numa tag, mas o cliente Lua dos hooks não o
escapa no endereço e o `get_object` pela tag dá 400 (medido a 2026-11-23); um consumidor por S3 arriscava o mesmo.

Uso:  python -m gold.release release 2026-11-23   |   current   |   list   |   rollback [--to TAG]   |   point TAG
      python -m gold.release trace TAG            (do manifest até aos commits de bronze; o DoD da Fase 6)
"""
import io
import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone

import pyarrow.parquet as pq

from .build import GOLD_VERSION

SCHEMA_VERSION = "1.0"               # MAJOR.MINOR do esquema do gold (MAJOR = quebra; MINOR = tabela/coluna nova)
PREFIX = "gold-v"
POINTER = "_releases/LATEST_STABLE"
UTC = timezone.utc


class ReleaseError(RuntimeError):
    """Não há release: o dia não tem gold atestado, ou o G3 falhou."""


@dataclass
class ReleaseResult:
    tag: str
    commit: str
    created: bool                    # False = a release deste gold já existia (repetir não cria outra)
    pointer_moved: bool
    g3: str


def manifest_path(tag: str) -> str:
    return f"_releases/{tag}/manifest.json"


def tag_name(version: str, when: datetime) -> str:
    return f"{PREFIX}{version}_{when.astimezone(UTC):%Y%m%d%H%M}"


def _when(tag: str) -> str:
    """O carimbo de uma tag (para ordenar as releases por quando foram feitas)."""
    return tag.rsplit("_", 1)[1] if "_" in tag else ""


def releases(lake) -> list[tuple[str, str]]:
    """(tag, commit) de todas as releases, da mais antiga à mais recente."""
    return sorted(lake.tags(PREFIX), key=lambda t: (_when(t[0]), t[0]))


def read_manifest(lake, tag: str) -> dict:
    return json.loads(lake.read(tag, manifest_path(tag)))


def current(lake, ref: str = "main") -> str | None:
    """A release para onde o `LATEST_STABLE` aponta agora (None se ainda não houver)."""
    try:
        return json.loads(lake.read(ref, POINTER))["release"]
    except (KeyError, ValueError, LookupError):
        return None
    except Exception as error:                   # o SDK do lakeFS levanta NotFoundException
        if "not found" in str(error).lower() or "404" in str(error):
            return None
        raise


def _code() -> dict:
    image = os.environ.get("MODBUS_PIPELINES_IMAGE", "")
    return {"git_commit": image.rsplit(":", 1)[-1] if ":" in image else "desconhecido (fora do Airflow)",
            "image": image or "desconhecida", "dag": "gold_daily", "gold_version": GOLD_VERSION}


def build_manifest(lake, commit: str, day: date, tag: str) -> dict:
    """O manifest de um dia a partir do que já está no commit: a atestação G3 (ficheiros, checksums e de onde veio
    a silver) e as contagens de linhas de cada tabela (lidas dos próprios ficheiros, pequenos)."""
    att = json.loads(lake.read(commit, f"_gates/g3/dt={day.isoformat()}.json"))
    tables = {}
    for path, checksum in sorted(att["files"].items()):
        rows = pq.ParquetFile(io.BytesIO(lake.read(commit, path))).metadata.num_rows
        tables[path] = {"rows": rows, "checksum": checksum}
    inputs = att.get("inputs", {})
    g2 = {path: part.get("g2", "desconhecido") for path, part in inputs.get("silver_partitions", {}).items()}
    return {
        "release": tag, "schema_version": tag[len(PREFIX):].rsplit("_", 1)[0],
        "covers": {"day": day.isoformat(), "from": f"{day.isoformat()}T00:00:00Z", "to": f"{date.fromordinal(day.toordinal() + 1).isoformat()}T00:00:00Z"},
        "tables": tables,
        "code": _code(),
        "gates": {"g3": att["status"], "g3_summary": att.get("summary", ""), "g2": g2},
        "inputs": inputs,            # silver_commit e, por hora, o checksum, o G2 e o bronze_commit (ADR-019, D8)
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _same_gold(manifest: dict, att: dict) -> bool:
    return {p: t["checksum"] for p, t in manifest.get("tables", {}).items()} == att["files"]


def _merge_one_file(lake, target: str, branch: str, path: str, data: bytes, message: str, metadata: dict) -> str:
    """Escreve um ficheiro num branch novo, faz commit e merge para o destino; devolve o commit do destino."""
    lake.ensure_branch(branch, target)
    try:
        lake.upload(branch, path, data)
        lake.commit(branch, message, metadata)
        lake.merge(branch, target, message)
    finally:
        lake.delete_branch(branch)
    return lake.resolve(target)


def point(lake, tag: str, *, target: str = "main", reason: str = "") -> float:
    """Aponta o `LATEST_STABLE` para `tag`. Devolve os segundos que levou (o rollback do DoD tem de ser < 1 min).
    O hook release_pointer_pre_merge recusa uma release que não existe ou cujo G3 falhou."""
    started = time.monotonic()
    if tag not in dict(lake.tags(PREFIX)):
        raise ReleaseError(f"a release {tag} não existe")
    if current(lake, target) == tag:
        return time.monotonic() - started
    body = json.dumps({"release": tag, "set_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "reason": reason},
                      ensure_ascii=False, sort_keys=True).encode()
    _merge_one_file(lake, target, f"pointer-{int(time.time() * 1000)}", POINTER, body,
                    f"LATEST_STABLE -> {tag}", {"release": tag, "reason": reason or "-"})
    return time.monotonic() - started


def release_day(lake, day: date, *, target: str = "main", now: datetime | None = None,
                move_pointer: bool = True) -> ReleaseResult:
    """A release do gold de `day`. Idempotente: se já existe uma release com exatamente este gold, devolve-a."""
    commit = lake.resolve(target)
    try:
        att = json.loads(lake.read(commit, f"_gates/g3/dt={day.isoformat()}.json"))
    except Exception as error:
        raise ReleaseError(f"{day}: não há gold atestado pelo G3 no {target}") from error
    if att.get("status") == "fail":
        raise ReleaseError(f"{day}: o G3 falhou ({att.get('summary', '')}); não há release")
    for tag, tag_commit in reversed(releases(lake)):     # este gold já tem release? (repetir o dia)
        m = read_manifest(lake, tag)
        if m.get("covers", {}).get("day") == day.isoformat() and _same_gold(m, att):
            # a release existe; o ponteiro só se mexe se devia (pass, ainda não aponta para ela, e não recua)
            moved = (move_pointer and att["status"] == "pass" and current(lake, target) != tag
                     and not _newer_pointer(lake, target, tag))
            if moved:
                point(lake, tag, target=target, reason=f"release diária de {day}")
            return ReleaseResult(tag, tag_commit, False, moved, att["status"])
    patch = sum(1 for t, _ in releases(lake) if read_manifest(lake, t).get("covers", {}).get("day") == day.isoformat())
    tag = tag_name(f"{SCHEMA_VERSION}.{patch}", now or datetime.now(UTC))
    manifest = build_manifest(lake, commit, day, tag)
    tagged = _merge_one_file(lake, target, f"release-{day.isoformat()}-{int(time.time() * 1000)}", manifest_path(tag),
                             json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=1).encode(),
                             f"release {tag}", {"release": tag, "day": day.isoformat(), "g3": att["status"]})
    lake.create_tag(tag, tagged)                 # o hook release_pre_create_tag confere manifest, G3 e checksums
    moved = False
    if move_pointer and att["status"] == "pass" and not _newer_pointer(lake, target, tag):
        point(lake, tag, target=target, reason=f"release diária de {day}")
        moved = True
    return ReleaseResult(tag, tagged, True, moved, att["status"])


def _newer_pointer(lake, target: str, tag: str) -> bool:
    """O ponteiro já está numa release de um dia MAIS RECENTE? (repetir um dia antigo não pode recuar o ponteiro)."""
    cur = current(lake, target)
    if cur is None or cur == tag:
        return False
    try:
        return read_manifest(lake, cur)["covers"]["day"] > read_manifest(lake, tag)["covers"]["day"]
    except Exception:
        return False


def rollback(lake, *, to: str | None = None, target: str = "main") -> tuple[str, str, float]:
    """Volta o `LATEST_STABLE` para `to`, ou para a release anterior à atual com G3 `pass`. (de, para, segundos)."""
    cur = current(lake, target)
    if to is None:
        tags = [t for t, _ in releases(lake)]
        if cur not in tags:
            raise ReleaseError("o LATEST_STABLE não aponta para nenhuma release: não há de onde voltar")
        older = [t for t in tags[:tags.index(cur)] if read_manifest(lake, t)["gates"]["g3"] == "pass"]
        if not older:
            raise ReleaseError(f"não há nenhuma release aprovada anterior a {cur}")
        to = older[-1]
    seconds = point(lake, to, target=target, reason=f"rollback de {cur}")
    return cur, to, seconds


def trace(lake, tag: str) -> dict:
    """Do manifest até ao bronze: o commit da silver e, por hora, o commit de bronze que a originou (DoD da Fase 6)."""
    m = read_manifest(lake, tag)
    parts = m.get("inputs", {}).get("silver_partitions", {})
    return {"release": tag, "day": m["covers"]["day"], "silver_commit": m.get("inputs", {}).get("silver_commit"),
            "bronze_commits": sorted({p["bronze_commit"] for p in parts.values()}),
            "hours": {path: p["bronze_commit"] for path, p in sorted(parts.items())}}


if __name__ == "__main__":
    import argparse

    from silver.lake import Lake

    parser = argparse.ArgumentParser(prog="python -m gold.release", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("release", help="cria a release do gold de um dia (AAAA-MM-DD)")
    r.add_argument("day", type=date.fromisoformat)
    r.add_argument("--no-pointer", action="store_true", help="não mover o LATEST_STABLE")
    sub.add_parser("current", help="a release do LATEST_STABLE")
    sub.add_parser("list", help="todas as releases")
    rb = sub.add_parser("rollback", help="volta o LATEST_STABLE para a release anterior aprovada (ou --to)")
    rb.add_argument("--to")
    pt = sub.add_parser("point", help="aponta o LATEST_STABLE para uma release (ex.: aceitar uma com G3 warn)")
    pt.add_argument("tag")
    pt.add_argument("--reason", default="decisão manual")
    tr = sub.add_parser("trace", help="do manifest de uma release até aos commits de bronze")
    tr.add_argument("tag")
    args = parser.parse_args()
    lake = Lake.from_env()
    if args.cmd == "release":
        print(release_day(lake, args.day, move_pointer=not args.no_pointer))
    elif args.cmd == "current":
        print(current(lake))
    elif args.cmd == "list":
        cur = current(lake)
        for tag, commit in releases(lake):
            m = read_manifest(lake, tag)
            print(f"{'*' if tag == cur else ' '} {tag}  dia {m['covers']['day']}  G3 {m['gates']['g3']}  commit {commit[:12]}")
    elif args.cmd == "rollback":
        before, after, seconds = rollback(lake, to=args.to)
        print(f"LATEST_STABLE: {before} -> {after} em {seconds:.2f} s")
    elif args.cmd == "point":
        print(f"LATEST_STABLE -> {args.tag} em {point(lake, args.tag, reason=args.reason):.2f} s")
    elif args.cmd == "trace":
        print(json.dumps(trace(lake, args.tag), indent=1, ensure_ascii=False))
