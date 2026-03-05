# Callstack - Modem Telephony Framework for Raspberry Pi

## Overview

Callstack is an async-first Python framework for managing GSM/LTE modem connections on Raspberry Pi. It provides a high-level API for voice calls (inbound/outbound, recording, tone playback, IVR), SMS (send/receive/subscribe), and raw AT command access -- all built on top of `asyncio` with proper state machines, typed events, and clean separation of concerns.

---

## Design Principles

1. **Async-first** -- `asyncio` throughout; no thread-per-feature sprawl
2. **Explicit state machines** -- every stateful subsystem (call, SMS, modem) uses a finite state machine with guarded transitions
3. **Typed event bus** -- dataclass-based events, pub/sub with filtering, async iteration
4. **Layered abstraction** -- hardware transport -> AT protocol -> feature services -> application API
5. **Fail-safe by default** -- automatic reconnection, command retries, resource cleanup via context managers
6. **Zero global state** -- everything flows through the `Modem` instance; fully testable with mock transports

---

## Layer Architecture

```
+------------------------------------------------------------------+
|                        Application Layer                          |
|   User code, IVR scripts, webhook integrations, CLI              |
+------------------------------------------------------------------+
|                        Service Layer                              |
|   CallService  |  SMSService  |  USSDService  |  NetworkService  |
+------------------------------------------------------------------+
|                      Protocol Layer                               |
|   ATCommandExecutor  |  ATResponseParser  |  URCDispatcher       |
+------------------------------------------------------------------+
|                      Transport Layer                              |
|   SerialTransport  |  MockTransport  (asyncio stream protocol)   |
+------------------------------------------------------------------+
|                      Hardware / OS                                |
|   /dev/ttyUSBx  |  /dev/ttyACMx  |  USB modeswitch               |
+------------------------------------------------------------------+
```

---

## Module Map

```
callstack/
  __init__.py              # Public API re-exports
  modem.py                 # Modem: top-level orchestrator, context manager
  config.py                # ModemConfig dataclass + defaults
  errors.py                # Exception hierarchy

  transport/
    __init__.py
    base.py                # Transport protocol (ABC)
    serial.py              # SerialTransport (pyserial-asyncio)
    mock.py                # MockTransport (for testing)

  protocol/
    __init__.py
    executor.py            # ATCommandExecutor: send command, await response
    parser.py              # ATResponseParser: structured response parsing
    urc.py                 # URCDispatcher: unsolicited result code routing
    commands.py            # AT command constants and builders

  events/
    __init__.py
    bus.py                 # EventBus: typed pub/sub + async iteration
    types.py               # Event dataclasses (Ring, CallState, SMS, DTMF, etc.)

  voice/
    __init__.py
    service.py             # CallService: dial, answer, hangup, hold, conference
    state.py               # CallStateMachine (Idle->Ringing->Active->Ended)
    audio.py               # AudioPipeline: PCM stream over serial, play/record WAV files
    dtmf.py                # DTMFCollector: buffered input with timeout/terminator
    player.py              # AudioPlayer: WAV file loading, format validation, queued playback
    ivr.py                 # IVRMenu: prompt + collect pattern for building menus

  sms/
    __init__.py
    service.py             # SMSService: send, read, delete, list, subscribe
    store.py               # SMSStore: in-memory + optional SQLite persistence
    types.py               # SMS dataclass, DeliveryReport, etc.
    pdu.py                 # PDU encoder/decoder (for PDU mode support)

  utils/
    __init__.py
    logger.py              # Structured logging with per-component loggers
    retry.py               # Retry/backoff decorator for AT commands
    signal_quality.py      # RSSI/BER parsing
```

---

## Core Components

### 1. Transport Layer

```python
# transport/base.py
class Transport(ABC):
    """Async byte stream interface to modem hardware."""

    @abstractmethod
    async def open(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def write(self, data: bytes) -> None: ...

    @abstractmethod
    async def read(self, size: int = -1) -> bytes: ...

    @abstractmethod
    async def readline(self) -> bytes: ...

    @abstractmethod
    def in_waiting(self) -> int: ...
```

```python
# transport/serial.py
class SerialTransport(Transport):
    """pyserial-asyncio based transport.

    Opens a serial port as an asyncio stream pair.
    Supports auto-reconnect on USB disconnect/reconnect.
    """

    def __init__(self, port: str, baudrate: int = 115200):
        self.port = port
        self.baudrate = baudrate
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None

    async def open(self):
        self._reader, self._writer = await serial_asyncio.open_serial_connection(
            url=self.port, baudrate=self.baudrate
        )

    async def write(self, data: bytes):
        self._writer.write(data)
        await self._writer.drain()

    async def readline(self) -> bytes:
        return await self._reader.readline()
```

Two transport instances are used per modem:
- **AT transport** (`/dev/ttyUSB2`) -- command/response + URCs
- **Audio transport** (`/dev/ttyUSB4`) -- raw PCM stream during calls

---

### 2. Protocol Layer

#### ATCommandExecutor

Serializes command access, handles response correlation, and separates URCs from command responses.

