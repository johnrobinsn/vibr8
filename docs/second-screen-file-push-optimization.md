# Second Screen: Direct File Push Optimization

## Problem

Pushing an existing file to a second screen requires two full LLM inference rounds:

1. **Round 1**: Model reasons about which file to send, calls `Read` tool
2. File contents return into the model's context window
3. **Round 2**: Model processes full context (now bloated with file contents), calls `show_on_second_screen` with content as parameter

This adds ~3-5 seconds of unnecessary latency and wastes context window space. The file contents pass through the model for no reason — it already decided what to send.

## Solution

Add a `push_file_to_second_screen` MCP tool that accepts a file path instead of content. The MCP server reads the file directly from disk and pushes it over WebSocket.

### New Tool: `push_file_to_second_screen`

**Parameters:**
- `path` (string, required) — absolute path to the file
- `content_type` (string, optional) — override auto-detection (`markdown`, `file`, `image`, `pdf`). Default: infer from extension.
- `title` (string, optional) — display title. Default: filename.

**Behavior:**
1. MCP server reads the file from disk
2. Detects content type from extension if not specified (`.md` → markdown, `.png/.jpg` → image, `.pdf` → pdf, everything else → file)
3. Pushes to all enabled second screens via existing WebSocket infrastructure
4. Returns success/failure to the model

### Auto-detection mapping

| Extension | Content Type |
|-----------|-------------|
| `.md`, `.markdown` | `markdown` |
| `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.svg` | `image` |
| `.pdf` | `pdf` |
| `.html` | `html` |
| Everything else | `file` |

## Impact

- Eliminates one full inference round (~2-4 seconds saved)
- File contents never enter the model's context window (saves tokens, avoids context bloat)
- No change to model reasoning — it still decides which file to push
- No special-case routing — just a better-designed tool

## Scope

Same pattern applies to other content that currently round-trips through the model:
- Images (currently base64-encoded into context)
- PDFs
- Any large file content

The existing `show_on_second_screen` tool remains for model-generated content (e.g., summaries, formatted output). The new tool is specifically for when the content already exists on disk.
