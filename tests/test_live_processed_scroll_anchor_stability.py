"""Tests for live Processed disclosure scroll stability during streaming & tool execution.

Fixes the issue where expanding Processed to view the latest thinking/tool output
causes the scroll position to jump back to the top of Processed on subsequent updates.
"""

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.find(marker)
    assert start >= 0, f"{name} not found"
    brace = src.find("{", start)
    assert brace >= 0, f"{name} body not found"
    depth = 0
    for i, ch in enumerate(src[brace:], brace):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"{name} body did not terminate")


def test_capture_message_viewport_anchor_identifies_live_turn():
    """Verify that when viewing content inside liveAssistantTurn, it captures live turn instead of previous msg."""
    script = f"""
const assert = require('assert');
let _messageScrollInputGeneration = 1;

const container = {{
  scrollTop: 1200,
  scrollHeight: 3000,
  clientHeight: 600,
  getBoundingClientRect() {{
    return {{ top: 100, bottom: 700 }};
  }},
  querySelectorAll(selector) {{
    if (selector === '[data-msg-idx]') {{
      return [
        {{
          dataset: {{ msgIdx: '0', sessionMsgIdx: '0', messageAnchorKey: 'user-0' }},
          getBoundingClientRect() {{
            // Historical user message is far above the viewport
            return {{ top: -500, bottom: -400 }};
          }}
        }}
      ];
    }}
    return [];
  }},
  querySelector() {{ return null; }}
}};

const liveTurn = {{
  getBoundingClientRect() {{
    // Live assistant turn header starts above viewport, but body extends through viewport
    return {{ top: -200, bottom: 2000 }};
  }}
}};

function $(id) {{
  if (id === 'messages') return container;
  if (id === 'liveAssistantTurn') return liveTurn;
  return null;
}}

{_function_body(UI_JS, "_captureMessageViewportAnchor")}

const anchor = _captureMessageViewportAnchor();
assert(anchor !== null, "Anchor should not be null");
assert.strictEqual(anchor.isLiveTurn, true, "Anchor must be recognized as isLiveTurn");
assert.strictEqual(anchor.top, 1200, "Anchor top must preserve current scrollTop");
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, f"Node script failed:\n{result.stderr}\n{result.stdout}"


def test_restore_message_viewport_anchor_restores_live_turn_position():
    """Verify that restoring a live turn anchor preserves the reader's scroll position."""
    script = f"""
const assert = require('assert');

const container = {{
  scrollTop: 400, // clamped or shifted position
  getBoundingClientRect() {{
    return {{ top: 100, bottom: 700 }};
  }}
}};

const liveTurn = {{
  getBoundingClientRect() {{
    return {{ top: -200, bottom: 2000 }};
  }}
}};

function $(id) {{
  if (id === 'messages') return container;
  if (id === 'liveAssistantTurn') return liveTurn;
  return null;
}}

{_function_body(UI_JS, "_restoreMessageViewportAnchor")}

const anchor = {{
  isLiveTurn: true,
  topOffset: -300,
  top: 1200,
  scrollHeightAtCapture: 3000
}};

const ok = _restoreMessageViewportAnchor(anchor, 0);
assert.strictEqual(ok, true, "Restore should succeed");
// targetTop was -300, liveRect.top - containerRect.top is -300, delta is 0 -> restores to anchor.top (1200)
assert.strictEqual(container.scrollTop, 1200, "Should restore to exact anchor.top");
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, f"Node script failed:\n{result.stderr}\n{result.stdout}"


def test_rebuild_guard_protects_min_height_even_when_near_bottom():
    """Verify that _prepareLiveAnchorScrollRebuildGuard protects minHeight even when near bottom (<= 250px)."""
    script = f"""
const assert = require('assert');

let _messageUserUnpinned = false;
let _scrollPinned = true;
let _nearBottomCount = 0;

const messagesEl = {{
  scrollHeight: 2500,
  scrollTop: 2000,
  clientHeight: 600, // bottomDistance = 2500 - 2000 - 600 = -100 (at bottom, clamped to 0)
}};

const msgInner = {{
  style: {{ minHeight: '' }},
  dataset: {{}}
}};

function $(id) {{
  if (id === 'messages') return messagesEl;
  if (id === 'msgInner') return msgInner;
  return null;
}}

{_function_body(UI_JS, "_prepareLiveAnchorScrollRebuildGuard")}

const scrollSnapshot = {{
  top: 2000,
  scrollHeight: 2500,
  pinned: true
}};

const guard = _prepareLiveAnchorScrollRebuildGuard(scrollSnapshot);
assert(guard !== null, "Guard must not be null");
assert(typeof guard.release === 'function', "Guard release must be a function");
assert.strictEqual(msgInner.style.minHeight, '2500px', "msgInner minHeight must be locked to prevent scroll collapse");

guard.release();
assert.strictEqual(msgInner.style.minHeight, '', "minHeight should be cleared after release");
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, f"Node script failed:\n{result.stderr}\n{result.stdout}"


if __name__ == "__main__":
    test_capture_message_viewport_anchor_identifies_live_turn()
    test_restore_message_viewport_anchor_restores_live_turn_position()
    test_rebuild_guard_protects_min_height_even_when_near_bottom()
    print("All live Processed scroll anchor stability tests PASSED!")
