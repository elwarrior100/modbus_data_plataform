# ADR-015: retenção e crescimento do lake

**Estado:** implementado (2026-11-15). Falta só ligar a remoção da GC (`GC_SWEEP=true`) depois de ler o relatório de 22-11.
**Liga a:** ADR-007 (retenção de branches), ADR-013 (backups), ADR-014 (ensaio de restauro).

## Contexto
Medição de 2026-11-14 na Oracle:
- o lake tinha 33 MB ao fim de 9,5 h de recolha, cerca de 80 MB por dia ou 30 GB por ano;
- 30% desse espaço são metadados do lakeFS, porque a escrita faz um commit por minuto.

A este ritmo, três limites chegam em cerca de meio ano:
- o alarme dos 12 GB na cópia do lake, em cerca de 5 meses;
- os 20 GB grátis de Object Storage, em cerca de 7–8 meses;
- o disco da VM, em cerca de 6 meses.

**Medição corrigida (2026-11-15):** a cópia do lake tinha 226 MB e 11 392 objetos ao fim de cerca de 30 h, ou seja
cerca de **180 MB por dia**, o dobro da primeira estimativa. Sem retenção, os 12 GB chegavam em cerca de 2 meses.

## Decisão
1. **Bronze crua: 15 dias** (decisão do Stéphane).
   - Todos os dias às 03:30 UTC, o DAG `lake_housekeeping` tira do `main` as partições `bronze/dt=D/` com D anterior
     a hoje − 15. Faz isso num ramo `retention-<data>`, com commit e merge, porque o `main` está protegido.
   - O hook do G2 só olha para a silver, por isso um merge que apaga bronze passa.
   - A **silver** e as **atestações dos gates** (`_gates/`) ficam para sempre.
   - Primeira remoção real: na Oracle a 2026-11-30 (os dados começam a 2026-11-14); no Mac, 15 dias depois do
     primeiro dia de dados.
2. **Garbage collection do lakeFS** (feita a 2026-11-15; `envs/oracle/lakefs-gc/`).
   - Apagar no `main` só retira os objetos do commit novo. Os commits antigos continuam a referenciá-los, por isso
     o espaço não volta.
   - A GC do lakeFS Community é um job Spark. Com a regra `default_retention_days: 15`, apaga os objetos que nenhum
     commit dos últimos 15 dias referencia.
   - CronJob semanal (domingo, 05:00 UTC) no namespace `data`, só na Oracle: o lake do Mac perdeu metadados nos
     incidentes de novembro (ADR-008), e a GC tem de ler a história toda.
   - Imagem própria `lakefs-gc` (Spark 3.5.9 com o cliente lakeFS 0.22.0 e o S3A), construída no CI com checksums.
     Assim o pod que tem as chaves nunca precisa de internet.
   - **Riscos aceites até 2027-02-15** (`images/lakefs-gc/.trivyignore`): netty e avro com CVEs CRITICAL, embutidos
     em jars de que a GC precisa e que não se podem trocar. Mitigação: o pod não tem saída para fora do cluster nem
     aceita ligações, o Spark só escuta em 127.0.0.1, corre sem root com o disco só de leitura, e existe poucos minutos
     por semana. Na data de validade o CI volta a falhar e a decisão é revista.
   - Primeira corrida (só a marcar): sucesso em 4 minutos, 2 objetos marcados.
   - Para libertar a bronze, a limpeza dos ramos `etl-*` passou de simulação a real (152 ramos apagados no Mac).
     O lakeFS só apaga um objeto quando já não está na ponta de nenhum ramo.
   - Primeiro corre só em modo de marcação (sem apagar), para se rever a lista.
3. **Cópia imutável do lake** (`platform-lake-backup`).
   - A VM continua sem poder apagar lá: é a proteção contra uma VM comprometida.
   - A poda faz-se com a identidade da pessoa, a partir do Mac, todos os meses (`scripts/lake-backup-prune.sh`):
     remove os objetos que já não existem no lake e foram copiados há mais de 30 dias. Por omissão só simula; para
     apagar pede o número escrito; recusa uma lista do lake vazia ou apagar mais de metade da cópia.
   - O alarme dos 12 GB é o lembrete se a poda falhar.
4. **Menos metadados** (a avaliar): juntar os commits da bronze, por exemplo de 10 em 10 minutos.

## Alternativas
- **Regra de ciclo de vida no bucket da cópia:** apagaria objetos antigos que o lake ainda usa (a silver do primeiro
  dia, por exemplo). A cópia ficaria incompleta até à cópia da noite seguinte, que é exatamente quando um restauro
  falharia.
- **Uma identidade OCI própria para a poda, com a chave no cofre:** a VM lê o cofre, por isso seria o mesmo que
  deixar a VM apagar.
- **Guardar tudo para sempre:** chega aos limites grátis em menos de um ano.

## Consequências
- Não se consegue voltar a ler commits com mais de 15 dias, depois de a GC correr. A silver atual, que fica no `main`,
  não se perde.
- Reprocessar a silver só é possível para os últimos 15 dias de bronze.
- Um passo manual por mês (a poda da cópia), com alarme se for esquecido.
