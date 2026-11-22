# Mapa de registos Modbus — simulador OT

Este documento é o **contrato** entre o simulador (Fase 1) e o gateway OPC UA (Fase 2).
O gateway é configurado a partir daqui; qualquer alteração ao simulador começa neste ficheiro.

## 1. Ligação

| Parâmetro | Valor |
|---|---|
| Modbus RTU (RS-232 virtual) | 19200 baud, 8 bits, sem paridade, 1 stop (8N1); 8E1 em linha física (`--parity E`) |
| Modbus TCP | porto 5020 no contentor; exposto como 502 no Service |
| Unit IDs | `PUMP-01` = 1, `MOTOR-01` = 2, `CHILLER-01` = 3 |

RS-232 é ponto a ponto, mas o simulador responde a três unit IDs na mesma linha, como um
conversor série de vários dispositivos. O mestre RTU é o gateway da Fase 2; o simulador é
o escravo.

Funções usadas: `0x03` (ler holding registers), `0x01` (ler coils). O simulador é só de
leitura do ponto de vista do cliente: escritas são rejeitadas com a exceção 0x01.

## 2. Convenções (onde nascem os problemas)

**Endereçamento: 0-based na linha.** A coluna `Addr` é o endereço enviado no pacote
(PDU). A coluna `Ref` é a notação clássica `4xxxx`, 1-based (`Ref = 40001 + Addr`). Um
cliente que subtraia ou some 1 por engano lê o registo vizinho, e não dá erro: devolve um
valor plausível e errado. O gateway **usa `Addr`**.

**Valores inteiros escalados.** Um registo é 16 bits. Valores com casas decimais são
guardados como inteiros: `valor real = bruto × escala`. Exemplo: `523` com escala `0.01` é
`5.23 bar`. `int16` é complemento para dois (temperaturas negativas).

**Valores de 32 bits ocupam 2 registos consecutivos.** Dentro de cada registo os bytes são
sempre big-endian (norma Modbus). A **ordem das palavras** varia por dispositivo:

| Ordem | Significado | Dispositivo |
|---|---|---|
| `ABCD` | palavra alta primeiro (registo de menor endereço = palavra alta) | `PUMP-01`, `CHILLER-01` |
| `CDAB` | palavra baixa primeiro (palavras trocadas) | `MOTOR-01` |

Exemplo `uint32` = `100000` = `0x000186A0`: em `ABCD` os registos são `[0x0001, 0x86A0]`;
em `CDAB` são `[0x86A0, 0x0001]`. Ler `CDAB` como `ABCD` dá `2 258 632 705`, sem erro.

`float32` segue IEEE 754. Valor de teste: `120.5` = `0x42F10000`, `[0x42F1, 0x0000]` em
`ABCD`.

**Não há timestamps em Modbus.** Cada dispositivo expõe um relógio próprio (secção 6)
e o gateway usa-o como `source_ts`. É assim que o cenário `clock_skew` se torna observável.

**Registos que não mudam a cada leitura.** Cada sinal só é atualizado ao seu `Scan`; entre
atualizações o registo repete o último valor. Um cliente que leia mais depressa vê valores
repetidos, e isso é comportamento normal de um autómato real, não um erro.

## 3. PUMP-01 (unit 1) — ordem de palavras `ABCD`

Holding registers (função 0x03):

| Addr | Ref | Sinal | Tipo | Escala | Unidade | EURange | Scan |
|---|---|---|---|---|---|---|---|
| 0 | 40001 | `outlet_pressure` | uint16 | ×0.01 | bar | 0 – 16 | 1 s |
| 1 | 40002 | `flow_rate` | uint16 | ×0.1 | m³/h | 0 – 500 | 1 s |
| 2 | 40003 | `motor_current` | uint16 | ×0.1 | A | 0 – 100 | 1 s |
| 3 | 40004 | `bearing_temp` | int16 | ×0.1 | °C | -20 – 150 | 5 s |
| 4 | 40005 | `vibration_rms` | uint16 | ×0.01 | mm/s | 0 – 50 | 1 s |
| 5–6 | 40006–07 | `run_hours` | uint32 | ×1 | h | 0 – 4 294 967 295 | 60 s |
| 7 | 40008 | `state` | uint16 | enum | — | ver 3.1 | à mudança |
| 8–9 | 40009–10 | `power_kw` | float32 | ×1 | kW | 0 – 100 | 1 s |
| 10 | 40011 | `inlet_pressure` | uint16 | ×0.01 | bar | 0 – 10 | 5 s |
| 20–21 | 40021–22 | `device_clock` | uint32 | ×1 | s (epoch UTC) | — | 1 s |

