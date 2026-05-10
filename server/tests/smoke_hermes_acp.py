#!/usr/bin/env python3
"""Smoke test: spawn `hermes acp`, run through the full ACP lifecycle.

Steps:
  1. Launch `hermes acp` subprocess
  2. JSON-RPC initialize handshake
  3. Create a session via session/new
  4. Send a prompt ("Reply with exactly: PONG")
  5. Collect streaming notifications until prompt response returns
  6. Clean shutdown

Run:  uv run python -m server.tests.smoke_hermes_acp
"""

import asyncio
import json
import os
import shutil
import sys
import time

HERMES_BIN = shutil.which("hermes") or os.path.expanduser("~/.local/bin/hermes")
CWD = "/tmp"
TIMEOUT = 60  # seconds for entire test

next_id = 0

def make_id():
    global next_id
    next_id += 1
    return next_id

async def write_rpc(stdin, method, params=None, rpc_id=None):
    msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if rpc_id is not None:
        msg["id"] = rpc_id
    line = json.dumps(msg) + "\n"
    stdin.write(line.encode())
    await stdin.drain()
    return rpc_id

async def write_response(stdin, rpc_id, result):
    msg = {"jsonrpc": "2.0", "id": rpc_id, "result": result}
    line = json.dumps(msg) + "\n"
    stdin.write(line.encode())
    await stdin.drain()

async def read_messages(stdout, timeout=10):
    """Read all available JSON-RPC messages within timeout."""
    messages = []
    buffer = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            data = await asyncio.wait_for(stdout.read(65536), timeout=min(remaining, 1.0))
            if not data:
                break
            buffer += data.decode("utf-8", errors="replace")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    print(f"  [WARN] Non-JSON line: {line[:200]}")
        except asyncio.TimeoutError:
            if messages:
                break
    return messages

async def read_until_response(stdout, rpc_id, timeout=30):
    """Read messages until we get a response matching rpc_id. Return (response, notifications)."""
    notifications = []
    server_requests = []
    buffer = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            data = await asyncio.wait_for(stdout.read(65536), timeout=min(remaining, 2.0))
            if not data:
                break
            buffer += data.decode("utf-8", errors="replace")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    print(f"  [WARN] Non-JSON line: {line[:200]}")
                    continue
                # Is this our response?
                if msg.get("id") == rpc_id and "method" not in msg:
                    return msg, notifications, server_requests
                # Server-initiated request (has both id and method)
                if "id" in msg and "method" in msg:
                    server_requests.append(msg)
                else:
                    notifications.append(msg)
        except asyncio.TimeoutError:
            continue
    return None, notifications, server_requests

def ok(step, msg):
    print(f"  ✓ Step {step}: {msg}")

def fail(step, msg):
    print(f"  ✗ Step {step}: {msg}")

