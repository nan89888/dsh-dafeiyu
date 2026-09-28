"""Phase 0 native BigFish helper.

The DSH plugin owns this process and sends newline-delimited JSON over stdin.
Closing stdin is a lifecycle signal: the helper exits instead of becoming an
independent desktop application.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import threading
import time
from json import JSONDecodeError
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any, TextIO

try:
    from .animation_model import AnimationModel, crossfade_duration
    from .activity_director import ActivityDirector
    from .layout_store import default_layout_path, load_layout, save_layout
    from .asset_paths import bundle_root
except ImportError:
    from animation_model import AnimationModel, crossfade_duration
    from activity_director import ActivityDirector
    from layout_store import default_layout_path, load_layout, save_layout
    from asset_paths import bundle_root


PROTOCOL_VERSION = 1
STATES = {"IDLE", "THINKING", "WORKING", "WAITING", "SUCCESS", "ERROR", "DISCONNECTED"}
# This is a presentation label only.  The transport and model selection still
# use the Codex protocol internally; conversation history is shown as the
# blue BigFish companion requested by the user.
ASSISTANT_DISPLAY_NAME = "蓝色大肥鱼"
# Authored non-core actions are intentionally held for about two seconds.
# The old values were stage-transition timings (300/840/300 ms), which meant
# the corresponding clips were cut off before their frames could be seen.
DRAG_RELEASE_MS = 2000
DRAG_FALLING_MS = 2000
DRAG_LANDING_MS = 2000
DRAG_DIZZY_MS = 2000
DRAG_PROTEST_MS = 2000
# A release has one falling animation followed by one deterministic landing
# recovery. The old chain selected a random dizzy outcome after every drag, so
# even a gentle move looked like a crash. The native physics path still shows
# dizziness only when its measured impact exceeds the hard threshold.
DRAG_RELEASE_STAGES = (
    ("falling", DRAG_FALLING_MS),
    ("landing", DRAG_LANDING_MS),
)
DRAG_RELEASE_OUTCOMES = (("landing", DRAG_LANDING_MS),)
# A decoded 412x344 ARGB32 frame costs ~0.55 MB.  Keep the authored reaction
# atlas (head/poke/tail plus the short daily clips) resident; the long 24-fps
# state loops remain bounded by the same cache and are decoded on demand.
# The previous 280-entry limit evicted the tail of a 96-frame interaction clip
# as soon as another reaction started, which looked like a one-frame action.
DECODED_FRAME_CACHE = 560
# A companion may react to the cursor, but should not pace continuously.  A
# short walk is deliberately a scarce activity: at most two starts in any
# rolling minute, with a sizeable gap before the next one.
WALK_HISTORY_WINDOW_MS = 60_000
MAX_WALKS_PER_WINDOW = 2
WALK_CLIPS = frozenset({
    "walk_left", "walk_right", "walk_start_left", "walk_start_right",
    "walk_stop_left", "walk_stop_right",
})
# These are local, finite visual activities rather than locomotion.  Once one
# starts it owns the sprite until its authored frame sequence has completed.
ACTION_LOCK_CLIPS = frozenset({
    "blink", "glance", "happy", "talk", "sweep", "sleep", "eating", "angry", "dizzy",
    "head_pat", "poke", "tail", "eat_token",
})


def configure_qt_platform() -> None:
    """Prefer XWayland when available so desktop-window controls keep working."""
    if sys.platform != "linux" or os.environ.get("QT_QPA_PLATFORM"):
        return
    platforms: list[str] = []
    if os.environ.get("DISPLAY"):
        platforms.append("xcb")
    if os.environ.get("WAYLAND_DISPLAY"):
        platforms.append("wayland")
    if platforms:
        os.environ["QT_QPA_PLATFORM"] = ";".join(platforms)


def configure_stdio() -> None:
    """Make the JSONL pipe UTF-8 regardless of the Windows console code page."""
    for stream, errors in ((sys.stdin, "strict"), (sys.stdout, "backslashreplace"), (sys.stderr, "backslashreplace")):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors=errors)


def configure_macos_app_identity() -> None:
    """Keep the Qt child out of Dock without calling Objective-C through ctypes.

    The old implementation invoked variadic ``objc_msgSend`` without declaring
    its argument ABI. On macOS this can truncate object pointers and crash the
    Python process inside ``objc_msgSend``. The native DSH launcher already
    sets the accessory activation policy, and Qt owns the application object,
    so there is no need for a second unsafe policy call here.
    """
    return


_window_level_bridge: Any = None


def apply_macos_window_level(widget: Any, mode: str) -> bool:
    """Apply the NSWindow level through the compiled AppKit bridge."""
    global _window_level_bridge
    if sys.platform != "darwin":
        return False
    try:
        import ctypes
        if _window_level_bridge is None:
            candidates = []
            asset_root = os.environ.get("DSH_DAFEIYU_ASSET_ROOT")
            if asset_root:
                candidates.append(Path(asset_root).parent / "libdsh-window-level.dylib")
            candidates.append(Path(__file__).resolve().parent.parent / "runtime/bin/darwin/DSH.app/Contents/Resources/libdsh-window-level.dylib")
            _window_level_bridge = ctypes.CDLL(next(str(path) for path in candidates if path.exists()))
            _window_level_bridge.dsh_apply_window_level.argtypes = [ctypes.c_size_t, ctypes.c_int]
            _window_level_bridge.dsh_apply_window_level.restype = None
        _window_level_bridge.dsh_apply_window_level(int(widget.winId()), int(mode == "topmost"))
        return True
    except Exception as error:
        print(f"Unable to apply macOS DSH window level: {error}", file=sys.stderr)
        return False


def parse_message(line: str) -> dict[str, Any]:
    message = json.loads(line)
    if not isinstance(message, dict):
        raise ValueError("message must be an object")
    if message.get("protocolVersion") != PROTOCOL_VERSION:
        raise ValueError("unsupported protocol version")
    kind = message.get("kind")
    if kind in {"state", "pulse"} and message.get("state") not in STATES:
        raise ValueError("unsupported companion state")
    return message


def emit_reply(kind: str, **payload: Any) -> None:
    print(
        json.dumps(
            {"protocolVersion": PROTOCOL_VERSION, "kind": kind, "timestamp": int(time.time() * 1000), **payload},
            ensure_ascii=False,
        ),
        flush=True,
    )


def walk_step_distance(speed: float, elapsed_ms: int | float) -> float:
    """Return the distance for one animation tick in screen pixels.

    The old wander loop multiplied the configured speed by a fixed 18 ms
    constant even though the timer runs at 30/60 Hz.  This made the body and
    foot cadence drift apart (most obvious in the right-facing cycle) and
    changed speed when a frame decode delayed a tick.  Use elapsed time so
    both directions share the same physical cadence.
    """
    try:
        speed_value = max(0.0, float(speed))
        elapsed_value = max(0.0, float(elapsed_ms))
    except (TypeError, ValueError):
        return 0.0
    return speed_value * elapsed_value / 1000.0


# ---- Glove cursor (native Win32 .cur) ----
def _cursor_api() -> tuple[Any, Any, Any]:
    """Return configured (LoadCursorFromFileW, SetCursor, LoadCursorW) callables.

    Function signatures are declared explicitly so HCURSOR values are handled
    as pointer-sized handles: without argtypes/restype, ctypes treats the
    return value as a C int and truncates 64-bit HCURSORs. wintypes has no
    HCURSOR type, so ``wintypes.HANDLE`` (pointer-sized) is used.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    load_from_file = user32.LoadCursorFromFileW
    load_from_file.argtypes = [wintypes.LPCWSTR]
    load_from_file.restype = wintypes.HANDLE

    set_cursor = user32.SetCursor
    set_cursor.argtypes = [wintypes.HANDLE]
    set_cursor.restype = wintypes.HANDLE

    load_cursor = user32.LoadCursorW
    load_cursor.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR]
    load_cursor.restype = wintypes.HANDLE

    return load_from_file, set_cursor, load_cursor


def load_native_cursor(path: Path) -> Any:
    """Load a .cur cursor with LoadCursorFromFileW (pointer-sized handles).

    Returns None outside Windows or on failure; callers then fall back to the
    Qt default cursor. The resource is loaded at its natural size: per the MSDN
    remarks, LoadCursorFromFileW does not participate in DPI virtualization, so
    the 32x32 .cur is NOT scaled up automatically; the OS renders the cursor
    bitmap on the same pipeline as system cursors.
    """
    if sys.platform != "win32":
        return None
    try:
        handle = _cursor_api()[0](str(path))
        return handle or None
    except Exception:
        return None


def set_native_cursor(handle: Any) -> None:
    """Set the current cursor immediately (bypasses Qt's cursor pipeline)."""
    try:
        _cursor_api()[1](handle)
    except Exception:
        pass


def reset_native_cursor() -> None:
    """Restore the default arrow cursor (IDC_ARROW)."""
    try:
        import ctypes

        # MAKEINTRESOURCE(32512): the identifier is passed as a pointer value.
        arrow = ctypes.cast(ctypes.c_void_p(32512), ctypes.c_wchar_p)
        _cursor_api()[2](None, arrow)
    except Exception:
        pass


class GloveCursorController:
    """Pure state machine for the glove cursor; no Qt or Win32 dependencies.

    ``open_h`` / ``closed_h`` may be None (non-Windows, or the .cur assets are
    missing): every query then returns None, so callers simply fall back to the
    default cursor. This keeps all cursor-state decisions testable.
    """

    WM_LBUTTONDOWN = 0x0201

    def __init__(self, open_h: Any, closed_h: Any) -> None:
        self.open_h = open_h
        self.closed_h = closed_h
        self.pressed = False

    def handle_for(self, closed: bool) -> Any:
        return self.closed_h if closed else self.open_h

    def on_enter(self) -> Any:
        """Pointer entered the pet window: show the open hand."""
        return self.open_h

    def on_leave(self) -> None:
        """Pointer left the pet window: clear the pressed flag, reset to arrow."""
        self.pressed = False
        return None

    def on_press(self) -> Any:
        """Left button pressed: show the closed fist."""
        self.pressed = True
        return self.closed_h

    def on_release(self, inside: bool) -> Any:
        """Left button released: open hand when still inside, otherwise None."""
        self.pressed = False
        return self.open_h if inside else None

    def on_wm_setcursor(self, mouse_msg: int) -> Any:
        """WM_SETCURSOR decision: closed on WM_LBUTTONDOWN (or while pressed)."""
        return self.handle_for(mouse_msg == self.WM_LBUTTONDOWN or self.pressed)


class EventRecorder:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._stream: TextIO | None = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._stream = path.open("a", encoding="utf-8")

    def record(self, message: dict[str, Any]) -> None:
        if self._stream is None:
            return
        self._stream.write(json.dumps(message, ensure_ascii=False) + "\n")
        self._stream.flush()

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()


def run_headless(recorder: EventRecorder) -> int:
    try:
        emit_reply("ready")
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                message = parse_message(line)
            except (ValueError, json.JSONDecodeError) as error:
                print(json.dumps({"kind": "error", "message": str(error)}), flush=True)
                continue
            recorder.record(message)
            if message.get("kind") == "ping":
                emit_reply("pong")
                continue
            if message.get("kind") == "shutdown":
                break
    finally:
        recorder.close()
    return 0


