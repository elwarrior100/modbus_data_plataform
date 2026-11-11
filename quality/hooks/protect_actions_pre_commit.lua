-- Hook pre-commit do lakeFS: ninguém altera nem apaga os hooks (pasta _lakefs_actions/) por um commit normal.
-- Sem isto, um branch podia apagar o hook do G2 e fazer merge sem gate (o lakeFS lê as ações do branch de origem).
local lakefs = require("lakefs")
-- exceção deliberada: o instalador dos gates marca o seu commit com este metadado
if action.commit.metadata and action.commit.metadata["gates_installer"] == "true" then return end
local diff_status, page = lakefs.diff_branch(action.repository_id, action.branch_id, "", 100, "_lakefs_actions/", "")
if diff_status ~= 200 then error("não consegui verificar _lakefs_actions/ (" .. diff_status .. ")") end
if #page.results > 0 then
  error("os ficheiros em _lakefs_actions/ não se alteram por commits normais (ver docs/gates-contract.md)")
end
