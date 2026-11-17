const express = require('express');
const axios = require('axios');
const sql = require('mssql');
const app = express();

async function load(host, table) {
  try {
    await axios.get(`https://${host}/data`);
  } catch (e) {}
  return sql.query('SELECT * FROM ' + table);
}

module.exports = { load };
