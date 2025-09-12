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

# Global queue for incoming BLE chunks to prevent loss during task switching
ble_chunk_queue = []

# Global configuration state
endpoint_config = {"configured": False, "endpoint": None}

# Global BLE connection for MTU exchange
current_connection = None

# Global state for chunked message reassembly
chunked_messages = (
    {}
)  # messageId -> {"chunks": [], "total": int, "received": int, "type": str, "timestamp": int}

# Global transfer state for dynamic task prioritization
transfer_state = {
    "active": False,
    "last_chunk_time": 0,
    "chunk_rate": 0,
    "active_messages": 0,
}

# Cleanup old messages every 100 iterations to prevent memory leaks
cleanup_counter = 0


def setup():
    """Initialize hardware, LoRa, BLE, and display (supports both source and requester)."""

    # MTU Configuration - Set maximum supported MTU at startup
    print("Configuring BLE MTU...")
    try:
        # Configure ESP32 to support maximum MTU (517 bytes) for optimal throughput
        aioble.config(mtu=517)
        print("MTU configured to 517 bytes (ESP32 maximum)")
        print(
            "Effective payload will be negotiated with iOS device (~184 bytes expected)"
        )
    except Exception as e:
        # Fallback to a lower MTU if 517 is not supported by firmware
        try:
            aioble.config(mtu=256)
            print(f"MTU fallback to 256 bytes: {e}")
        except Exception as e2:
            print(f"MTU configuration failed: {e2}")
            print("Using default MTU (23 bytes) - reduced throughput expected")

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

    # Message characteristic for regular chat messages - use BufferedCharacteristic for better chunk handling
    msg_characteristic = aioble.BufferedCharacteristic(
        msg_service,
        _ENV_BLE_MESSAGE_UUID,
        read=True,
        write=True,
        notify=True,
        capture=True,
        max_len=512,  # Large buffer to handle burst chunks
        append=False,  # Replace mode - we process chunks individually
    )

    # Configuration characteristic for endpoint setup - also buffered for reliability
    config_characteristic = aioble.BufferedCharacteristic(
        msg_service,
        _ENV_BLE_CONFIG_UUID,
        read=True,
        write=True,
        notify=True,
        capture=True,
        max_len=128,  # Smaller buffer for config messages
        append=False,
    )

    print("Using BufferedCharacteristics for improved burst data handling")
    print("Large buffer sizes (512B message, 128B config) to reduce chunk loss")

    aioble.register_services(msg_service)
    print("BLE services registered successfully")

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


async def send_chunked_message(
    characteristic, message, message_type="f", chunk_delay_ms=30
):
    """Send a large message using the chunked protocol to BLE clients.

    Args:
        characteristic: BLE characteristic to write to
        message: Message content to send
        message_type: Message type identifier (default: 't' for text)
        chunk_delay_ms: Delay between chunks in milliseconds

    Returns:
        bool: True if successful, False otherwise
    """
    try:
        # Generate a random 3-character message ID
        import os

        message_id = ""
        for _ in range(3):
            message_id += chr(ord("a") + (os.urandom(1)[0] % 26))

        # Calculate chunk size based on MTU (assume 20 bytes for compatibility)
        # Reserve space for protocol overhead: "C:abc:999:" = 9 bytes
        max_chunk_size = 20 - 9

        # Split message into chunks
        chunks = []
        for i in range(0, len(message), max_chunk_size):
            chunks.append(message[i : i + max_chunk_size])

        total_chunks = len(chunks)
        print(
            f"Sending chunked message to BLE: id={message_id}, chunks={total_chunks}, type={message_type}"
        )

        # Send START message
        start_msg = f"S:{message_id}:{total_chunks}:{message_type}"
        characteristic.write(_encode_message(start_msg), send_update=True)
        await asyncio.sleep_ms(chunk_delay_ms)

        # Send chunks
        for i, chunk in enumerate(chunks, 1):
            chunk_msg = f"C:{message_id}:{i}:{chunk}"
            characteristic.write(_encode_message(chunk_msg), send_update=True)
            await asyncio.sleep_ms(chunk_delay_ms)

        # Send END message
        end_msg = f"E:{message_id}:{total_chunks}"
        characteristic.write(_encode_message(end_msg), send_update=True)

        print(f"Chunked message sent successfully: {total_chunks} chunks")
        return True

    except Exception as e:
        print(f"Error sending chunked message: {e}")
        return False


