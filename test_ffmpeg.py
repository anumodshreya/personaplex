
import subprocess
import numpy as np

def test_ffmpeg():
    # Generate 1s of silence (24k, mono, s16le)
    audio = np.zeros(24000, dtype=np.int16).tobytes()
    
    cmd = [
        "ffmpeg", "-y",
        "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "pipe:0",
        "-c:a", "libopus", "-b:a", "24k", "-f", "ogg", "pipe:1"
    ]
    
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate(input=audio)
    
    print(f"Output size: {len(out)} bytes")
    print(f"Error: {err.decode()[-200:]}")

if __name__ == "__main__":
    test_ffmpeg()
