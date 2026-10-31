'use strict';

// Loaded only into the offline CLI and its Node children, never npm install.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const events = [];
const localIpc = [];
function deny(kind) {
  return function () {
    events.push({ kind, stack: new Error().stack.split('\n').slice(1, 9) });
    throw new Error(`Offline evaluation denies ${kind}`);
  };
}
const net = require('node:net');
function localPipe(args) {
  let value = args[0];
  while (Array.isArray(value)) value = value[0];
  const pipe = typeof value === 'string' ? value : value?.path;
  if (typeof pipe !== 'string') return false;
  // tsx attempts a parent IPC pipe when it registers its loader. Only this
  // process's dedicated temporary tree is allowed; TCP never reaches here.
  const expected = '\\\\?\\pipe\\' + path.join(process.env.TEMP,
    `tsx-${os.userInfo().username}`, `${process.ppid}.pipe`);
  return pipe === expected;
}
for (const [owner, key, kind] of [[net.Socket.prototype, 'connect', 'socket.connect'],
  [net, 'connect', 'net.connect'], [net, 'createConnection', 'net.createConnection']]) {
  const original = owner[key];
  owner[key] = function (...args) {
    if (localPipe(args)) {
      localIpc.push({kind});
      return original.apply(this, args);
    }
    return deny(kind)();
  };
}
const tls = require('node:tls');
tls.connect = deny('tls.connect');
const dns = require('node:dns');
for (const key of ['lookup', 'resolve', 'resolve4', 'resolve6', 'resolveAny', 'reverse']) {
  if (typeof dns[key] === 'function') dns[key] = deny(`dns.${key}`);
  if (typeof dns.promises[key] === 'function') dns.promises[key] = deny(`dns.promises.${key}`);
}
const dgram = require('node:dgram');
dgram.createSocket = deny('dgram.createSocket');
globalThis.fetch = deny('fetch');
// In pinned 0.125.1, disabling telemetry still sends a one-time "telemetry
// disabled" event. Mark that receipt as already recorded on the documented
// process singleton before the CLI uses it. No dependency source is patched.
let telemetry;
let telemetryIntercepts = 0;
Object.defineProperty(process, Symbol.for('promptfoo.telemetry.instance'), {
  configurable: true,
  get: () => telemetry,
  set: value => { value.telemetryDisabledRecorded = true; telemetry = value; telemetryIntercepts++; },
});
process.on('exit', () => {
  const target = process.env.ASTER_PROMPTFOO_NODE_GUARD;
  if (target) fs.writeFileSync(`${target}.${process.pid}.json`, JSON.stringify({
    pid: process.pid, external_attempts: events.length, events, local_ipc: localIpc,
    telemetry_singletons_suppressed: telemetryIntercepts,
  }, null, 2));
});
