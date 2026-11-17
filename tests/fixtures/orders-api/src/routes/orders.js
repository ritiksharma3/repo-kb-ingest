const express = require('express');
const router = express.Router();
const ctrl = require('../controllers/orderController');
const { requireAuth } = require('../utils/auth');

router.get('/:id', requireAuth, ctrl.getOrder);
router.post('/', requireAuth, ctrl.createOrder);
router.route('/:id/ship').post(ctrl.shipOrder);

module.exports = router;
