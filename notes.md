Server connection is closing after switching protocol to WS. Retry every few seconds. Web page hangs.

Claude responded: The issue is likely that websocket.
The issue is likely that websocket.close(1008, ...) on an auth failure is crashing the legacy handler before the connection fully completes. Let me also make the frontend more resilient — don't block on auth, handle the close gracefully, and keep retrying.

Find the auth section and close call in server
Find the auth section and close call in server
The real problem: browsers send the subprotocol in the handshake but the server never accepts it, so the connection is rejected at the protocol level before any messages can flow. The server needs to explicitly accept the matched subprotocol. Also str.removeprefix requires Python 3.9+. Let me fix both, and also make the frontend retry cleaner.

Fix subprotocol acceptance and removeprefix compatibility
Fix subprotocol acceptance and removeprefix compatibility
Now fix the frontend — don't pass subprotocols via the third argument (unsupported), use the standard two-argument new WebSocket(url, protocols), and handle auth_fail gracefully without retrying forever:

Several fixes in this update:
Root cause — subprotocol not accepted. When a browser sends Sec-WebSocket-Protocol: bearer.SUDOKUv00, the server must echo back an accepted subprotocol in the handshake response, otherwise the browser drops the connection immediately (this is the WebSocket spec). The server now sets websocket.subprotocol = matched_proto so the legacy library includes it in the 101 response.
Other fixes:

Removed the bogus new WebSocket(url, [], {headers:...}) call — that third argument isn't part of the browser WebSocket API. The frontend now simply does new WebSocket(WS_URL, ['bearer.SUDOKUv00']).
str.removeprefix replaced with a Python 3.8-compatible equivalent.
Server sends an auth_fail message before closing so the client knows why the connection dropped.
Frontend sets authFailed = true on auth_fail and stops retrying — previously a wrong key would hammer the server in a 3-second loop forever.
Added a try/catch around JSON.parse in onmessage to avoid an unhandled exception breaking the message loop.
