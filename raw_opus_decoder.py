"""
Raw Opus Decoder - Experimental alternative to OGG Opus decoding.

This decoder accepts raw Opus packets without OGG container.
Designed for bridge-internal use only. Input is unwrapped from OGG before decoding.
"""
import asyncio
import ctypes
import ctypes.util
import logging
import time
from collections import deque

logger = logging.getLogger("raw_opus_decoder")

# Opus constants
OPUS_OK = 0


class RawOpusDecoder:
    """
    Raw Opus decoder using libopus directly via ctypes.
    
    Accepts raw Opus packets (no OGG container) for telephony use.
    Validates 20ms frame boundaries and packet integrity.
    """
    
    def __init__(self, observability=None):
        self.sample_rate = 24000  # Must match encoder
        self.channels = 1  # Mono
        self.tag = "raw_opus_decoder"
        self.observability = observability
        
        # Frame configuration (MUST match encoder)
        self.frame_duration_ms = 20
        self.frame_size_samples = int(self.sample_rate * self.frame_duration_ms / 1000)  # 480
        self.frame_size_bytes = self.frame_size_samples * 2  # int16 = 2 bytes/sample
        
        # State
        self.decoder = None
        self._closed = False
        self.input_queue = deque()
        self.output_buffer = bytearray()
        
        # Metrics
        self.write_total = 0
        self.read_total = 0
        self.packets_decoded = 0
        self.validation_logged = 0  # Log first 10 packets
        
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
        self.opus.opus_decoder_create.argtypes = [
            ctypes.c_int,  # sample_rate
            ctypes.c_int,  # channels
            ctypes.POINTER(ctypes.c_int)  # error
        ]
        self.opus.opus_decoder_create.restype = ctypes.c_void_p
        
        self.opus.opus_decode.argtypes = [
            ctypes.c_void_p,  # decoder
            ctypes.POINTER(ctypes.c_ubyte),  # data
            ctypes.c_int32,  # len
            ctypes.POINTER(ctypes.c_int16),  # pcm
            ctypes.c_int,  # frame_size
            ctypes.c_int  # decode_fec
        ]
        self.opus.opus_decode.restype = ctypes.c_int
        
        self.opus.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
        self.opus.opus_decoder_destroy.restype = None
    
    def start(self):
        """Initialize Opus decoder."""
        if self.decoder:
            logger.warning(f"{self.tag}: Decoder already started")
            return
        
        error = ctypes.c_int()
        self.decoder = self.opus.opus_decoder_create(
            self.sample_rate,
            self.channels,
            ctypes.byref(error)
        )
        
        if error.value != OPUS_OK or not self.decoder:
            raise RuntimeError(f"{self.tag}: opus_decoder_create failed with error {error.value}")
        
        logger.info(
            f"{self.tag} started: Raw Opus → PCM{self.sample_rate}Hz, "
            f"expected_frame_size={self.frame_size_samples} samples ({self.frame_duration_ms}ms)"
        )
    
    def write(self, opus_bytes: bytes) -> bool:
        """
        Write raw Opus packet to decoder input queue.
        Each write should be exactly one Opus packet (20ms frame).
        Returns True on success, False if decoder is closed.
        """
        if self._closed or not self.decoder:
            return False
        
        if not opus_bytes:
            logger.warning(f"{self.tag}: Received empty packet, skipping")
            return True
        
        self.input_queue.append(opus_bytes)
        self.write_total += len(opus_bytes)
        
        if self.observability:
            self.observability.update_counter('decoder_in_bytes', bytes_delta=len(opus_bytes))
            self.observability.update_activity('decoder_in')
        
        # Decode immediately
        self._decode_packet(opus_bytes)
        
        return True
    
    def _decode_packet(self, opus_packet: bytes):
        """Decode exactly one Opus packet to PCM."""
        # Prepare input buffer
        packet_len = len(opus_packet)
        input_buffer = (ctypes.c_ubyte * packet_len).from_buffer_copy(opus_packet)
        
        # Allocate output buffer (max samples for one frame)
        max_frame_size = self.frame_size_samples * 2  # Allow some overhead
        output_buffer = (ctypes.c_int16 * max_frame_size)()
        
        # Decode
        decoded_samples = self.opus.opus_decode(
            self.decoder,
            input_buffer,
            packet_len,
            output_buffer,
            max_frame_size,
            0  # decode_fec=0 (no FEC)
        )
        
        if decoded_samples < 0:
            logger.error(f"{self.tag}: opus_decode failed with error {decoded_samples}, packet_size={packet_len}B")
            logger.info(
                f"[METRIC][RAW_OPUS][OPUS_FRAME_VALIDATION] frame#{self.packets_decoded + 1}: "
                f"MALFORMED packet_size={packet_len}B, error={decoded_samples}"
            )
            return
        
        if decoded_samples == 0:
            logger.warning(f"{self.tag}: opus_decode returned 0 samples")
            return
        
        # Convert to bytes
        pcm_bytes = bytes(output_buffer[:decoded_samples])
        self.output_buffer.extend(pcm_bytes)
        self.read_total += len(pcm_bytes)
        self.packets_decoded += 1
        
        if self.observability:
            self.observability.update_counter('decoder_out_bytes', bytes_delta=len(pcm_bytes))
        
        # Validation logging (first 10 packets)
        if self.validation_logged < 10:
            actual_duration_ms = decoded_samples / self.sample_rate * 1000
            expected_duration_ms = self.frame_duration_ms
            duration_error = abs(actual_duration_ms - expected_duration_ms)
            
            status = "OK" if duration_error < 0.1 else "WARN"
            logger.info(
                f"[METRIC][RAW_OPUS][OPUS_FRAME_VALIDATION] frame#{self.packets_decoded}: "
                f"status={status}, packet_size={packet_len}B, samples={decoded_samples}, "
                f"expected_duration={expected_duration_ms}ms, actual_duration={actual_duration_ms:.2f}ms, "
                f"error={duration_error:.3f}ms"
            )
            self.validation_logged += 1
    
    async def read(self, nbytes: int, timeout: float = 0.1) -> bytes:
        """
        Read decoded PCM24k bytes from output buffer.
        Returns up to nbytes of PCM data.
        """
        if self._closed:
            return b""
        
        # Wait for some data
        start_time = time.monotonic()
        while len(self.output_buffer) < nbytes:
            if time.monotonic() - start_time > timeout:
                break
            await asyncio.sleep(0.001)
        
        if not self.output_buffer:
            return b""
        
        # Return available data (up to nbytes)
        result = bytes(self.output_buffer[:nbytes])
        del self.output_buffer[:nbytes]
        return result
    
    def stop(self):
        """Stop and cleanup decoder."""
        if self._closed:
            return
        
        self._closed = True
        
        if self.decoder:
            self.opus.opus_decoder_destroy(self.decoder)
            self.decoder = None
        
        logger.info(
            f"{self.tag} stopped: packets_decoded={self.packets_decoded}, "
            f"total_in={self.write_total}B, total_out={self.read_total}B"
        )


