const sql = require('mssql');
const logger = require('../utils/logger');

async function findUser(name) {
  const pool = await sql.connect(process.env.DB_URL);
  const r = await pool.request().input('n', name).query('SELECT u.id, u.name, u.hash FROM dbo.Users u WHERE u.name = @n');
  return r.recordset[0];
}

async function login(req, res) {
  const user = await findUser(req.body.name);
  if (!user) {
    logger.warn(`Login failed for ${req.body.name}`);
    return res.status(401).send('denied');
  }
  logger.info(`User ${user.id} logged in`);
  res.json({ token: 'abc' });
}

async function refresh(req, res) {
  res.json({ token: 'def' });
}

async function verify(req, res) {
  if (!req.headers.authorization) {
    return res.status(401).send('missing token');
  }
  res.json({ valid: true });
}

module.exports = { login, refresh, verify };
