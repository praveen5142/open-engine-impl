#!/usr/bin/env python
"""
mcp_server.py — Stdlib-only Model Context Protocol (MCP) server for Open Engine.
Speaks MCP's actual stdio transport: Content-Length-prefixed JSON-RPC 2.0
frames (identical framing to LSP), NOT newline-delimited JSON. Exposes the
knowledge base (rules, wisdom, documents) to MCP-compatible clients.

Found via audit (not caught by this file's own tests, which mock I/O
entirely and only exercise dispatch logic): the first implementation of
this file used newline-delimited JSON-RPC, which real MCP clients
(Claude Desktop, Claude Code, the official `mcp` SDK) do not speak — they
require a `Content-Length: <n>\\r\\n\\r\\n` header before each JSON payload,
same as LSP. A client talking the real protocol would never successfully
parse a response from the old implementation. Framing is read/written on
the raw byte buffers (sys.stdin.buffer / sys.stdout.buffer), not the
text-mode wrappers, since Content-Length counts encoded bytes, not
characters, and text-mode newline translation could corrupt an exact
byte-count read on Windows.
"""

import sys
import os
import json
import sqlite3
import traceback
from contextlib import closing

# Ensure parent directory is in sys.path so we can import adapters
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from adapters.sqlite_memory import SQLiteMemoryStore

# Overridable via env var so tests (and alternate deployments) can point this
# at a different database without editing the file.
DB_PATH = os.environ.get("OPEN_ENGINE_DB_PATH") or os.path.join(BASE_DIR, "db.sqlite")

# Force UTF-8 encoding for stderr text logging. stdin/stdout are read/written
# via their raw .buffer below, so no text-mode reconfiguration is needed (or
# safe to rely on) for the actual protocol frames.
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')


def log_debug(msg: str):
    """Write debug output to stderr only, never stdout (stdout is the
    protocol channel - any stray text on it would corrupt framing)."""
    print(f"[mcp-debug] {msg}", file=sys.stderr, flush=True)


def send_response(response: dict):
    """Serialize and write a JSON-RPC response as a Content-Length-prefixed
    frame - the actual MCP stdio wire format, not a newline-terminated line."""
    body = json.dumps(response, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    sys.stdout.buffer.write(header)
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def read_message():
    """Read one Content-Length-prefixed JSON-RPC frame from stdin. Returns
    the parsed dict, or None at EOF. Raises json.JSONDecodeError if the body
    isn't valid JSON (caller decides how to respond)."""
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None  # EOF before/within headers
        line = line.decode("ascii", errors="replace").rstrip("\r\n")
        if line == "":
            break  # blank line ends the header block
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()

    length = int(headers.get("content-length", 0) or 0)
    if length <= 0:
        return {}

    body = b""
    while len(body) < length:
        chunk = sys.stdin.buffer.read(length - len(body))
        if not chunk:
            break  # EOF mid-body; return what we have, json.loads will raise
        body += chunk

    return json.loads(body.decode("utf-8"))


def send_error(msg_id, code: int, message: str, data=None):
    """Send a standard JSON-RPC 2.0 error."""
    err = {
        "code": code,
        "message": message
    }
    if data is not None:
        err["data"] = data
    send_response({
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": err
    })


# -- Tool Handlers -------------------------------------------------------------

def handle_search_knowledge(arguments: dict) -> dict:
    query = arguments.get("query")
    if not query:
        raise ValueError("Missing 'query' argument")
    k = arguments.get("k", 5)
    
    store = SQLiteMemoryStore(DB_PATH)
    results = store.search(query, k=k)
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(results, indent=2, ensure_ascii=False)
            }
        ],
        "isError": False
    }


def handle_get_document(arguments: dict) -> dict:
    doc_id = arguments.get("id")
    if doc_id is None:
        raise ValueError("Missing 'id' argument")
    
    store = SQLiteMemoryStore(DB_PATH)
    doc = store.get_document(int(doc_id))
    if not doc:
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Document with ID {doc_id} not found."
                }
            ],
            "isError": True
        }
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(doc, indent=2, ensure_ascii=False)
            }
        ],
        "isError": False
    }


