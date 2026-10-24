# ADR-001 — Runtime de Kubernetes: k3s numa VM Lima

**Data:** 2026-10-24 · **Estado:** aceite

## Contexto
A plataforma tem de correr on-prem e o desenvolvimento faz-se num MacBook (12 cores,
36 GB RAM). Kubernetes precisa de Linux. A Fase 1 precisa também de portas série virtuais
(`socat`), que não existem de forma útil no macOS.

## Alternativas
| Opção | Prós | Contras |
|---|---|---|
| **k3s numa VM Lima** | Linux a sério, armazenamento e rede realistas, reconstruível por ficheiro de config | Reserva RAM fixa à VM |
| k3d (k3s em Docker) | Arranque rápido | "Nó" é um contentor dentro da VM do Docker Desktop; volumes e rede menos fiéis a on-prem; sem Linux real para o RS-232 |
| Cluster em Raspberry Pi | Máximo realismo | Hardware e rede extra, ARM e RAM limitadas |

## Decisão
k3s numa VM Lima (Ubuntu, 8 vCPU / 16 GB / 150 GB), configuração versionada em
`platform/lima/`.

## Consequências
- 16 dos 36 GB ficam reservados à VM (reduzido de 24 GB por precaução em 2026-10-24); corre-se só
  o perfil `core` do plano (§9) e sobe-se a RAM quando a Fase 5 (OpenMetadata) o exigir.
- O simulador Modbus/RS-232 vive dentro da VM.
- A VM tem de ser recriável com um comando (critério da Fase 9).