def parse_chunked_message(chunk_data):
    """Parse chunked message data and return complete message when all chunks received.

    Chunked message protocol (revised - single character identifiers):
    S:<messageId>:<totalChunks>:<messageType>  # START message
    C:<messageId>:<chunkIndex>:<data>          # CHUNK message
    E:<messageId>:<totalChunks>                # END message

    Returns tuple: (is_complete, message_content, message_type)
    """
    global cleanup_counter, chunked_messages
    try:
        # Decode from bytes to string (chunked protocol is text-based)
        if isinstance(chunk_data, (bytes, bytearray)):
            # Chunked protocol messages are UTF-8 encoded, not base64
            chunk_str = chunk_data.decode("utf-8").strip()
        else:
            chunk_str = str(chunk_data).strip()

        # Cleanup old messages periodically
        cleanup_counter += 1
        if cleanup_counter % 100 == 0:
            current_time = utime.ticks_ms()
            to_remove = []
            for msg_id, msg_info in chunked_messages.items():
                if (
                    utime.ticks_diff(current_time, msg_info.get("timestamp", 0)) > 60000
                ):  # 60 seconds
                    to_remove.append(msg_id)
            for msg_id in to_remove:
                del chunked_messages[msg_id]

        if chunk_str.startswith("S:"):
            # Format: S:<messageId>:<totalChunks>:<messageType>
            parts = chunk_str.split(":", 3)
            if len(parts) >= 4:
                message_id = parts[1]
                total_chunks = int(parts[2])
                message_type = parts[3]

                # Expand message type abbreviations
                if message_type == "t":
                    message_type = "text"
                elif message_type == "f":
                    message_type = "file"
                elif message_type == "c":
                    message_type = "config"

                # Initialize message tracking
                chunked_messages[message_id] = {
                    "chunks": [None] * total_chunks,
                    "total": total_chunks,
                    "received": 0,
                    "type": message_type,
                    "timestamp": utime.ticks_ms(),
                }
                print(
                    f"Started chunked message {message_id} with {total_chunks} chunks, type: {message_type}"
                )

        elif chunk_str.startswith("C:"):
            # Format: C:<messageId>:<chunkIndex>:<data>
            parts = chunk_str.split(":", 3)
            if len(parts) >= 4:
                message_id = parts[1]
                chunk_index = int(parts[2])
                chunk_data = parts[3]

                if message_id not in chunked_messages:
                    # Auto-recover: We missed the START message - create message entry
                    if chunk_index > 0:
                        print(
                            f"MISSING START MESSAGE - received chunk {chunk_index} without START for {message_id}"
                        )

                    # Estimate total chunks more conservatively to avoid huge arrays
                    # Base estimate on the chunk index we're seeing
                    if chunk_index < 10:
                        estimated_total = max(
                            chunk_index + 100, 200
                        )  # Small-medium file
                    elif chunk_index < 100:
                        estimated_total = max(
                            chunk_index + 200, 500
                        )  # Medium-large file
                    else:
                        estimated_total = max(
                            chunk_index + 100, chunk_index * 2
                        )  # Very large file

                    print(
                        f"Auto-creating message {message_id} with estimated {estimated_total} chunks"
                    )
                    chunked_messages[message_id] = {
                        "chunks": [None] * estimated_total,
                        "total": estimated_total,
                        "received": 0,
                        "type": "file",  # Default to file for missing START
                        "timestamp": utime.ticks_ms(),
                        "missing_start": True,  # Flag that we missed the START message
                    }

                msg_info = chunked_messages[message_id]

                # Expand chunks array if needed
                if chunk_index >= len(msg_info["chunks"]):
                    # For large files, expand more generously to avoid frequent reallocations
                    new_size = max(chunk_index + 100, len(msg_info["chunks"]) * 2)
                    old_size = len(msg_info["chunks"])
                    msg_info["chunks"].extend([None] * (new_size - old_size))
                    # Don't update total here - wait for END message to get actual total
                    print(
                        f"Expanded chunks array for {message_id} from {old_size} to {new_size}"
                    )

                if chunk_index < len(msg_info["chunks"]):
                    if (
                        msg_info["chunks"][chunk_index] is None
                    ):  # Don't overwrite existing chunks
                        msg_info["chunks"][chunk_index] = chunk_data
                        msg_info["received"] += 1

                        # Progress reporting for large transfers
                        if msg_info["received"] % 200 == 0:
                            estimated_total = max(msg_info["total"], chunk_index + 1)
                            progress_pct = (
                                (msg_info["received"] / estimated_total) * 100
                                if estimated_total > 0
                                else 0
                            )
                            print(
                                f"Progress for {message_id}: {msg_info['received']} chunks received, {progress_pct:.1f}%"
                            )
                    # Detect message type from first chunk
                    if chunk_index == 0:
                        if chunk_data.startswith("IMG:"):
                            msg_info["type"] = "image"
                        elif chunk_data.startswith("FILE:"):
                            msg_info["type"] = "file"

        elif chunk_str.startswith("E:"):
            # Format: E:<messageId>:<totalChunks>
            parts = chunk_str.split(":")
            if len(parts) >= 3:
                message_id = parts[1]
                expected_total = int(parts[2])

                if message_id in chunked_messages:
                    msg_info = chunked_messages[message_id]

                    # Update the total from END message (authoritative)
                    old_total = msg_info["total"]
                    msg_info["total"] = expected_total

                    if old_total != expected_total:
                        print(
                            f"Updated total chunks for {message_id}: {old_total} -> {expected_total}"
                        )

                        # Resize chunks array if needed
                        if expected_total > len(msg_info["chunks"]):
                            msg_info["chunks"].extend(
                                [None] * (expected_total - len(msg_info["chunks"]))
                            )
                        elif expected_total < len(msg_info["chunks"]):
                            msg_info["chunks"] = msg_info["chunks"][:expected_total]

                    # Check if all chunks received
                    non_none_chunks = sum(
                        1
                        for chunk in msg_info["chunks"][:expected_total]
                        if chunk is not None
                    )
                    print(
                        f"Received {non_none_chunks}/{expected_total} chunks for message {message_id}"
                    )

                    # Warn about missing START if we had to auto-recover
                    if msg_info.get("missing_start", False):
                        print(
                            f"Note: START message was missing for {message_id} - auto-recovered"
                        )

                    if non_none_chunks == expected_total:
                        # Calculate transfer statistics
                        transfer_duration = utime.ticks_diff(
                            utime.ticks_ms(), msg_info["timestamp"]
                        )
                        chunks_per_sec = (
                            (expected_total / transfer_duration) * 1000
                            if transfer_duration > 0
                            else 0
                        )

                        print(f"TRANSFER SUCCESS for message {message_id}")
                        print(
                            f"Transfer stats: {expected_total} chunks in {transfer_duration}ms ({chunks_per_sec:.1f} chunks/sec)"
                        )

                        # Reassemble message
                        reassembled = "".join(msg_info["chunks"][:expected_total])
                        message_type = msg_info["type"]

                        # Mark transfer as completed - reset active state immediately
                        transfer_state["active"] = False
                        transfer_state["chunk_rate"] = 0

                        # Cleanup
                        del chunked_messages[message_id]

                        return True, reassembled, message_type
                    else:
                        missing_chunks = []
                        present_chunks = []
                        for i in range(expected_total):
                            if msg_info["chunks"][i] is None:
                                missing_chunks.append(i)
                            else:
                                present_chunks.append(i)

                        print(
                            f"Missing {len(missing_chunks)} chunks for message {message_id}"
                        )
                        if len(missing_chunks) <= 20:
                            print(f"Missing chunks: {missing_chunks}")
                else:
                    print(f"Received END for unknown message {message_id}")
        # If not a chunked message or incomplete, return as-is
        return False, chunk_str, "text"

    except Exception as e:
        print(f"Error parsing chunked message: {e}")
        return False, str(chunk_data), "text"


