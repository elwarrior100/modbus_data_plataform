"""Instala os hooks dos gates num repositório lakeFS (docs/gates-contract.md, secção 4).

Escreve `_lakefs_actions/g2.yaml` e `_lakefs_actions/protect.yaml` no `main`, a partir dos ficheiros Lua desta pasta,
num commit marcado `gates_installer=true` (a exceção que o hook `protect_actions` admite).
Idempotente: se já estiver igual, não faz commit.

    cd pipelines && uv run python ../quality/install_hooks.py            # LAKEFS_* no ambiente; repositório industrial-lake
"""
import os
import sys
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
    target = repo.branch(branch)
    for name, (header, lua) in ACTIONS.items():
        target.object(f"_lakefs_actions/{name}.yaml").upload(data=render(header, lua), mode="w")
    try:
        target.commit("gates: instalar hooks G2", metadata={"gates_installer": "true"})
        return True
    except lakefs.exceptions.LakeFSException as error:
        if "no changes" in str(error).lower():
            return False
        raise


if __name__ == "__main__":
    client = lakefs.Client(host=os.environ["LAKEFS_ENDPOINT"], username=os.environ["LAKEFS_ACCESS_KEY_ID"],
                           password=os.environ["LAKEFS_SECRET_ACCESS_KEY"])
    name = os.environ.get("LAKEFS_REPOSITORY", "industrial-lake")
    changed = install(lakefs.Repository(name, client=client))
    print(f"{name}: hooks {'instalados/atualizados' if changed else 'já estavam atualizados'}")
    sys.exit(0)
