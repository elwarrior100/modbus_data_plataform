"""Os ficheiros das ações do lakeFS geram-se bem (YAML válido, com o Lua dentro), sem precisar do lakeFS.
O comportamento dos hooks prova-se no lakeFS real (test_gates_hook.py, test_gates_hook_gold.py)."""
import sys
from collections import Counter
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "quality"))
from install_hooks import ACTIONS, GOLD_ACTIONS, HOOKS, render  # noqa: E402


def _events(action) -> list[str]:
    doc = yaml.safe_load(render(*action))
    return list(doc.get("on", doc.get(True)) or {})          # YAML 1.1: a chave `on` lê-se como True


def test_every_action_renders_to_valid_yaml_with_its_lua_scripts_in_order():
    for action in {**ACTIONS, **GOLD_ACTIONS}.values():
        doc = yaml.safe_load(render(*action))
        assert doc["name"] and _events(action)
        _, _, hooks = action
        assert [h["id"] for h in doc["hooks"]] == [hook_id for hook_id, _ in hooks]
        for h, (_, lua) in zip(doc["hooks"], hooks):
            assert h["type"] == "lua" and h["properties"]["script"].strip() == (HOOKS / lua).read_text().strip()


def test_one_action_per_event_so_hooks_never_run_in_parallel():
    """Ações diferentes no mesmo evento correm em paralelo, e dois hooks Lua em paralelo derrubam o lakeFS 1.86
    ("concurrent map writes", 2026-11-23). Tanto o que está em produção como o que o gold vai instalar: 1 ação/evento."""
    for actions in (ACTIONS, {**ACTIONS, **GOLD_ACTIONS}):
        per_event = Counter(e for a in actions.values() for e in _events(a))
        assert all(n == 1 for n in per_event.values()), per_event


def test_the_gold_hooks_are_not_installed_by_default_yet():
    assert set(ACTIONS) == {"g2", "protect"}                   # o __main__ do instalador só põe estes no industrial-lake
    assert [h for h, _ in ACTIONS["g2"][2]] == ["g2_attestation"]
    assert [h for h, _ in GOLD_ACTIONS["g2"][2]] == ["g2_attestation", "g3_attestation"]    # o G3 depois do G2
    events = {n: _events(GOLD_ACTIONS[n]) for n in GOLD_ACTIONS}
    assert events == {"g2": ["pre-merge"], "release_create": ["pre-create-tag"], "release_delete": ["pre-delete-tag"]}
