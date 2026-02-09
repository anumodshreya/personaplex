"""
Raw Opus Encoder - Experimental alternative to OGG Opus encoding.

This encoder produces raw Opus packets without OGG container overhead.
Designed for bridge-internal use only. Output is wrapped in OGG before sending to engine.
"""
import asyncio
import ctypes
import ctypes.util
import logging
import time
from collections import deque

logger = logging.getLogger("raw_opus_encoder")

# Opus constants (from opus_defines.h)
OPUS_APPLICATION_VOIP = 2048
OPUS_SET_BITRATE_REQUEST = 4002
OPUS_SET_VBR_REQUEST = 4006
OPUS_SET_PACKET_LOSS_PERC_REQUEST = 4014
OPUS_GET_FINAL_RANGE_REQUEST = 4031

OPUS_OK = 0
OPUS_BAD_ARG = -1
OPUS_BUFFER_TOO_SMALL = -2
OPUS_INTERNAL_ERROR = -3
OPUS_INVALID_PACKET = -4
OPUS_UNIMPLEMENTED = -5
OPUS_INVALID_STATE = -6
OPUS_ALLOC_FAIL = -7


class RawOpusEncoder:
    """
    Raw Opus encoder using libopus directly via ctypes.
    
    Produces raw Opus packets (no OGG container) for telephony use.
    Configured for minimal latency with predictable 20ms framing.
    """
    
    def __init__(self, sample_rate: int, observability=None):
        self.sample_rate = sample_rate
        self.channels = 1  # Mono
        self.tag = "raw_opus_encoder"
        self.observability = observability
        
        # Frame configuration (CRITICAL: explicit, not defaults)
        self.frame_duration_ms = 20  # 20ms frames
        self.frame_size_samples = int(sample_rate * self.frame_duration_ms / 1000)  # 480 @ 24kHz
        self.frame_size_bytes = self.frame_size_samples * 2  # int16 = 2 bytes/sample
        
        # Opus encoder config
        self.bitrate = 24000  # 24 kbps (match existing FFmpeg encoder)
        self.application = OPUS_APPLICATION_VOIP  # Low latency mode
        
        # State
        self.encoder = None
        self._closed = False
        self.input_buffer = bytearray()
        self.output_queue = deque()
        
        # Metrics
        self.encode_in_bytes_total = 0
        self.encode_out_bytes_total = 0
        self.frames_encoded = 0
        self.validation_logged = 0  # Log first 10 frames for validation
        
        # Load libopus
        self.opus = None
        self._load_libopus()
    
    def _load_libopus(self):
        """Load libopus shared library via ctypes."""
        lib_name = ctypes.util.find_library('opus')
        if not lib_name:
            raise RuntimeError(f"{self.tag}: libopus not found. Install with: apt-get install -y libopus0")
        
        try:
            self.opus = ctypes.CDLL(lib_name)
            logger.info(f"{self.tag}: Loaded libopus from {lib_name}")
        except Exception as e:
            raise RuntimeError(f"{self.tag}: Failed to load libopus: {e}")
        
        # Setup function signatures
        self.opus.opus_encoder_get_size.argtypes = [ctypes.c_int]
        self.opus.opus_encoder_get_size.restype = ctypes.c_int
        
        self.opus.opus_encoder_create.argtypes = [
            ctypes.c_int,  # sample_rate
            ctypes.c_int,  # channels
            ctypes.c_int,  # application
            ctypes.POINTER(ctypes.c_int)  # error
        ]
        self.opus.opus_encoder_create.restype = ctypes.c_void_p
        
        self.opus.opus_encode.argtypes = [
            ctypes.c_void_p,  # encoder
            ctypes.POINTER(ctypes.c_int16),  # pcm
            ctypes.c_int,  # frame_size
            ctypes.POINTER(ctypes.c_ubyte),  # data
            ctypes.c_int32  # max_data_bytes
        ]
        self.opus.opus_encode.restype = ctypes.c_int32
        
        self.opus.opus_encoder_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.opus.opus_encoder_ctl.restype = ctypes.c_int
        
        self.opus.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
        self.opus.opus_encoder_destroy.restype = None
    
    def start(self):
        """Initialize Opus encoder with explicit configuration."""
        if self.encoder:
            logger.warning(f"{self.tag}: Encoder already started")
            return
        
        error = ctypes.c_int()
        self.encoder = self.opus.opus_encoder_create(
            self.sample_rate,
            self.channels,
            self.application,
            ctypes.byref(error)
        )
        
        if error.value != OPUS_OK or not self.encoder:
            raise RuntimeError(f"{self.tag}: opus_encoder_create failed with error {error.value}")
        
        # Configure encoder explicitly (DO NOT rely on defaults)
        ret = self.opus.opus_encoder_ctl(
            self.encoder,
            OPUS_SET_BITRATE_REQUEST,
            ctypes.c_int(self.bitrate)
        )
        if ret != OPUS_OK:
            raise RuntimeError(f"{self.tag}: Failed to set bitrate: {ret}")
        
        ret = self.opus.opus_encoder_ctl(
            self.encoder,
            OPUS_SET_VBR_REQUEST,
            ctypes.c_int(0)  # Disable VBR for predictable framing
        )
        if ret != OPUS_OK:
            raise RuntimeError(f"{self.tag}: Failed to disable VBR: {ret}")
        
        ret = self.opus.opus_encoder_ctl(
            self.encoder,
            OPUS_SET_PACKET_LOSS_PERC_REQUEST,
            ctypes.c_int(0)  # No FEC overhead
        )
        if ret != OPUS_OK:
            logger.warning(f"{self.tag}: Failed to set packet loss perc: {ret}")
        
        logger.info(
            f"{self.tag} started: PCM{self.sample_rate}Hz → Raw Opus, "
            f"frame_size={self.frame_size_samples} samples ({self.frame_duration_ms}ms), "
            f"bitrate={self.bitrate}bps, VBR=off, application=VOIP"
        )
    
    def write(self, pcm_bytes: bytes) -> bool:
        """
        Write PCM16LE bytes to encoder input buffer.
        Returns True on success, False if encoder is closed.
        """
        if self._closed or not self.encoder:
            return False
        
        self.input_buffer.extend(pcm_bytes)
        self.encode_in_bytes_total += len(pcm_bytes)
        
        if self.observability:
            self.observability.update_counter('encoder_in_bytes', bytes_delta=len(pcm_bytes))
            self.observability.update_activity('encoder_in')
        
        # Encode frames as they become available
        while len(self.input_buffer) >= self.frame_size_bytes:
            frame_bytes = bytes(self.input_buffer[:self.frame_size_bytes])
            del self.input_buffer[:self.frame_size_bytes]
            self._encode_frame(frame_bytes)
        
        return True
    
    def _encode_frame(self, pcm_frame: bytes):
        """Encode exactly one 20ms frame of PCM to Opus."""
        # Convert bytes to int16 array
        pcm_array = (ctypes.c_int16 * self.frame_size_samples).from_buffer_copy(pcm_frame)
        
        # Allocate output buffer (max Opus packet size is ~1276 bytes for 20ms)
        max_packet_size = 4000
        output_buffer = (ctypes.c_ubyte * max_packet_size)()
        
        # Encode
        encoded_len = self.opus.opus_encode(
            self.encoder,
            pcm_array,
            self.frame_size_samples,
            output_buffer,
            max_packet_size
        )
        
        if encoded_len < 0:
            logger.error(f"{self.tag}: opus_encode failed with error {encoded_len}")
            return
        
        if encoded_len == 0:
            logger.warning(f"{self.tag}: opus_encode returned 0 bytes (DTX/silence?)")
            return
        
        # Extract encoded packet
        opus_packet = bytes(output_buffer[:encoded_len])
        self.output_queue.append(opus_packet)
        self.encode_out_bytes_total += encoded_len
        self.frames_encoded += 1
        
        if self.observability:
            self.observability.update_counter('encoder_out_bytes', bytes_delta=encoded_len)
        
        # Validation logging (first 10 frames)
        if self.validation_logged < 10:
            actual_duration_ms = self.frame_size_samples / self.sample_rate * 1000
            logger.info(
                f"[METRIC][RAW_OPUS][OPUS_FRAME_VALIDATION] frame#{self.frames_encoded}: "
                f"packet_size={encoded_len}B, expected_duration={self.frame_duration_ms}ms, "
                f"actual_duration={actual_duration_ms:.2f}ms, samples={self.frame_size_samples}"
            )
            self.validation_logged += 1
    
    async def read(self, nbytes: int = 4096, timeout: float = 0.1) -> bytes:
        """
        Read encoded Opus packets from output queue.
        Returns one complete Opus packet per call (20ms frame).
        """
        if self._closed:
            return b""
        
        # Wait for at least one packet
        start_time = time.monotonic()
        while not self.output_queue:
            if time.monotonic() - start_time > timeout:
                return b""
            await asyncio.sleep(0.001)
        
        # Return exactly ONE packet (critical for framing)
        if self.output_queue:
            return self.output_queue.popleft()
        return b""
    
    def stop(self):
        """Stop and cleanup encoder."""
        if self._closed:
            return
        
        self._closed = True
        
        if self.encoder:
            self.opus.opus_encoder_destroy(self.encoder)
            self.encoder = None
        
        logger.info(
            f"{self.tag} stopped: frames_encoded={self.frames_encoded}, "
            f"total_in={self.encode_in_bytes_total}B, total_out={self.encode_out_bytes_total}B"
        )


