'use strict';
// Read-only SQL consumer of the DB produced by the unmodified upstream CLI.
const fs = require('node:fs');
const {DuckDBInstance} = require('@duckdb/node-api');
(async () => {
  const [dbPath, sqlPath] = process.argv.slice(2);
  if (!dbPath || !sqlPath) throw new Error('usage: node query_duckdb.cjs database.duckdb queries.sql');
  const instance = await DuckDBInstance.create(dbPath, {access_mode: 'read_only'});
  const connection = await instance.connect();
  try {
    await connection.run('LOAD icu; LOAD spatial;');
    const rows = (await connection.runAndReadAll(fs.readFileSync(sqlPath, 'utf8'))).getRowObjectsJson();
    if (rows.length !== 1) throw new Error('expected exactly one metrics row');
    const versions = (await connection.runAndReadAll('SELECT version() AS engine')).getRowObjectsJson();
    console.log(JSON.stringify({engine: versions[0].engine, node_api: require('@duckdb/node-api/package.json').version, metrics: rows[0]}));
  } finally {
    connection.closeSync();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
