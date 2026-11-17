const service = require('../src/services/orderService');

test('findOrder is exported', () => {
  expect(typeof service.findOrder).toBe('function');
});
