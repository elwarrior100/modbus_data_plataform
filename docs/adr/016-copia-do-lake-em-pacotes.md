# ADR-016: cópia do lake em pacotes, para caber nos pedidos grátis

**Estado:** aceite e implementado (2026-11-15; revisto a 2026-11-16: completo semanal e remoção aos 9 dias) · **Liga a:** ADR-013 (backups), ADR-014 (ensaio), ADR-015 (retenção)

## Contexto
O Object Storage grátis inclui **50 000 pedidos por mês**. Em novembro a cópia do lake passou esse limite em dois
dias (cerca de 88 mil pedidos, €0,012), porque tratava o lake ficheiro a ficheiro:
- **a cópia noturna** fazia um pedido por objeto novo (cerca de 6 000 por noite, porque o lake cria milhares de
  ficheiros pequenos por dia);
- **o ensaio de restauro diário** lia todos os objetos da cópia (cerca de 11 000 por dia, e a crescer).

Ao ritmo normal seriam mais de 500 mil pedidos por mês, e a crescer com o lake.

## Decisão
1. **A cópia passa a ser feita em pacotes** (`envs/oracle/backup/lake-sync.yaml`), todas as noites:
   - lista o lake, o que é grátis porque está no cluster;
   - lê o catálogo mais recente (`catalog/<hora>.txt.gz`, uma linha "objeto, pacote" por cada objeto já copiado);
   - junta só os objetos novos em `bundles/<hora>-incr.tar.gz`; quando o completo mais recente tem 7 dias ou mais
     junta tudo em `bundles/<hora>-full.tar.gz`, para um restauro nunca precisar de mais de uma semana de pacotes;
   - escreve um catálogo novo.

   São cerca de 6 pedidos por noite. Os nomes levam a hora, por isso nada se substitui. A VM continua sem poder
   apagar nem reescrever na cópia.
2. **O ensaio de restauro lê o catálogo mais recente** e descarrega só os pacotes de que precisa: 1 pedido por
   pacote, no máximo cerca de uma semana deles.
3. **A limpeza é do próprio Object Storage** (revisto a 2026-11-16, auditoria de custos): uma regra de ciclo de vida
   apaga `bundles/` e `catalog/` com mais de 9 dias (`terraform/oracle/backups.tf`). Um completo semanal mais 2 dias
   de folga garante que o completo de que o catálogo mais recente precisa nunca é apagado antes de haver outro. A
   VM continua sem poder apagar. O `scripts/lake-backup-prune.sh` fica só para uma poda manual excecional.
4. **Alarme de pedidos** (`objectstorage_requests_high`): mais de 1 500 num dia, o que dá um ritmo de 45 mil por
   mês, dispara um email antes de haver custo.

## Resultado medido (2026-11-15)
- **Primeiro pacote completo:** 13 139 objetos, 222 MB, num só ficheiro.
- **Incremental minutos depois:** 14 objetos, 400 KB.
- **Ensaio a partir de 2 pacotes:** 13 153 objetos restaurados; os 1890 objetos do `main` têm todos o MD5 certo.

## Limites de capacidade (10.ª revisão)
- **A cópia guarda cerca de 2,5 vezes o tamanho do lake:** no máximo dois completos semanais e os incrementais dos 9
  dias (antes, com ciclos mensais e poda manual, podia chegar a ~7 vezes).
- **O lake-sync** precisa de 2 vezes o lake no volume temporário (12 Gi). **O ensaio** precisa de 1 vez em cada um
  dos seus dois volumes (20 Gi cada).
- **Para tudo continuar grátis e caber nestes volumes, o lake deve ficar abaixo de cerca de 5 GB.** Hoje tem cerca de
  0,3 GB. A retenção de 15 dias e a GC (ADR-015) é que o mantêm assim. O alarme dos 12 GB no bucket é o último aviso.
- **Um restauro real** usa o primeiro catálogo escrito **depois** do dump que se restaura (o ensaio usa o mais
  recente, que é sempre posterior ao dump das 02:30).
- **Robustez:**
  - um erro a listar a cópia faz a noite falhar, em vez de criar um pacote completo às escondidas;
  - se a noite do completo falhar, a seguinte faz o completo (a regra é "o completo mais recente tem 7 dias ou mais");
  - se não houver completo nenhum (por exemplo, apagado pelos 9 dias depois de semanas sem cópia), a noite seguinte
    faz um completo;
  - os envios a meio são abortados pelo Object Storage ao fim de 2 dias;
  - a poda nunca apaga um pacote que o catálogo mais recente use.

## Revisão de 2026-11-21: pacotes em partes
- **Problema:** um só envio aceita no máximo 5 GiB sem multipart (e o multipart precisa de `OBJECT_OVERWRITE`, que a
  VM não tem de propósito). Com o lake a crescer, o pacote completo semanal passaria esse limite por volta do fim de
  dezembro.
- **Decisão:** cada pacote divide-se em **partes de até 3 GiB de objetos**: `bundles/<hora>-<modo>-NNN.tar.gz`, um
  pedido cada. O catálogo diz a parte de cada objeto, por isso o ensaio e o restauro não mudaram. As partes vão uma de
  cada vez (no máximo uma parte no disco temporário). O catálogo é escrito no fim, só depois de todas as partes.
- **Também:** o "completo mais recente" passa a ler-se do **catálogo**, e não da lista de pacotes. Um completo que
  falhe a meio deixa partes que nenhum catálogo refere, e não pode contar como o completo da semana; senão o
  verdadeiro podia expirar aos 9 dias ainda em uso. Poupa também 1 pedido por noite.
- **Testado na Oracle:** incremental em 3 partes (partes forçadas a 20 KB); o ensaio restaurou 27 515 objetos de 10
  pacotes, com os 3 939 do `main` com o MD5 certo.
- **Falha passageira descoberta:** um 401 ao renovar o token da identidade da VM. O primeiro pedido à Oracle passa a ter
  até 5 tentativas na cópia e no ensaio.

## Alternativas
- **Ensaio semanal em vez de diário:** um alarme de ausência da OCI espera no máximo 3 dias, por isso o ensaio
  ficaria sem alarme. E a cópia noturna continuaria a passar o limite sozinha.
- **Juntar os commits da bronze, por exemplo de 10 em 10 minutos:** reduz os ficheiros a um décimo, mas sozinho não
  chega. Fica em aberto (decisão do Stéphane).
- **Pagar os pedidos:** cerca de €0,15–0,30 por mês e a crescer. Contra o requisito de custo zero.

## Consequências
- A cópia antiga, objeto a objeto (12 913 objetos, 281 MB), foi apagada a 2026-11-16 a partir do Mac, com a
  identidade da pessoa e a pedido do Stéphane (aceitou até ~€0,004 em novembro). O bucket ficou só com `bundles/` e
  `catalog/` (6 objetos, 236 MB).
- Um restauro real usa a mesma lógica do ensaio: o catálogo mais recente, os pacotes dele, e depois o dump do lakeFS.
- Um pacote completo tem o tamanho do lake. A retenção de 15 dias e a GC (ADR-015) mantêm-no limitado, e o alarme dos
  12 GB avisa se crescer demais.
