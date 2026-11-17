const sql = require('mssql');

async function getById(id) {
  const pool = await sql.connect(process.env.DB_URL);
  const r = await pool.request().input('id', id).query('SELECT o.id, o.total FROM dbo.Orders o JOIN dbo.Customers c ON c.id = o.customer_id WHERE o.id = @id');
  return r.recordset[0];
}

async function insertOrder(body, price) {
  const pool = await sql.connect(process.env.DB_URL);
  const r = await pool.request().input('p', price).query('INSERT INTO dbo.Orders (customer_id, total) VALUES (1, @p)');
  await pool.request().execute('dbo.usp_AuditOrder');
  return r;
}

async function search(term) {
  const pool = await sql.connect(process.env.DB_URL);
  return pool.request().query(`SELECT * FROM dbo.Orders WHERE name LIKE '%${term}%'`);
}

module.exports = { getById, insertOrder, search };
