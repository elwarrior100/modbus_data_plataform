"""Instala os hooks dos gates num repositório lakeFS (docs/gates-contract.md, secção 4).

Escreve `_lakefs_actions/g2.yaml` e `_lakefs_actions/protect.yaml` a partir dos ficheiros Lua desta pasta, num commit
marcado `gates_installer=true` (a exceção que o hook `protect_actions` admite). O commit é feito num branch temporário
e entra no `main` por merge: o `main` tem proteção de branch (não aceita commits diretos, só merges).
Idempotente: se já estiver igual, não faz commit nem merge.

    cd pipelines && uv run python ../quality/install_hooks.py            # LAKEFS_* no ambiente; repositório industrial-lake
"""
import os
import sys
import time
from pathlib import Path

import lakefs

HOOKS = Path(__file__).parent / "hooks"

# Uma ação = um ficheiro `_lakefs_actions/<nome>.yaml`: (nome, evento em YAML, [(id do hook, ficheiro Lua), ...]).
# **Os hooks da mesma ação correm um a seguir ao outro; ações diferentes no mesmo evento correm EM PARALELO.** Com o
# lakeFS 1.86, dois hooks Lua em paralelo que chamam `lakefs.diff_refs` derrubam o servidor ("fatal error: concurrent
# map writes" em pkg/logging, visto a 2026-11-23 no cluster local com o G2 e o G3 em ações separadas). Por isso cada
# evento tem UMA só ação, e o G3 entra como segundo hook da ação `g2` (um teste garante isto).
PRE_MERGE_MAIN = 'pre-merge:\n    branches: ["main"]'
ACTIONS = {
    "g2": ("g2", PRE_MERGE_MAIN, [("g2_attestation", "g2_pre_merge.lua")]),
    "protect": ("protect_actions", "pre-commit:", [("protect_actions_dir", "protect_actions_pre_commit.lua")]),
}

# Os hooks do gold (ADR-019, D6 e D9): G3 no merge, o LATEST_STABLE e a imutabilidade das tags de release. Provados num
# repositório descartável do lakeFS real (tests/test_gates_hook_gold.py) e instalados pelo `__main__` desde a ida do
# gold para produção (2026-11-23). A ação `g2` daqui SUBSTITUI a de ACTIONS (`{**ACTIONS, **GOLD_ACTIONS}`): o mesmo
# ficheiro, com o G3 e o ponteiro a seguir ao G2, por ordem.
GOLD_ACTIONS = {
    "g2": ("g2", PRE_MERGE_MAIN, [("g2_attestation", "g2_pre_merge.lua"), ("g3_attestation", "g3_pre_merge.lua"),
                                  ("release_pointer", "release_pointer_pre_merge.lua")]),
    "release_create": ("release_create", "pre-create-tag:", [("release_needs_proof", "release_pre_create_tag.lua")]),
    "release_delete": ("release_delete", "pre-delete-tag:", [("release_immutable", "release_pre_delete_tag.lua")]),
}


def render(name: str, on: str, hooks: list[tuple[str, str]]) -> str:
    out = f"name: {name}\non:\n  {on}\nhooks:\n"
    for hook_id, lua_file in hooks:
        body = (HOOKS / lua_file).read_text().splitlines()
        out += f"  - id: {hook_id}\n    type: lua\n    properties:\n      script: |\n" + "".join(f"        {line}\n" for line in body)
    return out


def install(repo: "lakefs.Repository", branch: str = "main", actions: dict | None = None) -> bool:
    """True se instalou ou atualizou algo. `actions`: por omissão, só os hooks já em produção (ACTIONS)."""
    work = repo.branch(f"gates-install-{int(time.time())}").create(source_reference=branch)
    try:
        for file, action in (actions or ACTIONS).items():
            work.object(f"_lakefs_actions/{file}.yaml").upload(data=render(*action), mode="w")
        try:
            work.commit("gates: instalar hooks G2, G3 e releases", metadata={"gates_installer": "true"})
        except lakefs.exceptions.LakeFSException as error:
            if "no changes" in str(error).lower():
                return False
            raise
        work.merge_into(repo.branch(branch), message="gates: instalar hooks G2, G3 e releases")
        return True
    finally:
        work.delete()


if __name__ == "__main__":
    client = lakefs.Client(host=os.environ["LAKEFS_ENDPOINT"], username=os.environ["LAKEFS_ACCESS_KEY_ID"],
                           password=os.environ["LAKEFS_SECRET_ACCESS_KEY"])
    name = os.environ.get("LAKEFS_REPOSITORY", "industrial-lake")
    changed = install(lakefs.Repository(name, client=client), actions={**ACTIONS, **GOLD_ACTIONS})
    print(f"{name}: hooks {'instalados/atualizados' if changed else 'já estavam atualizados'}")
    sys.exit(0)
