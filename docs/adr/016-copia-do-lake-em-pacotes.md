# ADR-016: cópia do lake em pacotes, para caber nos pedidos grátis

**Estado:** aceite e implementado (2026-11-15) · **Liga a:** ADR-013 (backups), ADR-014 (ensaio), ADR-015 (retenção)

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
   - junta só os objetos novos em `bundles/<hora>-incr.tar.gz`; no dia 1 de cada mês junta tudo em
     `bundles/<hora>-full.tar.gz`, para um restauro nunca precisar de mais de um mês de pacotes;
   - escreve um catálogo novo.

   São cerca de 4 pedidos por noite. Os nomes levam a hora, por isso nada se substitui. A VM continua sem poder
   apagar nem reescrever na cópia.
2. **O ensaio de restauro lê o catálogo mais recente** e descarrega só os pacotes de que precisa: 1 pedido por
   pacote, no máximo cerca de um mês deles.
3. **A poda mensal** (feita por uma pessoa, a partir do Mac):
   - guarda os dois últimos ciclos mensais e apaga os pacotes e catálogos mais antigos;
   - retira a cópia antiga, objeto a objeto, quando houver um pacote completo com pelo menos 7 dias;
   - conta os pedidos que vai gastar e tem um teto (`--max`).
4. **Alarme de pedidos** (`objectstorage_requests_high`): mais de 1 500 num dia, o que dá um ritmo de 45 mil por
   mês, dispara um email antes de haver custo.

## Resultado medido (2026-11-15)
- **Primeiro pacote completo:** 13 139 objetos, 222 MB, num só ficheiro.
- **Incremental minutos depois:** 14 objetos, 400 KB.
- **Ensaio a partir de 2 pacotes:** 13 153 objetos restaurados; os 1890 objetos do `main` têm todos o MD5 certo.

## Alternativas
- **Ensaio semanal em vez de diário:** um alarme de ausência da OCI espera no máximo 3 dias, por isso o ensaio
  ficaria sem alarme. E a cópia noturna continuaria a passar o limite sozinha.
- **Juntar os commits da bronze, por exemplo de 10 em 10 minutos:** reduz os ficheiros a um décimo, mas sozinho não
  chega. Fica em aberto (decisão do Stéphane).
- **Pagar os pedidos:** cerca de €0,15–0,30 por mês e a crescer. Contra o requisito de custo zero.

## Consequências
- A cópia antiga, objeto a objeto (cerca de 12 900 objetos), fica até ser podada. Apagá-la gasta cerca de 13 mil
  pedidos, por isso faz-se no início de um mês (dezembro).
- Um restauro real usa a mesma lógica do ensaio: o catálogo mais recente, os pacotes dele, e depois o dump do lakeFS.
- Um pacote completo tem o tamanho do lake. A retenção de 15 dias e a GC (ADR-015) mantêm-no limitado, e o alarme dos
  12 GB avisa se crescer demais.
