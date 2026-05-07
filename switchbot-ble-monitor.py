from __future__ import annotations

import asyncio
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import tkinter as tk
from tkinter import ttk, messagebox

try:
    from bleak import BleakScanner
    from bleak.backends.device import BLEDevice
    from bleak.backends.scanner import AdvertisementData
except ImportError:
    BleakScanner = None
    BLEDevice = Any  # type: ignore[assignment]
    AdvertisementData = Any  # type: ignore[assignment]


SWITCHBOT_COMPANY_IDS = (0x0969, 0x02E5, 0x0059)

SERVICE_UUIDS = (
    "0000fd3d-0000-1000-8000-00805f9b34fb",
    "0000000d-0000-1000-8000-00805f9b34fb",
    "00000d00-0000-1000-8000-00805f9b34fb",
)

METER_MODEL_IDS = {
    0x54,
    0x74,
    0x69,
    0x77,
    0x34,
    0x14,
    0x35,
    0x15,
}

CO2_MODEL_IDS = {0x35, 0x15}


@dataclass(frozen=True)
class SensorReading:
    key: str
    ble_address: str
    mac_address: str
    name: str
    temperature_c: Optional[float]
    humidity: Optional[int]
    co2_ppm: Optional[int]
    battery: Optional[int]
    rssi: Optional[int]
    seen_at: float


def _mac_text(bs: bytes) -> str:
    return ":".join(f"{b:02X}" for b in bs)


def _mac_sort_key(mac: str) -> str:
    return "".join(ch.upper() for ch in mac if ch.isalnum())


def _strip_service_uuid_prefix(data: bytes) -> bytes:
    if len(data) >= 2 and data[:2] in (
        b"\x00\x0d",
        b"\x0d\x00",
        b"\x3d\xfd",
        b"\xfd\x3d",
    ):
        return data[2:]
    return data


def _strip_company_prefix(company_id: int, data: bytes) -> bytes:
    prefix = company_id.to_bytes(2, "little", signed=False)
    if len(data) >= 2 and data[:2] == prefix:
        return data[2:]
    return data


def _decode_temp_humidity(
    temp_frac_byte: int,
    temp_int_sign_byte: int,
    humidity_byte: int,
) -> tuple[float, int]:
    sign = 1 if (temp_int_sign_byte & 0b1000_0000) else -1
    temp_c = sign * (
        (temp_int_sign_byte & 0b0111_1111)
        + (temp_frac_byte & 0b0000_1111) / 10.0
    )
    humidity = humidity_byte & 0b0111_1111
    return round(temp_c, 1), int(humidity)


def _get_service_payload(service_data: dict[str, bytes]) -> Optional[bytes]:
    normalized = {str(k).lower(): bytes(v) for k, v in service_data.items()}

    for uuid in SERVICE_UUIDS:
        if uuid in normalized:
            payload = _strip_service_uuid_prefix(normalized[uuid])
            if payload:
                return payload

    for uuid, value in normalized.items():
        if "fd3d" in uuid or "000d" in uuid or "0d00" in uuid:
            payload = _strip_service_uuid_prefix(value)
            if payload:
                return payload

    return None


def _get_switchbot_mfr_payload(
    manufacturer_data: dict[int, bytes],
) -> Optional[bytes]:
    for company_id in SWITCHBOT_COMPANY_IDS:
        if company_id in manufacturer_data:
            payload = _strip_company_prefix(
                company_id,
                bytes(manufacturer_data[company_id]),
            )
            if payload:
                return payload
    return None