```python
# protocol/executor.py
class ATCommandExecutor:
    """Send AT commands and await structured responses."""

    def __init__(self, transport: Transport, urc_dispatcher: URCDispatcher):
        self._transport = transport
        self._urc = urc_dispatcher
        self._lock = asyncio.Lock()  # one command at a time

    async def execute(
        self,
        command: str,
        expect: list[str] = ("OK",),
        timeout: float = 5.0,
    ) -> ATResponse:
        """Send an AT command and wait for a final result code.

        Returns ATResponse(success, raw_lines, parsed_data).
        URCs received while waiting are dispatched to URCDispatcher.
        """
        async with self._lock:
            await self._transport.write(f"{command}\r\n".encode())
            return await self._collect_response(expect, timeout)

    async def _collect_response(self, expect, timeout) -> ATResponse:
        lines = []
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise ATTimeoutError(f"Timeout waiting for {expect}")
            try:
                raw = await asyncio.wait_for(
                    self._transport.readline(), timeout=remaining
                )
            except asyncio.TimeoutError:
                raise ATTimeoutError(f"Timeout waiting for {expect}")

            line = raw.decode("ascii", errors="replace").strip()
            if not line:
                continue

            # Check if this is a URC (unsolicited result code)
            if self._urc.is_urc(line):
                await self._urc.dispatch(line)
                continue

            lines.append(line)

            # Check for final result codes
            if any(e in line for e in expect):
                return ATResponse(success=True, lines=lines)
            if "ERROR" in line or "+CME ERROR" in line or "+CMS ERROR" in line:
                return ATResponse(success=False, lines=lines)
```

#### URCDispatcher

Routes unsolicited result codes (RING, +DTMF, +CMT, +CMTI, VOICE CALL, etc.) to registered handlers.

```python
# protocol/urc.py
class URCDispatcher:
    """Routes unsolicited result codes to registered async handlers."""

    URC_PREFIXES = (
        "RING", "+CLIP", "+DTMF", "RXDTMF",
        "+CMT", "+CMTI", "+CDSI",
        "VOICE CALL", "NO CARRIER", "BUSY", "NO ANSWER",
        "+CUSD", "+CREG", "+CGREG",
    )

    def __init__(self, event_bus: EventBus):
        self._bus = event_bus

    def is_urc(self, line: str) -> bool:
        return any(line.startswith(p) for p in self.URC_PREFIXES)

    async def dispatch(self, line: str):
        """Parse URC line and emit typed event on the bus."""
        if line == "RING":
            await self._bus.emit(RingEvent())
        elif line.startswith("+CLIP:"):
            number = self._parse_clip(line)
            await self._bus.emit(CallerIDEvent(number=number))
        elif line.startswith("+DTMF:") or line.startswith("RXDTMF:"):
            digit = line.split(":")[1].strip()
            await self._bus.emit(DTMFEvent(digit=digit))
        elif line == "VOICE CALL: BEGIN":
            await self._bus.emit(CallStateEvent(state=CallState.ACTIVE))
        elif line == "VOICE CALL: END":
            await self._bus.emit(CallStateEvent(state=CallState.ENDED))
        elif line.startswith("+CMT:") or line.startswith("+CMTI:"):
            await self._bus.emit(IncomingSMSEvent(raw=line))
        # ... etc
```

---

### 3. Event System

```python
# events/types.py
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto

class CallState(Enum):
    IDLE = auto()
    DIALING = auto()
    RINGING = auto()
    ACTIVE = auto()
    HELD = auto()
    ENDED = auto()

@dataclass(frozen=True)
class Event:
    timestamp: datetime = field(default_factory=datetime.now)

@dataclass(frozen=True)
class RingEvent(Event):
    pass

@dataclass(frozen=True)
class CallerIDEvent(Event):
    number: str = ""

@dataclass(frozen=True)
class DTMFEvent(Event):
    digit: str = ""

@dataclass(frozen=True)
class CallStateEvent(Event):
    state: CallState = CallState.IDLE

@dataclass(frozen=True)
class IncomingSMSEvent(Event):
    sender: str = ""
    body: str = ""
    raw: str = ""

@dataclass(frozen=True)
class SMSSentEvent(Event):
    recipient: str = ""
    reference: int = 0

@dataclass(frozen=True)
class SignalQualityEvent(Event):
    rssi: int = 0
    ber: int = 0

@dataclass(frozen=True)
class ModemDisconnectedEvent(Event):
    reason: str = ""

@dataclass(frozen=True)
class ModemReconnectedEvent(Event):
    pass
```

```python
# events/bus.py
class EventBus:
    """Typed async event bus with pub/sub and async iteration."""

    def __init__(self):
        self._subscribers: dict[type, list[Callable]] = defaultdict(list)
        self._queues: dict[type, list[asyncio.Queue]] = defaultdict(list)

    def on(self, event_type: type[Event]):
        """Decorator to subscribe a coroutine to an event type."""
        def decorator(fn):
            self._subscribers[event_type].append(fn)
            return fn
        return decorator

    async def emit(self, event: Event):
        """Emit an event to all subscribers and queues."""
        for fn in self._subscribers.get(type(event), []):
            asyncio.create_task(fn(event))
        for q in self._queues.get(type(event), []):
            await q.put(event)

    @asynccontextmanager
    async def stream(self, event_type: type[Event]):
        """Async iterator for events of a given type.

        Usage:
            async with bus.stream(DTMFEvent) as events:
                async for event in events:
                    print(event.digit)
        """
        q: asyncio.Queue[Event] = asyncio.Queue()
        self._queues[event_type].append(q)
        try:
            yield EventStream(q)
        finally:
            self._queues[event_type].remove(q)


class EventStream:
    """Async iterator wrapper around a queue."""

    def __init__(self, queue: asyncio.Queue):
        self._queue = queue

    def __aiter__(self):
        return self

    async def __anext__(self) -> Event:
        return await self._queue.get()

    async def next(self, timeout: float = None) -> Optional[Event]:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            return None
```

---

### 4. Voice / Call Service

#### CallStateMachine

