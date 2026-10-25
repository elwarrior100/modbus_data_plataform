# ADR-005 — GitOps com Argo CD (app-of-apps)

**Data:** 2026-10-25 · **Estado:** aceite (antecipa o ADR-015 do plano, previsto para a Fase 9)

## Contexto
Os manifestos de `platform/k8s/` eram aplicados à mão com `kubectl apply`. A cada fase o
cluster e o git divergiriam sem ninguém dar por isso.

## Alternativas
| Opção | Prós | Contras |
|---|---|---|
| **Argo CD** | Interface que mostra estado e diferenças; *self-heal*; padrão de mercado | ~0,6 a 1 GB de RAM |
| Flux CD | Mais leve, sem interface | Pouco visível para demonstrar; menos intuitivo para aprender |
| `kubectl apply` à mão | Zero infraestrutura | Sem deteção de divergência nem rollback por git |

## Decisão
Argo CD 3.5 (chart 10.11.1), instalado por `platform/scripts/install-argocd.sh`, com limites
de memória e sem Dex nem notificações. Estrutura **app-of-apps**: a aplicação `root`
(`platform/argocd/root-app.yaml`) gere as de `platform/argocd/apps/`: `namespaces`, `data`
e `ot`. O repositório é privado e lido por SSH com uma *deploy key* de só leitura
(`platform/scripts/argocd-repo-access.sh`).

## Consequências
- **`prune` desligado em `namespaces` e `data`**: um manifesto apagado do git nunca apaga
  um namespace nem uma base de dados. Ligado em `ot` (sem estado).
- **Segredos continuam fora do git** (`bootstrap-secrets.sh`); o Argo não os gere nem os
  apaga. SOPS ou sealed-secrets ficam para a Fase 9.
- **As imagens continuam a ser construídas à mão** (`build-image.sh`). Mudar a tag no
  manifesto e fazer *push* é o que atualiza o cluster; o *build* automático é CI (Fase 9).
- `ignoreDifferences` nos `volumeClaimTemplates` dos StatefulSets: o Kubernetes preenche
  campos que o git não tem e o Argo mostraria falsas diferenças.
- Um repositório privado só funciona depois de o `main` ter sido enviado para o GitHub.
