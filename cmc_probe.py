#!/usr/bin/env python3
"""Probe the CMC Skill Hub over Streamable-HTTP MCP (direct, no MCP install in the
trusted session). Enumerate tools + skills so we know what data we can pull for
features (funding, F&G, derivatives, on-chain, social) and the broader token set.
"""
import json, requests

EP = "https://mcp.coinmarketcap.com/skill-hub/stream"
KEY = open(".cmc_key").read().strip()
H = {"X-CMC-MCP-API-KEY": KEY, "Content-Type": "application/json",
     "Accept": "application/json, text/event-stream"}

def parse(resp):
    """Streamable-HTTP MCP may return SSE framing (event:/data:) or plain JSON."""
    ct = resp.headers.get("content-type", "")
    if "text/event-stream" in ct:
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                try: return json.loads(line[5:].strip())
                except: pass
        return None
    try: return resp.json()
    except: return None

def rpc(method, params=None, sid=None):
    h = dict(H)
    if sid: h["Mcp-Session-Id"] = sid
    r = requests.post(EP, headers=h, timeout=60,
                      data=json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params or {}}))
    return r, parse(r)

# initialize → capture session id
r, init = rpc("initialize", {"protocolVersion":"2025-06-18","capabilities":{},
                             "clientInfo":{"name":"bnbhack","version":"0.1"}})
sid = r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id")
print("server:", (init or {}).get("result",{}).get("serverInfo"))
# notify initialized
requests.post(EP, headers={**H, **({"Mcp-Session-Id":sid} if sid else {})}, timeout=30,
              data=json.dumps({"jsonrpc":"2.0","method":"notifications/initialized","params":{}}))

# tools/list
r, tl = rpc("tools/list", {}, sid)
tools = (tl or {}).get("result",{}).get("tools",[])
print(f"\n=== {len(tools)} TOOLS ===")
for t in tools:
    print(f"- {t['name']}: {t.get('description','')[:110]}")

# try find_skill to enumerate the skills library (data coverage)
r, fs = rpc("tools/call", {"name":"find_skill","arguments":{"query":"funding rate fear greed derivatives"}}, sid)
print("\n=== find_skill('funding rate fear greed derivatives') ===")
res = (fs or {}).get("result",{})
content = res.get("content", res)
print(json.dumps(content, indent=2)[:2500])