```python
# voice/state.py
class CallStateMachine:
    """Enforces valid call state transitions."""

    TRANSITIONS = {
        CallState.IDLE:    {CallState.DIALING, CallState.RINGING},
        CallState.DIALING: {CallState.ACTIVE, CallState.ENDED},
        CallState.RINGING: {CallState.ACTIVE, CallState.ENDED},
        CallState.ACTIVE:  {CallState.HELD, CallState.ENDED},
        CallState.HELD:    {CallState.ACTIVE, CallState.ENDED},
        CallState.ENDED:   {CallState.IDLE},
    }

    def __init__(self):
        self._state = CallState.IDLE
        self._listeners: list[Callable] = []

    @property
    def state(self) -> CallState:
        return self._state

    async def transition(self, new_state: CallState):
        if new_state not in self.TRANSITIONS.get(self._state, set()):
            raise InvalidStateTransition(self._state, new_state)
        old = self._state
        self._state = new_state
        for fn in self._listeners:
            await fn(old, new_state)
```

#### CallService

```python
# voice/service.py
class CallService:
    """High-level voice call operations."""

    def __init__(self, executor: ATCommandExecutor, audio: AudioPipeline, bus: EventBus):
        self._at = executor
        self._audio = audio
        self._bus = bus
        self._fsm = CallStateMachine()
        self._active_call: Optional[CallSession] = None

        # Wire URC events to state machine
        bus.on(RingEvent)(self._on_ring)
        bus.on(CallStateEvent)(self._on_call_state)

    # -- Outbound --

    async def dial(self, number: str) -> CallSession:
        """Initiate an outbound call. Returns a CallSession handle."""
        await self._fsm.transition(CallState.DIALING)
        resp = await self._at.execute(f"ATD{number};", expect=["OK"], timeout=30)
        if not resp.success:
            await self._fsm.transition(CallState.ENDED)
            raise DialError(resp.lines)
        session = CallSession(number=number, direction="outbound", service=self)
        self._active_call = session
        return session

    # -- Inbound --

    async def answer(self) -> CallSession:
        """Answer an incoming call."""
        resp = await self._at.execute("ATA", expect=["OK", "VOICE CALL: BEGIN"])
        if not resp.success:
            raise AnswerError(resp.lines)
        await self._fsm.transition(CallState.ACTIVE)
        session = CallSession(
            number=self._pending_caller or "unknown",
            direction="inbound",
            service=self,
        )
        self._active_call = session
        return session

    async def hangup(self):
        """End the current call."""
        await self._at.execute("ATH", expect=["OK", "VOICE CALL: END"])
        await self._fsm.transition(CallState.ENDED)
        await self._cleanup()

    # -- Audio bridge --

    async def _enable_audio(self):
        """Register PCM audio channel after call connects."""
        await self._at.execute("AT+CPCMREG=1", expect=["OK"])
        await self._audio.start()

    async def _disable_audio(self):
        await self._audio.stop()
        await self._at.execute("AT+CPCMREG=0", expect=["OK", "ERROR"])

    # -- Internal event handlers --

    async def _on_ring(self, event: RingEvent):
        if self._fsm.state == CallState.IDLE:
            await self._fsm.transition(CallState.RINGING)

    async def _on_call_state(self, event: CallStateEvent):
        if event.state == CallState.ACTIVE:
            await self._enable_audio()
        elif event.state == CallState.ENDED:
            await self._disable_audio()
            await self._fsm.transition(CallState.ENDED)


@dataclass
class CallSession:
    """Handle for an active call. Provides audio and DTMF methods."""
    number: str
    direction: str  # "inbound" | "outbound"
    service: CallService

    async def hangup(self):
        await self.service.hangup()

    async def play(self, audio_path: str):
        """Play a WAV recording to the caller."""
        await self.service._audio.play_file(audio_path)

    async def play_sequence(self, paths: list[str]):
        """Play multiple WAV files back-to-back."""
        await self.service._audio.play_sequence(paths)

    async def play_loop(self, audio_path: str, cancel: asyncio.Event = None):
        """Loop a WAV file (e.g. hold music) until cancelled."""
        await self.service._audio.play_loop(audio_path, cancel)

    async def record(self, output_path: str, max_duration: float = 60.0,
                     stop_on_dtmf: bool = False) -> str:
        """Record caller audio to a WAV file."""
        return await self.service._audio.record(
            output_path, max_duration, stop_on_dtmf
        )

    async def collect_dtmf(
        self,
        max_digits: int = 1,
        timeout: float = 10.0,
        terminator: str = "#",
    ) -> str:
        collector = DTMFCollector(self.service._bus, max_digits, timeout, terminator)
        return await collector.collect()

    async def play_and_collect(
        self,
        audio_path: str,
        max_digits: int = 1,
        timeout: float = 10.0,
        terminator: str = "#",
        interrupt: bool = True,
    ) -> str:
        """Play a prompt and collect DTMF input (IVR pattern)."""
        collector = DTMFCollector(self.service._bus, max_digits, timeout, terminator)

        if interrupt:
            # Start collection immediately; stop audio on first digit
            play_task = asyncio.create_task(self.play(audio_path))
            first = await collector.collect_one(timeout=timeout)
            if first:
                play_task.cancel()
                remaining = await collector.collect(
                    max_digits=max_digits - 1, timeout=timeout
                )
                return first + remaining
            return ""
        else:
            await self.play(audio_path)
            return await collector.collect()
```

#### AudioPipeline

