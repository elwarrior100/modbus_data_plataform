# ADR-018: precisão do relógio do equipamento e a regra G2-02

**Estado:** proposto, código por rever (2026-11-21) · **Liga a:** ADR-007 (silver), ADR-008 (gates)

## Contexto
A regra G2-02 (unicidade) avisava quando havia `(ativo, tag, instante)` repetido com valores diferentes. Deu aviso em
**todas** as horas: 196 a 287 "conflitos" por hora (~0,5% das linhas), sempre em sinais rápidos (vibração, potência,
caudal).

A causa: o `source_ts` vem do relógio do equipamento, que tem precisão de **1 segundo** (o habitual nos PLC Modbus),
e o simulador atualiza ~1 vez por segundo. Duas leituras verdadeiras, ~1 s uma da outra, ficam com o mesmo `source_ts`.
O `server_ts` do OPC UA (ms) mostra que são diferentes. Exemplo real: PUMP-01.flow_rate 219,4 e 224,2, ambas com
`source_ts` 09:02:24, e `server_ts` .839 e 25.838.

Um aviso que dispara sempre ensina a ignorá-lo: um duplicado verdadeiro passaria despercebido.

## Decisão (a solução intermédia)
1. **O `source_ts` fica honesto:** é exatamente o que o equipamento diz. O gateway não inventa milissegundos que o
   equipamento não mediu; se um equipamento os der, passam sem mudança nenhuma.
2. **O G2-02 desempata pelo `server_ts`:** só é conflito o mesmo `(ativo, tag, source_ts, server_ts)` com valores
   diferentes, ou seja, a MESMA leitura com dois valores. Isso continua a dar aviso.
3. **A silver ordena-se pelo `server_ts` dentro do mesmo segundo** (`ORDER BY ts_utc, asset_id, tag_name, server_ts, …`),
   para a sequência real das leituras ficar certa. O ficheiro muda, por isso `silver_version` passa a "2".
4. **O contrato da silver diz a precisão:** o `ts_utc` tem a precisão do relógio do equipamento; para ordenar dentro do
   mesmo segundo usa-se o `server_ts`.

## Verificado
- **Testes:** duas leituras no mesmo segundo com `server_ts` diferentes não dão aviso; o mesmo `server_ts` com dois
  valores dá. Os 45 testes dos pipelines passam.
- **Dados reais** (11 horas da silver local): 2 542 "conflitos" com a regra antiga, 0 com a nova.

## Alternativas
- **a) O gateway dá milissegundos ao `source_ts`:** no simulador era fácil; com um PLC real seria inventar precisão.
  Rejeitada.
- **b) Só desempatar no G2-02:** resolve o aviso, mas deixa a ordem dentro do mesmo segundo ao acaso. Ficou incluída
  na decisão.
- **c) Aceitar o aviso como limitação:** um aviso permanente deixa de proteger. Rejeitada.

## Consequências
- As horas reprocessadas com o código novo têm `silver_version` "2". As antigas ficam com "1" até serem reprocessadas;
  o conteúdo é o mesmo, só muda a ordem das linhas.