def decode_switchbot_meter(
    *,
    ble_address: str,
    local_name: str,
    rssi: Optional[int],
    service_data: dict[str, bytes],
    manufacturer_data: dict[int, bytes],
) -> Optional[SensorReading]:
    service_payload = _get_service_payload(service_data)

    if not service_payload or len(service_payload) < 3:
        return None

    model_id = service_payload[0] & 0x7F

    if model_id not in METER_MODEL_IDS:
        return None

    battery = service_payload[2] & 0x7F
    mfr_payload = _get_switchbot_mfr_payload(manufacturer_data)

    mac_address = ""
    if mfr_payload and len(mfr_payload) >= 6:
        mac_address = _mac_text(mfr_payload[:6])

    temperature_c: Optional[float] = None
    humidity: Optional[int] = None
    co2_ppm: Optional[int] = None

    if mfr_payload and len(mfr_payload) >= 11:
        t, h = _decode_temp_humidity(
            mfr_payload[8],
            mfr_payload[9],
            mfr_payload[10],
        )
        if -60.0 <= t <= 100.0 and 0 <= h <= 100:
            temperature_c = t
            humidity = h

    if (temperature_c is None or humidity is None) and len(service_payload) >= 6:
        t, h = _decode_temp_humidity(
            service_payload[3],
            service_payload[4],
            service_payload[5],
        )
        if -60.0 <= t <= 100.0 and 0 <= h <= 100:
            temperature_c = t
            humidity = h

    if model_id in CO2_MODEL_IDS and mfr_payload and len(mfr_payload) >= 15:
        co2 = int.from_bytes(mfr_payload[13:15], byteorder="big", signed=False)
        if 0 <= co2 <= 9999:
            co2_ppm = co2

    if temperature_c is None and humidity is None and co2_ppm is None:
        return None

    key = mac_address or ble_address or local_name or f"unknown-{model_id:02X}"

    return SensorReading(
        key=key,
        ble_address=ble_address,
        mac_address=mac_address,
        name=local_name,
        temperature_c=temperature_c,
        humidity=humidity,
        co2_ppm=co2_ppm,
        battery=battery,
        rssi=rssi,
        seen_at=time.time(),
    )


class SwitchBotBleMonitorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("SwitchBot BLE Monitor")
        self.root.geometry("590x300")
        self.root.minsize(580, 280)
        self.root.resizable(False, False)

        self.q: queue.Queue[object] = queue.Queue()
        self.stop_event = threading.Event()
        self.scan_thread: Optional[threading.Thread] = None
        self.status_var = tk.StringVar(value="Idle")

        self._build_ui()
        self.root.after(100, self._poll_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=(8, 8, 8, 6))
        top.pack(fill="x")

        self.start_button = ttk.Button(
            top,
            text="START",
            command=self.start_scan,
        )
        self.start_button.pack(side="left")

        self.stop_button = ttk.Button(
            top,
            text="STOP",
            command=self.stop_scan,
            state="disabled",
        )
        self.stop_button.pack(side="left", padx=(8, 14))

        ttk.Label(top, textvariable=self.status_var).pack(side="left")

        table_frame = ttk.Frame(self.root, padding=(8, 0, 8, 8))
        table_frame.pack(fill="both", expand=True)

        cols = (
            "mac",
            "temperature",
            "humidity",
            "co2",
            "battery",
            "rssi",
            "time",
        )

        self.tree = ttk.Treeview(
            table_frame,
            columns=cols,
            show="headings",
            height=10,
        )

        headings = {
            "mac": "MAC",
            "temperature": "Temp C",
            "humidity": "Hum %",
            "co2": "CO2",
            "battery": "Batt %",
            "rssi": "RSSI",
            "time": "Last seen",
        }

        widths = {
            "mac": 142,
            "temperature": 72,
            "humidity": 68,
            "co2": 64,
            "battery": 68,
            "rssi": 55,
            "time": 82,
        }

        for col in cols:
            self.tree.heading(col, text=headings[col])
            self.tree.column(
                col,
                width=widths[col],
                minwidth=widths[col],
                anchor="center",
                stretch=False,
            )

        y_scroll = ttk.Scrollbar(
            table_frame,
            orient="vertical",
            command=self.tree.yview,
        )

        self.tree.configure(yscrollcommand=y_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")

        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

    def start_scan(self) -> None:
        if BleakScanner is None:
            messagebox.showerror(
                "Missing dependency",
                "Please install bleak:\n\npip install bleak",
            )
            return

        if self.scan_thread and self.scan_thread.is_alive():
            return

        self.stop_event.clear()

        self.scan_thread = threading.Thread(
            target=self._scanner_thread_main,
            daemon=True,
        )
        self.scan_thread.start()

        self.start_button.config(state="disabled")
        self.stop_button.config(state="normal")
        self.status_var.set("Starting...")

    def stop_scan(self) -> None:
        self.stop_event.set()
        self.start_button.config(state="disabled")
        self.stop_button.config(state="disabled")
        self.status_var.set("Stopping...")

    def _scanner_thread_main(self) -> None:
        try:
            asyncio.run(self._scan_loop())
        except Exception as e:
            self.q.put(("error", str(e)))

    async def _scan_loop(self) -> None:
        cache: dict[str, dict[str, Any]] = {}

        def callback(device: BLEDevice, adv: AdvertisementData) -> None:
            ble_address = getattr(device, "address", "") or ""

            if not ble_address:
                return

            entry = cache.setdefault(
                ble_address,
                {
                    "service_data": {},
                    "manufacturer_data": {},
                    "local_name": "",
                    "rssi": None,
                },
            )

            adv_service_data = getattr(adv, "service_data", None) or {}
            adv_manufacturer_data = getattr(adv, "manufacturer_data", None) or {}

            if adv_service_data:
                entry["service_data"].update(
                    {str(k).lower(): bytes(v) for k, v in adv_service_data.items()}
                )

            if adv_manufacturer_data:
                entry["manufacturer_data"].update(
                    {int(k): bytes(v) for k, v in adv_manufacturer_data.items()}
                )

            local_name = (
                getattr(adv, "local_name", None)
                or getattr(device, "name", None)
                or ""
            )

            if local_name:
                entry["local_name"] = local_name

            rssi = getattr(adv, "rssi", None)

            if rssi is not None:
                entry["rssi"] = rssi

            reading = decode_switchbot_meter(
                ble_address=ble_address,
                local_name=str(entry["local_name"]),
                rssi=entry["rssi"],
                service_data=entry["service_data"],
                manufacturer_data=entry["manufacturer_data"],
            )

            if reading is not None:
                self.q.put(reading)

        scanner = BleakScanner(callback, scanning_mode="active")

        await scanner.start()
        self.q.put(("status", "Scanning"))

        try:
            while not self.stop_event.is_set():
                await asyncio.sleep(0.2)
        finally:
            await scanner.stop()
            self.q.put(("status", "Stopped"))

    def _poll_queue(self) -> None:
        try:
            while True:
                item = self.q.get_nowait()

                if isinstance(item, SensorReading):
                    self._update_reading(item)
                    continue

                if isinstance(item, tuple) and len(item) == 2:
                    kind, value = item

                    if kind == "status":
                        self.status_var.set(str(value))

                        if str(value) == "Stopped":
                            self.start_button.config(state="normal")
                            self.stop_button.config(state="disabled")

                    elif kind == "error":
                        self.status_var.set("Error")
                        messagebox.showerror("BLE Scan Error", str(value))
                        self.start_button.config(state="normal")
                        self.stop_button.config(state="disabled")

        except queue.Empty:
            pass

        self.root.after(100, self._poll_queue)

    def _update_reading(self, r: SensorReading) -> None:
        display_mac = r.mac_address or r.ble_address or r.key
        row_id = r.ble_address or r.mac_address or r.key

        last_seen = datetime.fromtimestamp(r.seen_at).strftime("%H:%M:%S")
        temp = "" if r.temperature_c is None else f"{r.temperature_c:.1f}"
        hum = "" if r.humidity is None else str(r.humidity)
        co2 = "" if r.co2_ppm is None else str(r.co2_ppm)
        battery = "" if r.battery is None else str(r.battery)
        rssi = "" if r.rssi is None else str(r.rssi)

        values = (
            display_mac,
            temp,
            hum,
            co2,
            battery,
            rssi,
            last_seen,
        )

        if self.tree.exists(row_id):
            self.tree.item(row_id, values=values)
        else:
            self.tree.insert("", "end", iid=row_id, values=values)

        self._sort_rows_by_mac()

    def _sort_rows_by_mac(self) -> None:
        rows = list(self.tree.get_children(""))
        rows.sort(key=lambda item_id: _mac_sort_key(str(self.tree.set(item_id, "mac"))))

        for index, item_id in enumerate(rows):
            self.tree.move(item_id, "", index)

    def _on_close(self) -> None:
        self.stop_event.set()
        self.root.after(150, self.root.destroy)


def main() -> None:
    root = tk.Tk()
    SwitchBotBleMonitorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()