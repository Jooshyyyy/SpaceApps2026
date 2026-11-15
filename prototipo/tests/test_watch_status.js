// Run with: node prototipo/tests/test_watch_status.js
const assert = require('node:assert/strict');
const { watchStatusLabel } = require('../watch-status.js');
assert.equal(watchStatusLabel({ws: true, wifi: null, usb: false}), 'WebSocket');
assert.equal(watchStatusLabel({wifi: '192.168.1.4', usb: true}), 'WiFi 192.168.1.4');
assert.equal(watchStatusLabel({usb: true}), 'USB');
assert.equal(watchStatusLabel({ws: false, wifi: null, usb: false}), 'desconectado');
console.log('watch status: 4 checks passed');
