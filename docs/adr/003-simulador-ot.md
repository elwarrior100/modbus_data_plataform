# ADR-003 — Simulador OT: escravo Modbus próprio em Python

**Data:** 2026-10-24 · **Estado:** aceite

## Contexto
A Fase 1 precisa de um simulador Modbus RTU (RS-232) e TCP com cenários de falha
controláveis: silêncio total (`comms_gap`), valores que só mudam ao seu *scan*, ordens de
palavras diferentes por dispositivo, relógio do dispositivo desalinhável.

## Alternativas
| Opção | Prós | Contras |
|---|---|---|
| **Escravo próprio (funções 0x01/0x03, RTU+TCP)** | Controlo total do comportamento; sem dependência de API instável; os bytes do protocolo ficam visíveis | ~200 linhas de protocolo a manter |
| pymodbus como servidor | Menos código | Em 3.16 o `ModbusServerContext` está deprecated e devolve `DEVICE_BUSY`; a API nova (`SimDevice`) é de transição, mal documentada e sem forma clara de alterar valores em execução |
| Simulador pronto (diagslave, ModRSsim) | Zero código | Sem cenários, sem estado dinâmico, sem controlo programático |

## Decisão
Escravo Modbus próprio. O `pymodbus` é usado **apenas como cliente nos testes**: sendo uma
implementação independente, valida a nossa. Se as duas concordam, o mapa de registos
(`docs/register-map.md`) está correto.

## Consequências
- O protocolo cobre só leitura (0x01, 0x03). Escritas e outras funções respondem com a
  exceção 0x01 (função ilegal).
- Um teste automático compara as tabelas de `docs/register-map.md` com a especificação em
  código: o documento deixa de poder divergir do simulador.
- Stack: Python 3.12, `pyserial` (RS-232), `pyyaml` (cenários), `pytest`.
- Par série virtual em 8N1: no pty do Linux, configurar paridade par só funciona na
  primeira configuração do par e depois falha com `EINVAL`; num pty a paridade não tem
  efeito. A paridade fica configurável (`--parity`) para uma linha física.
- Reavaliar o uso do pymodbus como servidor quando a v4 estabilizar.
