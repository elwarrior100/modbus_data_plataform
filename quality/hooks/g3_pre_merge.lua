-- Hook pre-merge do lakeFS: só deixa entrar gold que o gate G3 atestou (docs/gates-contract.md, secção 6).
-- É o g2_pre_merge aplicado ao prefixo gold/: cada ficheiro gold/<tabela>/dt=D/... precisa da atestação
-- _gates/g3/dt=D.json no branch de origem, sem "fail", com o checksum exato do ficheiro. error() aborta o merge (HTTP 412).
local lakefs = require("lakefs")
local json = require("encoding/json")
local strings = require("strings")

local repo, source = action.repository_id, action.source_ref

-- 1. o que muda no gold com este merge?
local changed, after = {}, ""
repeat
  local diff_status, page = lakefs.diff_refs(repo, action.branch_id, source, after, "gold/", "", 1000)
  if diff_status ~= 200 then error("G3: não consegui comparar o branch com o destino (" .. diff_status .. ")") end
  for _, d in ipairs(page.results) do
    if d.type == "added" or d.type == "changed" then changed[#changed + 1] = d.path end
    after = d.path
  end
until not page.pagination.has_more

if #changed == 0 then return end                  -- nada de gold neste merge: o G3 não se aplica

-- 2. cada dia que muda precisa da sua atestação (G3), que não pode ser "fail"
local cache = {}
local function attestation_for(path)
  -- gold/<tabela>/dt=D/data.parquet  ->  _gates/g3/dt=D.json
  local parts = strings.split(path, "/")
  if #parts < 4 or parts[1] ~= "gold" or string.sub(parts[3], 1, 3) ~= "dt=" then
    error("G3: caminho gold inesperado: " .. path)
  end
  local att_path = "_gates/g3/" .. parts[3] .. ".json"
  if cache[att_path] == nil then
    local status, content = lakefs.get_object(repo, source, att_path)
    if status ~= 200 then error("G3: " .. path .. " sem atestação (" .. att_path .. ")") end
    local att = json.unmarshal(content)
    if att.gate ~= "G3" then error("G3: atestação inválida em " .. att_path) end
    if att.status == "fail" then error("G3: o gate falhou em " .. parts[3] .. " (" .. (att.summary or "sem resumo") .. ")") end
    cache[att_path] = att
  end
  return cache[att_path]
end

-- 3. cada ficheiro gold que muda tem de ser o que o G3 validou (mesmo checksum)
for _, path in ipairs(changed) do
  local att = attestation_for(path)
  local expected = att.files and att.files[path]
  if expected == nil then error("G3: " .. path .. " não foi validado pelo gate") end
  local stat_status, raw = lakefs.stat_object(repo, source, path)
  local stat = stat_status == 200 and json.unmarshal(raw) or {}
  if stat.checksum ~= expected then
    error("G3: " .. path .. " mudou depois de validado (checksum diferente)")
  end
end
