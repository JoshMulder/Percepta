"""The supported-device list, as data.

One entry per device type the station knows how to fit. Adding support for
another weather head is a row here plus a driver — not a change in five places —
which is the whole reason this is a table rather than a switch.

Four columns do work that is easy to miss:

**`connection`** is how it attaches, and the shapes genuinely differ. A Pi
camera on the CSI ribbon has no address and no credentials; an ONVIF camera on
the network has both and no device path. `contract/enrolment.md` §7 describes
the network case only, and bending one into the other's shape is how a config
form ends up asking for the password of a camera that is a ribbon cable.

**`provides`** is which telemetry values this device can genuinely source. It is
the difference between a field being zero and a field being *absent*, and it is
the honest answer to "what does the console have no source for on this station".

**`resource`** is a physical thing the device consumes — today, an SDR tuner. A
tuner serves one job at a time: 108–137 MHz airband and 1090 MHz ADS-B cannot be
received simultaneously by one dongle, whatever the software does. Resources are
keyed on **serial number, never on USB index**, because two identical dongles
enumerate in an order that changes between boots, and an allocation that moves
when the box reboots is worse than no allocation at all.

**`driver`** may be `None`, meaning the station supports selecting this device
but cannot yet talk to it. That is reported as exactly that. It is never
silently replaced with a simulation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SLOTS = ("adsb", "radio", "weather", "power", "light", "camera")

#: Which stream a slot sources, so an absent device can be reported as an absent
#: *stream* rather than as an empty one.
#:
#: `camera` maps to `video`, which is not a telemetry kind — it is its own
#: channel with its own schema — but it is the same fact and the console needs
#: it in the same place: a station with no camera reports `video` in
#: `unsourced_streams`, exactly as one with no receiver reports `adsb`.
SLOT_TELEMETRY = {
    "adsb": "adsb",
    "radio": "radio",
    "weather": "weather",
    "power": "power",
    "light": "light",
    "camera": "video",
}

CONNECTIONS = (
    "serial",         # a USB-UART or a native UART: path plus baud
    "usb-sdr",        # a software-defined radio, allocated by serial number
    "csi",            # the Pi camera ribbon. No address, no credentials
    "network",        # an IP device: address and credentials
    "gpio",           # a pin on the header
    "simulated",      # no hardware, and says so everywhere it appears
    "none",           # nothing fitted in this slot
)


@dataclass(frozen=True)
class Parameter:
    name: str
    label: str
    type: str = "text"           # text | password | number | select | bool
    default: object = ""
    required: bool = True
    help: str = ""
    choices: tuple = ()


@dataclass(frozen=True)
class DeviceType:
    id: str
    slot: str
    label: str
    connection: str
    #: Dotted path to the adapter, or None where the station can be *configured*
    #: with this device but cannot yet read it.
    driver: str | None = None
    vendor: str = ""
    parameters: tuple[Parameter, ...] = ()
    #: Telemetry fields this device can genuinely source.
    provides: tuple[str, ...] = ()
    #: Fields the console renders for this slot that this device cannot source.
    #: Stated rather than discovered, so the gap is visible at selection time.
    absent: tuple[str, ...] = ()
    #: A physical resource this device consumes, e.g. "rtlsdr".
    resource: str | None = None
    simulated: bool = False
    notes: str = ""


#: Serial parameters, with the baud that device actually ships at.
#:
#: The port default is **empty on purpose**. A default of `/dev/ttyUSB0` is a
#: trap on this box: two USB-UARTs are fitted, they enumerate in the order the
#: kernel probed them, and that order changes between boots — so the weather
#: head and the ADS-B receiver swap over and each driver reads the other's
#: traffic. That presents as both instruments failing, which is a long way from
#: the real fault. An empty value produces a message naming the ports that are
#: actually present, which is a better first boot than a plausible wrong guess.
def _serial_parameters(baud: int, baud_help: str) -> tuple[Parameter, ...]:
    return (
        Parameter(
            "port", "Serial port", "text", "",
            help="Use a /dev/serial/by-id/… name. It is derived from the "
                 "adapter's own identity and survives a reboot; ttyUSB "
                 "numbering does not, and two adapters will swap over. The "
                 "setup page lists what is plugged in.",
        ),
        Parameter("baud", "Baud", "number", baud, help=baud_help),
    )


NMEA_SERIAL_PARAMETERS = _serial_parameters(
    4800, "4800 is the NMEA 0183 standard rate and the 110WX default.",
)

#: uAvionix ship the ping RX Pro at 57600. It is configurable on the device, so
#: this is a default rather than a constant — but a wrong baud reads as silence,
#: not as an error, which is why it is stated here rather than assumed.
MAVLINK_SERIAL_PARAMETERS = _serial_parameters(
    57600, "57600 is the ping RX Pro's factory rate. If it was reconfigured, "
           "a wrong value here looks exactly like a dead receiver: bytes "
           "arrive and no frame ever parses.",
)

MODBUS_SERIAL_PARAMETERS = _serial_parameters(
    19200, "19200 8N1 is the Victron VE.Direct/Modbus default.",
)


REGISTRY: tuple[DeviceType, ...] = (
    # --- ADS-B ---------------------------------------------------------
    DeviceType(
        id="uavionix-ping-rx-pro",
        slot="adsb",
        label="uAvionix ping RX Pro (MAVLink over serial)",
        vendor="uAvionix",
        connection="serial",
        driver="gsu.devices.pingrx:PingRxAdsb",
        parameters=MAVLINK_SERIAL_PARAMETERS,
        provides=("icao", "callsign", "latitude", "longitude", "altitude",
                  "track", "speed", "range_km", "bearing", "alert"),
        notes="Emits ADSB_VEHICLE. Position, altitude, heading, velocity and "
              "callsign each carry a validity flag, which the driver honours: "
              "an unflagged value is published as null, never as zero.",
    ),
    DeviceType(
        id="rtlsdr-dump1090",
        slot="adsb",
        label="RTL-SDR + dump1090 (1090 MHz)",
        connection="usb-sdr",
        driver=None,
        resource="rtlsdr",
        parameters=(
            Parameter("gain", "Gain", "text", "auto", required=False),
            Parameter("sample_rate", "Sample rate", "number", 2_400_000, required=False),
        ),
        provides=("icao", "callsign", "latitude", "longitude", "altitude",
                  "track", "speed", "range_km", "bearing", "alert"),
        notes="Needs a tuner of its own: 1090 MHz and airband cannot share one. "
              "Driver not implemented — the station would supervise dump1090 "
              "and read its Beast/SBS output.",
    ),
    DeviceType(
        id="simulated-adsb",
        slot="adsb",
        label="Simulated ADS-B (no hardware)",
        connection="simulated",
        driver="gsu.devices.pingrx:SimulatedPingRx",
        simulated=True,
        provides=("icao", "callsign", "latitude", "longitude", "altitude",
                  "track", "speed", "range_km", "bearing", "alert"),
        notes="Generates real MAVLink ADSB_VEHICLE frames and decodes them "
              "through the same parser the hardware path uses.",
    ),

    # --- airband radio -------------------------------------------------
    DeviceType(
        id="rtlsdr-airband",
        slot="radio",
        label="RTL-SDR airband receiver (108–137 MHz)",
        connection="usb-sdr",
        driver=None,
        resource="rtlsdr",
        parameters=(
            Parameter("gain", "Tuner gain (dB)", "number", 37.2,
                      help="Fixed, not auto: the tuner's AGC desenses near "
                           "strong transmitters badly enough that a stronger "
                           "signal can read lower."),
            Parameter("ppm", "Crystal correction (ppm)", "number", 0, required=False),
        ),
        provides=("freq_hz", "rssi_db", "noise_floor_db", "threshold_db",
                  "squelch_open", "auto_squelch", "monitor", "gain", "gains",
                  "ppm", "audio"),
        absent=("tx",),
        notes="Receive only. Driver not implemented: it would supervise the "
              "radio process and must stop it through that process's own "
              "shutdown endpoint, never with a signal — a dongle killed "
              "mid-transfer needs a physical replug.",
    ),
    DeviceType(
        id="simulated-airband",
        slot="radio",
        label="Simulated airband receiver (no hardware)",
        connection="simulated",
        driver="gsu.radio.simulated:SimulatedFrontEnd",
        simulated=True,
        provides=("freq_hz", "rssi_db", "noise_floor_db", "threshold_db",
                  "squelch_open", "auto_squelch", "monitor", "gain", "gains",
                  "ppm", "audio"),
        absent=("tx",),
    ),

    # --- weather -------------------------------------------------------
    DeviceType(
        id="airmar-110wx",
        slot="weather",
        label="Airmar 110WX WeatherStation (NMEA 0183)",
        vendor="Airmar",
        connection="serial",
        driver="gsu.devices.airmar:AirmarWeather",
        parameters=NMEA_SERIAL_PARAMETERS + (
            Parameter("humidity_module", "Relative humidity module fitted",
                      "bool", False, required=False,
                      help="Airmar sell the 110WX with and without the RH "
                           "module. Without it there is no humidity source at "
                           "all, and the field is published as absent."),
            Parameter("mast_offset_deg", "Mast orientation (° true)", "number", 0,
                      required=False,
                      help="Applied to relative wind angle to give a true "
                           "direction. The instrument has no compass."),
        ),
        provides=("wind_kt", "gust_kt", "wind_dir_deg", "temperature_c",
                  "pressure_hpa"),
        absent=("humidity_pct", "rain_rate_mmh", "rain_mm_today",
                "visibility_km", "sky"),
        notes="Ultrasonic wind, air temperature and barometric pressure; "
              "relative humidity only with the optional module. It has no rain "
              "gauge, no visibility sensor and no sky observation, so those "
              "console fields have no source on this station. Gust is derived "
              "station-side as the peak of a rolling window, not measured.",
    ),
    DeviceType(
        id="generic-nmea-weather",
        slot="weather",
        label="Generic NMEA 0183 weather head",
        connection="serial",
        driver="gsu.devices.airmar:AirmarWeather",
        parameters=NMEA_SERIAL_PARAMETERS + (
            Parameter("humidity_module", "Reports humidity", "bool", True,
                      required=False),
        ),
        provides=("wind_kt", "gust_kt", "wind_dir_deg", "temperature_c",
                  "pressure_hpa", "humidity_pct"),
        absent=("rain_rate_mmh", "rain_mm_today", "visibility_km", "sky"),
        notes="Same decoder, different capability declaration — which is what "
              "the registry is for.",
    ),
    DeviceType(
        id="simulated-weather",
        slot="weather",
        label="Simulated weather station, full sensor set (no hardware)",
        connection="simulated",
        driver="gsu.sensors.simulated:SimulatedWeather",
        simulated=True,
        provides=("wind_kt", "gust_kt", "wind_dir_deg", "temperature_c",
                  "humidity_pct", "pressure_hpa", "visibility_km", "sky",
                  "is_day", "rain_rate_mmh", "rain_mm_today"),
        notes="Models an instrument that has every sensor the console renders, "
              "including a tipping-bucket rain gauge. No real instrument in "
              "this box does.",
    ),

    # --- power ---------------------------------------------------------
    DeviceType(
        id="victron-mppt-modbus",
        slot="power",
        label="Victron MPPT charge controller (Modbus RTU)",
        vendor="Victron",
        connection="serial",
        driver=None,
        parameters=MODBUS_SERIAL_PARAMETERS + (
            Parameter("unit_id", "Modbus unit id", "number", 1),
        ),
        provides=("soc_pct", "battery_v", "pv_w", "load_w", "runtime_h"),
        notes="Driver not implemented.",
    ),
    DeviceType(
        id="simulated-power",
        slot="power",
        label="Simulated solar and battery (no hardware)",
        connection="simulated",
        driver="gsu.sensors.simulated:SimulatedPower",
        simulated=True,
        provides=("soc_pct", "battery_v", "pv_w", "load_w", "runtime_h"),
    ),

    # --- floodlight ----------------------------------------------------
    DeviceType(
        id="gpio-relay",
        slot="light",
        label="Floodlight relay on a GPIO pin",
        connection="gpio",
        driver=None,
        parameters=(
            Parameter("chip", "GPIO chip", "text", "gpiochip0"),
            Parameter("line", "Line", "number", 17),
            Parameter("active_low", "Active low", "bool", False, required=False),
            Parameter("readback_line", "Read-back line", "number", 0, required=False,
                      help="A separate input that reports the contactor's actual "
                           "state. Without one the station can only report what "
                           "it commanded, which the contract asks it not to."),
        ),
        provides=("on",),
        notes="Driver not implemented.",
    ),
    DeviceType(
        id="simulated-light",
        slot="light",
        label="Simulated floodlight (no hardware)",
        connection="simulated",
        driver="gsu.sensors.simulated:SimulatedFloodlight",
        simulated=True,
        provides=("on",),
    ),

    # --- camera --------------------------------------------------------
    DeviceType(
        id="raspberry-pi-csi",
        slot="camera",
        label="Raspberry Pi camera (CSI ribbon)",
        connection="csi",
        driver="gsu.camera.picsi:PiCsiCamera",
        parameters=(
            Parameter("resolution", "Resolution", "select", "640x480",
                      choices=("640x480", "1280x720", "1920x1080"), required=False,
                      help="640x480 by default, and not because the camera "
                           "cannot do better: at 2 fps it is already around half "
                           "a megabit per second on a metered satellite link. "
                           "HARDWARE.md §8 has the measured cost of each."),
            Parameter("quality", "JPEG quality", "number", 75, required=False,
                      help="1-100, as libjpeg means it. Below about 50 the "
                           "picture is visibly blocked; above about 85 the file "
                           "grows fast for very little."),
            Parameter("rotation", "Rotation", "select", 0, choices=(0, 180),
                      required=False,
                      help="180 for a camera mounted upside down. Only these "
                           "two: the sensor rotates in 180° steps and anything "
                           "else would be a CPU-side rotate of every frame."),
        ),
        provides=("video",),
        notes="No address and no credentials: it is a ribbon cable, not a "
              "network device. Bookworm, so libcamera: the driver uses "
              "picamera2 if it imports and rpicam-jpeg if it does not. Motion "
              "JPEG rather than H.264 — the reasoning is in "
              "contract/schemas/video.schema.json. **Never run against real "
              "hardware** (HARDWARE.md §7).",
    ),
    DeviceType(
        id="simulated-camera",
        slot="camera",
        label="Simulated camera, test card (no hardware)",
        connection="simulated",
        driver="gsu.camera.synthetic:SyntheticCamera",
        simulated=True,
        parameters=(
            Parameter("resolution", "Resolution", "select", "640x480",
                      choices=("640x480", "1280x720", "320x240"), required=False),
            Parameter("quality", "JPEG quality", "number", 75, required=False),
        ),
        provides=("video",),
        notes="Draws a test card with the capture time across it, so that a "
              "frame is unmistakable for a photograph and a stalled stream is "
              "visible without instrumentation. Its frames are several times "
              "smaller than a real camera's — see HARDWARE.md §8 before "
              "planning bandwidth from them.",
    ),
    DeviceType(
        id="onvif-network-camera",
        slot="camera",
        label="Network camera (ONVIF / RTSP)",
        connection="network",
        driver=None,
        parameters=(
            Parameter("address", "Address", "text", "", help="Host or IP on the "
                      "station's local network."),
            Parameter("username", "Username", "text", "", required=False),
            Parameter("password", "Password", "password", "", required=False,
                      help="Stored on the box no less carefully than the "
                           "station's own credential."),
            Parameter("rtsp_path", "RTSP path", "text", "/Streaming/Channels/101",
                      required=False),
        ),
        provides=("video",),
        notes="The case contract/enrolment.md §7 describes. Driver not "
              "implemented: it would pull RTSP and re-encode, which is a "
              "different job from grabbing a still off the CSI ribbon and is "
              "the one place a station would need a decoder of its own.",
    ),
)


def by_slot(slot: str) -> tuple[DeviceType, ...]:
    return tuple(device for device in REGISTRY if device.slot == slot)


def get(type_id: str) -> DeviceType | None:
    for device in REGISTRY:
        if device.id == type_id:
            return device
    return None


def default_fitted() -> dict[str, str]:
    """What a box ships with before anyone has told it anything.

    Everything simulated, because that is what is true of this build: there is
    no hardware attached and nothing here will claim otherwise.
    """
    return {
        "adsb": "simulated-adsb",
        "radio": "simulated-airband",
        "weather": "simulated-weather",
        "power": "simulated-power",
        "light": "simulated-light",
        # Simulated like everything else here. A box with no camera selected
        # publishes `available: false` on the video channel, which is correct
        # and is also a blank panel on somebody's console the first time they
        # look — so a fresh station shows a test card that says SYNTHETIC
        # rather than nothing at all.
        "camera": "simulated-camera",
    }