def process_file_message(file_data):
    """Process file message and extract filename and base64 data.

    Expected formats:
    - FILE:<filename>:<base64_data>  (preferred)
    - FILE:<filename><base64_data>   (fallback - no separator colon)

    Returns: (success, filename, base64_data)
    """
    try:
        if file_data.startswith("FILE:"):
            parts = file_data.split(":", 2)  # Split into 3 parts: FILE, filename, data
            if len(parts) >= 3:
                # Standard format: FILE:filename:base64data
                filename = parts[1]
                base64_data = parts[2]
                print(
                    f"Extracted file (standard format): {filename}, data: {len(base64_data)} characters"
                )
            elif len(parts) == 2:
                # Fallback format: FILE:filename<base64data> - need to split filename from base64
                remainder = parts[1]

                # Try to find where filename ends and base64 starts
                # Base64 typically contains only A-Z, a-z, 0-9, +, /, = characters
                # Filename likely contains alphanumeric and common file characters
                filename_end = 0
                for i, char in enumerate(remainder):
                    if (
                        char
                        in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
                    ):
                        # Could be base64, but check if it looks like a valid transition
                        if i > 0:  # Must have some filename
                            # Check if the remaining part looks like base64
                            potential_base64 = remainder[i:]
                            if len(potential_base64) > 10 and all(
                                c
                                in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
                                for c in potential_base64
                            ):
                                filename_end = i
                                break

                if filename_end > 0:
                    filename = remainder[:filename_end]
                    base64_data = remainder[filename_end:]
                else:
                    # Fallback: assume filename is short and rest is base64
                    # Look for typical file extensions
                    possible_extensions = [".txt", ".json", ".xml", ".csv", ".pdf"]
                    filename_end = 4  # Default short filename
                    for ext in possible_extensions:
                        if ext in remainder[:20]:  # Check first 20 chars
                            filename_end = remainder.find(ext) + len(ext)
                            break

                    filename = (
                        remainder[:filename_end]
                        if filename_end < len(remainder)
                        else remainder[:10]
                    )
                    base64_data = (
                        remainder[filename_end:]
                        if filename_end < len(remainder)
                        else ""
                    )

                print(
                    f"Extracted file (fallback format): {filename}, data: {len(base64_data)} characters"
                )
            else:
                print(
                    f"Invalid file format: expected FILE:<filename>:<data>, got {len(parts)} parts"
                )
                return False, None, None

            # Validate base64 data
            if base64_data:
                try:
                    decoded_size = len(ubinascii.a2b_base64(base64_data))
                    print(f"File decoded size: {decoded_size} bytes")
                except Exception as decode_error:
                    print(f"Base64 decode verification failed: {decode_error}")

            return True, filename, base64_data
        else:
            return False, None, None

    except Exception as e:
        print(f"Error processing file message: {e}")
        return False, None, None


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
    global transfer_state
    print("LoRa requester task started - waiting for endpoint configuration...")

    last_message_hash = None  # Track last message to prevent consecutive duplicates

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

        async with connector_lock:
            # Try to receive a file (complete CTP transaction)
            # Use null timeout during active BLE transfers to avoid blocking chunk processing
            timeout = (
                0 if transfer_state["active"] else 30
            )  # 0s during transfer, 30s when idle
            file = requester_node.listen_to_endpoint(
                current_endpoint, timeout, one_file=True
            )

            if file:
                # Full file received
                file_content = file.get_content()
                if file_content:
                    try:
                        if isinstance(file_content, (bytes, bytearray)):
                            content_str = file_content.decode("utf-8")
                        else:
                            content_str = str(file_content)

                        # Prevent consecutive duplicate files
                        message_hash = hash(content_str)
                        if message_hash != last_message_hash:
                            last_message_hash = message_hash
                            print(f"Received LoRa file: {file.get_name()}")

                            # Check if this is file content that needs to be reformatted for BLE
                            if content_str.startswith("FILE_CONTENT:"):
                                # Format: FILE_CONTENT:filename:decoded_content
                                parts = content_str.split(":", 2)
                                if len(parts) >= 3:
                                    filename = parts[1]
                                    decoded_content = parts[2]
                                    # Re-encode as base64 for BLE transmission
                                    base64_content = (
                                        ubinascii.b2a_base64(
                                            decoded_content.encode("utf-8")
                                        )
                                        .decode("utf-8")
                                        .strip()
                                    )
                                    ble_message = f"FILE:{filename}:{base64_content}"
                                    print(f"Reformatted file for BLE: {filename}")
                                else:
                                    ble_message = content_str
                            elif content_str.startswith("FILE_BASE64:"):
                                # Format: FILE_BASE64:filename:base64_data
                                parts = content_str.split(":", 2)
                                if len(parts) >= 3:
                                    filename = parts[1]
                                    base64_content = parts[2]
                                    ble_message = f"FILE:{filename}:{base64_content}"
                                    print(
                                        f"Reformatted base64 file for BLE: {filename}"
                                    )
                                else:
                                    ble_message = content_str
                            else:
                                # Regular message, forward as-is
                                ble_message = content_str

                            # Use chunked sending for large messages (>100 chars)
                            if len(ble_message) > 100:
                                await send_chunked_message(
                                    msg_characteristic, ble_message, "f", 30
                                )
                                print("Forwarded large file to BLE")
                            else:
                                msg_characteristic.write(
                                    _encode_message(ble_message), send_update=True
                                )
                                print("Forwarded to BLE")
                        else:
                            print("Consecutive duplicate file message ignored")
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
                            # Prevent consecutive duplicate raw messages
                            message_hash = hash(content_str)
                            if message_hash != last_message_hash:
                                last_message_hash = message_hash

                                # Check if this is file content that needs to be reformatted for BLE
                                if content_str.startswith("FILE_CONTENT:"):
                                    # Format: FILE_CONTENT:filename:decoded_content
                                    parts = content_str.split(":", 2)
                                    if len(parts) >= 3:
                                        filename = parts[1]
                                        decoded_content = parts[2]
                                        # Re-encode as base64 for BLE transmission
                                        base64_content = (
                                            ubinascii.b2a_base64(
                                                decoded_content.encode("utf-8")
                                            )
                                            .decode("utf-8")
                                            .strip()
                                        )
                                        ble_message = (
                                            f"FILE:{filename}:{base64_content}"
                                        )
                                        print(
                                            f"Reformatted raw file for BLE: {filename}"
                                        )
                                    else:
                                        ble_message = content_str
                                elif content_str.startswith("FILE_BASE64:"):
                                    # Format: FILE_BASE64:filename:base64_data
                                    parts = content_str.split(":", 2)
                                    if len(parts) >= 3:
                                        filename = parts[1]
                                        base64_content = parts[2]
                                        ble_message = (
                                            f"FILE:{filename}:{base64_content}"
                                        )
                                        print(
                                            f"Reformatted raw base64 file for BLE: {filename}"
                                        )
                                    else:
                                        ble_message = content_str
                                else:
                                    # Regular message, forward as-is
                                    ble_message = content_str

                                # Use chunked sending for large messages (>100 chars)
                                if len(ble_message) > 100:
                                    await send_chunked_message(
                                        msg_characteristic, ble_message, "f", 30
                                    )
                                    print("Forwarded large inline reply to BLE")
                                else:
                                    msg_characteristic.write(
                                        _encode_message(ble_message), send_update=True
                                    )
                                    print("Forwarded inline reply to BLE")
                            else:
                                print("Consecutive duplicate raw message ignored")

                            # Clear the last_reply to prevent resending
                            requester_node.last_reply = None
                    except Exception as e:
                        print(f"Error forwarding raw reply: {e}")
                        # Clear the last_reply even on error to prevent infinite loop
                        requester_node.last_reply = None

        # Back off aggressively during active BLE transfers to give priority to chunk processing
        sleep_time = 5000 if transfer_state["active"] else 1000
        await asyncio.sleep_ms(sleep_time)


