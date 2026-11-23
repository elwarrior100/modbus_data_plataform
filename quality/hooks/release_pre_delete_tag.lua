-- Hook pre-delete-tag do lakeFS: as tags de release (gold-v*) não se apagam (ADR-019, D6).
-- No lakeFS uma tag apaga-se com uma chamada; sem isto, "release imutável" seria só uma promessa. As outras tags
-- continuam a poder apagar-se. Para apagar uma release é preciso, de propósito, mudar este hook (com o instalador).
if action.tag_id ~= nil and string.sub(action.tag_id, 1, 6) == "gold-v" then
  error("release: " .. action.tag_id .. " é uma release do gold e as releases são imutáveis (não se apagam)")
end
