from silver.retention import DAY, prune

NOW = 1_000_000_000.0


class Stub:
    def __init__(self, branches, main_history):
        self.branches, self.main_history, self.deleted = branches, set(main_history), []

    def branch_infos(self):
        return self.branches

    def history_ids(self, ref="main"):
        return self.main_history

    def delete_branch(self, name):
        self.deleted.append(name)


def age(days):
    return NOW - days * DAY


def lake():
    return Stub(
        [("main", "m", age(0)),
         ("etl-0123456789abcdef", "c1", age(8)),       # juntado, velho -> apaga
         ("etl-aaaaaaaaaaaaaaaa", "c2", age(2)),       # juntado, recente -> fica
         ("etl-bbbbbbbbbbbbbbbb", "x1", age(10)),      # por juntar, dentro dos 30 dias -> fica
         ("etl-cccccccccccccccc", "x2", age(31)),      # por juntar, passou os 30 dias -> apaga
         ("silver-2026-11-07-17", "c3", age(9)),       # juntado, velho -> apaga
         ("etl-gap", "g", age(400)),                   # manual: nunca se toca
         ("minha-experiencia", "e", age(400))],        # manual: nunca se toca
        main_history={"m", "c1", "c2", "c3"})


def test_the_policy_deletes_only_old_pipeline_branches():
    report = prune(lake(), NOW, dry_run=False)
    assert sorted(report.delete) == ["etl-0123456789abcdef", "etl-cccccccccccccccc", "silver-2026-11-07-17"]
    assert (report.keep_merged, report.keep_unmerged, report.ignored) == (1, 1, 3)


def test_dry_run_deletes_nothing():
    stub = lake()
    report = prune(stub, NOW)                                   # simulação é o comportamento por omissão
    assert len(report.delete) == 3 and stub.deleted == []


def test_real_run_deletes_exactly_what_the_report_says():
    stub = lake()
    report = prune(stub, NOW, dry_run=False)
    assert sorted(stub.deleted) == sorted(report.delete)