async def config_task(config_characteristic):
    """Handle endpoint configuration via dedicated BLE characteristic."""
    print("Configuration task started - waiting for endpoint setup...")

    while True:
        try:
            # Wait for configuration data
            _, config_data = await config_characteristic.written()

            if config_data:
                # Try to parse as chunked message first
                is_complete, message_content, message_type = parse_chunked_message(
                    config_data
                )

                if is_complete:
                    print(f"Complete config message received: {message_content}")
                    config_message = message_content
                else:
                    # Try to decode as simple message (fallback for direct sends)
                    try:
                        if isinstance(config_data, (bytes, bytearray)):
                            # Try base64 decode first
                            try:
                                decoded_bytes = ubinascii.a2b_base64(config_data)
                                config_message = decoded_bytes.decode("utf-8").strip()
                            except:
                                # Fallback to direct UTF-8 decode
                                config_message = config_data.decode("utf-8").strip()
                        else:
                            config_message = str(config_data).strip()
                    except Exception as decode_err:
                        print(f"Failed to decode config message: {decode_err}")
                        continue

                # Parse the configuration
                success, new_endpoint = parse_endpoint_config(config_message)

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


# file_task function removed - files now handled via chunked protocol on message characteristic


async def ble_chunk_collector_task(msg_characteristic):
    """Dedicated task to collect BLE chunks and queue them for processing."""
    global ble_chunk_queue, transfer_state
    print("BLE chunk collector task started")

    chunk_count = 0
    last_activity_update = 0

    while True:
        try:

            # Wait for BLE data with minimal processing - just queue it
            _, data = await msg_characteristic.written()

            if data:
                current_time = utime.ticks_ms()
                chunk_count += 1

                # Update transfer state
                transfer_state["last_chunk_time"] = current_time
                transfer_state["active"] = True

                # Calculate chunk rate (chunks per second)
                if current_time - last_activity_update > 1000:  # Update every second
                    time_diff = utime.ticks_diff(current_time, last_activity_update)
                    if time_diff > 0:
                        recent_chunks = sum(
                            1
                            for item in ble_chunk_queue
                            if utime.ticks_diff(current_time, item["timestamp"]) < 1000
                        )
                        transfer_state["chunk_rate"] = recent_chunks
                    last_activity_update = current_time

                # Add timestamp and sequence number to track chunk arrival order
                ble_chunk_queue.append(
                    {"data": data, "timestamp": current_time, "sequence": chunk_count}
                )

                # Progress logging for large transfers
                if transfer_state["active"] and chunk_count % 100 == 0:
                    print(
                        f"ACTIVE TRANSFER - Collected {chunk_count} chunks, rate: {transfer_state['chunk_rate']}/s"
                    )

                # Prevent queue from growing too large (memory protection) - increased for large chunked transfers
                if len(ble_chunk_queue) > 15000:
                    print(
                        f"BLE chunk queue full ({len(ble_chunk_queue)} items), dropping oldest chunk"
                    )
                    dropped = ble_chunk_queue.pop(0)
                    print(
                        f"Dropped chunk #{dropped['sequence']} to prevent memory overflow"
                    )

        except asyncio.TimeoutError:
            # Check if transfer should be marked inactive
            current_time = utime.ticks_ms()
            if (
                transfer_state["active"]
                and utime.ticks_diff(current_time, transfer_state["last_chunk_time"])
                > 2000
            ):
                transfer_state["active"] = False
                transfer_state["chunk_rate"] = 0
                print("Transfer marked as inactive")
        except Exception as e:
            print(f"BLE chunk collector error: {e}")

        # Dynamic sleep based on transfer activity
        if transfer_state["active"]:
            await asyncio.sleep_ms(1)  # Stay very responsive during transfers
        else:
            await asyncio.sleep_ms(10)  # Longer sleep when idle


