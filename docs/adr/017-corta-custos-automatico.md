# ADR-017: corta-custos automático (quotas a zero e VM desligada a €1)

**Estado:** aceite e implementado (2026-11-16) · **Liga a:** ADR-013 (backups), ADR-016 (pedidos grátis)

## Contexto
A conta da Oracle é **pay-as-you-go**: se algo passar os limites grátis, a Oracle cobra, não bloqueia. Até aqui
tínhamos só avisos (orçamento a €0,01 real e €0,05 previsto, alarmes de pedidos e de espaço). Um aviso depende de
alguém ler o email a tempo. O Stéphane pediu um mecanismo que **pare sozinho** e escolheu (2026-11-16):
- **ação:** quotas a zero + desligar a VM, **nunca apagar dados**;
- **limite:** €1,00 no mês.

## Decisão
1. **Um CronJob horário no cluster** (`envs/oracle/ops/cost-guard.yaml`), com a identidade da VM (instance
   principal), sem chaves nem imagens novas:
   - lê o gasto real do mês no orçamento da conta (API de Budgets, grátis);
   - publica-o como `cost_guard_spend`, o que também serve de sinal de vida.
2. **Se o gasto chegar a €1,00 e o mês não tiver sido "reconhecido":**
   - cria a quota `cost-guard-zero`, que põe a zero 48 famílias de serviços pagos (compute, block storage, object
     storage, bases de dados, balanceadores, Vault, IA, etc.). Ficam de fora as famílias de governo da conta
     (compartimentos, identidade, limites, regiões), para se poder sempre recuperar;
   - faz **SOFTSTOP** à própria VM: um encerramento ordenado (o k3s tem encerramento gracioso), para o lakeFS e o
     SeaweedFS não ficarem corrompidos. Com a VM parada não corre nenhum job, logo não há pedidos ao Object Storage.
   - **Nunca apaga nada:** dados, buckets, volumes e backups ficam.
3. **Permissões mínimas** (`terraform/oracle/cost-guard.tf`):
   - ler orçamentos;
   - criar e consultar quotas, mas **não alterar nem apagar** (uma VM comprometida não consegue levantar uma quota);
   - ações de energia em instâncias (a VM é a única da conta).
4. **Alarmes:** "COST LIMIT reached" quando `cost_guard_spend ≥ 1`, e "cost guard not running" quando não há valor
   há 3 h.
5. **Retoma** por uma pessoa, a partir do Mac (`scripts/cost-guard-resume.sh`):
   - apaga a quota;
   - etiqueta a VM com `cost-guard-ack=<AAAA-MM>:<gasto nesse momento>`;
   - liga a VM.

   O guarda lê a etiqueta nos metadados da instância. Nesse mês o limite passa a ser o gasto no momento da retoma
   mais €1,00. Sem a etiqueta voltaria a desligar a VM uma hora depois, porque o gasto do mês continua acima de
   €1,00. Mas também nunca fica sem teto (11.ª revisão; antes, um mês retomado ficava só a avisar). No mês seguinte
   o limite volta a €1,00 sozinho.
6. **Robustez (11.ª revisão):**
   - o gasto é lido com o `float` do Python, por isso um valor como `3.4e-07` não faz o guarda falhar;
   - uma falha a publicar a métrica só fica registada: o guarda decide na mesma.

## Verificado (2026-11-16)
- **Dry-run na VM real** com o limite a €0,001: o guarda leu €0,0198 e respondeu "would create quota cost-guard-zero
  (48 families) and SOFTSTOP".
- **As 48 regras de quota** foram aceites pela Oracle, criadas pela própria VM (prova da permissão) num compartimento
  vazio de teste. A quota e o compartimento foram apagados logo a seguir.
- **Corrida real:** €0,0198, abaixo do limite, nada a fazer.
- **Teste real completo** (07:21, a pedido do Stéphane), com o limite a €0,001:
  - o guarda criou a quota `cost-guard-zero` e a VM passou a STOPPED em ~1,5 min, com encerramento ordenado;
  - `scripts/cost-guard-resume.sh` apagou a quota e ligou a VM: RUNNING em 15 s;
  - em ~10 min as 19 aplicações voltaram Synced/Healthy, o lakeFS respondeu "alive!", o SeaweedFS arrancou sem
    erros e o writer voltou a exportar lotes;
  - a plataforma esteve parada ~8 min.
  - Corrigido a seguir: o script só põe a etiqueta `cost-guard-ack` se o gasto passar mesmo €1,00, para um teste
    não desarmar o guarda no resto do mês.

## Alternativas
- **Oracle Functions + evento do orçamento** (o que a própria Oracle sugere, "Zero Quota Policy Creator"): reage
  ao alerta em vez de perguntar de hora a hora. Mas precisa de uma imagem no registo da Oracle e de um token de
  autenticação para a enviar, mais uma peça a manter e a pagar se passar os limites. O CronJob usa o que já existe.
- **Apagar recursos ao passar o limite:** destrói backups para poupar cêntimos, e os únicos custos possíveis vêm dos
  pedidos, que param com a VM parada. Rejeitado pelo Stéphane.
- **Só avisos:** dependem de alguém ler o email a tempo.

## Consequências e limites
- **A faturação chega com 6 a 24 h de atraso.** O guarda põe um teto a um descontrolo, não evita os primeiros
  cêntimos; isso é trabalho dos avisos a €0,01.
- **Com a VM parada, toda a plataforma na Oracle para**, incluindo a ingestão: o que o PLC produzir nesse intervalo
  não é recolhido. É o preço de garantir custo zero.
- **Com a quota ativa** falha o backup semanal do volume de arranque e não se cria nada novo. O que existe continua.
- **O que ainda pode custar com tudo parado:** o espaço guardado, se passasse os 20 GB grátis. O alarme dos 15 GB
  avisa muito antes, e a regra de 9 dias continua a apagar pacotes antigos.