```python
# voice/audio.py
class AudioPipeline:
    """Manages PCM audio streaming over the modem's audio serial port.

    Audio format: 8000 Hz, 16-bit signed LE, mono (standard GSM).
    """

    SAMPLE_RATE = 8000
    SAMPLE_WIDTH = 2  # bytes (16-bit)
    CHANNELS = 1
    CHUNK_FRAMES = 320  # 40ms at 8kHz -- good balance of latency/throughput

    def __init__(self, transport: Transport, bus: EventBus):
        self._transport = transport
        self._bus = bus
        self._player = AudioPlayer(transport)
        self._running = False
        self._play_task: Optional[asyncio.Task] = None
        self._record_task: Optional[asyncio.Task] = None

    async def start(self):
        await self._transport.open()
        self._running = True

    async def stop(self):
        self._running = False
        if self._play_task:
            self._play_task.cancel()
        if self._record_task:
            self._record_task.cancel()
        await self._transport.close()

    async def play_file(self, path: str, cancel: asyncio.Event = None):
        """Stream a WAV recording to the modem audio port."""
        await self._player.play(path, cancel)

    async def play_sequence(self, paths: list[str], cancel: asyncio.Event = None):
        """Play multiple WAV files back-to-back."""
        await self._player.play_sequence(paths, cancel)

    async def play_loop(self, path: str, cancel: asyncio.Event = None):
        """Loop a WAV file (e.g. hold music) until cancelled."""
        await self._player.play_loop(path, cancel)

    async def record(
        self, output_path: str, max_duration: float = 60.0,
        stop_on_dtmf: bool = False,
    ) -> str:
        """Record incoming audio to a WAV file."""
        stop = asyncio.Event()
        if stop_on_dtmf:
            self._bus.on(DTMFEvent)(lambda _: stop.set())

        with wave.open(output_path, "wb") as wf:
            wf.setnchannels(self.CHANNELS)
            wf.setsampwidth(self.SAMPLE_WIDTH)
            wf.setframerate(self.SAMPLE_RATE)

            deadline = asyncio.get_event_loop().time() + max_duration
            while self._running and not stop.is_set():
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    data = await asyncio.wait_for(
                        self._transport.read(640), timeout=min(0.1, remaining)
                    )
                    wf.writeframes(data)
                except asyncio.TimeoutError:
                    continue

        return output_path
```

#### DTMFCollector

```python
# voice/dtmf.py
class DTMFCollector:
    """Collects DTMF digits with configurable terminator and timeout."""

    def __init__(self, bus: EventBus, max_digits: int = 10,
                 timeout: float = 10.0, terminator: str = "#"):
        self._bus = bus
        self.max_digits = max_digits
        self.timeout = timeout
        self.terminator = terminator

    async def collect(self, max_digits: int = None, timeout: float = None) -> str:
        max_d = max_digits or self.max_digits
        tout = timeout or self.timeout
        digits = []

        async with self._bus.stream(DTMFEvent) as events:
            deadline = asyncio.get_event_loop().time() + tout
            while len(digits) < max_d:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                event = await events.next(timeout=remaining)
                if event is None:
                    break  # timeout
                if event.digit == self.terminator:
                    break
                digits.append(event.digit)

        return "".join(digits)

    async def collect_one(self, timeout: float = None) -> Optional[str]:
        result = await self.collect(max_digits=1, timeout=timeout or self.timeout)
        return result if result else None
```

#### AudioPlayer

Audio playback is done exclusively through pre-recorded WAV files streamed over the modem's PCM serial port -- the same approach used in RaspberryPBX. There is no synthesized tone generation; all prompts, tones, hold music, and DTMF feedback are WAV recordings stored on disk.

```python
# voice/player.py
class AudioPlayer:
    """Loads, validates, and streams WAV files over the audio transport.

    All audio MUST be: 8000 Hz, 16-bit signed LE, mono.
    Files that don't match are rejected at load time (not silently resampled).

    Supports:
    - Single file playback (blocking until complete or cancelled)
    - Queued sequential playback (play a list of files back-to-back)
    - Looped playback (e.g. hold music)
    - Interruptible playback (cancel on DTMF or external signal)
    """

    REQUIRED_RATE = 8000
    REQUIRED_CHANNELS = 1
    REQUIRED_SAMPLE_WIDTH = 2  # 16-bit
    CHUNK_FRAMES = 320  # 40ms chunks

    def __init__(self, transport: Transport):
        self._transport = transport

    def validate(self, path: str):
        """Check WAV file matches modem audio format. Raises on mismatch."""
        with wave.open(path, "rb") as wf:
            if wf.getframerate() != self.REQUIRED_RATE:
                raise AudioFormatError(
                    f"{path}: sample rate {wf.getframerate()}, need {self.REQUIRED_RATE}"
                )
            if wf.getnchannels() != self.REQUIRED_CHANNELS:
                raise AudioFormatError(
                    f"{path}: {wf.getnchannels()} channels, need {self.REQUIRED_CHANNELS}"
                )
            if wf.getsampwidth() != self.REQUIRED_SAMPLE_WIDTH:
                raise AudioFormatError(
                    f"{path}: sample width {wf.getsampwidth()}, need {self.REQUIRED_SAMPLE_WIDTH}"
                )

    async def play(self, path: str, cancel: asyncio.Event = None):
        """Stream a WAV file to the audio transport in real-time.

        Blocks until the file finishes or cancel is set.
        """
        self.validate(path)
        with wave.open(path, "rb") as wf:
            while True:
                if cancel and cancel.is_set():
                    break
                data = wf.readframes(self.CHUNK_FRAMES)
                if not data:
                    break
                await self._transport.write(data)
                await asyncio.sleep(self.CHUNK_FRAMES / self.REQUIRED_RATE)

    async def play_sequence(self, paths: list[str], cancel: asyncio.Event = None):
        """Play multiple WAV files back-to-back."""
        for path in paths:
            if cancel and cancel.is_set():
                break
            await self.play(path, cancel)

    async def play_loop(self, path: str, cancel: asyncio.Event = None):
        """Loop a WAV file until cancel is set (e.g. hold music)."""
        self.validate(path)
        while not (cancel and cancel.is_set()):
            await self.play(path, cancel)
```

