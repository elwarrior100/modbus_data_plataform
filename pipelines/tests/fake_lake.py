"""lakeFS em memória, com a semântica de que o pipeline depende: só o que está commitado se junta,
e repetir uma operação já feita não faz mal."""


class FakeLake:
    def __init__(self):
        self.branches = {"main": {"committed": {}, "staged": {}}}
        self.commits = {"main": 0}
        self.snapshots = {}                 # ID de commit → ficheiros nesse commit (para ler de um commit fixo)
        self.commit_metadata = {}           # (branch, mensagem) → metadados do último commit com essa mensagem

    def ensure_branch(self, name, source="main"):
        if name not in self.branches:
            self.branches[name] = {"committed": dict(self.branches[source]["committed"]), "staged": {}}
            self.commits[name] = 0

    def resolve(self, ref):
        """Como no lakeFS: o commit (só o que está commitado) para onde o branch aponta agora. O ID é um hash do
        conteúdo, por isso o mesmo estado dá o mesmo ID; o estado fica guardado para se ler dele mais tarde."""
        import hashlib
        if ref in self.snapshots:
            return ref
        files = dict(self.branches[ref]["committed"])
        digest = hashlib.sha256()
        for p in sorted(files):
            digest.update(p.encode() + hashlib.md5(files[p]).digest())
        cid = digest.hexdigest()[:16]
        self.snapshots[cid] = files
        return cid

    def _view(self, ref):
        if ref in self.snapshots:
            return self.snapshots[ref]
        b = self.branches[ref]
        return {p: d for p, d in {**b["committed"], **b["staged"]}.items() if d is not None}

    def list(self, ref, prefix):
        return sorted(p for p in self._view(ref) if p.startswith(prefix))

    def partitions(self, ref, prefix):
        out = set()
        for p in self._view(ref):
            if p.startswith(prefix) and "/" in p[len(prefix):]:
                out.add(prefix + p[len(prefix):].split("/")[0] + "/")
        return sorted(out)

    def delete(self, branch, paths):
        for p in paths:
            self.branches[branch]["staged"][p] = None          # None = apagado (por commitar)

    def read(self, ref, path):
        return self._view(ref)[path]

    def upload(self, branch, path, data):
        self.branches[branch]["staged"][path] = data

    def checksum(self, branch, path):
        import hashlib
        return hashlib.md5(self._view(branch)[path]).hexdigest()

    def commit(self, branch, message, metadata):
        b = self.branches[branch]
        changed = {p: d for p, d in b["staged"].items() if b["committed"].get(p) != d}
        b["staged"] = {}
        b["committed"].update(changed)
        for p in [p for p, d in b["committed"].items() if d is None]:
            del b["committed"][p]
            b.setdefault("deleted", set()).add(p)               # lembrado para o merge (como um merge a 3)
        if changed:
            self.commits[branch] += 1
            self.commit_metadata[(branch, message)] = dict(metadata)
        return bool(changed)

    def merge(self, branch, into, message):
        src, dst = self.branches[branch]["committed"], self.branches[into]["committed"]
        changed = {p: d for p, d in src.items() if dst.get(p) != d}
        gone = [p for p in self.branches[branch].get("deleted", ()) if p in dst]   # só o que o branch apagou
        dst.update(changed)
        for p in gone:
            del dst[p]
        changed.update(dict.fromkeys(gone))
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