# OGG wrapper state (module-level for session persistence)
_ogg_wrapper_state = {
    "serial_number": None,
    "page_sequence": 0,
    "granule_position": 0,
    "headers_sent": False,
    "packet_count": 0
}


def _ogg_crc32(data: bytes) -> int:
    """Calculate OGG CRC32 checksum."""
    import struct
    
    # OGG CRC lookup table
    crc_table = [
        0x00000000, 0x04c11db7, 0x09823b6e, 0x0d4326d9,
        0x130476dc, 0x17c56b6b, 0x1a864db2, 0x1e475005,
        0x2608edb8, 0x22c9f00f, 0x2f8ad6d6, 0x2b4bcb61,
        0x350c9b64, 0x31cd86d3, 0x3c8ea00a, 0x384fbdbd,
        0x4c11db70, 0x48d0c6c7, 0x4593e01e, 0x4152fda9,
        0x5f15adac, 0x5bd4b01b, 0x569796c2, 0x52568b75,
        0x6a1936c8, 0x6ed82b7f, 0x639b0da6, 0x675a1011,
        0x791d4014, 0x7ddc5da3, 0x709f7b7a, 0x745e66cd,
        0x9823b6e0, 0x9ce2ab57, 0x91a18d8e, 0x95609039,
        0x8b27c03c, 0x8fe6dd8b, 0x82a5fb52, 0x8664e6e5,
        0xbe2b5b58, 0xbaea46ef, 0xb7a96036, 0xb3687d81,
        0xad2f2d84, 0xa9ee3033, 0xa4ad16ea, 0xa06c0b5d,
        0xd4326d90, 0xd0f37027, 0xddb056fe, 0xd9714b49,
        0xc7361b4c, 0xc3f706fb, 0xceb42022, 0xca753d95,
        0xf23a8028, 0xf6fb9d9f, 0xfbb8bb46, 0xff79a6f1,
        0xe13ef6f4, 0xe5ffeb43, 0xe8bccd9a, 0xec7dd02d,
        0x34867077, 0x30476dc0, 0x3d044b19, 0x39c556ae,
        0x278206ab, 0x23431b1c, 0x2e003dc5, 0x2ac12072,
        0x128e9dcf, 0x164f8078, 0x1b0ca6a1, 0x1fcdbb16,
        0x018aeb13, 0x054bf6a4, 0x0808d07d, 0x0cc9cdca,
        0x7897ab07, 0x7c56b6b0, 0x71159069, 0x75d48dde,
        0x6b93dddb, 0x6f52c06c, 0x6211e6b5, 0x66d0fb02,
        0x5e9f46bf, 0x5a5e5b08, 0x571d7dd1, 0x53dc6066,
        0x4d9b3063, 0x495a2dd4, 0x44190b0d, 0x40d816ba,
        0xaca5c697, 0xa864db20, 0xa527fdf9, 0xa1e6e04e,
        0xbfa1b04b, 0xbb60adfc, 0xb6238b25, 0xb2e29692,
        0x8aad2b2f, 0x8e6c3698, 0x832f1041, 0x87ee0df6,
        0x99a95df3, 0x9d684044, 0x902b669d, 0x94ea7b2a,
        0xe0b41de7, 0xe4750050, 0xe9362689, 0xedf73b3e,
        0xf3b06b3b, 0xf771768c, 0xfa325055, 0xfef34de2,
        0xc6bcf05f, 0xc27dede8, 0xcf3ecb31, 0xcbffd686,
        0xd5b88683, 0xd1799b34, 0xdc3abded, 0xd8fba05a,
        0x690ce0ee, 0x6dcdfd59, 0x608edb80, 0x644fc637,
        0x7a089632, 0x7ec98b85, 0x738aad5c, 0x774bb0eb,
        0x4f040d56, 0x4bc510e1, 0x46863638, 0x42472b8f,
        0x5c007b8a, 0x58c1663d, 0x558240e4, 0x51435d53,
        0x251d3b9e, 0x21dc2629, 0x2c9f00f0, 0x285e1d47,
        0x36194d42, 0x32d850f5, 0x3f9b762c, 0x3b5a6b9b,
        0x0315d626, 0x07d4cb91, 0x0a97ed48, 0x0e56f0ff,
        0x1011a0fa, 0x14d0bd4d, 0x19939b94, 0x1d528623,
        0xf12f560e, 0xf5ee4bb9, 0xf8ad6d60, 0xfc6c70d7,
        0xe22b20d2, 0xe6ea3d65, 0xeba91bbc, 0xef68060b,
        0xd727bbb6, 0xd3e6a601, 0xdea580d8, 0xda649d6f,
        0xc423cd6a, 0xc0e2d0dd, 0xcda1f604, 0xc960ebb3,
        0xbd3e8d7e, 0xb9ff90c9, 0xb4bcb610, 0xb07daba7,
        0xae3afba2, 0xaafbe615, 0xa7b8c0cc, 0xa379dd7b,
        0x9b3660c6, 0x9ff77d71, 0x92b45ba8, 0x9675461f,
        0x8832161a, 0x8cf30bad, 0x81b02d74, 0x857130c3,
        0x5d8a9099, 0x594b8d2e, 0x5408abf7, 0x50c9b640,
        0x4e8ee645, 0x4a4ffbf2, 0x470cdd2b, 0x43cdc09c,
        0x7b827d21, 0x7f436096, 0x7200464f, 0x76c15bf8,
        0x68860bfd, 0x6c47164a, 0x61043093, 0x65c52d24,
        0x119b4be9, 0x155a565e, 0x18197087, 0x1cd86d30,
        0x029f3d35, 0x065e2082, 0x0b1d065b, 0x0fdc1bec,
        0x3793a651, 0x3352bbe6, 0x3e119d3f, 0x3ad08088,
        0x2497d08d, 0x2056cd3a, 0x2d15ebe3, 0x29d4f654,
        0xc5a92679, 0xc1683bce, 0xcc2b1d17, 0xc8ea00a0,
        0xd6ad50a5, 0xd26c4d12, 0xdf2f6bcb, 0xdbee767c,
        0xe3a1cbc1, 0xe760d676, 0xea23f0af, 0xeee2ed18,
        0xf0a5bd1d, 0xf464a0aa, 0xf9278673, 0xfde69bc4,
        0x89b8fd09, 0x8d79e0be, 0x803ac667, 0x84fbdbd0,
        0x9abc8bd5, 0x9e7d9662, 0x933eb0bb, 0x97ffad0c,
        0xafb010b1, 0xab710d06, 0xa6322bdf, 0xa2f33668,
        0xbcb4666d, 0xb8757bda, 0xb5365d03, 0xb1f740b4,
    ]
    
    crc = 0
    for byte in data:
        crc = (crc << 8) ^ crc_table[((crc >> 24) & 0xff) ^ byte]
        crc &= 0xffffffff
    return crc


