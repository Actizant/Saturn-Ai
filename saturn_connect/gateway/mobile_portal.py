"""
saturn_connect/gateway/mobile_portal.py
Zero-Install Mobile Companion Web Portal for Saturn.
Provides instant lock-screen notifications, Web Audio chimes, live WebSocket mesh syncing,
and bidirectional voice & remote controls on Android and iOS devices without installing an app.
"""

def get_mobile_portal_html(host_ip: str = "127.0.0.1", port: int = 8765) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<meta name="theme-color" content="#030712">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>⚡ Saturn • Mobile Companion</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }}
  body {{
    background: #030712;
    color: #f8fafc;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    padding: 16px;
    background-image: 
      radial-gradient(ellipse at 50% 0%, rgba(0, 229, 255, 0.15) 0%, transparent 60%),
      radial-gradient(circle at 100% 100%, rgba(168, 85, 247, 0.1) 0%, transparent 40%);
  }}
  .container {{
    max-width: 480px;
    margin: 0 auto;
    width: 100%;
    display: flex;
    flex-direction: column;
    gap: 16px;
    flex: 1;
  }}
  .header {{
    text-align: center;
    padding: 18px 12px;
    background: rgba(15, 23, 42, 0.85);
    border: 1px solid rgba(0, 229, 255, 0.3);
    border-radius: 16px;
    box-shadow: 0 0 24px rgba(0, 229, 255, 0.1);
  }}
  .title {{
    font-size: 20px;
    font-weight: 800;
    letter-spacing: 0.08em;
    color: #00e5ff;
    text-shadow: 0 0 12px rgba(0, 229, 255, 0.5);
  }}
  .subtitle {{
    font-size: 11px;
    color: #94a3b8;
    margin-top: 4px;
    text-transform: uppercase;
    letter-spacing: 0.12em;
  }}
  .card {{
    background: rgba(15, 23, 42, 0.7);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 14px;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }}
  .card-title {{
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: #64748b;
    font-weight: 700;
  }}
  .status-row {{
    display: flex;
    align-items: center;
    justify-content: space-between;
  }}
  .status-badge {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.05em;
  }}
  .status-online {{
    background: rgba(34, 197, 94, 0.15);
    color: #4ade80;
    border: 1px solid rgba(34, 197, 94, 0.4);
  }}
  .status-dot {{
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #4ade80;
    box-shadow: 0 0 8px #4ade80;
    animation: pulse 2s infinite;
  }}
  @keyframes pulse {{
    0%, 100% {{ opacity: 1; transform: scale(1); }}
    50% {{ opacity: 0.5; transform: scale(0.85); }}
  }}
  .btn {{
    width: 100%;
    padding: 14px 18px;
    border-radius: 12px;
    font-size: 14px;
    font-weight: 700;
    letter-spacing: 0.04em;
    cursor: pointer;
    border: none;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
  }}
  .btn-primary {{
    background: linear-gradient(135deg, #00e5ff 0%, #0284c7 100%);
    color: #030712;
    box-shadow: 0 0 18px rgba(0, 229, 255, 0.35);
  }}
  .btn-primary:active {{
    transform: scale(0.98);
    box-shadow: 0 0 8px rgba(0, 229, 255, 0.6);
  }}
  .btn-active {{
    background: rgba(34, 197, 94, 0.2);
    color: #4ade80;
    border: 1px solid rgba(34, 197, 94, 0.5);
  }}
  .btn-secondary {{
    background: rgba(30, 41, 59, 0.7);
    color: #e2e8f0;
    border: 1px solid rgba(255, 255, 255, 0.1);
  }}
  .btn-secondary:active {{
    background: rgba(51, 65, 85, 0.9);
  }}
  .btn-danger {{
    background: rgba(239, 68, 68, 0.15);
    color: #f87171;
    border: 1px solid rgba(239, 68, 68, 0.3);
  }}
  .grid-2 {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
  }}
  .log-box {{
    background: rgba(0, 0, 0, 0.5);
    border: 1px solid rgba(255, 255, 255, 0.05);
    border-radius: 10px;
    padding: 10px;
    font-family: monospace;
    font-size: 11px;
    color: #94a3b8;
    max-height: 140px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-all;
  }}
  .voice-input-row {{
    display: flex;
    gap: 8px;
  }}
  .voice-input {{
    flex: 1;
    background: rgba(0, 0, 0, 0.4);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 10px;
    padding: 10px 12px;
    color: #f8fafc;
    font-size: 13px;
    outline: none;
  }}
  .voice-input:focus {{
    border-color: #00e5ff;
  }}
  .banner {{
    padding: 10px 14px;
    border-radius: 10px;
    font-size: 12px;
    background: rgba(0, 229, 255, 0.1);
    border: 1px solid rgba(0, 229, 255, 0.25);
    color: #38bdf8;
    display: none;
  }}
  .footer {{
    text-align: center;
    font-size: 11px;
    color: #475569;
    margin-top: auto;
    padding: 12px 0 4px;
  }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="title">⚡ SATURN</div>
    <div class="subtitle">Zero-Install Mobile Companion Node</div>
  </div>

  <div id="alertBanner" class="banner"></div>

  <div class="card">
    <div class="status-row">
      <div class="card-title">Network Mesh Status</div>
      <div class="status-badge status-online">
        <div class="status-dot"></div>
        <span id="meshStatus">LINKED</span>
      </div>
    </div>
    <div style="font-size: 12px; color: #cbd5e1;">
      Host Desktop: <strong style="color: #00e5ff;">{host_ip}:{port}</strong><br>
      This Node: <strong id="nodeDeviceName">Detecting...</strong>
    </div>
  </div>

  <div class="card">
    <div class="card-title">Lock Screen & Push Alerts</div>
    <p style="font-size: 12px; color: #94a3b8; line-height: 1.4;">
      Enable browser push notifications to receive real-time alerts whenever Saturn comes online or completes a task.
    </p>
    <button id="btnNotif" class="btn btn-primary" onclick="enableNotifications()">
      🔔 Enable Lock Screen Alerts
    </button>
  </div>

  <div class="card">
    <div class="card-title">🎙️ Send Voice Command to Desktop</div>
    <div class="voice-input-row">
      <input id="txtVoice" class="voice-input" type="text" placeholder="Type or tap Mic to speak...">
      <button class="btn btn-primary" style="width: auto; padding: 10px 16px;" onclick="sendVoiceCommand()">Send</button>
    </div>
    <button id="btnMic" class="btn btn-secondary" onclick="toggleSpeechRec()">
      🎤 Tap to Speak
    </button>
  </div>

  <div class="card">
    <div class="card-title">Remote Quick Actions</div>
    <div class="grid-2">
      <button class="btn btn-secondary" onclick="sendAction('ping')">📡 Ping PC</button>
      <button class="btn btn-secondary" onclick="sendAction('toggle_mute')">🔊 Mute PC</button>
      <button class="btn btn-danger" onclick="sendAction('lock_screen')">🔒 Lock PC</button>
      <button class="btn btn-secondary" onclick="triggerTestChime()">🔔 Test Alert</button>
    </div>
  </div>

  <div class="card">
    <div class="card-title">Event Telemetry Log</div>
    <div id="eventLog" class="log-box">Connecting to Saturn Mesh Gateway...</div>
  </div>

  <div class="footer">
    Saturn NetSentinel • Zero-Install Local Transport Layer
  </div>
</div>

<script>
  let ws = null;
  let recognition = null;
  const logBox = document.getElementById('eventLog');
  const alertBanner = document.getElementById('alertBanner');
  const btnNotif = document.getElementById('btnNotif');

  function log(msg) {{
    const t = new Date().toLocaleTimeString();
    logBox.innerText = `[${{t}}] ${{msg}}\\n` + logBox.innerText;
  }}

  // 1. Detect Device Identity
  const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent);
  const isAndroid = /Android/.test(navigator.userAgent);
  const devName = isIOS ? "Apple iOS Device" : (isAndroid ? "Android Smartphone" : "Mobile Node");
  document.getElementById('nodeDeviceName').innerText = devName;

  // 2. Web Audio Chime Generator (Two-tone cyberpunk chime)
  function playCyberChime() {{
    try {{
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const now = ctx.currentTime;
      const osc1 = ctx.createOscillator();
      const osc2 = ctx.createOscillator();
      const gain = ctx.createGain();

      osc1.type = 'sine';
      osc1.frequency.setValueAtTime(587.33, now); // D5
      osc1.frequency.exponentialRampToValueAtTime(880, now + 0.15); // A5

      osc2.type = 'triangle';
      osc2.frequency.setValueAtTime(880, now);
      osc2.frequency.exponentialRampToValueAtTime(1174.66, now + 0.25); // D6

      gain.gain.setValueAtTime(0.2, now);
      gain.gain.exponentialRampToValueAtTime(0.001, now + 0.5);

      osc1.connect(gain);
      osc2.connect(gain);
      gain.connect(ctx.destination);

      osc1.start(now);
      osc2.start(now + 0.05);
      osc1.stop(now + 0.5);
      osc2.stop(now + 0.5);
    }} catch (e) {{}}
  }}

  // 3. Native Push Notification Trigger
  function showLocalNotification(title, body) {{
    playCyberChime();
    if (navigator.vibrate) navigator.vibrate([200, 100, 200]);

    alertBanner.style.display = 'block';
    alertBanner.innerText = `⚡ ${{title}}: ${{body}}`;
    setTimeout(() => {{ alertBanner.style.display = 'none'; }}, 6000);

    if ('Notification' in window && Notification.permission === 'granted') {{
      try {{
        new Notification(title, {{
          body: body,
          icon: 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><circle cx="50" cy="50" r="45" fill="%2300e5ff"/></svg>',
          tag: 'saturn-alert',
          vibrate: [200, 100, 200],
        }});
      }} catch (e) {{}}
    }}
  }}

  async function enableNotifications() {{
    if (!('Notification' in window)) {{
      alert('This browser does not support Web Notifications. You can still use voice & remote controls!');
      return;
    }}
    const perm = await Notification.requestPermission();
    if (perm === 'granted') {{
      btnNotif.className = 'btn btn-active';
      btnNotif.innerText = '✅ Lock Screen Alerts Active';
      showLocalNotification('⚡ SATURN ONLINE', 'Phone Linked! You will receive live alerts from Desktop.');
      log('Web Notification permission granted.');
    }} else {{
      log('Notification permission denied or dismissed.');
    }}
  }}

  if ('Notification' in window && Notification.permission === 'granted') {{
    btnNotif.className = 'btn btn-active';
    btnNotif.innerText = '✅ Lock Screen Alerts Active';
  }}

  function triggerTestChime() {{
    showLocalNotification('⚡ SATURN TEST', 'Alert beacon successfully synchronized with mobile node.');
  }}

  // 4. WebSocket Gateway Connection
  function connectWebSocket() {{
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${{proto}}//${{location.host}}/ws`;
    ws = new WebSocket(wsUrl);

    ws.onopen = () => {{
      log('Connected to Saturn Gateway WebSocket.');
      document.getElementById('meshStatus').innerText = 'CONNECTED';
      // Register device
      ws.send(JSON.stringify({{
        type: 'hello',
        request_id: 'mob-' + Date.now(),
        timestamp: new Date().toISOString(),
        payload: {{
          device_name: devName,
          platform: isIOS ? 'ios' : (isAndroid ? 'android' : 'mobile'),
          capabilities: ['notifications', 'voice_input', 'remote_control']
        }}
      }}));
    }};

    ws.onmessage = (event) => {{
      try {{
        const msg = JSON.parse(event.data);
        const mtype = (msg.type || '').toLowerCase();
        log(`Received event: ${{mtype}}`);

        if (mtype === 'notification' || mtype === 'broadcast' || mtype === 'device_online' || mtype === 'alert') {{
          const text = msg.payload?.message || msg.payload?.text || 'Saturn Online // Node Linked';
          showLocalNotification('⚡ SATURN', text);
        }}
      }} catch (e) {{}}
    }};

    ws.onclose = () => {{
      log('WebSocket disconnected. Reconnecting in 3s...');
      document.getElementById('meshStatus').innerText = 'RECONNECTING';
      setTimeout(connectWebSocket, 3000);
    }};

    ws.onerror = (e) => {{
      log('WebSocket network error.');
    }};
  }}

  connectWebSocket();

  // 5. Remote Action Dispatcher
  function sendAction(actionName) {{
    if (!ws || ws.readyState !== WebSocket.OPEN) {{
      alert('Connecting to desktop... please retry in a second.');
      return;
    }}
    ws.send(JSON.stringify({{
      type: 'event',
      request_id: 'act-' + Date.now(),
      timestamp: new Date().toISOString(),
      payload: {{ action: actionName }}
    }}));
    log(`Action '${{actionName}}' dispatched to desktop.`);
    if (navigator.vibrate) navigator.vibrate(50);
  }}

  function sendVoiceCommand() {{
    const txt = document.getElementById('txtVoice').value.trim();
    if (!txt) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) {{
      alert('Not connected to desktop gateway.');
      return;
    }}
    ws.send(JSON.stringify({{
      type: 'chat_message',
      request_id: 'chat-' + Date.now(),
      timestamp: new Date().toISOString(),
      payload: {{ text: txt }}
    }}));
    log(`Sent prompt to Saturn: "${{txt}}"`);
    document.getElementById('txtVoice').value = '';
    showLocalNotification('⚡ COMMAND SENT', `Sent: "${{txt}}"`);
  }}

  // 6. Speech Recognition
  function toggleSpeechRec() {{
    const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRec) {{
      alert('Speech Recognition not supported on this browser. You can type in the box above!');
      return;
    }}
    if (recognition) {{
      recognition.stop();
      recognition = null;
      document.getElementById('btnMic').innerText = '🎤 Tap to Speak';
      return;
    }}
    recognition = new SpeechRec();
    recognition.lang = 'en-US';
    recognition.continuous = false;
    recognition.interimResults = false;

    document.getElementById('btnMic').innerText = '🔴 Listening... Speak now';
    if (navigator.vibrate) navigator.vibrate(80);

    recognition.onresult = (e) => {{
      const text = e.results[0][0].transcript;
      document.getElementById('txtVoice').value = text;
      sendVoiceCommand();
    }};

    recognition.onerror = () => {{
      document.getElementById('btnMic').innerText = '🎤 Tap to Speak';
      recognition = null;
    }};

    recognition.onend = () => {{
      document.getElementById('btnMic').innerText = '🎤 Tap to Speak';
      recognition = null;
    }};

    recognition.start();
  }}
</script>
</body>
</html>
"""
