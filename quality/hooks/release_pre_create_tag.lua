-- Hook pre-create-tag do lakeFS: uma tag de release (gold-v*) só se cria sobre um commit que leva a prova consigo
-- (ADR-019, D6 e D7; docs/gates-contract.md, secção 6). Outras tags não são com este hook.
-- No commit da tag tem de existir:
--   1. o manifest _releases/<tag>/manifest.json, com "release" igual ao nome da tag;
--   2. para cada dia das tabelas do manifest, a atestação _gates/g3/dt=D.json, do G3 e sem "fail";
--   3. cada ficheiro do manifest com o checksum que o G3 atestou e que está no commit.
-- A PROVAR num repositório descartável (lakeFS 1.86): os campos action.tag_id e action.commit_id deste evento.
local lakefs = require("lakefs")
local json = require("encoding/json")
local strings = require("strings")

local tag, repo, commit = action.tag_id, action.repository_id, action.commit_id
if tag == nil or string.sub(tag, 1, 6) ~= "gold-v" then return end

local manifest_path = "_releases/" .. tag .. "/manifest.json"
local status, content = lakefs.get_object(repo, commit, manifest_path)
if status ~= 200 then error("release: " .. tag .. " sem manifest no commit (" .. manifest_path .. ")") end
local manifest = json.unmarshal(content)
if manifest.release ~= tag then error("release: o manifest diz " .. tostring(manifest.release) .. ", a tag é " .. tag) end
if manifest.tables == nil then error("release: o manifest de " .. tag .. " não lista tabelas") end

local attestations, count = {}, 0
for path, info in pairs(manifest.tables) do
  count = count + 1
  local parts = strings.split(path, "/")
  if #parts < 4 or parts[1] ~= "gold" or string.sub(parts[3], 1, 3) ~= "dt=" then
    error("release: caminho inesperado no manifest: " .. path)
  end
  local att_path = "_gates/g3/" .. parts[3] .. ".json"
  if attestations[att_path] == nil then
    local s, raw = lakefs.get_object(repo, commit, att_path)
    if s ~= 200 then error("release: " .. parts[3] .. " sem atestação G3 no commit (" .. att_path .. ")") end
    local att = json.unmarshal(raw)
    if att.gate ~= "G3" then error("release: atestação inválida em " .. att_path) end
    if att.status == "fail" then error("release: o G3 falhou em " .. parts[3] .. "; não há release") end
    attestations[att_path] = att
  end
  local expected = attestations[att_path].files and attestations[att_path].files[path]
  local s, raw = lakefs.stat_object(repo, commit, path)
  local stat = s == 200 and json.unmarshal(raw) or {}
  if expected == nil or stat.checksum ~= expected or info.checksum ~= expected then
    error("release: " .. path .. " não é o ficheiro que o G3 validou")
  end
end
if count == 0 then error("release: o manifest de " .. tag .. " não lista tabelas") end
