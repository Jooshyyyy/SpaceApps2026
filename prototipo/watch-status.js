// Shared by the browser and the small Node regression test.
function watchStatusLabel(status) {
  if (status.ws) return 'WebSocket';
  if (status.wifi) return 'WiFi ' + status.wifi;
  if (status.usb) return 'USB';
  return 'desconectado';
}
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { watchStatusLabel };
}
