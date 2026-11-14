# ADR-015: retenção e crescimento do lake

**Estado:** em curso. A retenção da bronze está aplicada desde 2026-11-14; a GC e a poda da cópia vêm a seguir.
**Liga a:** ADR-007 (retenção de branches), ADR-013 (backups), ADR-014 (ensaio de restauro).

## Contexto
Medição de 2026-11-14 na Oracle:
- o lake tinha 33 MB ao fim de 9,5 h de recolha, cerca de 80 MB por dia ou 30 GB por ano;
- 30% desse espaço são metadados do lakeFS, porque a escrita faz um commit por minuto.

A este ritmo, três limites chegam em cerca de meio ano:
- o alarme dos 12 GB na cópia do lake, em cerca de 5 meses;
- os 20 GB grátis de Object Storage, em cerca de 7–8 meses;
- o disco da VM, em cerca de 6 meses.

## Decisão
1. **Bronze crua: 15 dias** (decisão do Stéphane).
   - Todos os dias às 03:30 UTC, o DAG `lake_housekeeping` tira do `main` as partições `bronze/dt=D/` com D anterior
     a hoje − 15. Faz isso num ramo `retention-<data>`, com commit e merge, porque o `main` está protegido.
   - O hook do G2 só olha para a silver, por isso um merge que apaga bronze passa.
   - A **silver** e as **atestações dos gates** (`_gates/`) ficam para sempre.
   - Primeira remoção real: na Oracle a 2026-11-30 (os dados começam a 2026-11-14); no Mac, 15 dias depois do
     primeiro dia de dados.
2. **Garbage collection do lakeFS** (a fazer antes de 2026-11-30).
   - Apagar no `main` só retira os objetos do commit novo. Os commits antigos continuam a referenciá-los, por isso
     o espaço não volta.
   - A GC do lakeFS Community é um job Spark. Com a regra `default_retention_days: 15`, apaga os objetos que nenhum
     commit dos últimos 15 dias referencia.
   - Primeiro corre só em modo de marcação (sem apagar), para se rever a lista.
3. **Cópia imutável do lake** (`platform-lake-backup`).
   - A VM continua sem poder apagar lá: é a proteção contra uma VM comprometida.
   - A poda faz-se com a identidade da pessoa, a partir do Mac, todos os meses: remove os objetos que já não existem
     no lake há mais de 30 dias. Antes de apagar, mostra a lista.
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