**Audio file conventions:**

All audio assets live in an `audio/` directory and must conform to the modem's PCM format:
- **Format:** WAV, 8000 Hz, 16-bit signed LE, mono
- **Naming:** descriptive, e.g. `greeting.wav`, `menu_invalid.wav`, `hold_music.wav`, `dtmf_1.wav`
- **Conversion:** Use `ffmpeg -i input.mp3 -ar 8000 -ac 1 -sample_fmt s16 -acodec pcm_s16le output.wav`

For playing DTMF-style feedback tones, pre-record short WAV files for each digit (`dtmf_0.wav` through `dtmf_9.wav`, `dtmf_star.wav`, `dtmf_hash.wav`) rather than synthesizing them at runtime.

#### IVR Menu Builder

```python
# voice/ivr.py
@dataclass
class MenuOption:
    digit: str
    description: str
    handler: Callable[["CallSession"], Awaitable[None]]

class IVRMenu:
    """Declarative IVR menu builder.

    Usage:
        menu = IVRMenu(prompt="audio/welcome.wav")
        menu.option("1", "Sales", handle_sales)
        menu.option("2", "Support", handle_support)
        menu.option("0", "Operator", handle_operator)
        await menu.run(session, retries=3, invalid_prompt="audio/invalid.wav")
    """

    def __init__(self, prompt: str, timeout: float = 10.0):
        self.prompt = prompt
        self.timeout = timeout
        self._options: dict[str, MenuOption] = {}

    def option(self, digit: str, description: str,
               handler: Callable[["CallSession"], Awaitable[None]]):
        self._options[digit] = MenuOption(digit, description, handler)

    async def run(self, session: CallSession, retries: int = 3,
                  invalid_prompt: str = None):
        for attempt in range(retries):
            choice = await session.play_and_collect(
                self.prompt, max_digits=1, timeout=self.timeout, interrupt=True
            )
            if choice in self._options:
                await self._options[choice].handler(session)
                return
            if invalid_prompt and attempt < retries - 1:
                await session.play(invalid_prompt)
        # Exhausted retries -- hang up or fall through
```

---

### 5. SMS Service

```python
# sms/types.py
@dataclass
class SMS:
    id: Optional[int] = None
    sender: str = ""
    recipient: str = ""
    body: str = ""
    timestamp: Optional[datetime] = None
    status: str = ""  # "unread", "read", "sent", "failed"
    reference: int = 0

@dataclass
class DeliveryReport:
    reference: int = 0
    recipient: str = ""
    status: str = ""  # "delivered", "failed", "pending"
    timestamp: Optional[datetime] = None
```

```python
# sms/service.py
class SMSService:
    """Full SMS capabilities: send, receive, subscribe, manage."""

    def __init__(self, executor: ATCommandExecutor, bus: EventBus,
                 store: Optional[SMSStore] = None):
        self._at = executor
        self._bus = bus
        self._store = store or SMSStore()
        self._initialized = False

        # Wire incoming SMS events
        bus.on(IncomingSMSEvent)(self._on_incoming)

    async def initialize(self):
        """Configure modem for SMS operations."""
        # Set text mode
        await self._at.execute("AT+CMGF=1")
        # Enable caller ID in SMS
        await self._at.execute("AT+CSCS=\"GSM\"")
        # Enable unsolicited SMS notifications
        # Mode 2: route directly to TE; Mode 1: notification only
        await self._at.execute("AT+CNMI=2,2,0,1,0")
        # Enable delivery reports
        await self._at.execute("AT+CSMP=49,167,0,0")
        self._initialized = True

    # -- Sending --

    async def send(self, to: str, body: str) -> SMS:
        """Send an SMS message. Returns SMS with reference number."""
        resp = await self._at.execute(f'AT+CMGS="{to}"', expect=[">"], timeout=10)
        if not resp.success:
            raise SMSSendError(f"Failed to initiate SMS: {resp.lines}")

        # Send message body + Ctrl+Z
        await self._at.execute(
            f"{body}\x1A",
            expect=["+CMGS:", "OK"],
            timeout=30,
        )
        sms = SMS(recipient=to, body=body, status="sent")
        await self._store.save(sms)
        await self._bus.emit(SMSSentEvent(recipient=to))
        return sms

    # -- Receiving --

    async def _on_incoming(self, event: IncomingSMSEvent):
        """Handle incoming SMS from URC."""
        sms = self._parse_incoming(event.raw)
        await self._store.save(sms)
        await self._bus.emit(sms)  # Re-emit as full SMS object

    # -- Subscription API --

    def on_message(self, handler: Callable[[SMS], Awaitable[None]],
                   filter_sender: str = None):
        """Subscribe to incoming messages with optional sender filter."""
        async def filtered_handler(sms: SMS):
            if filter_sender and sms.sender != filter_sender:
                return
            await handler(sms)
        self._bus.on(SMS)(filtered_handler)

    @asynccontextmanager
    async def messages(self, filter_sender: str = None):
        """Async iterator for incoming SMS messages.

        Usage:
            async with sms_service.messages() as inbox:
                async for msg in inbox:
                    print(f"From {msg.sender}: {msg.body}")
        """
        async with self._bus.stream(SMS) as stream:
            yield FilteredStream(stream, filter_sender)

    # -- Message Management --

    async def list_messages(self, status: str = "ALL") -> list[SMS]:
        """List messages stored on SIM. Status: ALL, REC UNREAD, REC READ, etc."""
        resp = await self._at.execute(f'AT+CMGL="{status}"', expect=["OK"], timeout=10)
        return self._parse_message_list(resp.lines)

    async def read_message(self, index: int) -> Optional[SMS]:
        resp = await self._at.execute(f"AT+CMGR={index}", expect=["OK"])
        return self._parse_single_message(resp.lines) if resp.success else None

    async def delete_message(self, index: int) -> bool:
        resp = await self._at.execute(f"AT+CMGD={index}", expect=["OK"])
        return resp.success

    async def delete_all(self) -> bool:
        resp = await self._at.execute("AT+CMGD=1,4", expect=["OK"])
        return resp.success
```

