const axios = require('axios');
const AWS = require('aws-sdk');
const logger = require('../utils/logger');

const sqs = new AWS.SQS();

async function listOrders(req, res) {
  await axios.get('https://login.internal/auth/verify', { timeout: 1500 });
  const order = await axios.get('https://orders.internal/api/orders/42', { timeout: 1500 });
  logger.info(`Relayed order ${order.data.id}`);
  res.json(order.data);
}

async function publishUpdate(id) {
  await sqs.sendMessage({ QueueUrl: 'https://sqs.eu-west-1.amazonaws.com/123456789012/order-updates', MessageBody: id }).promise();
}

module.exports = { listOrders, publishUpdate };
