import socketio, time, json, sys
COOKIE = "donutchip_session=7868bb7d70209733dbdea2636f759d1aeb8f0d8bd70d3d32594af936a1e6b937"
OUT = r"C:\Users\EyeSaber\Documents\EmbedGather\unscramble_probe.log"
sio = socketio.Client(logger=False, engineio_logger=False)
def log(obj):
    with open(OUT, 'a', encoding='utf-8') as f:
        f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
@sio.event
def connect():
    log({'ts': time.time(), 'event': 'connect'})
    sio.emit('chat:join')
@sio.event
def disconnect():
    log({'ts': time.time(), 'event': 'disconnect'})
try:
    sio.on('*', lambda event, data=None: log({'ts': time.time(), 'event': event, 'data': data}))
except Exception as e:
    log({'ts': time.time(), 'event': 'wildcard_error', 'error': str(e)})
for ev in ['chat:history','chat:message','chat:self','chat:deleted','chat:online','chat:error','rain:site','rain:start','rain:update','rain:end','rain:joined','chat:tip:success','chat:mutes','chat:mute','chat:unmute','balance:update','live-bet','site:update','withdraw:ignored']:
    sio.on(ev, lambda data=None, e=ev: log({'ts': time.time(), 'event': e, 'data': data}))
sio.connect('https://donutnova.com', transports=['websocket','polling'], headers={'Cookie': COOKIE})
end = time.time() + 15*60
while time.time() < end:
    time.sleep(1)
sio.disconnect()
