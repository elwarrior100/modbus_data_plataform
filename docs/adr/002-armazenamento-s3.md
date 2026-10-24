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
- Um único bucket, `lake`, que é o *storage namespace* de **um** repositório lakeFS
  (`industrial-lake`). Bronze, silver e gold são pastas dentro dele.
- Alternativa avaliada e rejeitada (2026-10-24): três buckets, três repositórios. Ganhava
  retenção por camada e isolamento, mas perdia a atomicidade do merge entre camadas, que é
  o que sustenta o Write-Audit-Publish do plano §2. Com o volume do projeto, os ganhos não
  compensam. Reabrir se o volume do bronze tornar a retenção por camada necessária.
- Nome do serviço no cluster: `seaweedfs:8333` (S3).
