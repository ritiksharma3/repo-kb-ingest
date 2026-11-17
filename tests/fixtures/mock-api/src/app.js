const express = require('express');
const logger = require('./utils/logger');
const gateway = require('./jobs/gateway');

const app = express();
app.get('/gateway/orders', gateway.listOrders);

app.listen(5000, () => {
  logger.info('mock-api listening on port 5000');
});

module.exports = app;
