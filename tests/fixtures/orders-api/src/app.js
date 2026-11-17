/**
 * Application entry point for the orders service.
 */
const express = require('express');
const logger = require('./utils/logger');
const ordersRouter = require('./routes/orders');
const healthRouter = require('./routes/health');

const app = express();
app.use(express.json());
app.use('/api/orders', ordersRouter);
app.use('/health', healthRouter);

process.on('unhandledRejection', (err) => {
  logger.error('Unhandled rejection in orders-api');
});

app.listen(3000, () => {
  logger.info('orders-api listening on port 3000');
});

module.exports = app;
