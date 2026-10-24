# Regras de trabalho — modbus_data_plataform

## Regra #1: o Claude escreve o código

Revista em 2026-10-24 por decisão do Stéphane: deixa de existir a proibição de escrever
código. O Claude implementa (aplicação, SQL, manifests, DAGs, configuração, testes).

O objetivo de aprendizagem mantém-se, por isso:
- Explica o que escreve e porquê, sobretudo decisões não óbvias, em português.
- O Stéphane revê e pode pedir mudanças; não se avança de fase sem ele perceber o que
  foi feito (as perguntas de verificação de compreensão continuam a ser bem-vindas).
- Cada fase fecha só quando o Definition of Done do plano estiver verificado com output
  real, não com "devia funcionar".
- Segredos nunca entram no git. Credenciais de exemplo só em ficheiros de exemplo.

## Regra #2: o plano é a fonte de verdade
`docs/PLAN.md` manda. Antes de propor trabalho novo, lê-o. Se a realidade divergir do
plano, atualiza o plano em vez de improvisar.

## Regra #3: decisões ficam registadas
Qualquer escolha técnica com alternativas reais vira um ADR curto em `docs/adr/`.
Escrever o ADR é planeamento, não código — podes escrevê-lo.

## Regra #4: língua
Responde sempre em português de Portugal.
