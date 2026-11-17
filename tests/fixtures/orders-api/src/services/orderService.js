const axios = require('axios');
const AWS = require('aws-sdk');
const repo = require('../repositories/orderRepo');
const logger = require('../utils/logger');

const sqs = new AWS.SQS();
const PRICING_URL = 'https://pricing.internal/api/price';

async function findOrder(id) {
  const order = await repo.getById(id);
  if (!order) {
    throw new Error('Order not found');
  }
  return order;
}

async function placeOrder(body) {
  const price = await axios.get(PRICING_URL, { timeout: 2000 });
  const order = await repo.insertOrder(body, price.data);
  logger.info(`Order ${order.id} placed`);
  return order;
}

async function notifyShipment(id) {
  await axios.post('https://shipping.internal/api/notify', { id });
  await sqs.sendMessage({ QueueUrl: 'https://sqs.eu-west-1.amazonaws.com/123456789012/shipment-events', MessageBody: id }).promise();
}

async function pollShipments() {
  return sqs.receiveMessage({ QueueUrl: 'https://sqs.eu-west-1.amazonaws.com/123456789012/order-updates' }).promise();
}

module.exports = { findOrder, placeOrder, notifyShipment, pollShipments };