async def chunk_processor_task():
    """Dedicated task to process chunks from the queue and reassemble messages."""
    global ble_chunk_queue
    print("Chunk processor task started")

    while True:
        try:
            # Process multiple chunks in a batch during active transfers - much larger batch size for high throughput
            batch_size = 100 if transfer_state["active"] else 1
            chunks_processed = 0

            while ble_chunk_queue and chunks_processed < batch_size:
                chunk_info = ble_chunk_queue.pop(0)
                data = chunk_info["data"]
                sequence = chunk_info["sequence"]
                chunks_processed += 1

                # Calculate processing latency
                processing_delay = utime.ticks_diff(
                    utime.ticks_ms(), chunk_info["timestamp"]
                )

                # Parse chunked message (handles both text and files)
                is_complete, message_content, message_type = parse_chunked_message(data)

                if is_complete:
                    print(f"Complete message assembled, type: {message_type}")

                    # Add to LoRa send queue
                    ble_to_lora_queue.append(
                        {
                            "content": message_content,
                            "type": message_type,
                            "timestamp": utime.ticks_ms(),
                        }
                    )
                    print("Message queued for LoRa transmission")
                # Micro-sleep between chunks in batch to maintain responsiveness
                if transfer_state["active"] and chunks_processed < batch_size:
                    await asyncio.sleep_ms(1)

            if not ble_chunk_queue:
                # Dynamic sleep based on transfer state
                if transfer_state["active"]:
                    await asyncio.sleep_ms(1)  # Minimal sleep during active transfer
                else:
                    await asyncio.sleep_ms(50)  # Longer sleep when idle

        except Exception as e:
            print(f"Chunk processor error: {e}")
            await asyncio.sleep_ms(100)