```python
# sms/store.py
class SMSStore:
    """In-memory SMS store with optional SQLite persistence."""

    def __init__(self, db_path: str = None):
        self._messages: list[SMS] = []
        self._db_path = db_path
        # If db_path, initialize SQLite table on first save

    async def save(self, sms: SMS): ...
    async def get(self, id: int) -> Optional[SMS]: ...
    async def list(self, **filters) -> list[SMS]: ...
    async def delete(self, id: int) -> bool: ...
```

---

### 6. Top-Level Modem Orchestrator

```python
# modem.py
class Modem:
    """Top-level entry point. Manages all subsystems.

    Usage:
        async with Modem(ModemConfig()) as modem:
            # Subscribe to incoming calls
            @modem.on_call
            async def handle_call(session: CallSession):
                await session.answer()
                dtmf = await session.play_and_collect("welcome.wav", max_digits=1)
                ...

            # Send SMS
            await modem.sms.send("+1234567890", "Hello from Callstack!")

            # Subscribe to incoming SMS
            async with modem.sms.messages() as inbox:
                async for msg in inbox:
                    print(msg)

            # Keep running
            await modem.run_forever()
    """

    def __init__(self, config: ModemConfig = None):
        self.config = config or ModemConfig()
        self.bus = EventBus()

        # Transport
        self._at_transport = SerialTransport(self.config.at_port, self.config.baudrate)
        self._audio_transport = SerialTransport(self.config.audio_port, self.config.baudrate)

        # Protocol
        self._urc = URCDispatcher(self.bus)
        self._executor = ATCommandExecutor(self._at_transport, self._urc)

        # Services
        self._audio = AudioPipeline(self._audio_transport, self.bus)
        self.call = CallService(self._executor, self._audio, self.bus)
        self.sms = SMSService(self._executor, self.bus)
        self.network = NetworkService(self._executor, self.bus)

    async def __aenter__(self):
        await self._at_transport.open()
        await self._initialize_modem()
        await self.sms.initialize()
        # Start URC reader loop
        self._urc_task = asyncio.create_task(self._urc_reader())
        return self

    async def __aexit__(self, *exc):
        self._urc_task.cancel()
        await self._audio_transport.close()
        await self._at_transport.close()

    async def _initialize_modem(self):
        """Send initialization AT commands."""
        await self._executor.execute("ATE0")         # Disable echo
        await self._executor.execute("AT+CLIP=1")    # Enable caller ID
        await self._executor.execute("AT+CVHU=0")    # Disconnect control
        await self._executor.execute("AT+COLP=1")    # Connected line ID

    async def _urc_reader(self):
        """Continuously read lines and dispatch URCs when no command is active."""
        while True:
            try:
                raw = await self._at_transport.readline()
                line = raw.decode("ascii", errors="replace").strip()
                if line and self._urc.is_urc(line):
                    await self._urc.dispatch(line)
            except asyncio.CancelledError:
                break
            except Exception as e:
                await self.bus.emit(ModemDisconnectedEvent(reason=str(e)))

    def on_call(self, handler: Callable[[CallSession], Awaitable[None]]):
        """Decorator: register handler for incoming calls.

        The handler receives a CallSession after the call is answered.
        """
        async def wrapper(event: RingEvent):
            session = await self.call.answer()
            await handler(session)
        self.bus.on(RingEvent)(wrapper)

    async def run_forever(self):
        """Block until cancelled."""
        await asyncio.Event().wait()
```

```python
# config.py
@dataclass
class ModemConfig:
    at_port: str = "/dev/ttyUSB2"
    audio_port: str = "/dev/ttyUSB4"
    baudrate: int = 115200
    command_timeout: float = 5.0
    auto_reconnect: bool = True
    reconnect_interval: float = 5.0
    sms_storage: str = "SM"         # SIM card storage
    sms_db_path: Optional[str] = None  # SQLite path for SMS persistence
    log_level: str = "INFO"
```

---

## Usage Examples

### Basic Incoming Call Handler

```python
import asyncio
from callstack import Modem, ModemConfig

async def main():
    async with Modem(ModemConfig()) as modem:

        @modem.on_call
        async def on_call(session):
            await session.play("audio/greeting.wav")
            choice = await session.collect_dtmf(max_digits=1, timeout=10)

            if choice == "1":
                await session.play("audio/sales.wav")
            elif choice == "2":
                await session.play("audio/support.wav")
            else:
                await session.play("audio/goodbye.wav")

            await session.hangup()

        await modem.run_forever()

asyncio.run(main())
```