`power_kw` (potência elétrica) e `inlet_pressure` (pressão de aspiração) entraram a 2026-11-22 (ADR-019, D11): com
eles o gold mede o consumo específico (kWh/m³) e o rendimento da bomba.

Coils (função 0x01):

| Addr | Ref | Sinal | Descrição |
|---|---|---|---|
| 0 | 00001 | `alarm` | ligado se `vibration_rms ≥ 12` ou `bearing_temp ≥ 90`; desliga abaixo de 10 e 85 (histerese) |

### 3.1 Enum `state` da bomba e do motor

| Valor | Estado |
|---|---|
| 0 | STOPPED |
| 1 | STARTING |
| 2 | RUNNING |
| 3 | FAULT |
| 4 | MAINTENANCE |

## 4. MOTOR-01 (unit 2) — ordem de palavras `CDAB`

| Addr | Ref | Sinal | Tipo | Escala | Unidade | EURange | Scan |
|---|---|---|---|---|---|---|---|
| 0–1 | 40001–02 | `speed_rpm` | uint32 | ×1 | rpm | 0 – 3600 | 1 s |
| 2 | 40003 | `torque` | int16 | ×0.1 | N·m | -500 – 1000 | 1 s |
| 3 | 40004 | `current` | uint16 | ×0.1 | A | 0 – 200 | 1 s |
| 4 | 40005 | `winding_temp` | int16 | ×0.1 | °C | -20 – 200 | 5 s |
| 5 | 40006 | `vibration_rms` | uint16 | ×0.01 | mm/s | 0 – 50 | 1 s |
| 6 | 40007 | `state` | uint16 | enum (3.1) | — | — | à mudança |
| 7–8 | 40008–09 | `power_kw` | float32 | ×1 | kW | 0 – 100 | 1 s |
| 20–21 | 40021–22 | `device_clock` | uint32 | ×1 | s (epoch UTC) | — | 1 s |

`power_kw` (potência elétrica, desde 2026-11-22): com o binário e a velocidade dá o rendimento do motor.

Coil 0 (`alarm`): ligado se `vibration_rms ≥ 10` ou `winding_temp ≥ 120`; histerese 8 e 110.

## 5. CHILLER-01 (unit 3) — ordem de palavras `ABCD`

| Addr | Ref | Sinal | Tipo | Escala | Unidade | EURange | Scan |
|---|---|---|---|---|---|---|---|
| 0 | 40001 | `inlet_temp` | int16 | ×0.1 | °C | 0 – 40 | 5 s |
| 1 | 40002 | `outlet_temp` | int16 | ×0.1 | °C | 0 – 30 | 5 s |
| 2–3 | 40003–04 | `power_kw` | float32 | ×1 | kW | 0 – 500 | 1 s |
| 4 | 40005 | `state` | uint16 | enum | — | ver 5.1 | à mudança |
| 5 | 40006 | `chw_flow` | uint16 | ×0.1 | m³/h | 0 – 300 | 5 s |
| 20–21 | 40021–22 | `device_clock` | uint32 | ×1 | s (epoch UTC) | — | 1 s |

`chw_flow` (caudal de água gelada, desde 2026-11-22): com a diferença entre `inlet_temp` e `outlet_temp` dá a potência
de frio (caudal × 4,19 × ΔT ÷ 3,6, em kW), e com o `power_kw` o COP.

### 5.1 Enum `state` do chiller

| Valor | Estado |
|---|---|
| 0 | OFF |
| 1 | STANDBY |
| 2 | COOLING |
| 3 | FAULT |

## 6. Relógio do dispositivo (`device_clock`)

`uint32` com os segundos desde 1970-01-01 UTC, no endereço 20–21 de cada dispositivo, na
ordem de palavras desse dispositivo. É a única noção de tempo que o dispositivo oferece.
Em operação normal coincide com o relógio da VM (±1 s).

## 7. Perfil de funcionamento normal

Os valores oscilam à volta destes pontos, com ruído gaussiano pequeno e uma variação lenta
ao longo do dia (±2 % em temperaturas e potência).