def _create_ogg_page(payload: bytes, granule_pos: int, serial: int, page_seq: int, 
                     header_type: int = 0, continuation: bool = False) -> bytes:
    """Create a single OGG page with one segment."""
    import struct
    
    # OGG page header structure
    capture_pattern = b'OggS'
    version = 0
    
    # Segment table (one segment)
    num_segments = 1
    segment_size = len(payload)
    if segment_size > 255:
        logger.error(f"OGG segment too large: {segment_size} bytes (max 255)")
        segment_size = 255
        payload = payload[:255]
    
    # Build header (without CRC)
    header = struct.pack('<4sBBQIIB',
        capture_pattern,     # 'OggS'
        version,             # 0
        header_type,         # 0=normal, 2=BOS, 4=EOS
        granule_pos,         # samples encoded so far
        serial,              # bitstream serial
        page_seq,            # page sequence
        num_segments         # number of segments
    )
    
    # Add segment table
    header += struct.pack('B', segment_size)
    
    # Calculate CRC with placeholder (checksum field is at offset 22)
    page_with_zero_crc = header[:22] + b'\x00\x00\x00\x00' + header[26:] + payload
    crc = _ogg_crc32(page_with_zero_crc)
    
    # Insert CRC at offset 22
    final_page = header[:22] + struct.pack('<I', crc) + header[26:] + payload
    
    return final_page


