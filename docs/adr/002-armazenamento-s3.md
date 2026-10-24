# ADR-002 — Armazenamento S3 on-prem: SeaweedFS

**Data:** 2026-10-24 · **Estado:** aceite (substitui a escolha inicial "MinIO" do plano)

## Contexto
O lakeFS precisa de um armazenamento de objetos com API S3. O plano previa MinIO.
Ao instalar (2026-10-24) verificou-se que a MinIO deixou de publicar imagens: o
repositório `quay.io/minio/minio` exige autenticação e o do Docker Hub não existe; nem
`latest` nem uma versão fixa antiga puxam.

## Alternativas
| Opção | Prós | Contras |
|---|---|---|
| **SeaweedFS** | Apache-2.0, maduro, imagem multi-arquitetura mantida, gateway S3 | Menos "caixa de fábrica" que o MinIO; compatibilidade com lakeFS a validar |
| MinIO compilado do código | Mantém o plano | AGPL, build própria sem atualizações, consola recente com funcionalidades removidas |
| RustFS | S3, Apache-2.0 | Projeto jovem, menos testado |
| Ceph | Muito robusto | Pesado demais para 16 GB |

## Decisão
SeaweedFS (`weed server` com master, volume, filer e gateway S3 num só pod).

## Consequências
- A compatibilidade com o lakeFS é validada no spike da Fase 0 (escrita, leitura e
  glob a partir do DuckDB). Se falhar, reabre-se este ADR.
- O lakeFS usa um único bucket (`lakefs-data`); bronze/silver/gold vivem como prefixos
  dentro do repositório lakeFS, não como buckets separados.
- Nome do serviço no cluster: `seaweedfs:8333` (S3).