| Sinal | Valor típico | Ruído (desvio) |
|---|---|---|
| `PUMP-01.outlet_pressure` | 5.0 bar | 0.05 |
| `PUMP-01.flow_rate` | 220 m³/h | 3 |
| `PUMP-01.motor_current` | 42 A | 0.5 |
| `PUMP-01.bearing_temp` | 55 °C | 0.3 |
| `PUMP-01.vibration_rms` | 2.5 mm/s | 0.15 |
| `MOTOR-01.speed_rpm` | 1480 rpm | 4 |
| `MOTOR-01.torque` | 180 N·m | 2 |
| `MOTOR-01.current` | 40 A | 0.5 |
| `MOTOR-01.winding_temp` | 75 °C | 0.4 |
| `MOTOR-01.vibration_rms` | 2.0 mm/s | 0.12 |
| `CHILLER-01.inlet_temp` | 12.0 °C | 0.1 |
| `CHILLER-01.outlet_temp` | 7.0 °C | 0.1 |
| `CHILLER-01.power_kw` | 120 kW | 2 |
| `CHILLER-01.chw_flow` | 103 m³/h | 1 |
| `PUMP-01.inlet_pressure` | 2.3 bar | 0.02 |
| `PUMP-01.power_kw` | ≈ 25 kW (derivada) | 0.2 |
| `MOTOR-01.power_kw` | ≈ 30 kW (derivada) | 0.2 |

**Potências derivadas.** As duas potências novas calculam-se a cada leitura a partir dos outros sinais, para uma
paragem ou uma deriva lhes mexerem de forma coerente (a física é inventada, os valores são plausíveis):
- bomba: `caudal × (saída − aspiração) ÷ 36 ÷ 0,65` (m³/h × bar ÷ 36 = kW; 0,65 = rendimento global): ≈ 25 kW,
  ou ≈ 0,115 kWh/m³;
- motor: `binário × rpm × 2π ÷ 60 ÷ 1000 ÷ 0,93` (0,93 = rendimento do motor): ≈ 30 kW.

Ficam incoerentes de propósito com as correntes (a 400 V, 42 A dariam ~25 kW na bomba, mas 40 A não dão 30 kW no
motor). As correntes já existiam e mudá-las alteraria dados antigos; o gold usa as potências.

`run_hours` avança 1 por hora **a funcionar** (parado não conta, desde 2026-11-22); sem paragens, `state` fica em
RUNNING (bomba, motor) e COOLING (chiller).

### 7.1 Ativo parado (desde 2026-11-22, ADR-019)

Em `FAULT`, `STOPPED` ou `MAINTENANCE` (no chiller, `FAULT` ou `OFF`) a máquina não roda:

| Sinais | Valor parado |
|---|---|
| caudais, correntes, velocidade, binário, vibrações, potências | **exatamente 0**, sem ruído |
| `PUMP-01.outlet_pressure` | 2.3 bar, a pressão da aspiração |
| `bearing_temp`, `winding_temp` | arrefecem até 25 °C (constante de tempo de 15 e 20 min), com ruído |
| `CHILLER-01.outlet_temp` | aproxima-se da entrada, 12 °C (10 min) |
| `inlet_pressure`, `CHILLER-01.inlet_temp`, `device_clock` | não dependem do estado |

**Consequência a jusante:** o gateway só escreve ao mudar, por isso um sinal parado a 0 deixa de gerar amostras.
Isto não é um sensor preso, e o G2-06 sabe distingui-lo (ver `docs/gates-contract.md`).

Ao sair de uma paragem, o ativo passa sempre por `STARTING` (no chiller, `STANDBY`) durante 5 s.

## 8. Cenários injetáveis

Cada cenário tem um efeito **observável no registo** e uma forma de ser **detetado a
jusante** (é o que os gates da Fase 5 têm de apanhar).

