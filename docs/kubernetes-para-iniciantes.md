# Kubernetes para quem nunca o usou

Lê isto antes de ler os manifestos em `platform/k8s/`. Cada secção tem um comando para
experimentares no teu cluster; para eles funcionarem, corre primeiro:

    export KUBECONFIG=~/.lima/industrial/copied-from-guest/kubeconfig.yaml

## 1. O problema que o Kubernetes resolve

Tens vários programas (Postgres, MinIO, lakeFS, Airflow…). Cada um tem de arrancar, ficar
vivo, reiniciar se morrer, guardar dados, e falar com os outros. Fazer isto à mão numa
máquina é frágil: reinicias o computador e metade não volta a subir.

O Kubernetes (k8s) é um **gestor** que faz isso por ti. A ideia central é uma só:

> Tu **descreves o que queres** (estado desejado). O Kubernetes **trabalha sem parar** para
> que a realidade seja igual à descrição.

É como um termóstato: dizes "quero 21 graus", e ele liga e desliga o aquecimento sozinho.
Tu não ligas o aquecimento; defines o objetivo.

Por isso se escrevem ficheiros YAML e não comandos passo a passo. O YAML é a descrição.
O comando `kubectl apply -f ficheiro.yaml` entrega-a ao cluster.

## 2. Os blocos de construção (do mais pequeno ao maior)

**Contentor.** Um programa empacotado com tudo o que precisa para correr (uma "imagem").
O Postgres vem numa imagem; não instalas nada no sistema.

**Pod.** A unidade mínima do k8s: um invólucro à volta de um (às vezes mais) contentores.
É **descartável**: pode morrer e ser recriado a qualquer momento, com outro IP. Nunca
trates um pod como uma coisa que dura.

    kubectl get pods -A

**Node (nó).** Uma máquina onde os pods correm. No teu caso é uma só: a VM Lima.

    kubectl get nodes

**Cluster.** O conjunto dos nós mais o "cérebro" que decide onde cada pod corre. O teu
cluster é um nó, mas o conceito é o mesmo de um com cem.

**Namespace.** Uma pasta lógica para separar recursos. Pods em namespaces diferentes não se
chocam nos nomes. Tens `kube-system` (o próprio k8s) e `data` (o teu armazenamento).

    kubectl get ns

## 3. Quem mantém os pods vivos

Raramente crias um pod diretamente. Crias um objeto que o gere:

**Deployment.** "Quero 1 cópia deste programa sempre a correr." Se o pod morrer, cria outro.
Serve para programas **sem estado** (um servidor web, o lakeFS).

**StatefulSet.** Igual, mas para programas **com estado** (uma base de dados). Dá ao pod um
nome fixo (`postgres-0`) e liga-o **sempre ao mesmo volume**, mesmo depois de recriado.

**DaemonSet.** "Quero um pod destes em cada nó." Usa-se para coisas de infraestrutura.

## 4. Como os pods falam entre si

Os pods mudam de IP quando são recriados, por isso nunca se usa o IP.

**Service.** Um nome estável e um IP fixo à frente de um grupo de pods. Quando o lakeFS
quer o Postgres, liga-se a `postgres:5432`; o Service encaminha para o pod certo, esteja
ele onde estiver. O k8s sabe quais pods pertencem ao Service através de **etiquetas**
(labels): o Service diz "quero pods com `app: postgres`" e o pod traz essa etiqueta.

    kubectl -n data get svc

## 5. Onde ficam os dados

Um pod é descartável, e o que está dentro dele **desaparece com ele**. Para a base de dados
não perder tudo:

**PersistentVolumeClaim (PVC).** Um pedido de disco: "preciso de 20 GB." A **StorageClass**
(`local-path` no teu cluster) satisfaz o pedido criando uma pasta no disco da VM. O pod
monta esse disco; se o pod morrer, o disco fica e o pod novo volta a encontrá-lo.

    kubectl -n data get pvc

## 6. Configuração e segredos

Não se mete configuração dentro da imagem. Separa-se:

**ConfigMap.** Configuração não sensível (um ficheiro, um script, opções).

**Secret.** O mesmo, para coisas sensíveis (palavras-passe). Atenção: está só em base64,
**não cifrado**. Quem tem acesso ao cluster lê-o. Não é um cofre; é uma forma de **não
escrever palavras-passe nos ficheiros que vão para o git**.

Ambos chegam ao pod como variáveis de ambiente ou como ficheiros montados.

## 7. Saúde e recursos

**Probes.** Perguntas que o k8s faz ao programa. A *readiness probe* ("já estás pronto?")
decide se o pod recebe tráfego; enquanto falhar, ninguém lhe fala. Foi isto que fez o
`rollout status` esperar até o Postgres estar mesmo vivo.

**Requests e limits.** `requests` é o mínimo que o pod reserva; `limits` é o teto. Se um
pod ultrapassa o limite de memória, o k8s mata-o. É o que impede um serviço de comer a RAM
dos outros numa VM de 16 GB.

## 8. Os cinco comandos que vais usar mais

    kubectl get pods -n data            # o que está a correr
    kubectl describe pod <nome> -n data # porque é que está assim (vê "Events" no fim)
    kubectl logs <nome> -n data         # o que o programa escreveu
    kubectl apply -f ficheiro.yaml      # entrega a descrição ao cluster
    kubectl delete pod <nome> -n data   # apaga; o Deployment/StatefulSet recria

Regra de ouro para erros: **`describe` primeiro, `logs` depois.** O `describe` diz-te se o
problema é do k8s (imagem não encontrada, sem memória, volume em falta); os `logs` dizem-te
se é do programa.

## 9. Mapa mental para o teu projeto

    Mac (kubectl)  ──►  VM Lima  ──►  k3s (o cluster)
                                        │
                              namespace "data"
                                        │
         ┌──────────────────────────────┼─────────────────────────┐
         │                              │                         │
    StatefulSet postgres        (a seguir) MinIO            (a seguir) lakeFS
      ├─ Pod postgres-0           mesmo padrão             Deployment + Service
      ├─ PVC (20 GB)
      ├─ Secret (palavras-passe)
      └─ Service "postgres"

Repara que o padrão é sempre o mesmo: **programa + disco + segredo + nome**. Quando
perceberes o Postgres, percebeste 80% do MinIO e do lakeFS.

## 10. Experiência de 2 minutos (recomendada)

Para veres a "reconciliação" com os teus olhos:

1. `kubectl -n data get pods` — vês o `postgres-0`.
2. `kubectl -n data delete pod postgres-0` — apagas o pod.
3. `kubectl -n data get pods -w` — vês o k8s a recriá-lo sozinho (Ctrl-C para sair).
4. Os dados continuam lá, porque o volume não foi apagado.

Isto é o estado desejado em ação: tu apagaste a realidade, o k8s restaurou-a.
