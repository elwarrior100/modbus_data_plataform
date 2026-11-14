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

ACTIONS = {
    "g2": ("name: g2\non:\n  pre-merge:\n    branches: [\"main\"]\nhooks:\n  - id: g2_attestation\n    type: lua\n    properties:\n      script: |\n",
           "g2_pre_merge.lua"),
    "protect": ("name: protect_actions\non:\n  pre-commit:\nhooks:\n  - id: protect_actions_dir\n    type: lua\n    properties:\n      script: |\n",
                "protect_actions_pre_commit.lua"),
}


def render(header: str, lua_file: str) -> str:
    body = (HOOKS / lua_file).read_text().splitlines()
    return header + "".join(f"        {line}\n" for line in body)


def install(repo: "lakefs.Repository", branch: str = "main") -> bool:
    """True se instalou ou atualizou algo."""
    work = repo.branch(f"gates-install-{int(time.time())}").create(source_reference=branch)
    try:
        for name, (header, lua) in ACTIONS.items():
            work.object(f"_lakefs_actions/{name}.yaml").upload(data=render(header, lua), mode="w")
        try:
            work.commit("gates: instalar hooks G2", metadata={"gates_installer": "true"})
        except lakefs.exceptions.LakeFSException as error:
            if "no changes" in str(error).lower():
                return False
            raise
        work.merge_into(repo.branch(branch), message="gates: instalar hooks G2")
        return True
    finally:
        work.delete()


if __name__ == "__main__":
    client = lakefs.Client(host=os.environ["LAKEFS_ENDPOINT"], username=os.environ["LAKEFS_ACCESS_KEY_ID"],
                           password=os.environ["LAKEFS_SECRET_ACCESS_KEY"])
    name = os.environ.get("LAKEFS_REPOSITORY", "industrial-lake")
    changed = install(lakefs.Repository(name, client=client))
    print(f"{name}: hooks {'instalados/atualizados' if changed else 'já estavam atualizados'}")
    sys.exit(0)
