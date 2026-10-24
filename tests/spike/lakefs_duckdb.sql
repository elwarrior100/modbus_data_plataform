-- Teste de fumo: o DuckDB lê o lakeFS por tag/branch e deteta um dia em falta.
-- Pressupõe: repositório industrial-lake, tag gold-v0.1.0 e branch etl-gap
-- (criados por run.sh). Falha com erro se qualquer verificação falhar.
.bail on

.print '== A) a tag gold-v0.1.0 lê 100000 linhas por glob'
SELECT CASE WHEN count(*) = 100000 THEN 'OK' ELSE error('A: contagem inesperada na tag') END AS resultado
FROM read_parquet('s3://industrial-lake/gold-v0.1.0/bronze/**/*.parquet');

.print '== B) main e a tag têm o mesmo conteúdo'
SELECT CASE WHEN m.n = t.n AND m.s = t.s THEN 'OK' ELSE error('B: main difere da tag') END AS resultado
FROM (SELECT count(*) AS n, sum(value) AS s FROM read_parquet('s3://industrial-lake/main/bronze/**/*.parquet')) m,
     (SELECT count(*) AS n, sum(value) AS s FROM read_parquet('s3://industrial-lake/gold-v0.1.0/bronze/**/*.parquet')) t;

.print '== C) dt vem do nome da pasta, não do ficheiro'
SELECT CASE WHEN count(*) = 0 THEN 'OK' ELSE error('C: dt está dentro do ficheiro') END AS resultado
FROM (DESCRIBE SELECT * FROM read_parquet('s3://industrial-lake/main/bronze/dt=2026-03-01/data_0.parquet', hive_partitioning = false))
WHERE column_name = 'dt';

.print '== D) duas refs na mesma consulta: o dia em falta é detetado'
WITH a AS (SELECT dt, count(*) AS n FROM read_parquet('s3://industrial-lake/gold-v0.1.0/bronze/**/*.parquet', hive_partitioning = true) GROUP BY dt),
     b AS (SELECT dt, count(*) AS n FROM read_parquet('s3://industrial-lake/etl-gap/bronze/**/*.parquet', hive_partitioning = true) GROUP BY dt)
SELECT coalesce(a.dt, b.dt) AS dt, a.n AS linhas_tag, b.n AS linhas_branch,
       coalesce(a.n, 0) - coalesce(b.n, 0) AS diferenca
FROM a FULL OUTER JOIN b ON a.dt = b.dt
ORDER BY dt;

WITH a AS (SELECT dt, count(*) AS n FROM read_parquet('s3://industrial-lake/gold-v0.1.0/bronze/**/*.parquet', hive_partitioning = true) GROUP BY dt),
     b AS (SELECT dt, count(*) AS n FROM read_parquet('s3://industrial-lake/etl-gap/bronze/**/*.parquet', hive_partitioning = true) GROUP BY dt)
SELECT CASE WHEN sum((b.n IS NULL)::INT) = 1 AND sum((a.n IS NULL)::INT) = 0
            THEN 'OK' ELSE error('D: o dia em falta não foi detetado') END AS resultado
FROM a FULL OUTER JOIN b ON a.dt = b.dt;