async def lora_source_task(lora_source, connector_lock):
    """Send processed messages from queue over LoRa."""
    global ble_to_lora_queue, transfer_state
    print("LoRa source task started")

    sending_timeout = 1 * 60 * 1000  # 1 minute in ms

    while True:
        try:
            # Process messages from the queue
            if ble_to_lora_queue:
                message_info = ble_to_lora_queue.pop(0)
                message_content = message_info["content"]
                message_type = message_info["type"]

                print(f"Processing message from queue, type: {message_type}")

                if message_type == "file" and message_content.startswith("FILE:"):
                    # Process file message
                    success, filename, base64_data = process_file_message(
                        message_content
                    )
                    if success:
                        try:
                            # Decode base64 to get original file content
                            decoded_content = ubinascii.a2b_base64(base64_data).decode(
                                "utf-8"
                            )
                            processed_data = (
                                f"FILE_CONTENT:{filename}:{decoded_content}"
                            )
                            print(f"File processed: {filename}")
                        except Exception as decode_err:
                            print(
                                f"Failed to decode file, sending base64: {decode_err}"
                            )
                            processed_data = f"FILE_BASE64:{filename}:{base64_data}"
                    else:
                        processed_data = "File processing failed"
                else:
                    # Regular text message
                    processed_data = message_content

                # Send via LoRa
                if endpoint_config["configured"]:
                    filename = "msg_" + str(utime.ticks_ms()) + ".txt"
                    async with connector_lock:
                        try:
                            lora_source.establish_connection()
                            lora_source.set_file(None)
                            for _ in range(3):
                                if not lora_source.got_file():
                                    file = CTP_File(
                                        name=filename,
                                        content=bytearray(
                                            processed_data.encode("utf-8")
                                        ),
                                        chunk_size=lora_source.chunk_size,
                                    )
                                    print(f"Sending LoRa file: {file.get_name()}")
                                    lora_source.set_file(file)
                                    t_0_send = utime.ticks_ms()
                                    success = lora_source.send_file(
                                        timeout=sending_timeout
                                    )
                                    if success:
                                        td = utime.ticks_diff(
                                            utime.ticks_ms(), t_0_send
                                        )
                                        print(f"File sent successfully in {td}ms")
                                    else:
                                        print("Error sending file")
                                utime.sleep(10)
                        except Exception as e:
                            print(f"LoRa send error: {e}")
                            break  # Exit the retry loop
                else:
                    print("Endpoint not configured")
            else:
                # No messages to send, yield to other tasks
                # Back off much more during active BLE transfers
                sleep_time = 1000 if transfer_state["active"] else 100
                await asyncio.sleep_ms(sleep_time)

        except Exception as e:
            print(f"LoRa source task error: {e}")
            await asyncio.sleep_ms(1000)


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
            print(f"BLE Connection established from {connection.device}")

            # Store connection globally for tasks that need MTU exchange
            global current_connection
            current_connection = connection

            # Log MTU information after connection
            try:
                mtu = getattr(connection, "mtu", None)
                if mtu:
                    effective_payload = mtu - 3  # Subtract ATT header
                    print(f"MTU negotiated: {mtu} bytes")
                    print(f"Effective payload size: {effective_payload} bytes")
            except Exception as e:
                print(f"Could not retrieve MTU info: {e}")

            await connection.disconnected(timeout_ms=None)
            print("BLE Connection disconnected")
            current_connection = None


