# Contrato da silver e do pipeline bronze → silver (Fase 4)

Define o que a silver contém e como o Airflow a produz a partir do bronze
(`docs/bronze-contract.md`). É o contrato com a Fase 5 (gates) e a Fase 6 (gold).

## 1. O que a silver é

O bronze guarda cada amostra **tal como chegou**. A silver guarda a **mesma informação, já
interpretada**: valor com tipo certo, unidade, qualidade normalizada, e avisos sobre o que parece
estranho. **Nunca se apaga uma amostra má: marca-se.** Uma linha de bronze dá exatamente uma
linha de silver (1 para 1), por isso contagens batem sempre.

## 2. O esquema (uma linha por amostra)

| Coluna | Tipo | Como se obtém |
|---|---|---|
| `asset_id` | string | do `opcua_node_id`: `ns=2;s=PUMP-01.outlet_pressure` → `PUMP-01` |
| `tag_name` | string | idem → `outlet_pressure` |
| `ts_utc` | timestamp UTC | `source_ts`; se faltar, `ingest_ts` (e fica o aviso `ts_fallback`). Precisão: a do relógio do equipamento (1 s nos PLC Modbus); dentro do mesmo segundo, as linhas vêm ordenadas pelo `server_ts` (ADR-018) |
| `value_double` | double, nulo | `raw_value` lido como JSON, para tags numéricas, contadores e estados |
| `value_bool` | bool, nulo | para tags booleanas |
| `value_str` | string, nulo | etiqueta do estado (`RUNNING`) ou o instante do `device_clock` |
| `unit` | string, nulo | código UNECE da unidade (`BAR`, `AMP`, `CEL`…), do catálogo |
| `quality` | `GOOD` / `UNCERTAIN` / `BAD` | dos dois bits mais altos do `opcua_status_code` |
| `quality_reason` | string | o nome do estado (`BadNoCommunication`…) |
| `dq_flags` | lista de strings | avisos (secção 4) |
| `ingest_ts`, `source_ts`, `server_ts` | timestamps UTC | copiados do bronze |
| `bronze_event_id`, `bronze_batch_id` | string | linhagem até à amostra original |
| `silver_version` | string | versão do código da transformação (constante: repetir uma hora dá o mesmo ficheiro) |

**Nunca há `value_*` preenchido num `BAD`**: o bronze já não tem valor nesses casos.

## 3. O catálogo de tags

Uma tabela (`pipelines/catalog.yaml`) diz o que cada nó significa: ativo, tag, tipo, unidade,
intervalo válido (`EURange`) e, para estados, as etiquetas. É gerada a partir da configuração
do gateway e um teste garante que não divergem.

**Qualidade** (pelos dois bits mais altos do estado OPC UA):

| Bits | `quality` | Exemplos |
|---|---|---|
| `00` | `GOOD` | `Good` (0x00000000) |
| `01` | `UNCERTAIN` | `UncertainEngineeringUnitsExceeded` (0x40940000) |
| `10` ou `11` | `BAD` | `BadNoCommunication` (0x80310000), `BadConfigurationError`, `BadDeviceFailure`, `BadCommunicationError`, `BadWaitingForInitialData` |

## 4. Os avisos (`dq_flags`)

| Aviso | Quando |
|---|---|
| `out_of_range` | o valor está fora do `EURange` do catálogo |
| `clock_skew` | `source_ts` e `server_ts` diferem em mais de 60 s |
| `late` | `ingest_ts - ts_utc` maior que 1 hora |
| `ts_fallback` | faltava `source_ts` e usou-se `ingest_ts` |
| `bad_quality` | `quality = BAD` (para filtrar sem comparar texto) |

**Um sensor congelado (`sensor_stuck`) não deixa nenhum aviso por linha, e isto é importante.** O
gateway só escreve uma variável quando o valor **muda**; um sensor parado deixa de gerar amostras,
logo o que se vê é uma **ausência**. Detetá-la é medir a completude por tag e hora (amostras
recebidas contra esperadas), trabalho do gate G2 (Fase 5).

## 5. Particionamento: decisão que se afasta do plano

    silver/dt=YYYY-MM-DD/hour=HH/data.parquet

A partição da silver é a **mesma hora de receção (`ingest_ts`) do bronze, 1 para 1**. O plano
previa partir por hora do evento e reprocessar partições antigas quando chegassem dados atrasados.
Particionar pela receção dispensa isso: uma execução do Airflow para a hora H lê o bronze da hora
H e **sobrescreve** a silver da hora H, sempre, sem olhar para outras horas. Os atrasos não exigem
reprocessamento (ficam com o aviso `late`); quem agrega por hora do evento (o gold) lê um
intervalo de partições.

## 6. Uma execução (uma hora)

O Airflow corre uma tarefa por hora. Para a hora H:

| # | Passo | Idempotente porque |
|---|---|---|
| 1 | cria o branch `silver-<data>-<hora>` a partir do `main` | `exist_ok` |
| 2 | resolve o `main` para um ID de commit (`bronze_commit`, ADR-019 D8); o DuckDB lê `bronze/dt=…/hour=H/*.parquet` **desse commit** e escreve `silver/…/data.parquet` | nome fixo e linhas ordenadas: mesmo resultado |
| 3 | commit no branch (com contagens e o `bronze_commit` nos metadados) | "sem alterações" conta como feito |
| 4 | verifica: linhas da silver = linhas do bronze | só leitura |
| 5 | merge para o `main` | "nada a juntar" conta como feito |

Se algo falhar a meio, **o `main` fica intacto** e o trabalho por acabar fica no branch. Repetir a
hora (o *clear* do Airflow) dá o mesmo resultado.

## 7. Critérios de verificação

1. um *backfill* de 7 dias produz **exatamente** o mesmo que 7 dias de execuções incrementais;
2. reprocessar a mesma hora duas vezes não altera as contagens nem os ficheiros;
3. uma falha a meio deixa o `main` intacto;
4. `silver` tem tantas linhas como o `bronze` em cada hora, e nenhuma amostra `BAD` tem valor.