def handle_add_rule(arguments: dict) -> dict:
    title = arguments.get("title")
    content = arguments.get("content")
    if not title or not content:
        raise ValueError("Missing 'title' or 'content' argument")
    
    store = SQLiteMemoryStore(DB_PATH)
    res = store.add_rule(title, content)
    return {
        "content": [
            {
                "type": "text",
                "text": f"Rule added successfully. ID: {res.get('id')}"
            }
        ],
        "isError": False
    }


# -- JSON-RPC Dispatcher -------------------------------------------------------

def dispatch_request(msg: dict):
    msg_id = msg.get("id")
    method = msg.get("method")
    params = msg.get("params", {})

    # Handshake / Lifecycle
    if method == "initialize":
        send_response({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {}
                },
                "serverInfo": {
                    "name": "open-engine-brain",
                    "version": "1.0.0"
                }
            }
        })
        return

    if method == "notifications/initialized":
        # Client notifying us initialization is complete (no response required)
        return

    if method == "ping":
        send_response({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {}
        })
        return

    # Tools management
    if method == "tools/list":
        send_response({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "tools": [
                    {
                        "name": "search_knowledge",
                        "description": "Search the open-engine knowledge base for rules, wisdom, or reference documents matching the query.",
                        "inputSchema": {
                          "type": "object",
                          "properties": {
                            "query": {
                              "type": "string",
                              "description": "The search query (uses keyword/phrase matching)"
                            },
                            "k": {
                              "type": "integer",
                              "description": "Maximum number of snippets to return",
                              "default": 5
                            }
                          },
                          "required": ["query"]
                        }
                    },
                    {
                        "name": "get_document",
                        "description": "Retrieve a single knowledge document (rule, wisdom, etc.) by its unique ID.",
                        "inputSchema": {
                          "type": "object",
                          "properties": {
                            "id": {
                              "type": "integer",
                              "description": "The unique document ID"
                            }
                          },
                          "required": ["id"]
                        }
                    },
                    {
                        "name": "add_rule",
                        "description": "Add a new rule document directly to the memory store knowledge base.",
                        "inputSchema": {
                          "type": "object",
                          "properties": {
                            "title": {
                              "type": "string",
                              "description": "Title of the rule"
                            },
                            "content": {
                              "type": "string",
                              "description": "Detailed text content of the rule"
                            }
                          },
                          "required": ["title", "content"]
                        }
                    }
                ]
            }
        })
        return

    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        
        try:
            if tool_name == "search_knowledge":
                res = handle_search_knowledge(arguments)
            elif tool_name == "get_document":
                res = handle_get_document(arguments)
            elif tool_name == "add_rule":
                res = handle_add_rule(arguments)
            else:
                send_error(msg_id, -32601, f"Tool '{tool_name}' not found")
                return
            
            send_response({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": res
            })
        except Exception as e:
            log_debug(f"Error executing tool {tool_name}: {traceback.format_exc()}")
            send_response({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Error: {str(e)}"
                        }
                    ],
                    "isError": True
                }
            })
        return

    # Fallback for unrecognized methods
    send_error(msg_id, -32601, f"Method '{method}' not found")


def main():
    log_debug("Starting Open Engine MCP Server...")
    while True:
        try:
            try:
                msg = read_message()
            except json.JSONDecodeError:
                send_error(None, -32700, "Parse error")
                continue

            if msg is None:
                break  # EOF - client closed the connection
            if not msg:
                continue  # empty/zero-length frame, nothing to dispatch

            if not isinstance(msg, dict) or "jsonrpc" not in msg:
                send_error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
                continue

            dispatch_request(msg)
        except Exception as e:
            log_debug(f"Loop error: {traceback.format_exc()}")


if __name__ == "__main__":
    main()
