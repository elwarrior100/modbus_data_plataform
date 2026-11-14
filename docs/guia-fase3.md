# Guia de leitura da Fase 3 (ingestão e bronze)

Para leres o código **como se o tivesses escrito**: por que ordem, o que procurar, e que
perguntas fazer a cada peça. Imagens `docs/img-07` a `img-12`. Código em `ingest/src/ingest/`
(600 linhas) e testes em `ingest/tests/`. **Nada disto está commitado: é teu para rever.**

---

## 1. O problema, em três promessas

A Fase 3 leva os dados do gateway até ao lakehouse. A dificuldade não é copiar dados, é
**o que acontece quando alguma coisa morre a meio**. O desenho tem de manter três promessas:

1. **Não perder** nenhuma amostra que chegou.
2. **Não duplicar** nenhuma, mesmo se algo for repetido depois de um crash.
3. **Não contaminar o `main`**: dados maus ficam à parte, nunca misturados com os bons.

👉 **Imagem 7**: quem faz o quê. O collector põe amostras num caderno durável (o spool);
o writer lê esse caderno, escreve no lakeFS num branch, valida e só então junta ao `main`.

---

## 2. A ordem de leitura

Do mais simples (sem rede) ao mais complexo. Cada ficheiro é pequeno.

| # | Ficheiro | Linhas | O que é | Imagem |
|---|---|---|---|---|
| 1 | `envelope.py` | 49 | o que é uma "amostra" e como se calcula a identidade | — |
| 2 | `spool.py` | 102 | o caderno durável (PostgreSQL) | 8 |
| 3 | `bronze.py` | 59 | o formato do Parquet e os caminhos | — |
| 4 | `g1.py` | 81 | as 8 regras do gate | 10 |
| 5 | `lake.py` | 53 | as operações no lakeFS | — |
| 6 | `writer.py` | 79 | **o coração**: o ciclo do lote | 9 |
| 7 | `collector.py` | 124 | a ligação ao gateway | 11 |
| 8 | `main.py` | 63 | os comandos (`collect`, `write`, `status`…) | — |

O contrato que estas peças cumprem está em `docs/bronze-contract.md`. Lê-o **antes** do código.

---

## 3. Ficheiro a ficheiro

### 3.1 `envelope.py`: a identidade de uma amostra

Uma amostra (`Event`, linha 36) tem 8 campos. O mais importante é o `event_id`
(`make_event_id`, **linha 24**): um UUID calculado a partir de **quem, onde, quando
(`source_ts`), estado e valor**. A mesma amostra dá sempre o mesmo id.

Repara no que **não** entra no cálculo: `server_ts` e `ingest_ts`. É de propósito. Se o
gateway reentregar o mesmo valor depois de uma reconexão, chega com outro `ingest_ts`, mas o
`event_id` é igual e o spool ignora-o. É assim que se evitam duplicados na origem.

`to_us` (linha 12) converte datas para **microssegundos inteiros**, porque números
decimais podem perder precisão e dois ids "iguais" deixariam de o ser.

❓ *Se duas amostras tiverem o mesmo valor e o mesmo `source_ts`, o que acontece?* (São a
mesma: ficam fundidas. Ver o ponto fraco 8 na imagem 12.)

### 3.2 `spool.py`: o caderno que não esquece

👉 **Imagem 8.** São duas tabelas no PostgreSQL (base `ingest`): `events` (as amostras) e
`batches` (os lotes). Os pontos a ler com atenção:

- **`SCHEMA`, linha 12**: o desenho das duas tabelas. Repara em `event_id TEXT NOT NULL UNIQUE`:
  é isto que impede duplicados, para sempre.
- **`_tx`, linha 41**: uma transação, numa ligação que **se refaz sozinha** se a anterior caiu
  (por exemplo, o Postgres reiniciou). Quase todos os métodos passam por aqui.
- **`add`, linha 52**: `ON CONFLICT (event_id) DO NOTHING`. Uma amostra repetida é ignorada pela
  própria base de dados. Devolve quantas eram realmente novas.
- **`assign_batch`, linha 59**: o método mais subtil. Dentro de **uma transação**, escolhe amostras
  sem lote (`FOR UPDATE`), calcula o `batch_id` (hash dos seus ids), marca as linhas e cria a
  linha do lote. Se interromperes a meio, a transação desfaz-se. Um lote **congelado** nunca
  mais é reagrupado.

❓ *A durabilidade antes vinha de `synchronous=FULL`. De onde vem agora?* (De o Postgres só
confirmar o `COMMIT` depois de gravar no disco o seu registo de transações.)

### 3.3 `bronze.py`: o formato

`BRONZE_SCHEMA` (linha 12) é o contrato em código: 10 colunas com tipos exatos.
`partitions` agrupa as amostras por **hora de receção** (`ingest_ts`) e `path_for` produz
`bronze/dt=2026-11-07/hour=16/<batch_id>.parquet`. O nome do ficheiro é o `batch_id`:
**determinístico**, por isso carregar de novo sobrescreve o mesmo ficheiro.

### 3.4 `g1.py`: o gate

👉 **Imagem 10.** `validate` (**linha 41**) percorre o lote e junta as violações das 8 regras.
O que reparar:

- Rejeita lote **inteiro**, não linhas soltas (decisão do ADR-006; ver ponto fraco 2).
- A regra G1-02 **recalcula** o `event_id` de cada linha: se alguém alterasse um valor no
  ficheiro, o id deixava de bater.
- A regra G1-06 só apanha **lixo** (datas absurdas). Um relógio 10 minutos adiantado passa,
  porque detetá-lo é trabalho do gate seguinte (G2, Fase 5).

### 3.5 `lake.py`: o "já está feito" é sucesso

Cada método é uma chamada ao lakeFS. O truque está em `commit` (**linha 29**) e `merge`
(**linha 39**): quando o lakeFS responde "no changes", devolvem `False` em vez de dar
erro. Repetir uma operação já feita não é um problema, **conta como feita**. Isto foi
descoberto num protótipo contra o lakeFS real, antes de escrever o writer.

### 3.6 `writer.py`: o coração (lê este duas vezes)

👉 **Imagem 9.** `process` (**linha 23**) percorre o ciclo de um lote, **sempre do
princípio**, com um passo de cada vez:

    branch → upload → commit → ler de volta e validar (G1) → merge → exportado

A ideia que sustenta tudo: **cada passo é repetível**. Por isso "retomar um lote a meio" e
"fazê-lo outra vez desde o princípio" são a mesma coisa. Não há lógica de recuperação
separada, o que a torna muito mais difícil de errar.

Duas coisas a notar:

- **`self.hook(...)`** aparece depois de cada passo. Em produção não faz nada. Nos testes
  serve para **simular um crash** em qualquer ponto: é assim que o teste mata o writer em 24
  sítios diferentes.
- **`_reject`, linha 55**: se o G1 falhar, grava o relatório **no branch**, deixa-o por
  juntar e marca o lote como `rejected`. O `main` nunca é tocado.

`run_once` (linha 66) retoma os lotes por acabar e só depois forma novos.

❓ *O que acontece se o writer morrer depois do merge mas antes de marcar `exported`?*
(Ao reiniciar repete o lote: o merge devolve "nada a juntar", que conta como feito.)

### 3.7 `collector.py`: ouvir sem perder

👉 **Imagem 11.** Divide-se em duas classes:

- **`_Handler`, linha 35**: chamado pelo `asyncua` a cada alteração. Não escreve nada: só põe a
  amostra na fila.
- **`_Queue`, linha 45** (e `flush_forever`, **linha 62**): a fila das amostras ainda não gravadas.
  Grava-as no spool e **só as tira da fila depois de gravadas**. Se o Postgres falhar, ficam em
  memória e tenta-se outra vez a cada segundo. É a peça que compensa o spool ter passado a
  depender da rede.
- **`Collector.run`, linha 87**: o ciclo de reconexão ao gateway, com espera crescente (1 s, 2 s,
  4 s… até 30 s).
- **`_session`, linha 107**: liga, descobre as variáveis, subscreve e fica num batimento de
  2 em 2 s. Repara em `OverflowPolicy.DISCONNECT`: se a fila local encher, **desliga-se** e
  reconecta, em vez de deitar amostras fora em silêncio (o comportamento por omissão).

### 3.8 `main.py`

Só liga as peças. O `inject-bad` serve para provar que o G1 trava um lote com uma amostra
malformada.

---

## 4. Como ver isto a funcionar

```
../platform/scripts/ui.sh --open                                      # lakeFS em http://localhost:8082
kubectl -n ingest exec deploy/ingest -c collector -- ingest status   # estado do spool
./tests/phase3/run.sh                                                # reconcilia spool com lakeFS
cd ingest && uv run pytest -v                                        # 27 testes (os do lakeFS real precisam de variáveis)
```

No lakeFS (porto 8082) vê o repositório `industrial-lake` → ramo `main` → pasta `bronze/`.
Cada ficheiro `.parquet` é um lote; o nome é o `batch_id`.

---

## 5. Onde melhorar (para decidirmos juntos)

👉 **Imagem 12.** Os pontos fracos, ordenados. Os que eu atacava primeiro:

| # | Ponto | Porquê agora |
|---|---|---|
| 1 | **Perda com o collector parado** | é o único buraco real nas "3 promessas": o OPC UA não guarda histórico |
| 2 | **Um lote mau trava os bons** | uma só amostra malformada põe 60 s de dados em quarentena |
| 3 | **O spool nunca limpa** | cresce sem fim; é fácil de resolver |

Os outros (admin, latência, métricas, um merge por lote) são válidos, mas não bloqueiam
nada e encaixam melhor nas Fases 7 e 9.

---

## 6. Perguntas para teres a certeza de que percebeste

1. Porque é que o `event_id` **não** inclui o `ingest_ts`?
2. O que garante que dois processos não atribuem a mesma amostra a dois lotes?
3. Se o writer morrer a meio do `upload`, o que fica no lakeFS e o que faz o reinício?
4. Porque é que o lote rejeitado deixa o branch por juntar, em vez de o apagar?
5. O que acontece às amostras enquanto o collector está desligado? (Este é o ponto fraco 1.)
6. O que acontece às amostras se o Postgres cair com o collector a correr? (Ficam em memória, na `_Queue`, e repetem-se.)
