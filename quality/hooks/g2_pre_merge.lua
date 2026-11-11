-- Hook pre-merge do lakeFS: só deixa entrar silver que o gate G2 atestou (docs/gates-contract.md).
-- Corre dentro do lakeFS, antes de o merge acontecer. error() aborta o merge (HTTP 412).
local lakefs = require("lakefs")
local json = require("encoding/json")
local strings = require("strings")

local repo, source = action.repository_id, action.source_ref

-- 1. o que muda no silver com este merge? (diferença entre o destino e a ponta do branch)
local changed, after = {}, ""
repeat
  local diff_status, page = lakefs.diff_refs(repo, action.branch_id, source, after, "silver/", "", 1000)
  if diff_status ~= 200 then error("G2: não consegui comparar o branch com o destino (" .. diff_status .. ")") end
  for _, d in ipairs(page.results) do
    if d.type == "added" or d.type == "changed" then changed[#changed + 1] = d.path end
    after = d.path
  end
until not page.pagination.has_more

if #changed == 0 then return end                  -- nada de silver neste merge: o G2 não se aplica

-- 2. cada partição silver que muda precisa da sua atestação (G2), que não pode ser "fail"
local cache = {}
local function attestation_for(path)
  -- silver/dt=D/hour=H/data.parquet  ->  _gates/g2/dt=D/hour=H.json
  local parts = strings.split(path, "/")
  if #parts < 4 or parts[1] ~= "silver" then error("G2: caminho silver inesperado: " .. path) end
  local dt, hour = parts[2], parts[3]
  local att_path = "_gates/g2/" .. dt .. "/" .. hour .. ".json"
  if cache[att_path] == nil then
    local status, content = lakefs.get_object(repo, source, att_path)
    if status ~= 200 then error("G2: " .. path .. " sem atestação (" .. att_path .. ")") end
    local att = json.unmarshal(content)
    if att.gate ~= "G2" then error("G2: atestação inválida em " .. att_path) end
    if att.status == "fail" then error("G2: o gate falhou em " .. dt .. " " .. hour .. " (" .. (att.summary or "sem resumo") .. ")") end
    cache[att_path] = att
  end
  return cache[att_path]
end

-- 3. cada ficheiro silver que muda tem de ter sido o que o G2 validou (mesmo checksum)
for _, path in ipairs(changed) do
  local att = attestation_for(path)
  local expected = att.files and att.files[path]
  if expected == nil then error("G2: " .. path .. " não foi validado pelo gate") end
  local stat_status, raw = lakefs.stat_object(repo, source, path)
  local stat = stat_status == 200 and json.unmarshal(raw) or {}
  if stat.checksum ~= expected then
    error("G2: " .. path .. " mudou depois de validado (checksum diferente)")
  end
end