### Outbound Call with Recording

```python
async with Modem() as modem:
    session = await modem.call.dial("+1234567890")
    await session.play("audio/voicemail_prompt.wav")
    recording = await session.record("recordings/msg.wav", max_duration=30, stop_on_dtmf=True)
    await session.play("audio/thank_you.wav")
    await session.hangup()
```

### IVR Menu

```python
from callstack.voice.ivr import IVRMenu

menu = IVRMenu(prompt="audio/main_menu.wav")
menu.option("1", "Check balance", check_balance_handler)
menu.option("2", "Make payment", make_payment_handler)
menu.option("0", "Speak to agent", transfer_handler)

@modem.on_call
async def on_call(session):
    await menu.run(session, retries=3, invalid_prompt="audio/invalid.wav")
```

### SMS Send and Subscribe

```python
async with Modem() as modem:
    # Send
    await modem.sms.send("+1234567890", "Hello from Callstack!")

    # Subscribe with callback
    @modem.sms.on_message
    async def on_sms(msg):
        print(f"From {msg.sender}: {msg.body}")
        # Auto-reply
        await modem.sms.send(msg.sender, f"Got your message: {msg.body[:50]}")

    # Or use async iterator
    async with modem.sms.messages() as inbox:
        async for msg in inbox:
            print(msg)
```

### Raw AT Command Access

```python
async with Modem() as modem:
    resp = await modem._executor.execute("AT+CSQ")  # Signal quality
    print(resp.lines)
```

---

## Key Improvements Over RaspberryPBX

| Aspect | RaspberryPBX | Callstack |
|--------|-------------|-----------|
| Concurrency | `threading` + locks | `asyncio` -- single event loop, no lock contention |
| State management | Implicit, flag-based | Explicit FSM with guarded transitions |
| Events | Single callback function | Typed event bus with pub/sub + async streams |
| SMS | Stub only | Full service: send, receive, subscribe, persist |
| Audio | Raw serial write loop | Paced pipeline with WAV playback, sequencing, looping, recording |
| DTMF | Single digit + threading.Event | Buffered collector with timeout + terminator |
| IVR | Manual prompt-and-wait | Declarative menu builder |
| Error handling | Check for "ERROR" string | Typed exceptions, retry decorator, auto-reconnect |
| Testing | Not possible (hardcoded serial) | MockTransport, fully testable without hardware |
| Configuration | Hardcoded paths | Dataclass config with sensible defaults |
| API style | Procedural, imperative | Context managers, decorators, async generators |
| Cleanup | Manual `.close()` | `async with` context manager |

---

## Dependencies

```
python >= 3.11
pyserial-asyncio >= 0.6
```

Optional:
```
aiosqlite    # SMS persistence
```

---

## File Count Summary

```
callstack/
  14 Python modules across 6 packages
  ~1200 lines of implementation code (estimated)
```

This is a focused, minimal framework -- no web server, no REST API, no ORM. It does one thing well: manage a GSM modem for voice and SMS on a Raspberry Pi.

---

## Implementation Phases

### Phase 1 -- Foundation (Transport + Protocol + Events)

**Goal:** Establish the core plumbing. After this phase, you can send AT commands and see modem responses.

**Modules to build:**

| Track | Modules | Depends On |
|-------|---------|------------|
| **A: Transport** | `transport/base.py`, `transport/serial.py`, `transport/mock.py` | nothing |
| **B: Events** | `events/types.py`, `events/bus.py` | nothing |
| **C: Config + Errors** | `config.py`, `errors.py` | nothing |

Track A, B, and C are **fully independent** -- build all three in parallel.

Then, sequentially:

| Module | Depends On |
|--------|------------|
| `protocol/parser.py` | A |
| `protocol/urc.py` | A, B |
| `protocol/commands.py` | (standalone constants) |
| `protocol/executor.py` | A, B, `parser`, `urc` |

**Milestone test:** Open serial transport, send `AT`, receive `OK`, parse response. Send `AT+CSQ`, parse signal quality. Verify URCs dispatch to EventBus.

```
Week 1 timeline:
  Day 1-2: Tracks A + B + C in parallel
  Day 3-4: protocol/parser.py, protocol/commands.py, protocol/urc.py
  Day 5:   protocol/executor.py + integration test with real modem
```

---

### Phase 2 -- Voice Core (Call State + Audio Playback + Recording)

**Goal:** Answer an incoming call, play a WAV recording, record caller audio, hang up.

**Modules to build:**

| Track | Modules | Depends On |
|-------|---------|------------|
| **D: Audio** | `voice/player.py`, `voice/audio.py` | Phase 1 (Transport) |
| **E: Call FSM** | `voice/state.py` | Phase 1 (Events) |

Track D and E are **independent** -- build in parallel.

Then, sequentially:

| Module | Depends On |
|--------|------------|
| `voice/service.py` (CallService + CallSession) | D, E, Phase 1 (Executor, URC, EventBus) |

**Milestone test:** Modem rings -> answer call -> `AT+CPCMREG=1` -> stream `greeting.wav` to caller -> record 10s of audio -> hang up. Verify state machine transitions: `IDLE -> RINGING -> ACTIVE -> ENDED -> IDLE`.

```
Week 2 timeline:
  Day 1-2: Track D (player.py + audio.py) and Track E (state.py) in parallel
  Day 3-4: voice/service.py (CallService wiring)
  Day 5:   End-to-end test: inbound call -> play -> record -> hangup
```

---

### Phase 3 -- DTMF + IVR

