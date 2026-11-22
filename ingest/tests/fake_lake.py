"""lakeFS em memória, com a mesma semântica de que o writer depende: o que não está commitado
não se junta, e repetir uma operação já feita não faz mal."""


class FakeLake:
    def __init__(self):
        self.branches = {"main": {"committed": {}, "staged": {}}}
        self.calls = []

    def _b(self, name):
        return self.branches[name]

    def ensure_branch(self, name, source="main"):
        if name not in self.branches:
            self.branches[name] = {"committed": dict(self._b(source)["committed"]), "staged": {}}

    def upload(self, branch, path, data):
        self._b(branch)["staged"][path] = data

    def read(self, branch, path):
        b = self._b(branch)
        return b["staged"].get(path, b["committed"].get(path))

    def commit(self, branch, message, metadata):
        b = self._b(branch)
        changed = {p: d for p, d in b["staged"].items() if b["committed"].get(p) != d}
        b["staged"] = {}
        b["committed"].update(changed)
        self.calls.append(("commit", branch, metadata))
        return bool(changed)

    def merge(self, branch, into, message):
        src, dst = self._b(branch)["committed"], self._b(into)["committed"]
        changed = {p: d for p, d in src.items() if dst.get(p) != d}
        dst.update(changed)
        self.calls.append(("merge", branch, into))
        return bool(changed)

    def delete_branch(self, name):
        self.branches.pop(name, None)
        self.calls.append(("delete_branch", name))

    def list(self, ref, prefix):
        return sorted(p for p in self._b(ref)["committed"] if p.startswith(prefix))

    def main_files(self, prefix="bronze/dt="):
        return {p: d for p, d in self.branches["main"]["committed"].items() if p.startswith(prefix)}