# OGG unwrapper state (module-level for session persistence)
_ogg_unwrapper_state = {
    "headers_received": False,
    "packet_count": 0,
    "last_granule_pos": 0
}


def unwrap_ogg_to_opus(ogg_data: bytes, reset: bool = False) -> bytes:
    """
    Extract raw Opus packet(s) from OGG page(s).
    
    Args:
        ogg_data: OGG page data (may contain multiple pages)
        reset: If True, reset state for new stream
    
    Returns:
        Raw Opus packet bytes (empty if header/invalid)
    """
    import struct
    
    global _ogg_unwrapper_state
    
    if reset:
        _ogg_unwrapper_state = {
            "headers_received": False,
            "packet_count": 0,
            "last_granule_pos": 0
        }
    
    # Parse all OGG pages in the data
    offset = 0
    opus_packets = b""
    
    while offset < len(ogg_data):
        try:
            # Validate OGG capture pattern
            if offset + 27 > len(ogg_data):
                break
            
            capture = ogg_data[offset:offset+4]
            if capture != b'OggS':
                logger.error(f"[OGG_UNWRAP] Invalid capture pattern at offset {offset}: {capture!r}")
                break
            
            # Parse OGG page header
            version = ogg_data[offset+4]
            header_type = ogg_data[offset+5]
            granule_pos = struct.unpack('<Q', ogg_data[offset+6:offset+14])[0]
            serial = struct.unpack('<I', ogg_data[offset+14:offset+18])[0]
            page_seq = struct.unpack('<I', ogg_data[offset+18:offset+22])[0]
            # CRC at offset+22:offset+26
            num_segments = ogg_data[offset+26]
            
            # Parse segment table
            if offset + 27 + num_segments > len(ogg_data):
                logger.error(f"[OGG_UNWRAP] Incomplete segment table")
                break
            
            segment_table = ogg_data[offset+27:offset+27+num_segments]
            payload_size = sum(segment_table)
            
            # Extract payload
            payload_offset = offset + 27 + num_segments
            if payload_offset + payload_size > len(ogg_data):
                logger.error(f"[OGG_UNWRAP] Incomplete payload")
                break
            
            payload = ogg_data[payload_offset:payload_offset+payload_size]
            
            # Check if this is a header page
            is_header = False
            if payload.startswith(b'OpusHead'):
                is_header = True
                logger.info(f"[OGG_UNWRAP] Received OpusHead header (BOS page)")
                _ogg_unwrapper_state["headers_received"] = True
            elif payload.startswith(b'OpusTags'):
                is_header = True
                logger.info(f"[OGG_UNWRAP] Received OpusTags comment header")
            
            # Only process data packets (not headers)
            if not is_header and _ogg_unwrapper_state["headers_received"]:
                opus_packets += payload
                _ogg_unwrapper_state["packet_count"] += 1
                _ogg_unwrapper_state["last_granule_pos"] = granule_pos
                
                # Validation logging (first 10 packets)
                if _ogg_unwrapper_state["packet_count"] <= 10:
                    expected_samples = 480  # 20ms @ 24kHz
                    expected_granule = _ogg_unwrapper_state["packet_count"] * expected_samples
                    granule_error = abs(granule_pos - expected_granule) if granule_pos > 0 else 0
                    
                    logger.info(
                        f"[METRIC][RAW_OPUS][OGG_UNWRAP] frame={_ogg_unwrapper_state['packet_count']} "
                        f"granule_pos={granule_pos} expected={expected_granule} "
                        f"error={granule_error} packet_size={len(payload)}B"
                    )
            
            # Move to next page
            offset = payload_offset + payload_size
            
        except Exception as e:
            logger.error(f"[OGG_UNWRAP] Error parsing OGG page at offset {offset}: {e}")
            break
    
    return opus_packets

