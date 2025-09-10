"""
LoRa Bidirectional Bridge Server

Supports both LoRa source and requester functionality.
Based on main_pro.py pattern but extended for bidirectional communication.

Functionality:
- REQUESTER: Listens for files from a specific LoRa endpoint and forwards to BLE clients
- SOURCE: Receives data from BLE clients and sends it over LoRa
- Displays status on OLED screen
- Includes LED activity indicator
- Uses connector lock to prevent conflicts between source and requester operations

Communication Flow:
- LoRa -> BLE: Received LoRa files are forwarded as BLE notifications
- BLE -> LoRa: BLE write operations are packaged and sent over LoRa
"""

import gc
import ujson
import utime
import machine
import asyncio
import aioble
import network
import ubinascii
import bluetooth

from lora32 import T3S3
from utils.oled_screen import OLED_Screen
from utils.led_alive import LED
from AlLoRa.File import CTP_File
from AlLoRa.Nodes.Source import Source
from AlLoRa.Nodes.Requester import Requester
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.Connectors.SX127x_connector import SX127x_connector
from micropython import const

# BLE UUIDs and constants
_ENV_BLE_UUID = bluetooth.UUID("0000180d-0000-1000-8000-00805f9b34fb")
_ENV_BLE_MESSAGE_UUID = bluetooth.UUID("00002a37-0000-1000-8000-00805f9b34fb")
_ENV_BLE_CONFIG_UUID = bluetooth.UUID("00002a38-0000-1000-8000-00805f9b34fb")
_ADV_APPEARANCE_GENERIC = const(768)
_ADV_INTERVAL_MS = 250_000

# LoRa/Display layout config
LAYOUT = [
    {
        "key": "MAC",
        "pos": {"x": 40, "y": 0},
        "area": {"x": 40, "y": 0, "w": 88, "h": 12},
        "static": True,
    },
    {
        "key": "BW",
        "pos": {"x": 40, "y": 12},
        "area": {"x": 40, "y": 12, "w": 30, "h": 12},
    },
    {
        "key": "TX_P",
        "pos": {"x": 70, "y": 12},
        "area": {"x": 70, "y": 12, "w": 25, "h": 12},
    },
    {
        "key": "SNR",
        "pos": {"x": 95, "y": 12},
        "area": {"x": 95, "y": 12, "w": 30, "h": 12},
    },
    {
        "key": "RSSI",
        "pos": {"x": 40, "y": 24},
        "area": {"x": 40, "y": 24, "w": 40, "h": 12},
    },
    {
        "key": "Chunk",
        "pos": {"x": 80, "y": 24},
        "area": {"x": 80, "y": 24, "w": 30, "h": 12},
    },
    {
        "key": "SF",
        "pos": {"x": 110, "y": 24},
        "area": {"x": 110, "y": 24, "w": 30, "h": 12},
    },
]

# Enable garbage collection
gc.enable()

# Global queue for BLE -> LoRa messages
ble_to_lora_queue = []

# Global configuration state
endpoint_config = {"configured": False, "endpoint": None}


def setup():
    """Initialize hardware, LoRa, BLE, and display (supports both source and requester)."""
    # Hardware setup
    device = T3S3()
    led = LED(device)

    # Load logo data
    with open("AlLoRa_logo.json", "r") as f:
        img_data = ujson.load(f)

    # Setup display with requester layout
    screen = OLED_Screen(device, img_data, button=False, layout_config=LAYOUT)

    # Setup LoRa connector with lock for concurrent access
    connector = SX127x_connector()
    connector_lock = asyncio.Lock()

    # Setup both requester and source nodes
    lora_requester = Requester(
        connector, config_file="LoRa.json", NEXT_ACTION_TIME_SLEEP=0.1
    )

    lora_source = Source(connector, config_file="LoRa.json")

    # Register screen subscribers
    lora_requester.register_subscriber(screen)
    lora_requester.notify_subscribers()
    lora_source.register_subscriber(screen)
    lora_source.notify_subscribers()

    led.run()

    # BLE GATT setup
    msg_service = aioble.Service(_ENV_BLE_UUID)

    # Message characteristic for regular chat messages
    msg_characteristic = aioble.Characteristic(
        msg_service,
        _ENV_BLE_MESSAGE_UUID,
        read=True,
        write=True,
        notify=True,
        capture=True,
    )

    # Configuration characteristic for endpoint setup
    config_characteristic = aioble.Characteristic(
        msg_service,
        _ENV_BLE_CONFIG_UUID,
        read=True,
        write=True,
        notify=True,
        capture=True,
    )

    aioble.register_services(msg_service)

    return {
        "lora_requester": lora_requester,
        "lora_source": lora_source,
        "connector_lock": connector_lock,
        "msg_characteristic": msg_characteristic,
        "config_characteristic": config_characteristic,
        "screen": screen,
        "led": led,
    }