def wrap_opus_in_ogg(opus_packet: bytes, granule_pos: int = None, reset: bool = False) -> bytes:
    """
    Wrap exactly ONE Opus packet into OGG pages.
    
    Args:
        opus_packet: Raw Opus packet (20ms frame)
        granule_pos: Granule position (samples encoded). If None, auto-increment by 480.
        reset: If True, reset state for new stream
    
    Returns:
        OGG pages (may include headers on first call)
    """
    import struct
    import random
    
    global _ogg_wrapper_state
    
    if reset:
        _ogg_wrapper_state = {
            "serial_number": None,
            "page_sequence": 0,
            "granule_position": 0,
            "headers_sent": False,
            "packet_count": 0
        }
    
    # Initialize serial number on first call
    if _ogg_wrapper_state["serial_number"] is None:
        _ogg_wrapper_state["serial_number"] = random.randint(0, 0xffffffff)
        logger.info(f"[OGG_WRAP] Initialized bitstream serial={_ogg_wrapper_state['serial_number']}")
    
    serial = _ogg_wrapper_state["serial_number"]
    pages = b""
    
    # Send headers on first packet
    if not _ogg_wrapper_state["headers_sent"]:
        # Opus Identification Header (RFC 7845)
        id_header = struct.pack('<8sBBHIhB',
            b'OpusHead',  # Magic signature
            1,            # Version
            1,            # Channel count (mono)
            3840,         # Pre-skip (80ms @ 48kHz, standard value)
            24000,        # Input sample rate (24kHz)
            0,            # Output gain (0 dB)
            0             # Channel mapping family (mono)
        )
        
        # Create BOS (Beginning Of Stream) page
        bos_page = _create_ogg_page(id_header, 0, serial, 0, header_type=2)
        pages += bos_page
        _ogg_wrapper_state["page_sequence"] += 1
        
        # Opus Comment Header (minimal)
        vendor_string = b'PersonaPlex-Bridge'
        comment_header = struct.pack('<8sI', b'OpusTags', len(vendor_string))
        comment_header += vendor_string
        comment_header += struct.pack('<I', 0)  # 0 user comments
        
        comment_page = _create_ogg_page(comment_header, 0, serial, 1, header_type=0)
        pages += comment_page
        _ogg_wrapper_state["page_sequence"] += 1
        
        _ogg_wrapper_state["headers_sent"] = True
        logger.info(f"[OGG_WRAP] Sent Opus headers (BOS + Comment)")
    
    # Update granule position
    if granule_pos is not None:
        _ogg_wrapper_state["granule_position"] = granule_pos
    else:
        _ogg_wrapper_state["granule_position"] += 480  # 20ms @ 24kHz
    
    # Create data page with Opus packet
    data_page = _create_ogg_page(
        opus_packet,
        _ogg_wrapper_state["granule_position"],
        serial,
        _ogg_wrapper_state["page_sequence"],
        header_type=0
    )
    pages += data_page
    _ogg_wrapper_state["page_sequence"] += 1
    _ogg_wrapper_state["packet_count"] += 1
    
    # Validation logging (first 10 packets)
    if _ogg_wrapper_state["packet_count"] <= 10:
        actual_duration_ms = 480 / 24000 * 1000  # 20ms
        logger.info(
            f"[METRIC][RAW_OPUS][OGG_WRAP] frame={_ogg_wrapper_state['packet_count']} "
            f"granule_pos={_ogg_wrapper_state['granule_position']} "
            f"duration={actual_duration_ms:.2f}ms packet_size={len(opus_packet)}B "
            f"ogg_size={len(data_page)}B"
        )
    
    return pages

