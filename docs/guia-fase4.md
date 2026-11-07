# Guia da Fase 4: do bronze à silver, com o Airflow

Para perceberes (e saberes explicar) o que foi feito, como se tivesses sido tu a fazê-lo.
Nada disto está commitado: é teu para rever.

---

## 0. Como ver o DAG que correu

```bash
./platform/scripts/ui.sh --open
```

Abre o Airflow em **http://localhost:8084** (utilizador `admin`). A palavra-passe copia-se para a
área de transferência (não aparece no ecrã) com:

```bash
KUBECONFIG=$HOME/.lima/industrial/copied-from-guest/kubeconfig.yaml kubectl -n airflow get secret airflow-admin -o jsonpath='{.data.password}' | base64 -d | pbcopy
```

(Se o `kubectl` não encontrar o cluster: `export KUBECONFIG=$HOME/.lima/industrial/copied-from-guest/kubeconfig.yaml`.)

No Airflow:
1. Menu **Dags** → `silver_hourly`.
2. Separador **Runs**: cada linha é uma hora processada (verde = sucesso).
3. Clica num run → a tarefa `bronze_to_silver` → **Logs**: vês o `RunResult` com as linhas.
4. Separador **Code**: o código do DAG tal como o Airflow o lê.

Para ver o resultado, no lakeFS (http://localhost:8082) → repositório `industrial-lake` →
`main` → `silver/dt=2026-11-07/hour=16/data.parquet`. Os commits dizem
"silver 2026-11-07T16" nos metadados.

Para ver os pods das tarefas a nascer e morrer: `k9s` (ou `kubectl -n airflow get pods -w`)
na hora em ponto + 10 minutos.

---

## 1. O que é a silver, em uma frase

O **bronze** é o que o gateway disse, tal e qual (texto JSON, códigos de estado OPC UA).
A **silver** é o mesmo, **arrumado e tipado**: um número é um número, um booleano é um booleano,
o estado tem nome (`GOOD`/`UNCERTAIN`/`BAD`) e cada linha leva *flags* se algo for suspeito.
**Não se perde nem se inventa nenhuma linha**: 1 linha bronze → 1 linha silver.

## 2. As peças, para um iniciante

| Peça | O que é | Para que serve aqui |
|---|---|---|
| **Airflow** | agendador de tarefas ("cron com interface") | corre o pipeline uma vez por hora e deixa histórico |
| **DAG** | um ficheiro Python que descreve *o que correr e quando* | `pipelines/dags/silver_hourly.py` (30 linhas) |
| **KubernetesExecutor** | cada tarefa corre num **pod novo** que desaparece no fim | só gasta memória quando trabalha |
| **Helm** | instalador de aplicações para k8s (o chart é a receita) | instalou o Airflow com `values.yaml` |
| **Imagem Docker** | o "disco" de onde o pod arranca | `airflow-pipelines` = Airflow + o nosso código |
| **DuckDB** | base de dados que corre dentro do próprio programa | faz a transformação com SQL |
| **lakeFS** | "git para dados": branches, commits, merges | o trabalho faz-se num branch e só entra no `main` se estiver certo |

### A ideia central: Write-Audit-Publish (WAP)

    1. WRITE    cria um branch  silver-2026-11-07-16   (cópia barata do main)
    2. escreve a silver no branch
    3. AUDIT    lê de volta e confirma: linhas silver == linhas bronze
    4. PUBLISH  merge para o main
    (se algo falhar antes do 4, o main nunca viu nada)

Pensa num rascunho: só se publica depois de revisto. Está testado: o teste "mata" o processo
em cada passo e confirma que o `main` fica intacto.

👉 **Imagem 19** (`docs/img-19-bronze-para-silver.svg`): o fluxo de uma hora.

### Idempotência (palavra que vais ouvir muito)

Correr a mesma hora 1 vez ou 10 vezes dá **exatamente o mesmo resultado**, até ao byte.
Por isso: se o Airflow repetir uma tarefa que falhou, ou se reprocessares uma hora, nada se
estraga. Como se consegue: ordem fixa nas linhas (`ORDER BY`), nada com "agora" dentro dos dados,
e "já está feito" conta como sucesso (branch já existe, commit sem alterações, nada para juntar).

---

## 3. Como foi construído, passo a passo (para o repetires)

1. **Contrato primeiro** (`docs/silver-contract.md`): colunas, flags, partições, critérios de
   verificação. Escrever o contrato antes do código obriga a decidir o que é "certo".
2. **Catálogo de tags** (`pipelines/src/silver/catalog.yaml`): para cada uma das 25 variáveis,
   o tipo (analógica, booleana, estado, relógio, contador), a unidade (código UNECE: BAR, AMP, CEL…)
   e os limites. É a "tabela de dicionário" que o SQL usa.
3. **Transformação** (`transform.py`): lê os Parquet do bronze com DuckDB, junta ao catálogo,
   converte `raw_value` (texto) para `value_double`/`value_bool`/`value_str`, traduz o
   `opcua_status_code`, calcula as flags e escreve um Parquet ordenado.
4. **Uma hora** (`run.py`, `run_hour`): o WAP acima. Aceita um `hook` por passo, só para os testes
   simularem falhas.
5. **Testes** (24): catálogo, transformação, falhas a meio, e uma **semana sintética** de 7 dias
   (302 mil eventos, com avarias injetadas) processada de duas maneiras (tudo em desordem vs hora a hora)
   que tem de dar ficheiros **idênticos**.
6. **Airflow**: instalado com Helm. Descobri duas coisas: o volume de logs pedia um tipo de disco que o
   k3s não tem (criámos o nosso), e o código tem de estar *dentro da imagem* porque o repositório é privado.
7. **O DAG**: ao minuto 10 de cada hora, trata a hora que **acabou de fechar** (👉 **imagem 20**, `docs/img-20-hora-fechada.svg`).
8. **Credenciais**: o script copia o Secret do lakeFS para o namespace do Airflow, sem o mostrar.

Para o explicares a alguém: *"o Airflow acorda de hora a hora, lança um pod, o pod copia os dados da
hora para um branch, transforma-os, confirma que não faltou nada e só então publica."*

---

## 4. O que a transformação faz (para verificares)

Exemplo real (silver, hora 17, CHILLER-01):

| Campo | Bronze | Silver |
|---|---|---|
| valor | `raw_value = "120.349"` (texto) | `value_double = 120.349` |
| unidade | (não existe) | `unit = KWT` (kW, código UNECE) |
| estado | `opcua_status_code = 0` | `quality = GOOD`, `quality_reason = Good` |
| nome | `ns=2;s=CHILLER-01.power_kw` | `asset_id = CHILLER-01`, `tag_name = power_kw` |
| tempo | `source_ts` (relógio do dispositivo) | `ts_utc = source_ts`, e guarda `ingest_ts`, `server_ts` |
| ligação | `event_id`, `batch_id` | `bronze_event_id`, `bronze_batch_id` |

Regras:
- **Quality**: pelos 2 bits de cima do código OPC UA: 0 = GOOD, 1 = UNCERTAIN, 2-3 = BAD.
  BAD nunca tem valor e leva a flag `bad_quality`.
- **Flags** (lista `dq_flags`): `out_of_range` (fora dos limites do catálogo), `clock_skew`
  (relógio do dispositivo a mais de 60 s do do gateway), `late` (chegou 1 h depois), `ts_fallback`
  (sem `source_ts`, usou-se outro), `bad_quality`.
- **Nunca se corrige um valor**: só se assinala. Um spike continua lá, com a flag.
- **Falha alto**: se houver `event_id` repetido na hora, nó desconhecido do catálogo, valor Good
  ilegível, ou nº de linhas diferente, a execução **falha** em vez de publicar algo duvidoso.

## 5. Como confirmar que está certo (o que fiz e podes repetir)

| Verificação | Resultado |
|---|---|
| Hora 16: linhas bronze vs silver | 39 247 = 39 247, `event_id` idênticos ✔ |
| Hora 17 (1.ª corrida, hora ainda aberta) | 42 132 vs 42 887 ✘: **erro de desenho meu**, corrigido para processar só horas fechadas |
| 24 testes | passam (incl. backfill == incremental em 169 ficheiros; crash em cada passo deixa o `main` intacto) |
| Hora 17 (run das 18:10, hora fechada) | 43 848 = 43 848, `event_id` idênticos ✔ |
| Repetir a hora 16 (run das 17:10) | sucesso sem alterar nada (idempotente) |

Para repetires tu, no Mac: `cd pipelines && uv run pytest -q` (≈25 s).

## 6. Pontos fracos conhecidos (honestidade)

- Dois eventos com o mesmo valor **e** o mesmo `source_ts` ao segundo fundem-se (ponto fraco 8 da
  Fase 3). Aconteceu na semana sintética no fim de um `clock_skew`.
- Falta ainda: teste contra um lakeFS real com branches descartáveis; backfill pelo Airflow
  (`airflow backfill create`); retenção de branches antigos (`etl-*`, `silver-*`).

## 7. Perguntas para teres a certeza

1. Porque é que o DAG processa `logical_date − 1 h` e não a hora corrente?
2. O que garante que repetir uma hora não duplica linhas?
3. Se o pod morrer depois do upload mas antes do merge, o que vê o `main`? E o que acontece na repetição?
4. Porque é que um valor `out_of_range` não é corrigido nem apagado?
5. Porque é que a silver tem a mesma contagem de linhas que o bronze?
