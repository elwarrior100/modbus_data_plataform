# ADR-014: acesso pelo browser, saída restrita e isolamento por omissão

**Estado:** aceite e implementado (2026-11-14) · **Liga a:** ADR-005 (GitOps), ADR-011 (cofre), ADR-013 (backups)

## Contexto
A 5.ª e a 6.ª revisões críticas (77,75 e 79,6/100) apontaram quatro coisas:
- abrir as interfaces exigia túneis (`ui.sh`);
- o backup tinha a chave de administrador do SeaweedFS e podia sair para qualquer endereço na porta 443;
- a aplicação `root` estava no projeto `default` do Argo CD, que deixa fazer tudo;
- um namespace novo nasce sem NetworkPolicy, por isso chega ao endpoint de metadados e, por ele, à identidade da VM.

## Decisão
1. **Ingress pelo Traefik** (já vem com o k3s), por nome:
   - no Mac: `http://<serviço>.localhost`;
   - na Oracle: `http://<serviço>.100.108.9.15.sslip.io`.

   O `sslip.io` é um DNS público que devolve o IP do próprio nome, aqui o IP da VM na tailnet. Só os dispositivos do
   Tailscale lá chegam, e o IP público não responde. O SeaweedFS não é publicado, porque não tem login.
2. **Chave só de leitura** para a cópia do lake: a identidade `lake-reader` tem `Read:lake` e `List:lake`, e a chave
   está no cofre como `backup-seaweedfs-reader`. O backup deixou de ter `data-seaweedfs-auth`. Testado: escrever é
   recusado.
3. **Saída do backup e do ops só para a Oracle:** só as faixas `OSN` e `OBJECT_STORAGE` de eu-madrid-1, retiradas da
   lista pública da Oracle. As faixas `OCI` ficam fechadas, porque são VMs de outros clientes. O CI compara a lista
   todas as segundas-feiras (`check-oci-cidrs.sh`).
4. **Projeto `bootstrap`** para a `root`: só aceita os tipos do Argo CD no namespace `argocd`. O `default` fica vazio.
   O `install-argocd.sh` cria o `bootstrap` antes da `root`, e o CI verifica essa ordem (lição da 6.ª revisão: num
   cluster novo, a `root` era recusada).
5. **Isolamento por omissão:** nenhum pod arranca num namespace sem a etiqueta `platform/network-reviewed`
   (ValidatingAdmissionPolicy). Os namespaces do Kubernetes ficam de fora. O `external-secrets` não tem política,
   de propósito: precisa do OCI Vault e da identidade da VM. Fica escrito ao lado da etiqueta.
6. **Ensaio de restauro diário do lake** (`ops/lake-restore-drill`, 04:15 UTC):
   - reconstrói o lakeFS a partir do dump e da cópia dos objetos, dentro de um pod descartável;
   - compara o MD5 de cada objeto do `main`;
   - envia `lake_drill_success`, e há um alarme se faltar durante 2 dias.

   É diário e não semanal porque um alarme de ausência da OCI espera no máximo 3 dias.

7. **HTTPS com nomes `*.ts.net` (2026-11-15), por Tailscale Services na VM** e não pelo operador Kubernetes:
   - `https://<serviço>.taila669aa.ts.net`, um Tailscale Service por interface, com certificado Let's Encrypt;
   - o Tailscale da VM (etiquetada `tag:platform`) entrega ao Traefik em 127.0.0.1:80 e mantém o nome do site, por
     isso os Ingress também respondem a estes nomes;
   - configurado por `envs/oracle/tailscale-services.sh`; os nomes `sslip.io` ficam de reserva;
   - grátis no plano Personal; 0 MB no cluster.

## Alternativas
- **Operador Kubernetes do Tailscale:** recusado. Desde a versão 1.78 os seus proxies correm como contentores
  privilegiados (precisam de criar `/dev/net/tun`), o que obrigaria a abrir uma exceção à regra "sem pods
  privilegiados" para pods expostos à tailnet. O modo sem privilégios (`TS_USERSPACE`) não é suportado. Custaria
  também 100–200 MB num nó apertado. Troca: os Services vivem na VM, fora do GitOps (o script é a fonte de verdade).
- **`/etc/hosts` no Mac:** dispensa o DNS de terceiros, mas só serve num computador.
- **Bloquear o 169.254.169.254 no iptables do nó:** apanha tudo, incluindo os jobs que precisam da identidade.
- **Projetos do Argo CD aplicados só por script:** a `root` não poderia alargar o próprio projeto, mas o GitOps
  ficava com duas fontes. A proteção de ramos no GitHub faz esse papel.

## Consequências
- Há dois caminhos para as interfaces: Ingress e `ui.sh`. O `ui.sh` fica para o SeaweedFS e como alternativa.
- Se a Oracle mudar as faixas, o backup falha até se atualizar a política. O CI avisa à segunda-feira e os alarmes
  `backup_missing` e `lake_sync_missing` disparam em 26 h.
- Um namespace novo obriga a pensar na rede antes de correr: é esse o objetivo.
