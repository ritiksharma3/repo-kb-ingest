const axios = require('axios');

function requireAuth(req, res, next) {
  if (!req.headers.authorization) {
    throw new Error('Missing authorization header');
  }
  next();
}

async function verifyToken(token) {
  return axios.get('https://login.internal/auth/verify', { headers: { authorization: token }, timeout: 1500 });
}

module.exports = { requireAuth, verifyToken };