def run_visual(recorder: EventRecorder, snapshot_path: Path | None = None) -> int:
    configure_qt_platform()
    try:
        from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QPoint, QRect, QRectF, Qt, QTimer, QUrl, Signal
        from PySide6.QtGui import QColor, QCursor, QDesktopServices, QFont, QFontMetrics, QIcon, QMouseEvent, QPainter, QPen, QPixmap, QImage
        from PySide6.QtWidgets import QApplication, QLineEdit, QMenu, QPushButton, QWidget
    except ImportError:
        print(
            "PySide6 is required for visual mode. Run with --headless for protocol tests.",
            file=sys.stderr,
        )
        recorder.close()
        return 2

    class Inbox(QObject):
        message = Signal(dict)
        closed = Signal()

    manifest_path = bundle_root() / "assets" / "pet-manifest.json"
    asset_root = manifest_path.parent / "pet"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"Unable to load BigFish asset manifest: {error}", file=sys.stderr)
        recorder.close()
        return 2

    class GloveCursorFilter(QAbstractNativeEventFilter):
        """Owns WM_SETCURSOR while the pointer is over the pet window.

        The pet shows a glove hand cursor: an open hand on hover and a closed
        fist while the left button is held. Native .cur cursors are loaded with
        LoadCursorFromFileW and handed to the OS cursor pipeline directly (the
        same rendering path as system cursors; note the API loads the resource
        at its natural 32x32 size and does not participate in DPI
        virtualization). The message is consumed immediately instead of going
        through Qt's QCursor bitmaps, which are re-scaled by Qt and only take
        effect on the next cursor update cycle.
        """

        def __init__(self, widget: Any, open_h: Any, closed_h: Any) -> None:
            super().__init__()
            self.widget = widget
            self.open_h = open_h
            self.closed_h = closed_h

        def nativeEventFilter(self, event_type: bytes, message: Any):
            try:
                if event_type != b"windows_generic_MSG":
                    return False, 0
                import ctypes
                from ctypes import wintypes

                msg = wintypes.MSG.from_address(int(message))
                if msg.message != 0x0020:  # WM_SETCURSOR
                    return False, 0
                hwnd = int(msg.hWnd) if msg.hWnd else 0
                if hwnd and hwnd != int(self.widget.winId()):
                    # Leave popups (context menu, …) to Qt's default cursor.
                    return False, 0
                hit_test = msg.lParam & 0xFFFF
                if hit_test != 1:  # HTCLIENT
                    return False, 0
                mouse_msg = (msg.lParam >> 16) & 0xFFFF
                handle = self.widget.glove.on_wm_setcursor(mouse_msg)
                if handle:
                    set_native_cursor(handle)
                    return True, 0
            except Exception:
                pass
            return False, 0

    class CompanionWindow(QWidget):
        LABELS = {
            "IDLE": "休息中",
            "THINKING": "思考中",
            "WORKING": "干活中",
            "WAITING": "等你呢",
            "SUCCESS": "完成啦",
            "ERROR": "出问题了",
            "DISCONNECTED": "已断开",
        }

        def __init__(self) -> None:
            super().__init__()
            self.layout_path = default_layout_path()
            self.layout = load_layout(self.layout_path)
            configured_scale = os.environ.get("DSH_DAFEIYU_SCALE")
            try:
                self.scale = min(1.4, max(0.55, float(configured_scale))) if configured_scale else self.layout["scale"]
            except ValueError:
                self.scale = self.layout["scale"]
            configured_bubble_scale = os.environ.get("DSH_DAFEIYU_BUBBLE_SCALE")
            try:
                self.bubble_scale = (
                    min(1.2, max(0.8, float(configured_bubble_scale)))
                    if configured_bubble_scale
                    else self.layout["bubbleScale"]
                )
            except ValueError:
                self.bubble_scale = self.layout["bubbleScale"]
            configured_reduced_motion = os.environ.get("DSH_DAFEIYU_REDUCED_MOTION")
            # Standard mode keeps the original idle loop and micro-actions.
            # An explicit environment/config setting can still opt into the
            # reduced-motion accessibility mode.
            self.reduced_motion = (
                configured_reduced_motion == "1"
                if configured_reduced_motion is not None
                else False
            )
            configured_sound_enabled = os.environ.get("DSH_DAFEIYU_SOUND_ENABLED")
            self.sound_enabled = configured_sound_enabled != "0"
            self.activity_level = os.environ.get("DSH_DAFEIYU_ACTIVITY_LEVEL", "normal")
            configured_bubble_mode = os.environ.get("DSH_DAFEIYU_BUBBLE_MODE")
            self.bubble_mode = (
                configured_bubble_mode
                if configured_bubble_mode in {"always", "hidden", "custom"}
                else self.layout.get("bubbleMode", "always")
            )
            configured_bubble_states = os.environ.get("DSH_DAFEIYU_BUBBLE_STATES")
            if configured_bubble_states is not None:
                self.bubble_states = [part.strip() for part in configured_bubble_states.split(",") if part.strip()]
            else:
                self.bubble_states = list(self.layout.get("bubbleStates", ["SUCCESS", "ERROR", "WAITING"]))
            self.model = AnimationModel(manifest)
            self.frame_sources: dict[str, bytes] = {}
            for clip in self.model.clips.values():
                for frame in clip.frames:
                    if frame in self.frame_sources:
                        continue
                    try:
                        self.frame_sources[frame] = (asset_root / frame).read_bytes()
                    except OSError as error:
                        raise RuntimeError(f"Unable to load BigFish frame: {frame}") from error
            self.decoded_frames: OrderedDict[str, QPixmap] = OrderedDict()
            # Fail here, not on the first paint, when the assets are unreadable or
            # the bundled Qt has no WebP image plugin.
            for clip in self.model.clips.values():
                if clip.frames:
                    self._pixmap(clip.frames[0])

            # Warm only the short interaction clips.  Long 24-fps loops stay
            # in the bounded LRU cache; decoding the complete idle/work set
            # here would waste hundreds of MB before the first paint.
            for clip in self.model.clips.values():
                if len(clip.frames) <= 16:
                    for frame in clip.frames:
                        self._pixmap(frame)
            # Force the short action atlas to remain resident.  The old cache
            # was large enough in theory, but long looping clips were inserted
            # while a reaction was playing and evicted its tail frames, making
            # the action appear to jump from one frame to another.
            self._reaction_frames = {
                frame
                for name, clip in self.model.clips.items()
                if name not in {"idle", "thinking", "working", "waiting", "success", "error", "dragging"}
                for frame in clip.frames
            }

            self.display_state = "IDLE"
            self.status_state = "IDLE"
            self.status_message = "我在这儿等新任务哦"
            self.status_detail = "DSH · 等待下一次任务"
            self.status_deadline_ms: int | None = self._now_ms() + 4200
            self.conversation_path = default_layout_path().parent / "conversation.json"
            self.conversation: list[dict[str, str]] = []
            try:
                saved_conversation = json.loads(self.conversation_path.read_text(encoding="utf-8"))
                if isinstance(saved_conversation, list):
                    self.conversation = [
                        {"role": str(item.get("role", "")), "text": str(item.get("text", ""))}
                        for item in saved_conversation[-100:]
                        if isinstance(item, dict) and item.get("text")
                    ]
            except (OSError, JSONDecodeError):
                pass
            # Glove cursor: native .cur handles (loaded at natural size).
            self.glove_open_h = load_native_cursor(asset_root.parent / "cursor_grab.cur")
            self.glove_closed_h = load_native_cursor(asset_root.parent / "cursor_grabbing.cur")
            self.glove = GloveCursorController(self.glove_open_h, self.glove_closed_h)
            self.overlay_state: str | None = None
            self.overlay_message = ""
            self.overlay_detail = ""
            self.overlay_deadline_ms: int | None = None
            self.task = ""
            self.tasks: list[dict[str, Any]] = []
            self.webui_url = os.environ.get("DSH_DAFEIYU_WEBUI_URL", "http://127.0.0.1:3080/")
            self.shake_timer: QTimer | None = None
            self.shake_origin: QPoint | None = None
            self.shake_count = 0
            self.drag_origin: QPoint | None = None
            self.pet_origin: QPoint | None = None
            self.scroll_drag_origin: int | None = None
            self.scroll_drag_start = 0.0
            self.pet_x = 0
            self.pet_y = 0
            # The native frames do not all have the same pixel canvas (the
            # side-walk frames are intentionally narrower than the front
            # view).  Keep the pet's logical canvas position separate from
            # the window position so changing to a walk frame never changes
            # the character scale or anchor.
            self.pet_local_x = 0.0
            self.dragging = False
            self.drag_chain_id = 0
            self.last_tick_ms = self._now_ms()
            self.fade_from_pixmap: QPixmap | None = None
            self.fade_started = 0.0
            self.fade_duration = 0.15
            self.animation_timer = QTimer(self)
            self.animation_timer.setTimerType(Qt.TimerType.PreciseTimer)
            self.animation_timer.timeout.connect(self._tick)
            self.animation_timer.start(self._animation_interval_ms())
            # macOS can re-stack a Qt tool window after a space/app switch.
            # Re-assert the user's selected topmost mode without stealing
            # keyboard focus from ChatGPT.
            self.keep_front_timer = QTimer(self)
            self.keep_front_timer.setInterval(2000)
            self.keep_front_timer.timeout.connect(self._keep_front)
            self.keep_front_timer.start()
            self.micro_timer = QTimer(self)
            self.micro_timer.setSingleShot(True)
            self.micro_timer.timeout.connect(self._play_idle_micro)
            self.activity_director = ActivityDirector()
            self.conversation_scroll = float("inf")
            self.conversation_expanded = False
            self.selected_model = os.environ.get("DSH_DAFEIYU_MODEL", "")
            self.reasoning_effort = os.environ.get("DSH_DAFEIYU_REASONING", "")
            self.movement_mode = self._normalise_movement_mode(
                os.environ.get("DSH_DAFEIYU_MOVEMENT_MODE", self.layout.get("movementMode", "follow"))
            )
            self.activity_director.set_mode(self.movement_mode)
            self.activity_level = {"quiet": "quiet", "lively": "lively"}.get(self.movement_mode, self.activity_level)
            self.activity_director.set_level(self.activity_level)
            if not self.reduced_motion:
                self.activity_director.schedule(initial=True)
                self._schedule_micro()
            try:
                self.walk_speed = float(os.environ.get("DSH_DAFEIYU_WALK_SPEED", "82"))
            except ValueError:
                self.walk_speed = 82.0
            self.walk_target_x: float | None = None
            self.walk_direction: str | None = None
            self.walk_pause_until = time.monotonic() + random.uniform(18.0, 28.0)
            self.walk_history: deque[float] = deque()
            self.walk_started = False
            # Explicit walk phases prevent a finite start/stop clip from
            # expiring back to idle while the window is still moving. The
            # token invalidates delayed callbacks when a task or drag
            # interrupts a walk.
            self.walk_phase = "idle"
            self.walk_token = 0
            self.drag_release_active = False
            # Falling and landing are a manually-owned two-stage sequence.
            # Keeping the current stage explicit prevents AnimationModel from
            # exposing its idle underlay for a timer tick at the hand-off.
            self.drag_release_stage: str | None = None
            self.action_lock_until_ms = 0
            self.action_lock_clip: str | None = None
            self.snapshot_saved = False
            self.setWindowTitle("DSH 大肥鱼")
            self.setWindowIcon(QIcon(self._pixmap("idle/idle_001.webp")))
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.Tool
            )
            configured_window_level = os.environ.get("DSH_DAFEIYU_WINDOW_LEVEL")
            persisted_window_level = self.layout.get("windowLevel", "topmost")
            self.window_level_mode = (
                configured_window_level
                if configured_window_level in {"topmost", "desktop"}
                else persisted_window_level
                if persisted_window_level in {"topmost", "desktop"}
                else "topmost"
            )
            self._apply_window_level()
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.setMouseTracking(True)
            self.input_box = QLineEdit(self)
            self.input_box.setPlaceholderText("输入消息给 Codex…")
            self.input_box.setStyleSheet(
                "QLineEdit { background: rgba(255,255,255,245); border: 1px solid #D8DCE3; "
                "border-radius: 10px; padding: 7px 10px; color: #25282D; font-size: 13px; }"
            )
            self.input_box.returnPressed.connect(self._submit_input)
            self.send_button = QPushButton("发送", self)
            self.send_button.setStyleSheet(
                "QPushButton { background: #3478F6; color: white; border: 0; border-radius: 10px; "
                "padding: 7px 13px; font-size: 13px; } QPushButton:disabled { background: #AAB4C5; }"
            )
            self.send_button.clicked.connect(self._submit_input)
            # Child controls must remain above the translucent parent paint
            # surface. Explicitly show/raise them because macOS can otherwise
            # leave a QLineEdit behind a translucent Tool window after a
            # geometry or window-level change.
            self.input_box.show()
            self.send_button.show()
            self.input_box.raise_()
            self.send_button.raise_()
            self._apply_window_size()
            QTimer.singleShot(0, self._restore_visible_position)

        def _submit_input(self) -> None:
            text = self.input_box.text().strip()
            if not text:
                return
            self.input_box.clear()
            self.input_box.setEnabled(False)
            self.send_button.setEnabled(False)
            self._append_conversation("你", text)
            emit_reply("user_input", text=text)
            self._show_status("已发送给 Codex", "等待 Codex 回复", "THINKING", None)
            self.update()

        def set_input_enabled(self, enabled: bool) -> None:
            self.input_box.setEnabled(enabled)
            self.send_button.setEnabled(enabled)

        def _append_conversation(self, role: str, text: str) -> None:
            self.conversation.append({"role": role, "text": text})
            self.conversation = self.conversation[-100:]
            # New messages keep the view at the bottom; the user can scroll up
            # afterwards without the next reply changing the selected history.
            self.conversation_scroll = float("inf")
            try:
                self.conversation_path.parent.mkdir(parents=True, exist_ok=True)
                self.conversation_path.write_text(
                    json.dumps(self.conversation, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except OSError:
                pass
            # The first message changes the card from the compact status card
            # into the fixed-height conversation viewport. Resize immediately
            # so the viewport and input controls are never painted outside the
            # native window bounds.
            self._sync_bubble_size()
            self.update()

        def _conversation_card(self) -> tuple[str, str, str] | None:
            if not self.conversation:
                return None
            latest = self.conversation[-1]
            role = latest.get("role", "")
            prefix = "你" if role == "你" else ASSISTANT_DISPLAY_NAME
            return prefix, latest.get("text", ""), "SUCCESS" if role != "你" else "THINKING"

        def apply_message(self, message: dict[str, Any]) -> None:
            recorder.record(message)
            kind = message.get("kind")
            if kind == "shutdown":
                QApplication.quit()
                return
            if kind == "reply":
                self.set_input_enabled(True)
                reply_text = str(message.get("message", ""))
                self._append_conversation(ASSISTANT_DISPLAY_NAME, reply_text)
                self._show_status(
                    reply_text or f"{ASSISTANT_DISPLAY_NAME} 回复完成",
                    str(message.get("detail", "")),
                    "SUCCESS",
                    10000,
                )
                self.update()
                return
            if kind == "reply_error":
                self.set_input_enabled(True)
                self._show_status(
                    str(message.get("message", f"{ASSISTANT_DISPLAY_NAME} 回复失败")),
                    str(message.get("detail", "")),
                    "ERROR",
                    10000,
                )
                self.update()
                return
            previous_frame = self.model.frame
            previous_clip = self.model.active_clip_name
            if kind == "task":
                self.task = str(message.get("task", ""))
                self._show_status(
                    str(message.get("message", self.task)),
                    str(message.get("detail", "")),
                    self.model.base_state,
                    None if self.model.base_state in {"THINKING", "WORKING", "WAITING", "ERROR"} else 6000,
                )
            elif kind == "tasks":
                raw_tasks = message.get("tasks")
                self.tasks = raw_tasks if isinstance(raw_tasks, list) else []
                self._sync_bubble_size()
            elif kind == "config":
                movement_mode = message.get("movementMode")
                if movement_mode in {"follow", "quiet", "lively", "still", "wander"}:
                    self.movement_mode = self._normalise_movement_mode(movement_mode)
                    self._cancel_walk_for_mode_change()
                    self.activity_director.set_mode(self.movement_mode)
                    self.activity_level = {"quiet": "quiet", "lively": "lively"}.get(self.movement_mode, "normal")
                    self.activity_director.set_level(self.activity_level)
                    self.activity_director.schedule()
                    if self.movement_mode == "quiet":
                        self._cancel_walk_for_mode_change()
                walk_speed = message.get("walkSpeed")
                if isinstance(walk_speed, (int, float)) and not isinstance(walk_speed, bool):
                    self.walk_speed = min(180.0, max(20.0, float(walk_speed)))
                self._sync_bubble_size()
                self._apply_config(message)
            elif kind in {"state", "pulse"}:
                # Task state changes cancel any local walk; local motion must
                # never replace DSH-driven state animations.
                self.walk_token += 1
                self.walk_target_x = None
                self.walk_started = False
                self.walk_phase = "idle"
                state = str(message.get("state", "IDLE"))
                self.display_state = state
                if kind == "pulse":
                    ttl_ms = max(250, int(message.get("ttlMs", 1800)))
                    resume_state = str(message.get("resumeState", self.model.base_state))
                    self.model.apply_pulse(
                        state,
                        ttl_ms,
                        self._now_ms(),
                        resume_state,
                        message.get("resumeActivity"),
                    )
                    self._show_status(
                        str(message.get("resumeMessage", self.LABELS.get(resume_state, resume_state))),
                        str(message.get("resumeDetail", "")),
                        resume_state,
                        None if resume_state in {"THINKING", "WORKING", "WAITING", "ERROR"} else ttl_ms + 2200,
                    )
                    self._show_overlay(
                        str(message.get("message", self.LABELS.get(state, state))),
                        str(message.get("detail", "")),
                        state,
                        ttl_ms,
                    )
                    if state in {"SUCCESS", "ERROR"}:
                        self._notify_alert(state)
                else:
                    activity = None if self.reduced_motion else message.get("activity")
                    self.model.apply_state(state, activity)
                    self._clear_overlay()
                    persistent = state in {"THINKING", "WORKING", "WAITING", "ERROR"}
                    self._show_status(
                        str(message.get("message", self.LABELS.get(state, state))),
                        str(message.get("detail", "")),
                        state,
                        None if persistent else 4200,
                    )
            self._sync_frame_transition(previous_frame, previous_clip)
            self._sync_bubble_size()
            self.update()
            if snapshot_path is not None and not self.snapshot_saved:
                QTimer.singleShot(180, self._save_snapshot)

        def _set_reduced_motion(self, enabled: bool) -> None:
            self.reduced_motion = enabled
            self.animation_timer.setInterval(self._animation_interval_ms())
            if enabled:
                self.micro_timer.stop()
                self._cancel_drag_release_chain()
            else:
                self._schedule_micro()

        def _apply_config(self, message: dict[str, Any]) -> None:
            """Apply a live CONFIG message without restarting the window."""
            scale = message.get("scale")
            if isinstance(scale, (int, float)) and not isinstance(scale, bool):
                self.scale = min(1.4, max(0.55, float(scale)))
            bubble_scale = message.get("bubbleScale")
            if isinstance(bubble_scale, (int, float)) and not isinstance(bubble_scale, bool):
                self.bubble_scale = min(1.2, max(0.8, float(bubble_scale)))
            reduced_motion = message.get("reducedMotion")
            if isinstance(reduced_motion, bool) and reduced_motion != self.reduced_motion:
                self._set_reduced_motion(reduced_motion)
            sound_enabled = message.get("soundEnabled")
            if isinstance(sound_enabled, bool):
                self.sound_enabled = sound_enabled
            activity_level = message.get("activityLevel")
            if activity_level in {"quiet", "normal", "lively"}:
                self.activity_level = activity_level
                self.activity_director.set_level(activity_level)
                self.activity_director.schedule()
                if not self.reduced_motion:
                    self._schedule_micro()
            bubble_mode = message.get("bubbleMode")
            if bubble_mode in {"always", "hidden", "custom"}:
                self.bubble_mode = bubble_mode
            bubble_states = message.get("bubbleStates")
            if isinstance(bubble_states, list):
                self.bubble_states = [str(state) for state in bubble_states if isinstance(state, str)]
            window_level = message.get("windowLevel")
            if window_level in {"topmost", "desktop"} and window_level != self.window_level_mode:
                self.window_level_mode = window_level
                self._apply_window_level()
            self._sync_bubble_size()
            self._save_layout()

        def _apply_glove(self, closed: bool) -> None:
            """Closed fist while the button is held, open hand otherwise.

            Sets the cursor directly so feedback is immediate instead of
            waiting for the next WM_SETCURSOR pass.
            """
            self.glove.pressed = closed
            handle = self.glove.handle_for(closed)
            if handle:
                set_native_cursor(handle)

        def enterEvent(self, event: Any) -> None:
            self._apply_glove(False)
            self._update_conversation_hover(self.mapFromGlobal(QCursor.pos()))
            super().enterEvent(event)

        def leaveEvent(self, event: Any) -> None:
            self.glove.pressed = False
            reset_native_cursor()
            if self.conversation_expanded:
                self.conversation_expanded = False
                self._sync_bubble_size()
            super().leaveEvent(event)

        def _update_conversation_hover(self, position: QPoint) -> None:
            """Expand history only while the pointer is over the conversation card."""
            if not self.conversation:
                return
            card_x, card_y, card_width, card_height = self._bubble_rect()
            hovered = QRect(card_x, card_y, card_width, card_height).contains(position)
            if hovered == self.conversation_expanded:
                return
            self.conversation_expanded = hovered
            self._sync_bubble_size()
            self.update()

        def _animation_interval_ms(self) -> int:
            if self.reduced_motion:
                return 40
            # Keep the normal 24 fps source loops near their native cadence.
            # Only high-frame-rate interaction clips (the 96-frame touch
            # reactions) use 60 Hz. Rendering every idle frame at 60 Hz
            # needlessly saturated a CPU core and was the practical cause of
            # action frames being dropped on this Mac.
            return 16 if self.model.active_clip.frame_ms <= 30 else 33

        def _apply_window_level(self) -> None:
            """Apply the persisted desktop-top/normal-window preference."""
            # Do not rebuild the complete flag set here.  On macOS Qt can
            # briefly destroy/recreate a translucent tool window when
            # ``setWindowFlags`` is used, which is why selecting the ordinary
            # layer used to make DSH disappear.  Toggle only the level bit so
            # the existing native panel, alpha surface and frame geometry stay
            # intact.
            # Qt may hide a native window while changing a flag.  Capture the
            # visibility first and explicitly restore it afterwards; checking
            # ``isVisible`` after setWindowFlag was the reason choosing
            # ordinary mode made the pet disappear permanently.
            was_visible = self.isVisible()
            self.setWindowFlag(
                Qt.WindowType.WindowStaysOnTopHint,
                self.window_level_mode == "topmost",
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            # Changing a Qt window flag can hide a native translucent tool
            # window. Always restore visibility here: this method is called
            # by an explicit level selection or host CONFIG message, never by
            # the “本次隐藏” action. This also repairs a window left hidden by
            # an older helper version.
            self.showNormal()
            if was_visible or self.window_level_mode == "topmost":
                self.show()
            # Explicit menu changes may bring the panel forward once; this
            # method is also called from the watchdog, where raising would
            # steal focus. The caller handles that distinction.
            apply_macos_window_level(self, self.window_level_mode)

        def _keep_front(self) -> None:
            if self.window_level_mode != "topmost" or not self.isVisible():
                return
            # Re-apply only the stacking hint when it was cleared by the
            # window server. Never raise/orderFront on the heartbeat: doing
            # that repeatedly steals the active app's keyboard focus, which
            # made it impossible to type in ChatGPT or another window.
            if not bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint):
                self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
                self.show()
            # The AppKit bridge may reassert the non-activating status level;
            # it deliberately does not order the window front on this path.
            apply_macos_window_level(self, "topmost")

        def _rearm_animation_interval(self) -> None:
            wanted = self._animation_interval_ms()
            if self.animation_timer.interval() != wanted:
                self.animation_timer.setInterval(wanted)

        def _pixmap(self, frame: str) -> QPixmap:
            cached = self.decoded_frames.get(frame)
            if cached is not None:
                self.decoded_frames.move_to_end(frame)
                return cached
            image = QImage()
            if not image.loadFromData(self.frame_sources[frame], "WEBP"):
                raise RuntimeError(f"Unable to load BigFish frame: {frame}")
            # The bundled WebP frames contain a one-byte matte around the
            # character (RGBA = 0,0,0,1). On macOS Qt can retain that matte as
            # a visible black silhouette when the translucent widget moves.
            # Remove every low-alpha fringe before caching, regardless of its
            # RGB value, so no decoder/backend can turn it into a shadow.
            image = image.convertToFormat(QImage.Format.Format_RGBA8888)
            bits = image.bits()
            raw = memoryview(bits)
            for offset in range(0, len(raw), 4):
                alpha = raw[offset + 3]
                # The distributed WebP atlas contains a black matte encoded
                # as very low alpha. Remove that fringe completely. Also
                # remove translucent near-black pixels: those are not part of
                # the authored outline and are exactly what macOS can retain
                # as a second silhouette when a translucent window moves.
                dark_translucent = alpha < 255 and raw[offset] <= 100 and raw[offset + 1] <= 100 and raw[offset + 2] <= 100
                # The idle atlas contains a decoder-generated (0,0,0,1)
                # matte across its entire transparent canvas. Remove only
                # the low-alpha matte/fringe; retain legitimate anti-aliased
                # colored edges so the character does not become jagged.
                if alpha <= 32 or dark_translucent:
                    raw[offset] = 0
                    raw[offset + 1] = 0
                    raw[offset + 2] = 0
                    raw[offset + 3] = 0
            pixmap = QPixmap.fromImage(image)
            self.decoded_frames[frame] = pixmap
            while len(self.decoded_frames) > DECODED_FRAME_CACHE:
                # Never evict an authored short reaction frame.  Those clips
                # must be decoded once and then advance at their declared
                # cadence; otherwise the first paint after an eviction blocks
                # the Qt timer and looks like a single-frame action.
                victim = next(
                    (key for key in self.decoded_frames if key not in getattr(self, "_reaction_frames", set())),
                    None,
                )
                if victim is None:
                    break
                self.decoded_frames.pop(victim, None)
            return pixmap

        def _tick(self) -> None:
            now_ms = self._now_ms()
            elapsed_ms = max(0, now_ms - self.last_tick_ms)
            self.last_tick_ms = now_ms
            had_pulse = self.model.pulse_state is not None
            previous_frame = self.model.frame
            previous_clip = self.model.active_clip_name
            # Accessibility mode suppresses decorative idle motion, but it
            # must never freeze authored action loops.  Walk cycles and the
            # interaction clips still need their declared frame cadence or a
            # movement-mode switch looks like a static pose.
            model_elapsed = elapsed_ms
            self.model.advance(model_elapsed, now_ms)
            if self.action_lock_until_ms and now_ms >= self.action_lock_until_ms:
                self.action_lock_until_ms = 0
                self.action_lock_clip = None
            if self.drag_release_active and not self.dragging and self.drag_release_stage:
                # Finite falling/landing clips are held by the release chain.
                # Re-arm the current stage if the model reached its last
                # frame before the scheduled hand-off timer fired; otherwise
                # an idle or walk pose flashes in the middle of a drop.
                if self.model.overlay_clip_name != self.drag_release_stage:
                    self._play_model_overlay(self.drag_release_stage, allow_fade=False, repaint=False)
            self._tick_wander(now_ms, elapsed_ms)
            self._sync_frame_transition(previous_frame, previous_clip)
            if had_pulse and self.model.pulse_state is None:
                self.display_state = self.model.base_state
            if self.overlay_deadline_ms is not None and now_ms >= self.overlay_deadline_ms:
                self._clear_overlay()
            self._rearm_animation_interval()
            self.update()

        def _trim_walk_history(self, now: float) -> None:
            cutoff = now - WALK_HISTORY_WINDOW_MS / 1000.0
            while self.walk_history and self.walk_history[0] <= cutoff:
                self.walk_history.popleft()

        def _can_start_walk(self, now: float) -> bool:
            """Enforce a rolling two-walk-per-minute budget."""
            self._trim_walk_history(now)
            return (
                now >= self.walk_pause_until
                and len(self.walk_history) < MAX_WALKS_PER_WINDOW
            )

        def _record_walk_start(self, now: float) -> None:
            self._trim_walk_history(now)
            self.walk_history.append(now)
            # Even when the rolling budget has room, leave a deliberate quiet
            # interval so lively mode does not turn into constant pacing.
            self.walk_pause_until = now + random.uniform(28.0, 42.0)

        def _cancel_walk_for_action(self) -> None:
            """Release a walk before a user/local action takes ownership."""
            self.walk_token += 1
            self.walk_target_x = None
            self.walk_started = False
            self.walk_phase = "idle"
            if self.model.overlay_clip_name in WALK_CLIPS:
                self.model.clear_overlay()

        def _action_duration_ms(self, clip_name: str) -> int:
            clip = self.model.clips.get(clip_name)
            if clip is None or clip.loop:
                return 0
            return max(1, len(clip.frames) * clip.frame_ms)

        def _tick_wander(self, now_ms: int, elapsed_ms: int) -> None:
            """Run mode-specific grounded motion while the companion is idle.

            Follow mode uses the cursor as the target: only a meaningful
            horizontal difference starts a directional walk. Quiet mode never
            walks. Lively mode takes periodic short walks and rotates through
            authored activities via ActivityDirector.
            """
            walk_clips = WALK_CLIPS
            protected_clips = {"dragging", "dragging_release", "dragging_dizzy", "falling", "landing", "dizzy"}
            if self.model.overlay_clip_name in protected_clips:
                self.walk_token += 1
                self.walk_target_x = None
                self.walk_started = False
                self.walk_phase = "idle"
                return
            # Reduced-motion controls decorative micro-motion, not the
            # selected movement mode. The old guard disabled follow/lively
            # walking entirely whenever the persisted accessibility toggle
            # was enabled, making every mode appear identical and frozen.
            if self.movement_mode == "quiet" or self.dragging or self.drag_release_active:
                return
            # A local action has priority over locomotion for its complete
            # authored sequence. This guard also covers the final timer tick
            # where AnimationModel is about to hand control back to idle.
            if now_ms < self.action_lock_until_ms:
                return
            if self.model.base_state != "IDLE":
                self.walk_token += 1
                self.walk_target_x = None
                self.walk_phase = "idle"
                if self.walk_started:
                    self.walk_started = False
                    if self.model.overlay_clip_name in walk_clips:
                        self.model.clear_overlay()
                return
            if self.model.overlay_clip_name not in {None, *walk_clips}:
                return
            # Let the authored stop pose finish before reacting to a new
            # cursor position or scheduling another lively walk. Otherwise a
            # second walk can replace the stop clip mid-frame and the two
            # transitions visibly jump.
            if self.walk_phase == "stop":
                return
            now = now_ms / 1000.0
            # In follow mode the cursor is the only walk target. The target is
            # clamped to the same reachable screen bounds used by the window,
            # so the character cannot keep stepping in place at an edge.
            if self.movement_mode == "follow" and self.walk_target_x is None:
                cursor = QCursor.pos()
                geometry = self._screen_geometry_at(self.pet_x, self.pet_y)
                if geometry is not None:
                    width, _ = self._pet_size()
                    left = geometry.left() + 12
                    right = geometry.right() - width - 12
                    desired = min(max(float(cursor.x() - width / 2), left), right)
                    delta = desired - self.pet_x
                    if abs(delta) >= max(34.0, width * 0.12) and self._can_start_walk(now):
                        self.walk_target_x = desired
                        self.walk_direction = "right" if delta > 0 else "left"
                        self.walk_started = True
                        self.walk_phase = "start"
                        self.walk_token += 1
                        self._record_walk_start(now)
                        token = self.walk_token
                        self._play_model_overlay(f"walk_start_{self.walk_direction}", allow_fade=False, repaint=False)
                        QTimer.singleShot(2050, lambda: self._begin_walk_loop(token))
                        return
                return
            if self.walk_target_x is None:
                if not self._can_start_walk(now):
                    return
                geometry = self._screen_geometry_at(self.pet_x, self.pet_y)
                if geometry is None:
                    return
                width, _height = self._pet_size()
                # The window is a large transparent container for the card;
                # the pet itself can occupy either side of that container.
                # Use the visible screen limits for the walk target rather
                # than the container's centre, otherwise the pet reaches the
                # right edge and keeps animating while the clamped window no
                # longer moves.
                # `pet_x` is the left edge of the logical pet canvas (the
                # same anchor used by `_pet_rect`), so use left-edge bounds.
                # Using half-width here made the final target unreachable by
                # the clamped container and caused an endless in-place gait.
                left = geometry.left() + 12
                right = geometry.right() - width - 12
                if right <= left:
                    return
                # Alternate the next trip after each completed walk.  Random
                # destinations are still used within that half of the screen,
                # but a short/random target can no longer make several walks
                # in a row look like a one-direction gait.
                if self.movement_mode == "follow":
                    return
                if self.walk_direction == "right":
                    target = random.uniform(left, min(right, max(left, self.pet_x - width * 0.45)))
                elif self.walk_direction == "left":
                    target = random.uniform(max(left, min(right, self.pet_x + width * 0.45)), right)
                else:
                    target = random.uniform(left, right)
                if abs(target - self.pet_x) < max(70.0, width * 0.45):
                    target = right if self.walk_direction == "left" else left
                self.walk_target_x = target
                self.walk_direction = "right" if target > self.pet_x else "left"
                self.walk_started = True
                self.walk_phase = "start"
                self.walk_token += 1
                self._record_walk_start(now)
                token = self.walk_token
                self._play_model_overlay(f"walk_start_{self.walk_direction}", allow_fade=False, repaint=False)
                # Two authored start frames at 1000 ms each. Add a small
                # guard interval so the model's finite clip cannot win a
                # race with this callback and expose an idle pose mid-walk.
                QTimer.singleShot(2050, lambda: self._begin_walk_loop(token))
                return
            if self.walk_phase == "start":
                # Keep the directional start pose until the body loop is
                # armed; movement never falls back to an unrelated idle clip.
                expected = f"walk_start_{self.walk_direction}"
                if self.model.overlay_clip_name != expected:
                    self._play_model_overlay(expected, allow_fade=False, repaint=False)
                # Do not translate the character while the two-frame start
                # pose is playing. Moving during this phase made the pet look
                # as if it slid sideways instead of walking (especially on
                # the right-facing sequence).
                return
            elif self.walk_phase == "body":
                expected = f"walk_{self.walk_direction}"
                if self.model.overlay_clip_name != expected:
                    self._play_model_overlay(expected, allow_fade=False, repaint=False)
            distance = self.walk_target_x - self.pet_x
            if abs(distance) <= 3.0:
                self._move_to_pet(round(self.walk_target_x), self.pet_y)
                self.walk_target_x = None
                self.walk_phase = "stop"
                # The next walk is governed by the rolling budget rather than
                # a short post-arrival pause; this keeps lively mode active
                # through other actions instead of constant pacing.
                self.walk_pause_until = max(self.walk_pause_until, now + random.uniform(28.0, 42.0))
                self.walk_token += 1
                token = self.walk_token
                self._play_model_overlay(f"walk_stop_{self.walk_direction or 'left'}", allow_fade=False, repaint=False)
                QTimer.singleShot(2050, lambda: self._clear_walk_overlay(token))
                return
            # Advance in real elapsed time.  Using a fixed 18 ms step here
            # desynchronised the four right-facing frames from the actual
            # movement whenever the precise timer delivered a 30 Hz tick.
            step = min(abs(distance), walk_step_distance(max(20.0, self.walk_speed), elapsed_ms))
            before = self.pet_x
            self._move_to_pet(round(self.pet_x + (step if distance > 0 else -step)), self.pet_y)
            # A window-level clamp must never leave a directional walk in an
            # endless in-place loop.  Treat a clamped move as arrival and run
            # the matching stop pose; the next walk is free to choose the
            # opposite direction.
            if abs(self.pet_x - before) < 0.01:
                self.walk_target_x = self.pet_x
                self.walk_phase = "body"
                self.walk_token += 1
                token = self.walk_token
                self._play_model_overlay(
                    f"walk_stop_{self.walk_direction or 'left'}",
                    allow_fade=False,
                    repaint=False,
                )
                self.walk_target_x = None
                self.walk_phase = "stop"
                QTimer.singleShot(2050, lambda: self._clear_walk_overlay(token))

        def _begin_walk_loop(self, token: int) -> None:
            if token != self.walk_token or self.walk_target_x is None or not self.walk_started or self.dragging:
                return
            self.walk_phase = "body"
            self._play_model_overlay(f"walk_{self.walk_direction or 'left'}", allow_fade=False, repaint=False)

        def _cancel_walk_for_mode_change(self) -> None:
            """Stop a scheduled/active walk before applying a new mode."""
            self.walk_token += 1
            self.walk_target_x = None
            self.walk_started = False
            self.walk_phase = "idle"
            if self.model.overlay_clip_name in WALK_CLIPS:
                self.model.clear_overlay()

        def _clear_walk_overlay(self, token: int | None = None) -> None:
            if token is not None and token != self.walk_token:
                return
            if self.walk_target_x is not None or self.walk_phase != "stop":
                return
            self.walk_started = False
            self.walk_phase = "idle"
            self._clear_drag_overlay()

        def _play_idle_micro(self) -> None:
            if self.reduced_motion:
                return
            if self.model.base_state != "IDLE" or self.dragging or self.walk_started or self.drag_release_active:
                self._schedule_micro()
                return
            previous_frame = self.model.frame
            previous_clip = self.model.active_clip_name
            clip_name = self.activity_director.choose_idle_clip()
            if clip_name is not None and clip_name in self.model.clips:
                self._play_model_overlay(clip_name)
            self._sync_frame_transition(previous_frame, previous_clip)
            self.update()
            self._schedule_micro()

        @staticmethod
        def _normalise_movement_mode(value: object) -> str:
            value = str(value or "follow").lower()
            return {"still": "quiet", "wander": "lively"}.get(
                value, value if value in {"follow", "quiet", "lively"} else "follow"
            )

        def _sync_frame_transition(
            self,
            previous_frame: str,
            previous_clip: str,
            *,
            allow_fade: bool = True,
        ) -> None:
            current_frame = self.model.frame
            if current_frame == previous_frame:
                return
            duration = crossfade_duration(previous_clip, self.model.active_clip_name) if allow_fade else None
            if duration is None:
                self.fade_from_pixmap = None
                return
            self.fade_from_pixmap = (
                self._pixmap(previous_frame) if previous_frame in self.frame_sources else None
            )
            self.fade_started = time.monotonic()
            self.fade_duration = duration

        def _play_model_overlay(
            self,
            clip_name: str,
            *,
            allow_fade: bool = True,
            repaint: bool = True,
        ) -> bool:
            if clip_name in ACTION_LOCK_CLIPS:
                # A deliberate local action must not be replaced by a walk.
                # If the user asks for it while walking, stop the walk first
                # and then let this clip run to its authored end.
                self._cancel_walk_for_action()
            previous_frame = self.model.frame
            previous_clip = self.model.active_clip_name
            if not self.model.play_overlay(clip_name):
                return False
            duration_ms = self._action_duration_ms(clip_name)
            if clip_name in ACTION_LOCK_CLIPS and duration_ms:
                self.action_lock_until_ms = max(
                    self.action_lock_until_ms,
                    self._now_ms() + duration_ms,
                )
                self.action_lock_clip = clip_name
            self._sync_frame_transition(previous_frame, previous_clip, allow_fade=allow_fade)
            if repaint:
                self.update()
            return True

        def _begin_drag(self) -> None:
            if self.dragging:
                return
            self.dragging = True
            self.drag_chain_id += 1
            self.walk_token += 1
            self.walk_target_x = None
            self.walk_started = False
            self.walk_phase = "idle"
            self.animation_timer.stop()
            self.micro_timer.stop()
            self._play_model_overlay("dragging", allow_fade=False, repaint=False)

        def _finish_drag(self) -> None:
            if not self.dragging:
                return
            now_ms = self._now_ms()
            previous_frame = self.model.frame
            previous_clip = self.model.active_clip_name
            # Expire an underlying pulse before revealing it after a long drag.
            self.model.advance(0, now_ms)
            self.model.clear_overlay()
            self._sync_frame_transition(previous_frame, previous_clip, allow_fade=False)
            self.dragging = False
            self.drag_release_active = False
            self.last_tick_ms = now_ms
            self.animation_timer.start(self._animation_interval_ms())
            if not self.reduced_motion:
                self._schedule_micro()
                self._run_drag_release_chain()

        def _run_drag_release_chain(self) -> None:
            """Play falling then exactly one randomly selected landing result.

            Every stage is driven by its authored duration.  A new grab (or a
            manifest without the stage clips) aborts the chain quietly.
            """
            self.drag_chain_id += 1
            token = self.drag_chain_id
            self.drag_release_active = True
            self.drag_release_stage = None
            # A release owns the whole visual sequence. Never resume a
            # partially scheduled walk underneath falling/landing frames.
            self.walk_token += 1
            self.walk_target_x = None
            self.walk_started = False
            self.walk_phase = "idle"
            # The physical throw path owns the falling/landing result.  This
            # visual-only chain is used only for a short, gentle release and
            # must not inject a second random dizzy outcome after physics has
            # already settled the character.
            stages = DRAG_RELEASE_STAGES

            def play(index: int) -> None:
                if token != self.drag_chain_id or self.dragging:
                    return
                if self.reduced_motion or index >= len(stages):
                    self.drag_release_active = False
                    self.drag_release_stage = None
                    self._clear_drag_overlay()
                    return
                clip_name, hold_ms = stages[index]
                self.drag_release_stage = clip_name
                if not self._play_model_overlay(clip_name, allow_fade=False):
                    self.drag_release_active = False
                    self.drag_release_stage = None
                    self._clear_drag_overlay()
                    return
                QTimer.singleShot(hold_ms, lambda: play(index + 1))

            QTimer.singleShot(0, lambda: play(0))

        def _clear_drag_overlay(self) -> None:
            if self.dragging:
                return
            previous_frame = self.model.frame
            previous_clip = self.model.active_clip_name
            self.model.clear_overlay()
            if self.drag_release_active and self.drag_release_stage is None:
                self.drag_release_active = False
            self._sync_frame_transition(previous_frame, previous_clip)
            self.update()

        def _cancel_drag_release_chain(self) -> None:
            self.drag_chain_id += 1
            self.drag_release_active = False
            self.drag_release_stage = None
            if not self.dragging and self.model.active_clip_name in {
                name for name, _ in (*DRAG_RELEASE_STAGES, *DRAG_RELEASE_OUTCOMES)
            }:
                self._clear_drag_overlay()

        def _schedule_micro(self) -> None:
            if self.reduced_motion:
                self.micro_timer.stop()
                return
            self.activity_director.set_level(self.activity_level)
            delay_ms = {
                "quiet": random.randint(18000, 34000),
                "normal": random.randint(10000, 22000),
                "lively": random.randint(5000, 13000),
            }.get(self.activity_level, random.randint(10000, 22000))
            self.micro_timer.start(delay_ms)

        def _bubble_visible(self) -> bool:
            if self.bubble_mode == "hidden":
                return False
            if self.bubble_mode == "always":
                return True
            if len(self.tasks) >= 2:
                return any(task.get("state") in self.bubble_states for task in self.tasks)
            state = self.overlay_state or self.status_state or self.model.base_state or "IDLE"
            return state in self.bubble_states

        def _sync_bubble_size(self) -> None:
            old_size = (self.width(), self.height())
            self._apply_window_size()
            if (self.width(), self.height()) != old_size:
                self._move_to_pet(self.pet_x, self.pet_y)

        def _apply_window_size(self) -> None:
            pet_width = round(int(manifest["maxFrameWidth"]) * self.scale)
            pet_height = round(int(manifest["maxFrameHeight"]) * self.scale)
            if self._bubble_visible():
                bubble_width = round(360 * self.bubble_scale)
                bubble_height = self._card_height()
                # Keep the input row and the pet visually close.  The old
                # 82px reserve left a large empty band below long dialogue
                # cards because the pet is anchored at the window bottom.
                self.setFixedSize(max(pet_width + 36, bubble_width + 24), pet_height + bubble_height + 40)
            else:
                self.setFixedSize(max(pet_width + 36, 360), pet_height + 40)
            self._position_input()

        def _position_input(self) -> None:
            if not hasattr(self, "input_box"):
                return
            card_x, card_y, card_width, card_height = self._bubble_rect()
            # Keep the input row close to the task/conversation card. The
            # previous 8px gap plus the 58px bottom reserve left a detached
            # empty band between the card and the pet.
            y = card_y + card_height + 1
            button_width = 62
            self.input_box.setGeometry(card_x, y, max(100, card_width - button_width - 8), 34)
            self.send_button.setGeometry(card_x + card_width - button_width, y, button_width, 34)
            self.input_box.show()
            self.send_button.show()
            self.input_box.raise_()
            self.send_button.raise_()

        def _screen_geometry_at(self, x: int, y: int):
            screen = QApplication.screenAt(QPoint(x, y)) or QApplication.primaryScreen()
            if screen is None:
                return None
            return screen.availableGeometry()

        def _pet_size(self) -> tuple[int, int]:
            return (
                round(int(manifest["maxFrameWidth"]) * self.scale),
                round(int(manifest["maxFrameHeight"]) * self.scale),
            )

        def _move_to_pet(self, pet_x: int, pet_y: int) -> None:
            """Move the window so the pet stands at (pet_x, pet_y).

            The pet position is the source of truth; the window is just the
            container that keeps the status bubble on screen.  While the window
            fits on screen the pet stays centered under it.  When the window
            would have to leave the screen, it is clamped and the pet shifts
            inside the window instead, so the pet can stand at any screen
            position while the bubble stays fully visible.
            """
            pet_width, pet_height = self._pet_size()
            geometry = self._screen_geometry_at(pet_x, pet_y)
            if geometry is None:
                self.pet_x = pet_x
                self.pet_y = pet_y
                self.move(
                    pet_x - (self.width() - pet_width) // 2,
                    pet_y - (self.height() - pet_height - 8),
                )
                self.update()
                return

            min_x = geometry.left()
            max_x = max(min_x, geometry.right() - self.width() + 1)
            min_y = geometry.top()
            max_y = max(min_y, geometry.bottom() - self.height() + 1)

            center_offset_x = (self.width() - pet_width) / 2
            window_x = min(max(pet_x - center_offset_x, min_x), max_x)
            # When the transparent container is clamped at a screen edge,
            # preserve the requested pet coordinate by shifting the pet
            # inside the container.  The previous implementation calculated
            # this offset and then discarded it, forcing every walk to remain
            # at the container centre and making right-edge walks look stuck.
            self.pet_local_x = min(max(pet_x - window_x, 0), self.width() - pet_width)
            self.pet_x = window_x + self.pet_local_x
            # Once the window is unclamped, return to the centered logical
            # anchor.  This prevents the character from remaining offset after
            # a previous edge visit or a long conversation card resize.
            if window_x > min_x and window_x < max_x:
                self.pet_local_x = center_offset_x
                self.pet_x = window_x + self.pet_local_x

            top_offset_y = self.height() - pet_height - 8
            window_y = min(max(pet_y - top_offset_y, min_y), max_y)
            self.pet_y = window_y + top_offset_y

            self.move(window_x, window_y)
            # A moving translucent macOS QWidget can retain the previous
            # backing-store region until the next event-loop pass. Repaint
            # synchronously after each anchor move so a previous walk frame
            # can never survive as the detached black silhouette.
            self.repaint()

        def _pet_offset_x(self, pet_width: int | float) -> float:
            # `pet_local_x` is the authoritative offset inside the window;
            # it changes only when the screen/container edge requires it.
            return float(min(max(self.pet_local_x, 0), self.width() - pet_width))

        def _pet_rect(self) -> tuple[int, int, int, int]:
            pet_width, pet_height = self._pet_size()
            return self._pet_offset_x(pet_width), self.height() - pet_height - 8, pet_width, pet_height

        def _bubble_rect(self) -> tuple[int, int, int, int]:
            card_width = round(360 * self.bubble_scale)
            card_height = self._card_height()
            pet_width, _ = self._pet_size()
            pet_center_x = self._pet_offset_x(pet_width) + pet_width // 2
            margin = 14
            card_x = pet_center_x - card_width // 2
            min_x = margin
            max_x = self.width() - card_width - margin
            if max_x < min_x:
                max_x = min_x
            card_x = min(max(card_x, min_x), max_x)
            return card_x, 7, card_width, card_height

        def _restore_visible_position(self) -> None:
            pet_width, pet_height = self._pet_size()
            top_offset = self.height() - pet_height - 8
            center_offset = (self.width() - pet_width) // 2
            saved_pet_x = self.layout.get("petX")
            saved_pet_y = self.layout.get("petY")
            if isinstance(saved_pet_x, int) and isinstance(saved_pet_y, int):
                pet_x, pet_y = saved_pet_x, saved_pet_y
            else:
                saved_x = self.layout.get("x")
                saved_y = self.layout.get("y")
                if isinstance(saved_x, int) and isinstance(saved_y, int):
                    # Legacy layouts stored the window position.  Recreate the
                    # pet position that the old centered layout would have had.
                    pet_x = saved_x + center_offset
                    pet_y = saved_y + top_offset
                else:
                    geometry = self._screen_geometry_at(self.x() + self.width() // 2, self.y() + self.height() // 2)
                    if geometry is None:
                        return
                    pet_x = geometry.right() - pet_width - 24
                    pet_y = geometry.bottom() - pet_height - 24
            self._move_to_pet(pet_x, pet_y)

        def _save_layout(self) -> None:
            self.layout = {
                "version": 1,
                "x": self.x(),
                "y": self.y(),
                "petX": self.pet_x,
                "petY": self.pet_y,
                "scale": self.scale,
                "bubbleScale": self.bubble_scale,
                "reducedMotion": self.reduced_motion,
                "bubbleMode": self.bubble_mode,
                "bubbleStates": self.bubble_states,
                "movementMode": self.movement_mode,
                "walkSpeed": self.walk_speed,
                "windowLevel": self.window_level_mode,
            }
            try:
                save_layout(self.layout_path, self.layout)
            except OSError as error:
                print(f"Unable to save BigFish layout: {error}", file=sys.stderr)

        def _save_snapshot(self) -> None:
            if snapshot_path is None or self.snapshot_saved:
                return
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            self.snapshot_saved = self.grab().save(str(snapshot_path), "PNG")

        def _show_status(self, message: str, detail: str, state: str, ttl_ms: int | None) -> None:
            self.status_message = message
            self.status_detail = detail
            self.status_state = state
            self.status_deadline_ms = None if ttl_ms is None else self._now_ms() + ttl_ms

        def _show_overlay(self, message: str, detail: str, state: str, ttl_ms: int) -> None:
            self.overlay_message = message
            self.overlay_detail = detail or self.status_detail
            self.overlay_state = state
            self.overlay_deadline_ms = self._now_ms() + ttl_ms

        def _clear_overlay(self) -> None:
            self.overlay_message = ""
            self.overlay_detail = ""
            self.overlay_state = None
            self.overlay_deadline_ms = None

        @staticmethod
        def _now_ms() -> int:
            return int(time.monotonic() * 1000)

        def _current_card(self) -> tuple[str, str, str] | None:
            now_ms = self._now_ms()
            if self.overlay_message and (
                self.overlay_deadline_ms is None or now_ms < self.overlay_deadline_ms
            ):
                return self.overlay_message, self.overlay_detail, self.overlay_state or self.status_state
            if self.status_message and (
                self.status_deadline_ms is None or now_ms < self.status_deadline_ms
            ):
                return self.status_message, self.status_detail, self.status_state
            return None

        @staticmethod
        def _status_colors(state: str) -> tuple[QColor, QColor]:
            return {
                "SUCCESS": (QColor("#D9F7E4"), QColor("#12B85A")),
                "ERROR": (QColor("#FDE3E3"), QColor("#E5484D")),
                "WAITING": (QColor("#FFF0CE"), QColor("#D88A00")),
                "THINKING": (QColor("#E2ECFF"), QColor("#4C78E8")),
                "WORKING": (QColor("#DDEBFF"), QColor("#3478F6")),
                "DISCONNECTED": (QColor("#ECEEF1"), QColor("#7B818A")),
            }.get(state, (QColor("#ECEEF1"), QColor("#747A84")))

        def _draw_status_icon(self, painter: QPainter, state: str, center_x: int, center_y: int) -> None:
            background, foreground = self._status_colors(state)
            radius = 23
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(background)
            painter.drawEllipse(center_x - radius, center_y - radius, radius * 2, radius * 2)
            pen = QPen(foreground, 3)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            if state == "SUCCESS":
                painter.drawLine(center_x - 10, center_y, center_x - 3, center_y + 8)
                painter.drawLine(center_x - 3, center_y + 8, center_x + 12, center_y - 10)
            elif state == "ERROR":
                painter.drawLine(center_x - 8, center_y - 8, center_x + 8, center_y + 8)
                painter.drawLine(center_x + 8, center_y - 8, center_x - 8, center_y + 8)
            elif state == "WAITING":
                painter.drawLine(center_x, center_y - 10, center_x, center_y + 3)
                painter.setBrush(foreground)
                painter.drawEllipse(center_x - 2, center_y + 9, 4, 4)
            elif state in {"THINKING", "WORKING"}:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(foreground)
                for offset in (-9, 0, 9):
                    painter.drawEllipse(center_x + offset - 3, center_y - 3, 6, 6)
            else:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(foreground)
                painter.drawEllipse(center_x - 5, center_y - 5, 10, 10)

        def _conversation_rows(self, card_width: int, s: float) -> list[tuple[str, str, int]]:
            """Measure the visible conversation rows so wrapped text gets room to paint."""
            detail_font = QFont("Microsoft YaHei UI")
            detail_font.setPointSizeF(max(7.0, 9.0 * s))
            metrics = QFontMetrics(detail_font)
            text_width = max(40, card_width - round(42 * s))
            flags = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop) | int(Qt.TextFlag.TextWordWrap)
            rows = []
            for item in self.conversation:
                role = item.get("role", "")
                label = "你" if role == "你" else ASSISTANT_DISPLAY_NAME
                line = f"{label}：{item.get('text', '')}"
                # Measure the complete wrapped message. Capping this at a few
                # lines makes the visible rows overlap when a response is long.
                bounds = metrics.boundingRect(QRect(0, 0, text_width, 100000), flags, line)
                height = max(metrics.lineSpacing(), bounds.height())
                rows.append((role, line, height))
            return rows

        def _conversation_viewport_height(self, s: float) -> int:
            # Keep the history card compact by default; hovering its area opens
            # the full viewport without changing the pet's anchored position.
            return max(72, round((200 if self.conversation_expanded else 72) * s))

        def _conversation_scroll_max(self, card_width: int, s: float) -> int:
            rows = self._conversation_rows(card_width, s)
            gap = round(8 * s)
            content_height = sum(height for _, _, height in rows) + max(0, len(rows) - 1) * gap
            return max(0, content_height - self._conversation_viewport_height(s))

        def _conversation_scroll_metrics(self) -> tuple[int, int, int, int, int] | None:
            """Return track geometry in widget coordinates for scrollbar drag."""
            if not self.conversation or not self._bubble_visible():
                return None
            card_x, card_y, card_width, card_height = self._bubble_rect()
            content_top = card_y + round(34 * self.bubble_scale)
            viewport_height = self._conversation_viewport_height(self.bubble_scale)
            scroll_max = self._conversation_scroll_max(card_width, self.bubble_scale)
            if scroll_max <= 0:
                return None
            track_x = card_x + card_width - round(10 * self.bubble_scale)
            track_width = max(8, round(7 * self.bubble_scale))
            thumb_height = max(
                round(24 * self.bubble_scale),
                round(viewport_height * viewport_height / (viewport_height + scroll_max)),
            )
            return track_x, content_top, track_width, viewport_height, thumb_height

        def _clamp_conversation_scroll(self) -> None:
            if not self.conversation:
                self.conversation_scroll = 0
                return
            card_width = round(360 * self.bubble_scale)
            maximum = self._conversation_scroll_max(card_width, self.bubble_scale)
            if self.conversation_scroll == float("inf"):
                self.conversation_scroll = maximum
            self.conversation_scroll = min(max(0.0, float(self.conversation_scroll)), float(maximum))

        def _card_height(self) -> int:
            if self.conversation:
                s = self.bubble_scale
                return round(38 * s) + self._conversation_viewport_height(s) + round(12 * s)
            if len(self.tasks) >= 2:
                rows = min(len(self.tasks), 3)
                return round((58 + rows * 26) * self.bubble_scale)
            return round(84 * self.bubble_scale)

        def _draw_card_background(
            self,
            painter: QPainter,
            card_x: int,
            card_y: int,
            card_width: int,
            card_height: int,
            corner_radius: int,
            s: float,
        ) -> None:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(17, 24, 39, 13))
            painter.drawRoundedRect(
                card_x + 1, card_y + round(13 * s), card_width - 2, card_height,
                corner_radius, corner_radius,
            )
            painter.setBrush(QColor(17, 24, 39, 18))
            painter.drawRoundedRect(
                card_x, card_y + round(7 * s), card_width, card_height,
                corner_radius, corner_radius,
            )
            painter.setPen(QPen(QColor(218, 221, 226, 205), 1))
            painter.setBrush(QColor(252, 252, 253, 248))
            painter.drawRoundedRect(
                card_x, card_y, card_width, card_height,
                corner_radius, corner_radius,
            )

        def _draw_multi_task_card(
            self,
            painter: QPainter,
            card_x: int,
            card_y: int,
            card_width: int,
            card_height: int,
            s: float,
        ) -> None:
            title_font = QFont("Microsoft YaHei UI")
            title_font.setPointSizeF(max(8.0, 11.0 * s))
            title_font.setWeight(QFont.Weight.DemiBold)
            detail_font = QFont("Microsoft YaHei UI")
            detail_font.setPointSizeF(max(7.0, 9.0 * s))
            text_x = card_x + round(16 * s)
            text_width = max(40, card_width - round(42 * s))
            painter.setFont(title_font)
            painter.setPen(QColor("#25282D"))
            title = f"{len(self.tasks)} 个任务进行中"
            painter.drawText(
                text_x,
                card_y + round(10 * s),
                text_width,
                max(12, round(22 * s)),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                QFontMetrics(title_font).elidedText(title, Qt.TextElideMode.ElideRight, text_width),
            )
            painter.setFont(detail_font)
            for index, task in enumerate(self.tasks[:3]):
                row_y = card_y + round((36 + index * 24) * s)
                state = str(task.get("state", "IDLE"))
                state_label = self.LABELS.get(state, state)
                label = task.get("project") or task.get("task") or task.get("message") or state_label
                line = f"{state_label} · {label}"
                _, foreground = self._status_colors(state)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(foreground)
                painter.drawEllipse(text_x, row_y + round(4 * s), round(8 * s), round(8 * s))
                painter.setPen(QColor("#747981"))
                painter.drawText(
                    text_x + round(14 * s),
                    row_y,
                    text_width - round(14 * s),
                    max(12, round(20 * s)),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    QFontMetrics(detail_font).elidedText(line, Qt.TextElideMode.ElideRight, text_width - round(14 * s)),
                )

        def _draw_conversation_card(
            self,
            painter: QPainter,
            card_x: int,
            card_y: int,
            card_width: int,
            card_height: int,
            s: float,
        ) -> None:
            title_font = QFont("Microsoft YaHei UI")
            title_font.setPointSizeF(max(8.0, 11.0 * s))
            title_font.setWeight(QFont.Weight.DemiBold)
            detail_font = QFont("Microsoft YaHei UI")
            detail_font.setPointSizeF(max(7.0, 9.0 * s))
            text_x = card_x + round(16 * s)
            text_width = max(40, card_width - round(42 * s))
            painter.setFont(title_font)
            painter.setPen(QColor("#25282D"))
            title = f"最近对话 · {len(self.conversation)} 条"
            painter.drawText(text_x, card_y + round(10 * s), text_width, max(12, round(22 * s)), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
            painter.setFont(detail_font)
            flags = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop) | int(Qt.TextFlag.TextWordWrap)
            content_top = card_y + round(34 * s)
            viewport_height = self._conversation_viewport_height(s)
            scroll_max = self._conversation_scroll_max(card_width, s)
            self._clamp_conversation_scroll()
            row_y = content_top - round(float(self.conversation_scroll))
            row_gap = round(8 * s)
            painter.save()
            painter.setClipRect(QRect(text_x, content_top, text_width, viewport_height))
            for role, line, row_height in self._conversation_rows(card_width, s):
                painter.setPen(QColor("#747981" if role == "你" else "#3478F6"))
                painter.drawText(text_x, row_y, text_width, row_height, flags, line)
                row_y += row_height + row_gap
            painter.restore()

            if scroll_max > 0:
                track_x = card_x + card_width - round(10 * s)
                track_y = content_top
                track_width = max(8, round(7 * s))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(120, 130, 145, 180))
                painter.drawRoundedRect(track_x, track_y, track_width, viewport_height, track_width, track_width)
                thumb_height = max(round(24 * s), round(viewport_height * viewport_height / (viewport_height + scroll_max)))
                thumb_range = max(0, viewport_height - thumb_height)
                thumb_y = track_y + round(thumb_range * float(self.conversation_scroll) / scroll_max)
                painter.setBrush(QColor(52, 120, 246, 235))
                painter.drawRoundedRect(track_x, thumb_y, track_width, thumb_height, track_width, track_width)

        def wheelEvent(self, event: Any) -> None:
            """Scroll conversation history while the pointer is over its card."""
            if self.conversation and self._bubble_visible():
                position = event.position().toPoint()
                card_x, card_y, card_width, card_height = self._bubble_rect()
                if QRect(card_x, card_y, card_width, card_height).contains(position):
                    self._clamp_conversation_scroll()
                    pixel_delta = event.pixelDelta().y()
                    angle_delta = event.angleDelta().y()
                    # Trackpad events carry pixels; traditional wheels carry
                    # 120-unit steps. Preserve the fractional value so both
                    # feel continuous instead of jumping row by row.
                    delta = pixel_delta if pixel_delta else (angle_delta / 120.0) * 34.0 * self.bubble_scale
                    if delta:
                        self.conversation_scroll -= float(delta)
                        self._clamp_conversation_scroll()
                        self.update()
                    event.accept()
                    return
            event.ignore()

        def _notify_alert(self, state: str) -> None:
            if self.sound_enabled:
                played = False
                if sys.platform == "win32":
                    try:
                        import winsound

                        sound_name = "success.wav" if state == "SUCCESS" else "error.wav"
                        sound_path = bundle_root() / "assets" / "sounds" / sound_name
                        winsound.PlaySound(
                            str(sound_path),
                            winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
                        )
                        played = True
                    except (ImportError, OSError, RuntimeError):
                        pass
                if not played:
                    try:
                        QApplication.beep()
                    except Exception:
                        pass
            self._shake_window()

        def _shake_window(self) -> None:
            if self.shake_timer is None:
                self.shake_timer = QTimer(self)
                self.shake_timer.timeout.connect(self._shake_tick)
            self.shake_origin = self.pos()
            self.shake_count = 0
            self.shake_timer.start(30)

        def _shake_tick(self) -> None:
            offsets = [(6, 0), (-6, 0), (4, 0), (-4, 0), (2, 0), (-2, 0), (0, 0)]
            if self.shake_origin is None:
                self.shake_timer.stop()
                return
            if self.shake_count < len(offsets):
                dx, dy = offsets[self.shake_count]
                self.move(self.shake_origin.x() + dx, self.shake_origin.y() + dy)
                self.shake_count += 1
            else:
                self.shake_timer.stop()
                self.move(self.shake_origin)

        def paintEvent(self, _event: Any) -> None:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            # 平滑缩放：放大/缩小时插值，避免锯齿和模糊
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            # A translucent QWidget does not always invalidate transparent
            # pixels on macOS. Clear the backing store before drawing the new
            # frame, otherwise the previous pet position remains as a shadow.
            # `Source` with a transparent brush is not sufficient on all
            # macOS translucent backing stores: pixels outside the current
            # dirty sub-rect can survive and appear as the old pet's black
            # silhouette. `Clear` explicitly erases the destination first.
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            card = self._current_card() if self._bubble_visible() else None
            bubble_height = 12
            card_x, card_y, card_width, card_height = self._bubble_rect()
            s = self.bubble_scale
            corner_radius = round(30 * s)

            if self.conversation and self._bubble_visible():
                bubble_height = card_y + card_height + 19
                self._draw_card_background(painter, card_x, card_y, card_width, card_height, corner_radius, s)
                self._draw_conversation_card(painter, card_x, card_y, card_width, card_height, s)
            elif len(self.tasks) >= 2 and self._bubble_visible():
                bubble_height = card_y + card_height + 19
                self._draw_card_background(painter, card_x, card_y, card_width, card_height, corner_radius, s)
                self._draw_multi_task_card(painter, card_x, card_y, card_width, card_height, s)
            elif card:
                title, detail, card_state = card
                bubble_height = card_y + card_height + 19
                self._draw_card_background(painter, card_x, card_y, card_width, card_height, corner_radius, s)
                icon_center_x = card_x + card_width - round(39 * s)
                icon_center_y = card_y + card_height // 2
                painter.save()
                painter.translate(icon_center_x, icon_center_y)
                painter.scale(s, s)
                painter.translate(-icon_center_x, -icon_center_y)
                self._draw_status_icon(painter, card_state, icon_center_x, icon_center_y)
                painter.restore()

                text_x = card_x + round(24 * s)
                text_width = max(40, card_width - round(102 * s))
                title_font = QFont("Microsoft YaHei UI")
                title_font.setPointSizeF(max(8.0, 11.0 * s))
                title_font.setWeight(QFont.Weight.DemiBold)
                detail_font = QFont("Microsoft YaHei UI")
                detail_font.setPointSizeF(max(7.0, 9.0 * s))
                painter.setFont(title_font)
                painter.setPen(QColor("#25282D"))
                title_text = QFontMetrics(title_font).elidedText(
                    title,
                    Qt.TextElideMode.ElideRight,
                    text_width,
                )
                painter.drawText(
                    text_x,
                    card_y + round(15 * s),
                    text_width,
                    max(12, round(27 * s)),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    title_text,
                )
                painter.setFont(detail_font)
                painter.setPen(QColor("#747981"))
                detail_text = QFontMetrics(detail_font).elidedText(
                    detail,
                    Qt.TextElideMode.ElideRight,
                    text_width,
                )
                painter.drawText(
                    text_x,
                    card_y + round(43 * s),
                    text_width,
                    max(12, round(24 * s)),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    detail_text,
                )

            pixmap = self._pixmap(self.model.frame)
            phase = time.monotonic()
            motion = self.model.active_clip.motion
            if self.reduced_motion:
                motion = None
            scale_extra = 1.0
            angle = 0.0
            offset_x = 0
            offset_y = 0
            clip_name = self.model.active_clip_name
            if motion == "breathe":
                # 独立版同款：缩放呼吸 + 轻摇摆（无位移）
                scale_extra = 1.0 + 0.02 * math.sin(phase * 2.5)
                angle = math.sin(phase * 2.5) * 1.5
            elif motion == "think":
                offset_y = math.sin(phase * 2.8) * 3
                angle = math.sin(phase * 1.3) * 0.8
            elif motion == "work":
                offset_x = math.sin(phase * 5.4) * 3
                angle = math.sin(phase * 3.1) * 1.0
            elif motion == "wait":
                offset_y = math.sin(phase * 1.8) * 1
                angle = math.sin(phase * 1.2) * 0.8
            elif motion == "bounce":
                offset_y = -abs(math.sin(phase * 5.2)) * 8
                scale_extra = 1.0 + 0.02 * math.sin(phase * 5.2)
            elif motion in {"shake", "dizzy"}:
                offset_x = math.sin(phase * 11.0) * 4
                angle = math.sin(phase * 11.0) * 1.5
            elif motion == "float":
                offset_y = math.sin(phase * 3.0) * 4
                angle = math.sin(phase * 1.6) * 1.0
            # Give walking clips a light bob and quick sway without changing frame timing.
            if clip_name in ("working_search", "working_command"):
                offset_y = -abs(math.sin(phase * 4.5)) * 5
                angle = math.sin(phase * 9.0) * 2.5

            # Scale procedural offsets with the character while retaining subpixel motion.
            offset_x = offset_x * self.scale
            offset_y = offset_y * self.scale

            # Transparent WebP transitions can expose the previous frame as a
            # black silhouette on macOS. Draw each frame at full opacity.
            fade_alpha = 1.0
            self.fade_from_pixmap = None

            def draw_pet(pix: QPixmap, alpha: float) -> None:
                # Draw every frame in the manifest's logical canvas. Walk
                # frames are narrower source images, but their authored
                # character height and ground anchor must match the front
                # frames. Scaling the source rectangle itself was the cause
                # of the visible size jump when walking began.
                canvas_width = int(manifest["maxFrameWidth"]) * self.scale
                canvas_height = int(manifest["maxFrameHeight"]) * self.scale
                source_width = pix.width() * self.scale
                source_height = pix.height() * self.scale
                pw = source_width * scale_extra
                ph = source_height * scale_extra
                logical_left = self._pet_offset_x(canvas_width)
                x = logical_left + (canvas_width - pw) / 2 + offset_x
                # The normalized frame already includes its transparent
                # canvas and ground padding.  Position that canvas once at
                # the shared bottom anchor; subtracting the source height a
                # second time put walking art far below the window.
                y = self.height() - ph - 8 + offset_y
                cx = x + pw / 2
                cy = y + ph / 2
                painter.save()
                painter.setOpacity(alpha)
                painter.translate(cx, cy)
                painter.rotate(angle)
                painter.translate(-cx, -cy)
                painter.drawPixmap(QRectF(x, y, pw, ph), pix, QRectF(0, 0, pix.width(), pix.height()))
                painter.restore()

            # Never paint the previous transparent frame underneath. With the
            # macOS WebP decoder that creates the black duplicate silhouette
            # seen at the right of the character.
            draw_pet(pixmap, fade_alpha)

        def mousePressEvent(self, event: QMouseEvent) -> None:
            if event.button() == Qt.MouseButton.LeftButton:
                metrics = self._conversation_scroll_metrics()
                if metrics is not None:
                    track_x, track_y, track_width, track_height, thumb_height = metrics
                    point = event.position().toPoint()
                    thumb_range = max(1, track_height - thumb_height)
                    self._clamp_conversation_scroll()
                    thumb_y = track_y + round(
                        thumb_range * float(self.conversation_scroll)
                        / max(1, self._conversation_scroll_max(round(360 * self.bubble_scale), self.bubble_scale))
                    )
                    if QRect(track_x - 6, track_y, track_width + 12, track_height).contains(point):
                        self.scroll_drag_origin = point.y()
                        self.scroll_drag_start = float(self.conversation_scroll)
                        # Clicking the track pages toward the pointer; dragging
                        # the thumb itself stays pixel-continuous.
                        if point.y() < thumb_y or point.y() > thumb_y + thumb_height:
                            ratio = min(1.0, max(0.0, (point.y() - track_y - thumb_height / 2) / thumb_range))
                            self.conversation_scroll = ratio * self._conversation_scroll_max(
                                round(360 * self.bubble_scale), self.bubble_scale
                            )
                            self._clamp_conversation_scroll()
                            self.scroll_drag_start = float(self.conversation_scroll)
                        self.update()
                        event.accept()
                        return
                self._apply_glove(True)
                self.drag_origin = event.globalPosition().toPoint()
                self.pet_origin = QPoint(self.pet_x, self.pet_y)
                self.dragging = False

        def mouseMoveEvent(self, event: QMouseEvent) -> None:
            self._update_conversation_hover(event.position().toPoint())
            if self.scroll_drag_origin is not None:
                metrics = self._conversation_scroll_metrics()
                if metrics is not None:
                    _, _, _, track_height, thumb_height = metrics
                    thumb_range = max(1, track_height - thumb_height)
                    scroll_max = self._conversation_scroll_max(round(360 * self.bubble_scale), self.bubble_scale)
                    self.conversation_scroll = self.scroll_drag_start + (
                        (event.position().y() - self.scroll_drag_origin) / thumb_range
                    ) * scroll_max
                    self._clamp_conversation_scroll()
                    self.update()
                event.accept()
                return
            if self.drag_origin is not None and self.pet_origin is not None:
                if not self.dragging and (event.globalPosition().toPoint() - self.drag_origin).manhattanLength() > 5:
                    self._begin_drag()
                delta = event.globalPosition().toPoint() - self.drag_origin
                self._move_to_pet(self.pet_origin.x() + delta.x(), self.pet_origin.y() + delta.y())

        def mouseReleaseEvent(self, event: QMouseEvent) -> None:
            if event.button() == Qt.MouseButton.LeftButton:
                if self.scroll_drag_origin is not None:
                    self.scroll_drag_origin = None
                    event.accept()
                    return
                if self.dragging:
                    self._finish_drag()
                    self._move_to_pet(self.pet_x, self.pet_y)
                    self._save_layout()
                else:
                    self._play_click_interaction(event.position().x(), event.position().y())
            self.drag_origin = None
            self.pet_origin = None
            self.dragging = False
            # The button state is authoritative: restore the open hand if the
            # pointer is still over the pet, or the default arrow otherwise.
            self.glove.pressed = False
            if self.rect().contains(event.position().toPoint()):
                self._apply_glove(False)
            else:
                reset_native_cursor()

        def _play_click_interaction(self, x: float, y: float) -> None:
            pet_x, pet_y, pet_width, pet_height = self._pet_rect()
            relative_x = max(0.0, x - pet_x) / max(1.0, pet_width)
            relative_y = max(0.0, y - pet_y) / max(1.0, pet_height)
            if 0.27 <= relative_x <= 0.73 and 0.20 <= relative_y <= 0.43:
                self._play_model_overlay("head_pat")
                self._show_overlay("摸摸脸就会有好心情~", self.status_detail, self.status_state, 1800)
            elif 0.14 <= relative_x <= 0.86 and 0.02 <= relative_y <= 0.40:
                self._play_model_overlay("head_pat")
                self._show_overlay("摸摸头，我会继续陪着你~", self.status_detail, self.status_state, 1800)
            elif relative_x >= 0.72 or relative_x <= 0.24:
                self._play_model_overlay("tail")
                self._show_overlay("尾巴不是进度条啦！", self.status_detail, self.status_state, 1500)
            else:
                self._play_model_overlay("poke")
                self._show_overlay("戳我干嘛，任务还在跑呢", self.status_detail, self.status_state, 1500)

        def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
            if event.button() == Qt.MouseButton.LeftButton:
                self._play_model_overlay("head_pat")
                self._show_overlay("好啦好啦，知道你喜欢我~", self.status_detail, self.status_state, 1800)

        def contextMenuEvent(self, event: Any) -> None:
            menu = QMenu(self)
            menu.setStyleSheet(
                "QMenu { background: #0b2b45; color: #e4f3fb; border: 1px solid #215779; "
                "padding: 5px; } QMenu::item { padding: 7px 24px 7px 12px; border-radius: 6px; } "
                "QMenu::item:selected { background: #215779; } QMenu::separator { height: 1px; "
                "background: #285a78; margin: 4px 8px; }"
            )
            quick_menu = menu.addMenu("快捷动作")
            quick_actions = {}
            for label, clip, message, state in (
                ("投喂", "eat_token", "给你一份小点心~", "SUCCESS"),
                ("开心", "success", "今天也一起加油！", "SUCCESS"),
                ("休息", "waiting", "我先安静陪着你。", "WAITING"),
                ("扫地", "sweep", "我把桌面清理一下~", "SUCCESS"),
                ("说话", "talk", "我在这里陪你。", "SUCCESS"),
                ("睡觉", "sleep", "先眯一会儿，等你叫我。", "WAITING"),
                ("生气", "angry", "连续戳我会生气的！", "ERROR"),
                ("吃东西", "eating", "嗯，吃饱了才有力气陪你。", "SUCCESS"),
                ("掉落", "falling", "抓稳了，我要掉下去啦！", "WAITING"),
                ("落地", "landing", "稳稳落地。", "SUCCESS"),
                ("眩晕", "dizzy", "刚才那一下有点晕……", "ERROR"),
            ):
                action = quick_menu.addAction(label)
                quick_actions[action] = (clip, message, state)
            move_menu = menu.addMenu("移动模式")
            move_actions = {}
            for label, value in (("跟随鼠标", "follow"), ("安静陪伴", "quiet"), ("活泼陪伴", "lively")):
                action = move_menu.addAction(label)
                action.setCheckable(True)
                action.setChecked(self.movement_mode == value)
                move_actions[action] = value
            menu.addSeparator()
            model_menu = menu.addMenu("模型")
            model_actions = {}
            for label, value in (
                ("Codex 默认", ""),
                ("gpt-5-mini", "gpt-5-mini"),
                ("gpt-5.6-terra", "gpt-5.6-terra"),
                ("gpt-5.5", "gpt-5.5"),
                ("gpt-5.6-sol", "gpt-5.6-sol"),
                ("gpt-5.6-luna", "gpt-5.6-luna"),
            ):
                action = model_menu.addAction(label)
                action.setCheckable(True)
                action.setChecked(self.selected_model == value)
                model_actions[action] = value
            effort_menu = menu.addMenu("推理强度")
            effort_actions = {}
            for label, value in (
                ("跟随模型", ""), ("最小", "minimal"), ("低", "low"),
                ("中", "medium"), ("高", "high"), ("极高", "xhigh"),
                ("最大", "max"), ("超高", "ultra"),
            ):
                action = effort_menu.addAction(label)
                action.setCheckable(True)
                action.setChecked(self.reasoning_effort == value)
                effort_actions[action] = value
            size_menu = menu.addMenu("大小")
            size_actions = {}
            for label, scale in (("迷你", 0.6), ("小", 0.8), ("标准", 0.9), ("大", 1.1)):
                action = size_menu.addAction(label)
                action.setCheckable(True)
                action.setChecked(abs(self.scale - scale) < 0.05)
                size_actions[action] = scale
            bubble_size_menu = menu.addMenu("气泡大小")
            bubble_size_actions = {}
            for label, bubble_scale in (("小", 0.8), ("标准", 1.0), ("大", 1.2)):
                action = bubble_size_menu.addAction(label)
                action.setCheckable(True)
                action.setChecked(abs(self.bubble_scale - bubble_scale) < 0.05)
                bubble_size_actions[action] = bubble_scale
            level_menu = menu.addMenu("窗口层级")
            level_actions = {}
            for label, value in (("桌面顶部（始终置顶）", "topmost"), ("普通窗口层级", "desktop")):
                action = level_menu.addAction(label)
                action.setCheckable(True)
                action.setChecked(self.window_level_mode == value)
                level_actions[action] = value
            reduced_action = menu.addAction("减少动态")
            reduced_action.setCheckable(True)
            reduced_action.setChecked(self.reduced_motion)
            open_webui_action = menu.addAction("打开 WebUI")
            menu.addSeparator()
            hide_action = menu.addAction("本次隐藏")
            exit_action = menu.addAction("本次关闭")
            selected = menu.exec(event.globalPos())
            if selected in quick_actions:
                clip, message, state = quick_actions[selected]
                self._play_model_overlay(clip, allow_fade=False)
                self._show_overlay(message, self.status_detail, state, 1800)
                self.update()
            elif selected in move_actions:
                self.movement_mode = self._normalise_movement_mode(move_actions[selected])
                self._cancel_walk_for_mode_change()
                self.activity_level = {"quiet": "quiet", "lively": "lively"}.get(self.movement_mode, "normal")
                self.activity_director.set_mode(self.movement_mode)
                self.activity_director.set_level(self.activity_level)
                self.activity_director.schedule()
                self._schedule_micro()
                self._save_layout()
                emit_reply("settings", movementMode=self.movement_mode, activityLevel=self.activity_level)
            elif selected in model_actions:
                self.selected_model = model_actions[selected]
                emit_reply("settings", model=self.selected_model)
                self._show_overlay("模型已切换", self.selected_model or "Codex 默认", "SUCCESS", 1800)
                self.update()
            elif selected in effort_actions:
                self.reasoning_effort = effort_actions[selected]
                emit_reply("settings", reasoningEffort=self.reasoning_effort)
                self._show_overlay("推理强度已切换", self.reasoning_effort or "跟随模型", "SUCCESS", 1800)
                self.update()
            elif selected in size_actions:
                self.scale = size_actions[selected]
                self._apply_window_size()
                self._move_to_pet(self.pet_x, self.pet_y)
                self._save_layout()
                emit_reply("settings", scale=self.scale)
            elif selected in bubble_size_actions:
                self.bubble_scale = bubble_size_actions[selected]
                self._apply_window_size()
                self._move_to_pet(self.pet_x, self.pet_y)
                self._save_layout()
                emit_reply("settings", bubbleScale=self.bubble_scale)
            elif selected in level_actions:
                self.window_level_mode = level_actions[selected]
                self._apply_window_level()
                self._save_layout()
                emit_reply("settings", windowLevel=self.window_level_mode)
                self._show_overlay(
                    "窗口层级已切换",
                    "桌面顶部（始终置顶）" if self.window_level_mode == "topmost" else "普通窗口层级",
                    "SUCCESS",
                    1800,
                )
                self.update()
            elif selected == reduced_action:
                self._set_reduced_motion(reduced_action.isChecked())
                self._save_layout()
                emit_reply("settings", reducedMotion=self.reduced_motion)
                self.update()
            elif selected == open_webui_action:
                QDesktopServices.openUrl(QUrl(self.webui_url))
            elif selected == hide_action:
                self.hide()
            elif selected == exit_action:
                self._save_layout()
                emit_reply("closed", reason="user")
                QApplication.quit()

    if sys.platform == "darwin":
        try:
            import ctypes
            ctypes.CDLL(None).setprogname(b"DSH")
        except Exception:
            pass
    if sys.argv:
        sys.argv[0] = "DSH"
    application = QApplication(sys.argv[:1])
    configure_macos_app_identity()
    application.setApplicationName("DSH")
    if hasattr(application, "setApplicationDisplayName"):
        application.setApplicationDisplayName("DSH")
    application.setQuitOnLastWindowClosed(False)
    inbox = Inbox()
    window = CompanionWindow()
    application.setWindowIcon(window.windowIcon())
    glove_filter = GloveCursorFilter(window, window.glove_open_h, window.glove_closed_h)
    application.installNativeEventFilter(glove_filter)
    inbox.message.connect(window.apply_message)
    inbox.closed.connect(application.quit)

    def read_stdin() -> None:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                message = parse_message(line)
                if message.get("kind") == "ping":
                    emit_reply("pong")
                inbox.message.emit(message)
            except (ValueError, json.JSONDecodeError) as error:
                print(json.dumps({"kind": "error", "message": str(error)}), flush=True)
        inbox.closed.emit()

    reader = threading.Thread(target=read_stdin, name="dsh-bigfish-stdin", daemon=True)
    reader.start()
    window.show()
    emit_reply("ready")
    code = application.exec()
    recorder.close()
    return code


def main() -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="DSH BigFish native helper")
    parser.add_argument("--headless", action="store_true", help="validate the protocol without opening a window")
    parser.add_argument("--event-log", type=Path, help="append received protocol messages to a JSONL file")
    parser.add_argument("--snapshot", type=Path, help="save one diagnostic visual frame after the first message")
    args = parser.parse_args()
    recorder = EventRecorder(args.event_log)
    return run_headless(recorder) if args.headless else run_visual(recorder, args.snapshot)


if __name__ == "__main__":
    raise SystemExit(main())