| Cenário | Efeito no simulador | Como se deteta |
|---|---|---|
| `normal` | perfil da secção 7 | linha de base |
| `drift` | `vibration_rms` e `bearing_temp` (bomba) ou `vibration_rms` e `winding_temp` (motor) sobem linearmente durante `duration_s` (a rampa) até ao alarme e à falha (`state = FAULT`); a falha **mantém-se** até o cenário ser cancelado, ou até `hold_s` segundos depois da rampa | tendência de subida; é o *ground truth* do ML (a hora da falha é conhecida) |
| `sensor_stuck` | um sinal congela no último valor e nunca mais muda (os outros continuam) | variância nula numa janela, com o resto do ativo a variar |
| `spike` | um sinal salta para um valor fora do normal durante 1–3 amostras e volta | valor fora do `EURange` ou salto isolado |
| `noise_burst` | o desvio do ruído de um sinal multiplica-se por K durante T segundos | variância anormal numa janela |
| `comms_gap` | o simulador deixa de responder (timeout) a um ou a todos os unit IDs durante T segundos | buracos temporais; quebra de completude |
| `clock_skew` | `device_clock` fica adiantado/atrasado de S segundos (opcionalmente a derivar) | `source_ts` ≠ `ingest_ts` além da tolerância |
| `power_cycle` | resposta interrompida ~10 s, `state` passa por STARTING, `run_hours` reinicia a 0 (bomba) | contador a recuar; `state` a transitar |
| `trip` | avaria súbita: `state = FAULT` e o ativo parado (7.1) durante `duration_s` (por omissão 900 s); alarme ligado | entrada em `FAULT`: conta para o MTBF e o MTTR |
| `planned_stop` | paragem planeada: `STOPPED` (chiller `OFF`) durante `duration_s` | tempo parado que não é avaria |
| `maintenance` | manutenção: `MAINTENANCE` (chiller `OFF`) durante `duration_s` | idem |

Desde 2026-11-22 o `drift` também **para** a máquina quando chega à falha: em `FAULT` a vibração e o caudal vão a 0
e as temperaturas arrefecem. Antes a máquina "avariada" continuava a vibrar, o que uma máquina real que dispara não faz.

**Avarias aleatórias** (`--trip-rate-per-day N`, variável `TRIP_RATE_PER_DAY` no contentor): em média N avarias
(`trip`) por ativo e por dia a funcionar, com duração aleatória (média de 20 min, entre 2 e 120). Por omissão 0.

### 8.0 Plano de produção (`--plan`, variável `PLAN`)

`ot/simulator/src/ot_simulator/plan.yaml` (com `--plan builtin`): 3 turnos de 8 h em hora de Lisboa (A 06–14, B 14–22,
C 22–06) e paragens semanais. O simulador cumpre-as, e o gold usa o mesmo ficheiro para o tempo planeado:

| Ativo | Paragem | Quando (Lisboa) |
|---|---|---|
| PUMP-01 | `maintenance` | domingo 06:00–10:00 |
| MOTOR-01 | `maintenance` | domingo 10:00–12:00 |
| CHILLER-01 | `planned_stop` | sábado 22:00 – domingo 06:00 |

### 8.1 Como se ativam

1. **Por ficheiro** (`scenarios.yaml`): lista de entradas `ativo`, `cenário`, `início`
   (relativo ao arranque ou absoluto), `duração` e `parâmetros`. É o modo reproduzível
   usado nos testes.
2. **Em execução** (HTTP, porto 8080): um pedido ativa ou cancela um cenário, para
   demonstrações ao vivo. Devolve o estado atual dos cenários.

Os dois modos escrevem no mesmo agendador; o estado ativo é visível por HTTP.

## 9. Critérios de verificação deste mapa

O mapa está correto quando um cliente Modbus independente (sem usar o código do
simulador) consegue:

1. ler `PUMP-01.outlet_pressure` e obter ≈ 5.0 bar por TCP **e** por RS-232;
2. ler `MOTOR-01.speed_rpm` com `CDAB` e obter ≈ 1480, e com `ABCD` obter um valor
   absurdo (prova de que a ordem está mesmo trocada);
3. ler `CHILLER-01.power_kw` como `float32` e obter ≈ 120;
4. ler o coil de alarme e observá-lo ligar-se em `drift`;
5. observar um sinal imóvel durante `sensor_stuck`;
6. ler `PUMP-01.power_kw` e `MOTOR-01.power_kw` como `float32` (≈ 25 e ≈ 30 kW; no motor na ordem `CDAB`),
   `PUMP-01.inlet_pressure` ≈ 2.3 bar e `CHILLER-01.chw_flow` ≈ 103 m³/h;
7. com `trip`, ver `state = 3` (FAULT), o caudal a 0 e o alarme ligado, e depois STARTING e RUNNING.
