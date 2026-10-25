# Espaço de endereços OPC UA — gateway OT

Contrato entre o gateway (Fase 2) e quem consome OPC UA (collector da Fase 3, UaExpert,
testes). O gateway lê o simulador por Modbus (`docs/register-map.md`) e publica isto.

## 1. Servidor

| Parâmetro | Valor |
|---|---|
| Endpoint | `opc.tcp://<host>:4840/ot-gateway` |
| Application URI | `urn:industrial-lake:ot-gateway` |
| Namespace de dados | `urn:industrial-lake:ot` (índice 2) |
| Segurança | nenhuma (`SecurityPolicy#None`), **só para desenvolvimento**; certificados na Fase 9 |
| Escrita por clientes | desativada: todas as variáveis são só de leitura |

## 2. Árvore

    Objects
     └─ Plant
         ├─ PUMP-01        NodeId ns=2;s=PUMP-01
         ├─ MOTOR-01
         └─ CHILLER-01

Cada ativo tem variáveis com NodeId de texto `ns=2;s=<ATIVO>.<sinal>`, por exemplo
`ns=2;s=PUMP-01.outlet_pressure`. O `BrowseName` é o nome do sinal.

| Variável | Tipo OPC UA | Origem | Propriedades |
|---|---|---|---|
| medidas analógicas (`outlet_pressure`, `flow_rate`, …) | Double | registo × escala | `EURange`, `EngineeringUnits` |
| `run_hours` | UInt32 | registo uint32 | `EngineeringUnits` |
| `state` | UInt16 | registo (enum) | `EnumStrings` |
| `alarm` | Boolean | coil 0 (só bomba e motor) | — |
| `device_clock` | DateTime | registos 20–21 | — |
| `Connected` | Boolean | resultado do último ciclo de leitura | — |

Os nomes dos sinais, tipos, escalas e intervalos são os de `docs/register-map.md`; um teste
compara as duas fontes.

### 2.1 Unidades (`EngineeringUnits`)

`EUInformation` com `NamespaceUri = http://www.opcfoundation.org/UA/units/un/cefact`. O
`UnitId` é o da tabela oficial `UNECE_to_OPCUA.csv` da OPC Foundation.

| Unidade no mapa | Código UNECE | UnitId | Nome mostrado |
|---|---|---|---|
| bar | `BAR` | 4342098 | bar |
| m3/h | `MQH` | 5067080 | m³/h |
| A | `AMP` | 4279632 | A |
| degC | `CEL` | 4408652 | °C |
| mm/s | `C16` | 4403510 | mm/s |
| rpm | `RPM` | 5394509 | r/min |
| N.m | `NU` | 20053 | N·m |
| kW | `KWT` | 4937556 | kW |
| h | `HUR` | 4740434 | h |
| s | `SEC` | 5457219 | s |

## 3. Tempo

- **`SourceTimestamp`** = `device_clock` do dispositivo (UTC). É o único relógio que o
  dispositivo tem; num `clock_skew` o `SourceTimestamp` desvia-se do `ServerTimestamp`.
- **`ServerTimestamp`** = instante em que o gateway escreveu o valor.
- Quando não há leitura válida (estado `Bad`), `SourceTimestamp` é o instante em que o
  gateway detetou a falha.

## 4. Estados (`StatusCode`): o mapeamento que importa

Regra de ouro: **uma falha nunca se apresenta como `Good`, nem com um valor antigo.**
Quando o estado é `Bad`, o valor é omitido (`None`), para que um consumidor que ignore o
estado não tenha um número velho para usar por engano.

| Situação | StatusCode | Valor |
|---|---|---|
| Leitura bem-sucedida, dentro do `EURange` | `Good` | valor |
| Leitura bem-sucedida, fora do `EURange` | `UncertainEngineeringUnitsExceeded` | valor |
| Sem resposta (timeout; `comms_gap`, `power_cycle`, linha cortada) | `BadNoCommunication` | omitido |
| Exceção Modbus 0x02 (endereço ilegal) | `BadConfigurationError` | omitido |
| Exceção Modbus 0x04 ou 0x06 (falha do dispositivo) | `BadDeviceFailure` | omitido |
| Outras exceções Modbus | `BadCommunicationError` | omitido |
| Ainda não houve nenhum ciclo de leitura | `BadWaitingForInitialData` | omitido |

Uma trama RTU com CRC errado é descartada pela biblioteca Modbus e vê-se como falta de
resposta (`BadNoCommunication`); não é distinguível de um timeout.

`Connected` fica `false` enquanto o último ciclo de um ativo falhou. O estado `FAULT` de um
ativo **não** torna as medidas `Bad`: a medição é válida, e a avaria vê-se na variável
`state`. A deteção de um `sensor_stuck` não é feita aqui (o gateway vê valores válidos);
é trabalho dos gates de qualidade da Fase 5.

## 5. Leitura e subscrições

- O gateway lê cada ativo de `poll_interval_s` em `poll_interval_s` (por omissão 1 s), em
  blocos contíguos (uma leitura por intervalo de registos e uma por coil).
- Cada variável só é reescrita quando o valor ou o estado muda, para que as subscrições
  de clientes (com `DataChangeFilter` e `deadband`) funcionem como esperado.
- O timeout Modbus é de 1 s e sem repetições: uma falha aparece como `Bad` em ≈1–2 s.

## 6. Critérios de verificação

O contrato está cumprido quando um cliente OPC UA independente (sem código do gateway):

1. navega `Objects > Plant > <ativo>` e vê todas as variáveis com `EURange` e unidades;
2. subscreve `PUMP-01.outlet_pressure` com `deadband` e só recebe alterações acima dele;
3. com o simulador parado ou em `comms_gap`, vê as variáveis passarem a `BadNoCommunication`
   em poucos segundos, sem valor;
4. depois de o simulador voltar, vê `Good` outra vez;
5. com `clock_skew`, vê o `SourceTimestamp` afastado do `ServerTimestamp`.
