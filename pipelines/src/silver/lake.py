"""As operações no lakeFS de que o pipeline precisa. Cada uma trata o "já está feito" como sucesso."""
import os
import warnings

import lakefs
from lakefs.exceptions import BadRequestException

warnings.filterwarnings("ignore", category=lakefs.LakeFSDeprecationWarning)


def _nothing_to_do(error: Exception) -> bool:
    text = str(error).lower()
    return "no changes" in text or "nothing to" in text


class Lake:
    def __init__(self, endpoint: str, access_key: str, secret_key: str, repository: str):
        self.client = lakefs.Client(host=endpoint, username=access_key, password=secret_key)
        self.repo = lakefs.Repository(repository, client=self.client)

    @classmethod
    def from_env(cls) -> "Lake":
        return cls(os.environ["LAKEFS_ENDPOINT"], os.environ["LAKEFS_ACCESS_KEY_ID"],
                   os.environ["LAKEFS_SECRET_ACCESS_KEY"], os.environ.get("LAKEFS_REPOSITORY", "industrial-lake"))

    def ensure_branch(self, name: str, source: str = "main") -> None:
        self.repo.branch(name).create(source_reference=source, exist_ok=True)

    def list(self, ref: str, prefix: str) -> list[str]:
        return sorted(o.path for o in self.repo.ref(ref).objects(prefix=prefix))

    def read(self, ref: str, path: str) -> bytes:
        return self.repo.ref(ref).object(path).reader(mode="rb").read()

    def upload(self, branch: str, path: str, data: bytes) -> None:
        self.repo.branch(branch).object(path).upload(data=data, mode="wb")

    def checksum(self, branch: str, path: str) -> str:
        """O checksum que o lakeFS guarda para o objeto (é o que o hook do G2 compara)."""
        return self.repo.branch(branch).object(path).stat().checksum

    def commit(self, branch: str, message: str, metadata: dict[str, str]) -> bool:
        """False se não havia nada para commitar (já estava feito)."""
        try:
            self.repo.branch(branch).commit(message=message, metadata=metadata)
            return True
        except BadRequestException as error:
            if _nothing_to_do(error):
                return False
            raise

    def merge(self, branch: str, into: str, message: str) -> bool:
        """False se não havia nada para juntar (já estava feito)."""
        try:
            self.repo.branch(branch).merge_into(self.repo.branch(into), message=message)
            return True
        except BadRequestException as error:
            if _nothing_to_do(error):
                return False
            raise

    def delete_branch(self, name: str) -> None:
        self.repo.branch(name).delete()

    def branch_infos(self) -> "list[tuple[str, str, float]]":
        """(nome, commit da ponta, data do commit em segundos) de cada branch."""
        out = []
        for b in self.repo.branches():
            commit = b.get_commit()
            out.append((b.id, commit.id, float(commit.creation_date)))
        return out

    def history_ids(self, ref: str = "main", limit: int = 20000) -> set[str]:
        """Os commits que fazem parte da história de `ref` (inclui os dos branches já juntados)."""
        return {c.id for c in self.repo.ref(ref).log(max_amount=limit)}
