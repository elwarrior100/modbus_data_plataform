# ADR-007 — Airflow 3 e o pipeline bronze → silver

**Data:** 2026-11-08 · **Estado:** aceite

## Contexto
A Fase 4 transforma o bronze em silver de hora a hora, com reprocessamento seguro. O contrato
está em `docs/silver-contract.md`. O Airflow é requisito do projeto (ADR-011 do plano).

## Decisões
| Decisão | Escolha | Alternativas | Razão |
|---|---|---|---|
| Versão | **Airflow 3.2.2**, chart oficial 1.22.0 | 2.x | a imagem `apache/airflow:3.2.2` verificou-se puxável no cluster (a lição do MinIO) |
| Executor | **KubernetesExecutor** (uma tarefa = um pod) | LocalExecutor | requisito do plano; isola cada execução e só gasta memória enquanto corre |
| Base de metadados | **base `airflow` no PostgreSQL existente** | Postgres próprio, SQLite | já temos o servidor; ficam separadas por base |
| Onde corre a transformação | **DuckDB dentro da tarefa**, a ler e escrever pelo gateway S3 do lakeFS | Spark, pyarrow em memória | validado no spike (ADR-000); o volume não justifica Spark |
| Entrega dos DAGs | **cozidos numa imagem própria** (`airflow-pipelines`) | git-sync | o repositório é privado e o git-sync pediria outra chave; mudar um DAG = reconstruir a imagem, o que é simples e reprodutível. Git-sync fica para a Fase 9 |
| Particionamento da silver | por **`ingest_ts`**, 1 para 1 com o bronze | por hora do evento | dispensa reprocessar partições antigas com dados atrasados (afasta-se do plano; ver contrato, secção 5) |
| Unidades | **mantêm-se as do gateway**, identificadas por código UNECE; existe um catálogo de conversão, hoje identidade | converter tudo para SI | converter bar para Pa torna os valores ilegíveis sem ganho com uma só fonte; o mecanismo fica pronto para quando houver mais fontes (afasta-se do plano) |
| Componentes | api-server, scheduler e dag-processor; **sem** triggerer, statsd nem redis | todos | menos memória; a VM tem 16 GB |
| Autenticação | gestor simples com palavra-passe de administrador gerada | SSO | só desenvolvimento; SSO na Fase 9 |

## Consequências
- Cada execução horária é independente e repetível: o *clear* do Airflow reprocessa uma hora.
- A silver depende do catálogo de tags; uma tag nova no gateway sem catálogo deve falhar de forma
  visível, não passar calada.
- Mudar um DAG exige reconstruir e importar a imagem.
- O Airflow é o serviço mais pesado do cluster; o orçamento é revisto depois de instalado.

## Notas da instalação (2026-11-08)
- **O volume de logs** tem de ser criado à mão (`platform/k8s/airflow/logs-pvc.yaml`): o chart pede-o
  em `ReadWriteMany` e o `local-path` do k3s só suporta `ReadWriteOnce`. Sem isto, os pods ficavam
  em `Pending` para sempre, com a mensagem a dizê-lo nos eventos do volume.
- **A imagem traz o DuckDB 2.0.0**, e o Mac tem o 1.5.5. Convém ter presente se alguma vez se
  trocarem ficheiros entre os dois.
- **Provado**: um DAG de teste corre a tarefa como um pod novo, na imagem própria
  (`pipelines/dags/hello_k8s.py`).
- A palavra-passe do administrador está no Secret `airflow-admin`, gerada, e entra no chart por
  ficheiro (nunca por argumentos nem no git).


## Notas da primeira corrida real (2026-11-07)
- **O DAG processa a hora que fechou, não a hora em curso.** Corre ao minuto 10 (`10 * * * *`) e trata
  `logical_date - 1 h`. A primeira versão processava a hora em curso e a silver ficou com 42 132 linhas
  contra 42 887 no bronze (o collector ainda estava a escrever). O minuto 10 é uma folga para o último
  lote do writer chegar ao `main`. Dados que cheguem depois disso corrigem-se com *clear* da hora.
- **`start_date` = primeira hora com bronze real.** Com `catchup=True`, uma data antiga gera centenas de
  execuções vazias (cada uma um pod).
- **Credenciais do lakeFS**: `install-airflow.sh` copia o Secret `lakefs-credentials` do namespace
  `ingest` para `airflow` sem o mostrar; os pods de tarefa recebem-no por `extraEnvFrom`.
- **Imagem**: instala o pacote `silver` (`pip install` de `pyproject.toml` + `src/`) e copia os DAGs.
- **Fixture de teste**: o gerador de 7 dias deduplica por `event_id`, como o spool. Quando o relógio do
  dispositivo volta atrás (fim do `clock_skew`), repete `source_ts` e valor, e o spool descartaria a
  segunda amostra (ponto fraco 8 da Fase 3: a identidade depende de `source_ts` ao segundo).
