"""Operações no lakeFS de que o writer precisa. Cada uma trata o "já está feito" como sucesso."""
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

    def ensure_branch(self, name: str, source: str = "main") -> None:
        self.repo.branch(name).create(source_reference=source, exist_ok=True)

    def upload(self, branch: str, path: str, data: bytes) -> None:
        self.repo.branch(branch).object(path).upload(data=data, mode="wb")

    def read(self, branch: str, path: str) -> bytes:
        return self.repo.branch(branch).object(path).reader(mode="rb").read()

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

    def list(self, ref: str, prefix: str) -> list[str]:
        return [o.path for o in self.repo.ref(ref).objects(prefix=prefix)]

    def delete_branch(self, name: str) -> None:
        self.repo.branch(name).delete()