**Goal:** Collect DTMF input during calls. Build the IVR menu pattern.

**Modules to build:**

| Track | Modules | Depends On |
|-------|---------|------------|
| **F: DTMF** | `voice/dtmf.py` | Phase 1 (EventBus, `DTMFEvent`) |
| **G: IVR** | `voice/ivr.py` | F, Phase 2 (CallSession) |

F can start as soon as Phase 1 is done (it only needs EventBus). G depends on both F and Phase 2.

**Milestone test:** Call in -> play menu prompt -> press "1" -> DTMF detected and routed -> play corresponding audio -> hang up. Test `play_and_collect` with interrupt (audio stops on first keypress).

```
Week 3 timeline:
  Day 1-2: voice/dtmf.py (DTMFCollector)
  Day 3-4: voice/ivr.py (IVRMenu)
  Day 5:   End-to-end IVR test with real phone call
```

---

### Phase 4 -- SMS

**Goal:** Full SMS send/receive/subscribe/persist.

**Modules to build:**

| Track | Modules | Depends On |
|-------|---------|------------|
| **H: SMS types** | `sms/types.py` | nothing |
| **I: SMS store** | `sms/store.py` | H |
| **J: SMS service** | `sms/service.py` | H, I, Phase 1 (Executor, EventBus, URC) |
| **K: PDU** | `sms/pdu.py` | H (optional, for PDU mode) |

H is standalone. I depends on H. J depends on Phase 1 + H + I. K is optional/parallel.

SMS is **fully independent of voice** -- Phase 4 can run in parallel with Phases 2-3 if you have two developers.

**Milestone test:** Send SMS from Pi -> received on phone. Send SMS from phone -> URC dispatched -> `on_message` callback fires -> stored in SMSStore. Test `async with modem.sms.messages()` iterator.

```
Week 3 timeline (parallel with Phase 3 if 2 devs):
  Day 1:   sms/types.py
  Day 2:   sms/store.py
  Day 3-4: sms/service.py
  Day 5:   Integration test: send + receive + subscribe
```

---

### Phase 5 -- Orchestrator + Polish

**Goal:** Wire everything into the top-level `Modem` class. Add utilities. Finalize the public API.

**Modules to build:**

| Module | Depends On |
|--------|------------|
| `modem.py` | All of Phases 1-4 |
| `utils/logger.py` | nothing (can be built anytime) |
| `utils/retry.py` | Phase 1 (Executor) |
| `__init__.py` | `modem.py` (re-exports) |

**Tasks:**
1. Wire `Modem.__aenter__` / `__aexit__` -- init sequence, URC reader task, cleanup
2. Implement `modem.on_call` decorator
3. Implement `modem.run_forever()`
4. Add auto-reconnect logic (detect USB disconnect, re-open transport)
5. Add retry decorator for flaky AT commands
6. Structured logging across all components
7. Write `__init__.py` with clean public API exports

**Milestone test:** Full end-to-end script:
```python
async with Modem() as modem:
    @modem.on_call
    async def on_call(session):
        choice = await session.play_and_collect("menu.wav", max_digits=1)
        if choice == "1":
            await session.play("info.wav")
        await session.hangup()

    modem.sms.on_message(lambda msg: print(msg))
    await modem.sms.send("+1234567890", "System online")
    await modem.run_forever()
```

```
Week 4 timeline:
  Day 1-2: modem.py orchestrator wiring
  Day 3:   utils (logger, retry), __init__.py
  Day 4:   Auto-reconnect, edge case handling
  Day 5:   Full integration test, cleanup
```

---

### Parallelization Summary

```
         Week 1              Week 2              Week 3              Week 4
    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
    │ A: Transport │    │ D: Audio     │    │ F: DTMF      │    │              │
    │ B: Events    │──>│ E: Call FSM  │──>│ G: IVR        │──>│  Modem.py    │
    │ C: Config    │    │              │    │               │    │  Utils       │
    │              │    │ CallService  │    │               │    │  __init__    │
    │ Protocol/*   │    │              │    │ H: SMS types  │    │  Reconnect   │
    └──────────────┘    └──────────────┘    │ I: SMS store  │    │  Polish      │
                                            │ J: SMS svc    │    └──────────────┘
                                            └──────────────┘
```

**Two-developer split:**

| Developer | Week 1 | Week 2 | Week 3 | Week 4 |
|-----------|--------|--------|--------|--------|
| **Dev 1** | Transport + Protocol | Audio + CallService | DTMF + IVR | Modem orchestrator |
| **Dev 2** | Events + Config + Errors | Call FSM + MockTransport tests | SMS (all) | Utils + integration tests |

**Solo developer:** Follow weeks 1-4 linearly. SMS (Phase 4) can be deferred to after Phase 5 if you want voice working first -- it's fully decoupled.

---

### Phase 0 (Pre-work, before coding)

Before writing any code:

1. **Prepare audio assets** -- Record or source WAV files for testing: a greeting, a menu prompt, an invalid-input prompt, hold music. Convert all to 8000 Hz / 16-bit / mono.
2. **Verify hardware** -- Confirm modem device paths (`/dev/ttyUSB2`, `/dev/ttyUSB4`), test basic AT commands manually with `minicom` or `screen`.
3. **Set up project** -- `pyproject.toml`, virtual env, `pyserial-asyncio` installed, test that `serial_asyncio.open_serial_connection()` works with the modem.
4. **Port the working test** -- Take the simplest working example from RaspberryPBX (`ancient/workingExample.py`) and rewrite it as a standalone async script. This validates the async serial approach before building the framework around it.
