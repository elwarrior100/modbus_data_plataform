-- Hook pre-merge do lakeFS: o ponteiro _releases/LATEST_STABLE só pode apontar para uma release que existe e cujo G3
-- não falhou (ADR-019, D9; docs/gates-contract.md, secção 6). É o 3.º hook da ação g2, a seguir ao G2 e ao G3: hooks
-- da mesma ação correm por ordem, e dois hooks Lua em paralelo derrubam o lakeFS 1.86 (ver quality/install_hooks.py).
-- Um rollback é só isto: um merge que muda este ficheiro para uma release anterior. O hook deixa-o passar se essa
-- release existir; não decide qual é a "melhor" (isso é do código e de quem faz o rollback).
local lakefs = require("lakefs")
local json = require("encoding/json")

local repo, source = action.repository_id, action.source_ref
local POINTER = "_releases/LATEST_STABLE"

-- 1. o merge muda o ponteiro? (a mesma comparação do G2, só para este ficheiro)
local status, page = lakefs.diff_refs(repo, action.branch_id, source, "", POINTER, "", 10)
if status ~= 200 then error("LATEST_STABLE: não consegui comparar o branch com o destino (" .. status .. ")") end
local change = nil
for _, d in ipairs(page.results) do
  if d.path == POINTER then change = d.type end
end
if change == nil then return end                 -- o ponteiro não muda neste merge: o hook não se aplica
if change == "removed" then error("LATEST_STABLE: o ponteiro não se apaga (aponta-se para outra release)") end

-- 2. para onde aponta agora
local s, raw = lakefs.get_object(repo, source, POINTER)
if s ~= 200 then error("LATEST_STABLE: não consegui ler o ponteiro novo (" .. s .. ")") end
local pointer = json.unmarshal(raw)
local tag = pointer.release
if type(tag) ~= "string" or string.sub(tag, 1, 6) ~= "gold-v" then
  error("LATEST_STABLE: o ponteiro tem de nomear uma release gold-v* (tem " .. tostring(tag) .. ")")
end

-- 3. essa release existe? Lê-se o manifest a partir da PRÓPRIA tag: uma tag que não existe dá 404. O manifest só
-- existe numa tag que passou o hook release_pre_create_tag (manifest + G3 sem fail + checksums).
local ms, mraw = lakefs.get_object(repo, tag, "_releases/" .. tag .. "/manifest.json")
if ms ~= 200 then error("LATEST_STABLE: a release " .. tag .. " não existe ou não tem manifest (" .. ms .. ")") end
local manifest = json.unmarshal(mraw)
if manifest.release ~= tag then error("LATEST_STABLE: o manifest de " .. tag .. " diz " .. tostring(manifest.release)) end
if manifest.gates == nil or manifest.gates.g3 == nil or manifest.gates.g3 == "fail" then
  error("LATEST_STABLE: " .. tag .. " não tem um G3 aprovado no manifest")
end