async def main():
    """Main entry point: setup and start both LoRa requester and source with BLE bridge."""
    print("Starting LoRa Bidirectional Bridge...")

    # Initialize hardware and services
    ctx = setup()

    # Start BLE chunk collector task FIRST (highest priority - must not miss chunks)
    ble_collector_task = asyncio.create_task(
        ble_chunk_collector_task(ctx["msg_characteristic"])
    )

    # Start chunk processor task (assembles messages from chunks)
    chunk_processor = asyncio.create_task(chunk_processor_task())

    # Start BLE advertising task
    ble_task = asyncio.create_task(peripheral_task())

    # Start configuration task
    config_task_handle = asyncio.create_task(config_task(ctx["config_characteristic"]))

    # Start LoRa requester task (LoRa -> BLE) - lower priority
    requester_task = asyncio.create_task(
        lora_requester_task(
            ctx["lora_requester"],
            ctx["connector_lock"],
            ctx["msg_characteristic"],
        )
    )

    # Start LoRa source task (BLE -> LoRa) - now uses processed message queue
    source_task = asyncio.create_task(
        lora_source_task(ctx["lora_source"], ctx["connector_lock"])
    )

    print("LoRa Bidirectional Bridge started.")
    print("Configuration service available on config characteristic")
    print("Files handled via chunked protocol on message characteristic")

    # Wait for all tasks
    await asyncio.gather(
        ble_task,
        config_task_handle,
        requester_task,
        ble_collector_task,
        chunk_processor,
        source_task,
    )


try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("\nShutting down LoRa-to-BLE Bridge...")
except Exception as e:
    print(f"Fatal error: {e}")
    # Optional: restart the device on fatal error
    # import machine
    # machine.reset()
