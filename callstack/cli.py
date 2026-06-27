"""Minimal command line interface for Callstack."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from dataclasses import asdict, fields
from typing import Any

from callstack.config import ConfigError, ModemConfig, load_modem_config_from_env
from callstack.events.serialize import format_event_human, serialize_event
from callstack.events.types import (
    CallerIDEvent,
    CallStateEvent,
    DTMFEvent,
    Event,
    IncomingSMSEvent,
    ModemDisconnectedEvent,
    ModemReconnectedEvent,
    RingEvent,
    SMSDeliveryReportEvent,
    SMSSentEvent,
    SignalQualityEvent,
    USSDResponseEvent,
)
from callstack.hardware.discovery import ModemDiscoveryReport
from callstack.hardware.probe import probe_modem_ports
from callstack.modem import Modem


_MONITOR_QUEUE_MAXSIZE = 100
_MONITOR_EVENT_TYPES: dict[str, tuple[type[Event], ...]] = {
    "sms.received": (IncomingSMSEvent,),
    "sms.sent": (SMSSentEvent,),
    "sms.delivery_report": (SMSDeliveryReportEvent,),
    "call.state": (CallStateEvent,),
    "call.ring": (RingEvent,),
    "call.caller_id": (CallerIDEvent,),
    "call.dtmf": (DTMFEvent,),
    "modem.state": (ModemDisconnectedEvent, ModemReconnectedEvent),
    "signal.quality": (SignalQualityEvent,),
    "ussd.response": (USSDResponseEvent,),
}


class _MonitorOverflowNotice:
    pass


_MONITOR_OVERFLOW_NOTICE = _MonitorOverflowNotice()


def _add_config_args(
    parser: argparse.ArgumentParser,
    defaults: ModemConfig,
    *,
    default: object = None,
    sim_pin_env_default: object = None,
) -> None:
    parser.add_argument(
        "--at-port",
        default=defaults.at_port if default is None else default,
        help="AT command serial port",
    )
    parser.add_argument(
        "--audio-port",
        default=defaults.audio_port if default is None else default,
        help="Audio serial port",
    )
    parser.add_argument(
        "--baudrate",
        type=int,
        default=defaults.baudrate if default is None else default,
        help="Serial baudrate",
    )
    parser.add_argument(
        "--sms-db-path",
        default=defaults.sms_db_path if default is None else default,
        help="Optional SMS SQLite database path",
    )
    parser.add_argument(
        "--sim-pin-env",
        default=sim_pin_env_default if default is None else default,
        help="Environment variable containing the SIM PIN",
    )
    parser.add_argument(
        "--log-level",
        default=defaults.log_level if default is None else default,
        help="Logging level (default: WARNING to avoid leaking private modem/SMS details)",
    )


def _build_parser() -> argparse.ArgumentParser:
    defaults = ModemConfig()
    parser = argparse.ArgumentParser(prog="callstack")
    _add_config_args(parser, defaults, default=argparse.SUPPRESS)

    subcommand_config = argparse.ArgumentParser(add_help=False)
    _add_config_args(subcommand_config, defaults, default=argparse.SUPPRESS)

    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser(
        "status",
        parents=[subcommand_config],
        help="Show modem/network status",
    )
    status_parser.add_argument("--json", action="store_true", dest="as_json", help="Emit machine-readable JSON")

    send_parser = subparsers.add_parser(
        "send",
        parents=[subcommand_config],
        help="Send an SMS message",
    )
    send_parser.add_argument("--to", required=True, help="Destination phone number")
    send_parser.add_argument("--body", required=True, help="SMS message body")

    doctor_parser = subparsers.add_parser(
        "doctor",
        parents=[subcommand_config],
        help="Safely probe modem ports with non-mutating identity commands",
    )
    doctor_parser.add_argument(
        "--ports",
        default=None,
        help=(
            "Comma-separated candidate AT serial ports to probe; "
            "defaults to the configured --at-port and performs no automatic hardware scan"
        ),
    )
    doctor_parser.add_argument("--json", action="store_true", dest="as_json", help="Emit machine-readable JSON")

    monitor_parser = subparsers.add_parser(
        "monitor",
        parents=[subcommand_config],
        help="PII-safe live event tailing",
    )
    monitor_parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit one sanitized JSON object per event",
    )
    monitor_parser.add_argument(
        "--events",
        type=_parse_monitor_events,
        default=None,
        help=(
            "Comma-separated event names to include "
            f"(default: {', '.join(_MONITOR_EVENT_TYPES)})"
        ),
    )
    monitor_parser.add_argument(
        "--once",
        type=_parse_once,
        default=None,
        metavar="N",
        help="Exit after printing N selected events (primarily useful for tests)",
    )

    return parser


def _config_from_args(args: argparse.Namespace) -> ModemConfig:
    env = dict(os.environ)
    if hasattr(args, "at_port"):
        env["CALLSTACK_AT_PORT"] = args.at_port
    if hasattr(args, "audio_port"):
        env["CALLSTACK_AUDIO_PORT"] = args.audio_port
    if hasattr(args, "baudrate"):
        env["CALLSTACK_BAUDRATE"] = str(args.baudrate)
    if hasattr(args, "sms_db_path"):
        env["CALLSTACK_SMS_DB_PATH"] = args.sms_db_path
    if hasattr(args, "sim_pin_env"):
        env["CALLSTACK_SIM_PIN_ENV"] = args.sim_pin_env
    if hasattr(args, "log_level"):
        env["CALLSTACK_LOG_LEVEL"] = args.log_level
    elif "CALLSTACK_LOG_LEVEL" not in env:
        env["CALLSTACK_LOG_LEVEL"] = "WARNING"

    return load_modem_config_from_env(env)


def _status_payload(registration: Any, signal: Any, operator: str | None) -> dict[str, Any]:
    return {
        "connected": True,
        "registration": {
            "registered": registration.registered,
            "roaming": registration.roaming,
            "description": registration.description,
        },
        "signal": {
            "rssi": signal.rssi,
            "dbm": signal.dbm,
            "description": signal.description,
            "ber": signal.ber,
            "ber_description": signal.ber_description,
        },
        "operator": operator or "unknown",
    }


def _print_human_status(payload: dict[str, Any]) -> None:
    print("Modem: connected" if payload["connected"] else "Modem: disconnected")
    print(f"Registration: {payload['registration']['description']}")
    print(f"Operator: {payload['operator'] or 'unknown'}")

    signal = payload["signal"]
    if signal["description"] == "unknown" or signal["dbm"] is None:
        print("Signal: unknown")
    else:
        print(
            f"Signal: {signal['description']} "
            f"({signal['dbm']} dBm, RSSI {signal['rssi']}, BER {signal['ber_description']})"
        )


def _parse_ports(value: str) -> list[str]:
    return [port.strip() for port in value.split(",") if port.strip()]


def _parse_monitor_events(value: str) -> tuple[str, ...]:
    raw_events = value.split(",")
    if any(not event.strip() for event in raw_events):
        raise argparse.ArgumentTypeError(
            "--events must not contain empty event names; "
            f"known events: {', '.join(_MONITOR_EVENT_TYPES)}"
        )

    requested = tuple(event.strip() for event in raw_events)
    unknown = [event for event in requested if event not in _MONITOR_EVENT_TYPES]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown event(s): {', '.join(unknown)}; "
            f"known events: {', '.join(_MONITOR_EVENT_TYPES)}"
        )

    return tuple(dict.fromkeys(requested))


def _parse_once(value: str) -> int:
    try:
        count = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--once must be a positive integer") from exc
    if count < 1:
        raise argparse.ArgumentTypeError("--once must be a positive integer")
    return count


def _doctor_payload(report: ModemDiscoveryReport) -> dict[str, Any]:
    return {
        "at_port": report.at_port,
        "audio_port": report.audio_port,
        "identity": asdict(report.identity),
        "capabilities": asdict(report.capabilities),
        "confidence": report.confidence,
        "notes": list(report.notes),
    }


def _unknown(value: str | None) -> str:
    return value or "unknown"


def _print_human_doctor(report: ModemDiscoveryReport) -> None:
    identity = report.identity
    capabilities = report.capabilities

    print("Callstack doctor")
    print(f"AT port: {_unknown(report.at_port)} (confidence: {report.confidence})")
    print(f"Audio port: {_unknown(report.audio_port)}")
    print(f"Manufacturer: {_unknown(identity.manufacturer)}")
    print(f"Model: {_unknown(identity.model)}")
    print(f"Revision: {_unknown(identity.revision)}")
    print("Capabilities:")
    for capability in fields(capabilities):
        print(f"  {capability.name}: {getattr(capabilities, capability.name)}")
    print("Notes:")
    if report.notes:
        for note in report.notes:
            print(f"  - {note}")
    else:
        print("  - No additional notes.")
    print("Safety: no SMS, USSD, call, SIM unlock, or storage commands were sent.")


async def _status(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    async with Modem(config) as modem:
        registration, signal, operator = await asyncio.gather(
            modem.network.registration(),
            modem.network.signal_quality(),
            modem.network.operator(),
        )

    payload = _status_payload(registration, signal, operator)
    if args.as_json:
        print(json.dumps(payload))
    else:
        _print_human_status(payload)
    return 0


async def _send(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    async with Modem(config) as modem:
        sms = await modem.sms.send(args.to, args.body)

    reference = getattr(sms, "reference", None)
    if reference is None:
        print("SMS sent")
    else:
        print(f"SMS sent (ref: {reference})")
    return 0


async def _doctor(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    ports = _parse_ports(args.ports) if args.ports else [config.at_port]
    report = await probe_modem_ports(ports, baudrate=config.baudrate)
    if args.as_json:
        print(json.dumps(_doctor_payload(report)))
    else:
        _print_human_doctor(report)
    return 0


def _monitor_overflow_payload() -> dict[str, Any]:
    return {"type": "monitor.overflow", "data": {"message": "events dropped"}}


def _print_monitor_item(item: Event | _MonitorOverflowNotice, as_json: bool) -> None:
    if isinstance(item, _MonitorOverflowNotice):
        if as_json:
            print(json.dumps(_monitor_overflow_payload()), flush=True)
        else:
            print("monitor overflow: events dropped", flush=True)
        return

    if as_json:
        print(json.dumps(serialize_event(item)), flush=True)
    else:
        print(format_event_human(item), flush=True)


async def _monitor(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    selected_names = args.events or tuple(_MONITOR_EVENT_TYPES)
    queue: asyncio.Queue[Event | _MonitorOverflowNotice] = asyncio.Queue(
        maxsize=_MONITOR_QUEUE_MAXSIZE
    )

    def enqueue_event(event: Event) -> None:
        try:
            queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass

        if queue.maxsize <= 1 or (args.once is not None and queue.maxsize <= args.once):
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
            queue.put_nowait(event)
            return

        # Preserve an explicit sanitized overflow notice, but do not let it
        # replace the selected event that triggered backpressure.  Dropping old
        # items keeps memory bounded while ensuring finite --once runs can still
        # observe the requested number of real events.
        for _ in range(2):
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()

        try:
            queue.put_nowait(_MONITOR_OVERFLOW_NOTICE)
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    async with Modem(config) as modem:
        subscriptions: list[tuple[type[Event], Any]] = []
        try:
            for event_name in selected_names:
                for event_type in _MONITOR_EVENT_TYPES[event_name]:
                    modem.bus.subscribe(event_type, enqueue_event)
                    subscriptions.append((event_type, enqueue_event))

            printed = 0
            while args.once is None or printed < args.once:
                item = await queue.get()
                _print_monitor_item(item, args.as_json)
                if not isinstance(item, _MonitorOverflowNotice):
                    printed += 1
        finally:
            for event_type, handler in subscriptions:
                modem.bus.unsubscribe(event_type, handler)

    return 0


async def _run(args: argparse.Namespace) -> int:
    if args.command == "status":
        return await _status(args)
    if args.command == "send":
        return await _send(args)
    if args.command == "doctor":
        return await _doctor(args)
    if args.command == "monitor":
        return await _monitor(args)
    raise RuntimeError(f"unsupported command: {args.command}")


def _print_error(exc: BaseException) -> None:
    exc_name = type(exc).__name__
    detail = f": {exc}" if isinstance(exc, ConfigError) and str(exc) else ""
    print(
        f"Error: {exc_name}{detail}. Check modem connection, SIM state, and arguments, then retry.",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        parser = _build_parser()
    except ConfigError as exc:
        _print_error(exc)
        return 1
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt as exc:
        _print_error(exc)
        return 1
    except Exception as exc:
        _print_error(exc)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
