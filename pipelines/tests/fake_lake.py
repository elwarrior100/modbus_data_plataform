"""lakeFS em memória, com a semântica de que o pipeline depende: só o que está commitado se junta,
e repetir uma operação já feita não faz mal."""


class FakeLake:
    def __init__(self):
        self.branches = {"main": {"committed": {}, "staged": {}}}
        self.commits = {"main": 0}

    def ensure_branch(self, name, source="main"):
        if name not in self.branches:
            self.branches[name] = {"committed": dict(self.branches[source]["committed"]), "staged": {}}
            self.commits[name] = 0

    def _view(self, ref):
        b = self.branches[ref]
        return {**b["committed"], **b["staged"]}

    def list(self, ref, prefix):
        return sorted(p for p in self._view(ref) if p.startswith(prefix))

    def read(self, ref, path):
        return self._view(ref)[path]

    def upload(self, branch, path, data):
        self.branches[branch]["staged"][path] = data

    def commit(self, branch, message, metadata):
        b = self.branches[branch]
        changed = {p: d for p, d in b["staged"].items() if b["committed"].get(p) != d}
        b["staged"] = {}
        b["committed"].update(changed)
        if changed:
            self.commits[branch] += 1
        return bool(changed)

    def merge(self, branch, into, message):
        src, dst = self.branches[branch]["committed"], self.branches[into]["committed"]
        changed = {p: d for p, d in src.items() if dst.get(p) != d}
        dst.update(changed)
        if changed:
            self.commits[into] += 1
        return bool(changed)

    def delete_branch(self, name):
        del self.branches[name]

    # --- para os testes ---
    def load_bronze(self, files: dict[str, bytes], branch="main"):
        self.branches[branch]["committed"].update(files)

    def silver(self, branch="main"):
        return {p: d for p, d in self.branches[branch]["committed"].items() if p.startswith("silver/")}
