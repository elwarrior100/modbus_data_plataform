# ADR-004 — Gateway OPC UA: asyncua + pymodbus, no mesmo contentor que a linha série

**Data:** 2026-10-25 · **Estado:** aceite

## Contexto
A Fase 2 publica em OPC UA o que o simulador expõe por Modbus (`docs/register-map.md`).
Tem de modelar unidades e intervalos, e traduzir falhas Modbus para estados OPC UA sem
nunca apresentar uma falha como `Good`.

## Decisões
| Decisão | Escolha | Alternativas | Razão |
|---|---|---|---|
| Biblioteca OPC UA | **asyncua 2.1** | open62541 (C), node-opcua | Python, mesma stack do resto; servidor e cliente (usado nos testes) |
| Cliente Modbus | **pymodbus** (assíncrono) | cliente próprio | Biblioteca madura para o lado cliente; o servidor próprio ficou na Fase 1 (ADR-003) |
| Onde corre | **mesmo contentor que o simulador** (`ot-edge`) | pod/contentor separado | Um par série virtual só existe no contentor que o criou; o gateway tem de abrir `/tmp/ttyV0` |
| Falha de comunicação | **`BadNoCommunication`, valor omitido** | `Uncertain` com o último valor | Um consumidor que ignore o estado não tem um número velho para usar por engano |
| Valor fora do `EURange` | `UncertainEngineeringUnitsExceeded` | `Good` | Medição suspeita, mas legível |
| Segurança | nenhuma (`#None`) | certificados | Só desenvolvimento; hardening na Fase 9 |

## Consequências
- O contrato é `docs/opcua-address-space.md`; testes comparam-no com o código e com o
  mapa de registos.
- O `pyserial` bloqueia a porta em exclusivo, logo só existe **um** mestre RTU por linha:
  o gateway. Verificações com outro mestre série deixam de caber dentro do pod.
- Simulador e gateway reiniciam juntos (se um terminar, o contentor termina).
- Um `sensor_stuck` é invisível ao gateway (os valores são válidos); deteta-se nos gates de
  qualidade (Fase 5).
- Armadilha encontrada: em YAML 1.1 `OFF` sem aspas é `False`. O carregador recusa etiquetas
  que não sejam texto.
