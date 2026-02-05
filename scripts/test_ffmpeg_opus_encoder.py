#!/usr/bin/env python3
"""STEP 6: Standalone FFmpeg Opus encoder test."""
import subprocess
import time
import sys

def test_ffmpeg_opus_encoder():
    """Test FFmpeg Opus encoder with 1 second of silence."""
    print("=" * 60)
    print("STEP 6: Standalone FFmpeg Opus Encoder Test")
    print("=" * 60)
    
    # Same command as FfmpegOpusEncoder
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-f", "s16le",
        "-ar", "24000",
        "-ac", "1",
        "-i", "pipe:0",
        "-c:a", "libopus",
        "-application", "voip",
        "-frame_duration", "20",
        "-vbr", "off",
        "-b:a", "24k",
        "-f", "ogg",
        "pipe:1"
    ]
    
    print(f"Command: {' '.join(command)}")
    print("")
    
    # Generate 1 second of PCM16LE silence at 24kHz
    # 24000 samples * 2 bytes = 48000 bytes
    silence_samples = 24000
    silence_bytes = b"\x00\x00" * silence_samples  # 16-bit signed little-endian zeros
    
    print(f"Feeding {len(silence_bytes)} bytes of PCM16LE silence (1 second @ 24kHz)...")
    
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        
        if proc.poll() is not None:
            stderr = proc.stderr.read().decode(errors="ignore")
            print(f"ERROR: Process exited immediately: {stderr}")
            return 1
        
        # Write input
        proc.stdin.write(silence_bytes)
        proc.stdin.flush()
        proc.stdin.close()
        
        # Read output with timeout
        print("Reading output (timeout: 2 seconds)...")
        start_time = time.time()
        timeout = 2.0
        output_chunks = []
        bytes_out = 0
        
        while time.time() - start_time < timeout:
            try:
                chunk = proc.stdout.read(4096)
                if chunk:
                    output_chunks.append(chunk)
                    bytes_out += len(chunk)
                    print(f"  Read chunk: {len(chunk)} bytes (total: {bytes_out} bytes)")
                elif proc.poll() is not None:
                    # Process finished
                    break
                else:
                    # No data yet, wait a bit
                    time.sleep(0.01)
            except Exception as e:
                print(f"ERROR reading stdout: {e}")
                break
        
        # Check stderr
        stderr_output = proc.stderr.read().decode(errors="ignore")
        if stderr_output:
            print(f"\nFFmpeg stderr:\n{stderr_output}")
        
        # Final status
        return_code = proc.wait(timeout=1)
        
        print("")
        print("=" * 60)
        print("Results:")
        print("=" * 60)
        print(f"bytes_out: {bytes_out}")
        if bytes_out > 0:
            first_32_bytes_hex = output_chunks[0][:32].hex() if output_chunks else ""
            print(f"first_32_bytes_hex: {first_32_bytes_hex}")
            print(f"return_code: {return_code}")
            print("")
            print("✓ SUCCESS: Encoder produced output")
            return 0
        else:
            print(f"return_code: {return_code}")
            print("")
            print("✗ FAIL: Encoder produced 0 bytes")
            return 1
            
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(test_ffmpeg_opus_encoder())
