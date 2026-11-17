/**
 * Entry point for the login service.
 */
const express = require('express');
const logger = require('./utils/logger');
const authRouter = require('./routes/auth');

const app = express();
app.use(express.json());
app.use('/auth', authRouter);

app.listen(4000, () => {
  logger.info('login-api listening on port 4000');
});

module.exports = app;
