"""O catálogo de tags: o que cada nó OPC UA significa (ativo, tag, tipo, unidade, intervalo válido)."""
from dataclasses import dataclass
from importlib import resources

import pyarrow as pa
import yaml

KINDS = ("analog", "counter", "state", "boolean", "clock")


@dataclass(frozen=True)
class Tag:
    node_id: str
    asset: str
    tag: str
    kind: str
    unit: str | None = None
    eu_min: float | None = None
    eu_max: float | None = None
    enum: tuple[str, ...] = ()
    moves_with_run: bool = False     # parado, fica constante e sem amostras (G2-06; ADR-019)


def load_catalog() -> list[Tag]:
    raw = yaml.safe_load(resources.files("silver").joinpath("catalog.yaml").read_text())["tags"]
    tags = []
    for item in raw:
        if item["kind"] not in KINDS:
            raise ValueError(f"{item['node_id']}: kind inválido {item['kind']}")
        for label in item.get("enum", ()):
            if not isinstance(label, str):      # OFF, ON, NO sem aspas viram booleanos em YAML 1.1
                raise ValueError(f"{item['node_id']}: etiqueta {label!r} não é texto; ponha-a entre aspas")
        tags.append(Tag(item["node_id"], item["asset"], item["tag"], item["kind"], item.get("unit"),
                        item.get("eu_min"), item.get("eu_max"), tuple(item.get("enum", ())),
                        bool(item.get("moves_with_run", False))))
    return tags


def to_arrow(tags: list[Tag]) -> pa.Table:
    """O catálogo como tabela, para o DuckDB a juntar ao bronze."""
    return pa.table({
        "node_id": [t.node_id for t in tags], "asset_id": [t.asset for t in tags], "tag_name": [t.tag for t in tags],
        "kind": [t.kind for t in tags], "unit": [t.unit for t in tags],
        "eu_min": pa.array([t.eu_min for t in tags], pa.float64()), "eu_max": pa.array([t.eu_max for t in tags], pa.float64()),
        "enum_labels": pa.array([list(t.enum) for t in tags], pa.list_(pa.string())),
    })