async def main():
    print(f"\n{'='*60}")
    print(f"Hermes ACP Smoke Test")
    print(f"Binary: {HERMES_BIN}")
    print(f"{'='*60}\n")

    if not os.path.isfile(HERMES_BIN):
        fail(0, f"hermes binary not found at {HERMES_BIN}")
        return False

    # Step 1: Launch hermes acp
    print("Step 1: Launching hermes acp subprocess...")
    try:
        proc = await asyncio.create_subprocess_exec(
            HERMES_BIN, "acp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=CWD,
        )
    except Exception as e:
        fail(1, f"Failed to launch: {e}")
        return False

    ok(1, f"Process launched (PID {proc.pid})")

    try:
        # Step 2: Initialize handshake
        print("\nStep 2: Sending initialize request...")
        init_id = make_id()
        await write_rpc(proc.stdin, "initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "vibr8-smoke-test", "version": "0.1.0"},
            "clientCapabilities": {},
        }, rpc_id=init_id)

        response, notifs, server_reqs = await read_until_response(proc.stdout, init_id, timeout=15)
        if response is None:
            fail(2, "No response to initialize (timeout)")
            # Read stderr for clues
            stderr = await asyncio.wait_for(proc.stderr.read(4096), timeout=2) if proc.stderr else b""
            if stderr:
                print(f"  stderr: {stderr.decode()[:500]}")
            return False

        if "error" in response:
            fail(2, f"Initialize returned error: {response['error']}")
            return False

        result = response.get("result", {})
        server_info = result.get("serverInfo", {})
        capabilities = result.get("capabilities", {})
        ok(2, f"Initialize succeeded — server: {server_info.get('name', '?')} v{server_info.get('version', '?')}")
        print(f"    Protocol: {result.get('protocolVersion', '?')}")
        print(f"    Capabilities: {list(capabilities.keys()) if capabilities else 'none reported'}")
        agent_info = result.get("agentInfo", {})
        if agent_info:
            print(f"    Agent: {agent_info.get('name', '?')} v{agent_info.get('version', '?')}")

        # Send initialized notification (required by ACP protocol)
        await write_rpc(proc.stdin, "notifications/initialized")

        # Step 3: Create session
        print("\nStep 3: Creating session via session/new...")
        session_id = make_id()
        await write_rpc(proc.stdin, "session/new", {
            "cwd": CWD,
            "mcpServers": [],
        }, rpc_id=session_id)

        response, notifs, server_reqs = await read_until_response(proc.stdout, session_id, timeout=15)
        if response is None:
            fail(3, "No response to session/new (timeout)")
            stderr = await asyncio.wait_for(proc.stderr.read(4096), timeout=2) if proc.stderr else b""
            if stderr:
                print(f"  stderr: {stderr.decode()[:500]}")
            return False

        if "error" in response:
            fail(3, f"session/new returned error: {response['error']}")
            return False

        session_result = response.get("result", {})
        hermes_session_id = session_result.get("sessionId") or session_result.get("session_id")
        models_info = session_result.get("models", {})
        current_model = models_info.get("current", "unknown") if isinstance(models_info, dict) else "unknown"
        # Extract model from availableModels with "current" in description
        if current_model == "unknown" and isinstance(models_info, dict):
            for m in models_info.get("availableModels", []):
                if "current" in m.get("description", "").lower():
                    current_model = m.get("name", current_model)
                    break
        ok(3, f"Session created — id: {hermes_session_id}, model: {current_model}")

        # Print any notifications we received during session creation
        for n in notifs:
            method = n.get("method", "?")
            update_type = n.get("params", {}).get("sessionUpdate", "")
            print(f"    notification: {method}" + (f" ({update_type})" if update_type else ""))

        # Step 4: Send prompt
        print('\nStep 4: Sending prompt ("Reply with exactly: PONG")...')
        prompt_id = make_id()
        await write_rpc(proc.stdin, "session/prompt", {
            "sessionId": hermes_session_id,
            "prompt": [{"type": "text", "text": "Reply with exactly: PONG"}],
        }, rpc_id=prompt_id)

        # Read until we get the prompt response, collecting all streaming notifications
        print("  Waiting for response (streaming)...")
        all_text = ""
        all_notifs = []
        tool_calls = []

        buffer = ""
        deadline = time.monotonic() + TIMEOUT
        prompt_response = None

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                data = await asyncio.wait_for(proc.stdout.read(65536), timeout=min(remaining, 2.0))
                if not data:
                    break
                buffer += data.decode("utf-8", errors="replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # Response to our prompt call
                    if msg.get("id") == prompt_id and "method" not in msg:
                        prompt_response = msg
                        break

                    # Server request (permission request, etc) — auto-approve
                    if "id" in msg and "method" in msg:
                        method = msg["method"]
                        if method == "session/request_permission":
                            options = msg.get("params", {}).get("options", [])
                            allow_id = None
                            for opt in options:
                                if opt.get("kind") in ("allow", "allow_always"):
                                    allow_id = opt.get("optionId")
                                    break
                            if not allow_id and options:
                                allow_id = options[0].get("optionId")
                            tool_info = msg.get("params", {}).get("toolCall", {})
                            print(f"    [permission] {tool_info.get('title', '?')} → auto-approve (optionId={allow_id})")
                            await write_response(proc.stdin, msg["id"], {
                                "outcome": {"outcome": "selected", "optionId": allow_id or "allow"},
                            })
                        else:
                            print(f"    [server-request] {method} → responding with null")
                            await write_response(proc.stdin, msg["id"], None)
                        continue

                    # Notification
                    method = msg.get("method", "")
                    params = msg.get("params", {})
                    update = params.get("update", params)
                    update_type = update.get("sessionUpdate", "")

                    if update_type:
                        print(f"    [{update_type}]", end=" ")
                        if update_type == "agent_message_chunk":
                            print(update.get("content", {}).get("text", ""), end="")
                        print()

                    if update_type == "agent_message_chunk":
                        content = update.get("content", {})
                        if content.get("type") == "text":
                            text = content.get("text", "")
                            all_text += text
                    elif update_type == "tool_call":
                        tc_title = update.get("title", "?")
                        tool_calls.append(tc_title)
                    elif update_type == "tool_call_update":
                        status = update.get("status", "?")
                        tc_title = update.get("title", "?")
                    elif update_type:
                        all_notifs.append(update_type)

                    if prompt_response:
                        break
            except asyncio.TimeoutError:
                continue

            if prompt_response:
                break

        if prompt_response is None:
            fail(4, "No response to session/prompt (timeout)")
            stderr_data = b""
            try:
                stderr_data = await asyncio.wait_for(proc.stderr.read(4096), timeout=2) if proc.stderr else b""
            except:
                pass
            if stderr_data:
                print(f"  stderr: {stderr_data.decode()[:500]}")
            return False

        if "error" in prompt_response:
            fail(4, f"Prompt returned error: {prompt_response['error']}")
            return False

        prompt_result = prompt_response.get("result", {})
        stop_reason = prompt_result.get("stopReason", "?")
        clean_text = all_text.strip()
        ok(4, f"Prompt completed — stopReason: {stop_reason}")
        print(f"    Response text: \"{clean_text}\"")
        print(f"    Notification types seen: {sorted(set(all_notifs))}")
        if tool_calls:
            print(f"    Tool calls: {tool_calls}")

        has_pong = "PONG" in clean_text.upper()
        if has_pong:
            print(f"    ✓ Response contains PONG")
        else:
            print(f"    ⚠ Response does not contain PONG (model may have elaborated)")

        # Step 5: Clean shutdown
        print("\nStep 5: Shutting down...")
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
            ok(5, f"Process terminated cleanly (exit code: {proc.returncode})")
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            ok(5, f"Process killed after timeout (exit code: {proc.returncode})")

        print(f"\n{'='*60}")
        print(f"SMOKE TEST PASSED — all 5 steps completed successfully")
        print(f"{'='*60}\n")
        return True

    except Exception as e:
        fail(0, f"Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except:
                proc.kill()

if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
