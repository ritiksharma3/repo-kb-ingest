const service = require('../services/orderService');
const logger = require('../utils/logger');

async function getOrder(req, res) {
  try {
    const order = await service.findOrder(req.params.id);
    res.json(order);
  } catch (err) {
    logger.error('Failed to fetch order');
    res.status(500).send('error');
  }
}

async function createOrder(req, res) {
  const order = await service.placeOrder(req.body);
  res.status(201).json(order);
}

async function shipOrder(req, res) {
  try {
    await service.notifyShipment(req.params.id);
    res.json({ shipped: true });
  } catch (e) {
    logger.warn('shipment notification failed, ignoring');
  }
}

module.exports = { getOrder, createOrder, shipOrder };