def _encode_message(text):
    """Encode a text message as UTF-8 for BLE characteristic."""
    if isinstance(text, str):
        encoded = text.encode("utf-8")
    else:
        encoded = bytes(text)
    return encoded


def parse_endpoint_config(message):
    """Parse endpoint configuration from BLE config characteristic.

    Expected format: "name=<name>,mac=<mac_address>"
    Example: "name=NodeX,mac=da5a17b4"

    Returns tuple: (success, endpoint) where endpoint is Digital_Endpoint or None
    """
    try:
        if isinstance(message, (bytes, bytearray)):
            message = message.decode("utf-8")

        message = message.strip()
        params = {}

        # Parse key=value pairs separated by commas
        for param in message.split(","):
            if "=" in param:
                key, value = param.split("=", 1)
                params[key.strip()] = value.strip()

        if "name" in params and "mac" in params:
            name = params["name"]
            mac_address = params["mac"]

            print(f"Configuring endpoint: name={name}, mac={mac_address}")

            endpoint = Digital_Endpoint(name=name, mac_address=mac_address, active=True)

            return True, endpoint
        else:
            print("Invalid config format: missing name or mac")
            return False, None

    except Exception as e:
        print(f"Error parsing endpoint config: {e}")
        return False, None


async def lora_requester_task(requester_node, connector_lock, msg_characteristic):
    """Periodically listen for LoRa files and forward to BLE."""
    print("LoRa requester task started - waiting for endpoint configuration...")

    seen_messages = set()  # Add message deduplication
    message_count = 0

    while True:
        # Check if endpoint is configured
        if not endpoint_config["configured"] or not endpoint_config["endpoint"]:
            print("Waiting for endpoint configuration via BLE...")
            await asyncio.sleep_ms(2000)
            continue

        current_endpoint = endpoint_config["endpoint"]
        print(
            f"Listening for LoRa messages from {current_endpoint.name} ({current_endpoint.mac_address})..."
        )

        # Clear seen messages every 50 iterations to prevent memory buildup
        message_count += 1
        if message_count % 50 == 0:
            seen_messages.clear()
            print("Cleared message deduplication cache")

        async with connector_lock:
            # Try to receive a file (complete CTP transaction)
            file = requester_node.listen_to_endpoint(
                current_endpoint, 30, one_file=True
            )

            if file:
                # Full file received
                file_content = file.get_content()
                if file_content:
                    try:
                        if isinstance(file_content, (bytes, bytearray)):
                            content_str = file_content.decode("utf-8", errors="ignore")
                        else:
                            content_str = str(file_content)

                        # Add deduplication for files
                        message_hash = hash(content_str)
                        if message_hash not in seen_messages:
                            seen_messages.add(message_hash)
                            print(f"Received LoRa file: {file.get_name()}")
                            print(f"Content: {content_str}")
                            msg_characteristic.write(
                                _encode_message(content_str), send_update=True
                            )
                            print("Forwarded to BLE")
                        else:
                            print("Duplicate file message ignored")
                    except Exception as e:
                        print(f"Error processing file: {e}")
                        print(f"Raw content: {file_content}")

            else:
                # Handle inline (non-file) replies too
                raw_reply = getattr(requester_node, "last_reply", None)
                if raw_reply:
                    try:
                        content_str = raw_reply.decode("utf-8")
                        if content_str.strip():
                            # Add deduplication for raw messages
                            message_hash = hash(content_str)
                            if message_hash not in seen_messages:
                                seen_messages.add(message_hash)
                                print(f"Raw LoRa message: {content_str}")
                                msg_characteristic.write(
                                    _encode_message(content_str), send_update=True
                                )
                                print("Forwarded inline reply to BLE")
                            else:
                                print("Duplicate raw message ignored")

                            # Clear the last_reply to prevent resending
                            requester_node.last_reply = None
                    except Exception as e:
                        print(f"Error forwarding raw reply: {e}")
                        # Clear the last_reply even on error to prevent infinite loop
                        requester_node.last_reply = None
                else:
                    print("No file or message received within timeout")

        await asyncio.sleep_ms(1000)


async def config_task(config_characteristic):
    """Handle endpoint configuration via dedicated BLE characteristic."""
    print("Configuration task started - waiting for endpoint setup...")

    while True:
        try:
            # Receive configuration data in chunks until a timeout occurs
            config_chunks = []
            while True:
                _, config_data = await config_characteristic.written()
                if config_data:
                    config_chunks.append(config_data)
                    print(f"Received config chunk: {config_data}")
                    print(f"Config chunk length: {len(config_data)}")
                    if b"\n" in config_data:  # End of message marker
                        break
                else:
                    break

            if config_chunks:
                full_config_data = b"".join(config_chunks).rstrip(b"\n")
                print(f"Full configuration data: {full_config_data}")
                print(f"Full config data length: {len(full_config_data)}")

                # Parse the configuration
                success, new_endpoint = parse_endpoint_config(full_config_data)

                if success and new_endpoint:
                    # Update global configuration
                    endpoint_config["endpoint"] = new_endpoint
                    endpoint_config["configured"] = True

                    print(
                        f"Endpoint configured: {new_endpoint.name} ({new_endpoint.mac_address})"
                    )

                    # Send confirmation back to BLE client
                    confirmation = f"SUCCESS:name={new_endpoint.name},mac={new_endpoint.mac_address}"
                    config_characteristic.write(
                        _encode_message(confirmation), send_update=True
                    )
                    print("Configuration confirmation sent to BLE client")

                else:
                    # Send error back to BLE client
                    error_msg = "ERROR:Invalid format. Use name=<name>,mac=<mac>"
                    config_characteristic.write(
                        _encode_message(error_msg), send_update=True
                    )
                    print("Configuration error sent to BLE client")

        except asyncio.TimeoutError:
            pass
        except Exception as e:
            print(f"Config task error: {e}")

        await asyncio.sleep_ms(100)


async def lora_source_task(lora_source, connector_lock, msg_characteristic):
    """Read BLE characteristic and send received data over LoRa."""
    sending_timeout = 1 * 60 * 1000  # 1 minute in ms
    while True:
        try:
            # Receive written data in chunks until a timeout occurs
            data_chunks = []
            while True:
                _, data = await msg_characteristic.written()
                if data:
                    data_chunks.append(data)
                    if b"\n" in data:  # End of message marker
                        break
                else:
                    break
            if data_chunks:
                full_data = b"".join(data_chunks).rstrip(b"\n")

                # Check if endpoint is configured before sending
                if not endpoint_config["configured"]:
                    error_msg = "ERROR:Endpoint not configured. Configure via config characteristic first"
                    msg_characteristic.write(
                        _encode_message(error_msg), send_update=True
                    )
                    print("Message rejected - endpoint not configured")
                else:
                    filename = "msg_" + str(utime.ticks_ms()) + ".txt"
                    async with connector_lock:
                        try:
                            lora_source.establish_connection()
                            print("Connection OK")
                            lora_source.set_file(None)
                            for _ in range(3):
                                if not lora_source.got_file():
                                    file = CTP_File(
                                        name=filename,
                                        content=bytearray(full_data),
                                        chunk_size=lora_source.chunk_size,
                                    )
                                    print("Sending LoRa file:", file.get_name())
                                    lora_source.set_file(file)
                                    t_0_send = utime.ticks_ms()
                                    success = lora_source.send_file(
                                        timeout=sending_timeout
                                    )
                                    if success:
                                        td = utime.ticks_diff(
                                            utime.ticks_ms(), t_0_send
                                        )
                                        if td > sending_timeout:
                                            print("Timeout sending file")
                                    else:
                                        print("Error sending file")
                                        machine.reset()
                                utime.sleep(10)
                        except Exception as e:
                            print("LoRa send error:", e)
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep_ms(50)


async def peripheral_task():
    """Advertise BLE service and handle connections."""
    while True:
        wlan_sta = network.WLAN(network.STA_IF)
        wlan_sta.active(True)
        wlan_mac = wlan_sta.config("mac")

        async with await aioble.advertise(
            _ADV_INTERVAL_MS,
            name=ubinascii.hexlify(wlan_mac).decode()[-8:],
            services=[_ENV_BLE_UUID],
            appearance=_ADV_APPEARANCE_GENERIC,
        ) as connection:
            print(f"BLE Connection from {connection.device}")
            await connection.disconnected(timeout_ms=None)
            print("BLE Connection disconnected")


async def main():
    """Main entry point: setup and start both LoRa requester and source with BLE bridge."""
    print("Starting LoRa Bidirectional Bridge...")

    # Initialize hardware and services
    ctx = setup()

    # Start BLE advertising task
    ble_task = asyncio.create_task(peripheral_task())

    # Start configuration task
    config_task_handle = asyncio.create_task(config_task(ctx["config_characteristic"]))

    # Start LoRa requester task (LoRa -> BLE)
    requester_task = asyncio.create_task(
        lora_requester_task(
            ctx["lora_requester"],
            ctx["connector_lock"],
            ctx["msg_characteristic"],
        )
    )

    # Start LoRa source task (BLE -> LoRa)
    source_task = asyncio.create_task(
        lora_source_task(
            ctx["lora_source"], ctx["connector_lock"], ctx["msg_characteristic"]
        )
    )

    print("LoRa Bidirectional Bridge started.")
    print("Configuration service available on config characteristic")
    print("LoRa - Listening for LoRa messages (forwarding to BLE)")
    print("BLE - Listening for BLE messages (forwarding to LoRa)")

    # Wait for all tasks
    await asyncio.gather(ble_task, config_task_handle, requester_task, source_task)


try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("\nShutting down LoRa-to-BLE Bridge...")
except Exception as e:
    print(f"Fatal error: {e}")
    # Optional: restart the device on fatal error
    # import machine
    # machine.reset()
